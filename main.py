"""astrbot_plugin_bilibili_analysis 插件入口。

移植自 koishi-plugin-bilibili-videolink-analysis（./old/src/index.ts）：
- 全量消息监听解析 B 站链接（old/src/middleware.ts）
- 生命周期装配（old/src/index.ts apply / dispose）

平台约束：仅支持 QQ（aiocqhttp / OneBot v11），见 AGENTS.md §2。
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Json
from astrbot.api.star import Context, Star, StarTools

from .core.bilibili_api import BilibiliApi
from .core.config import PluginConfig
from .core.link_parser import (
    collect_json_card_text,
    parse_bilibili_content,
    target_origin,
)
from .core.logger import PluginLogger
from .core.message_sender import is_supported_platform
from .core.parser_service import VideoParseService
from .core.rate_limiter import VideoRateLimiter
from .core.request_manager import RequestManager


# 插件元数据（name/author/desc/version/repo）统一由 metadata.yaml 提供；
# @register 装饰器自 AstrBot v3.5.19 起已废弃，Star 子类会被自动识别加载
class BilibiliAnalysisPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # AstrBot 实例化插件时按 __init__ 签名注入配置（无 schema 的旧版本
        # 会回退为只传 context），此处显式存储并保留空配置兜底
        self.config = config if config is not None else {}
        self._config = PluginConfig.from_user_config(self.config)
        self._logger = PluginLogger(self._config.debug_log)

    # ---- 生命周期（原版 index.ts apply / dispose）----

    async def initialize(self) -> None:
        request_manager = RequestManager(self._config, self._logger)
        self._request_manager = request_manager
        self._api = BilibiliApi(self._config, request_manager, self._logger)
        self._rate_limiter = VideoRateLimiter(self._config)
        video_dir = StarTools.get_data_dir("astrbot_plugin_bilibili_analysis") / "video"
        self._service = VideoParseService(
            self._config, self._api, self._logger, self._rate_limiter, video_dir
        )
        self._service.start()
        self._logger.info(
            f"插件已加载：解析={'开' if self._config.enablebilianalysis else '关'}，"
            f"来源={'+'.join(self._config.video_parse_mode)}，"
            f"请求模式={self._config.request_mode}"
        )

    async def terminate(self) -> None:
        if getattr(self, "_service", None):
            await self._service.close()
        if getattr(self, "_rate_limiter", None):
            self._rate_limiter.dispose()
        if getattr(self, "_request_manager", None):
            await self._request_manager.close()
        self._logger.info("插件已卸载")

    # ---- 链接解析监听（原版 middleware.ts）----

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_message(self, event: AstrMessageEvent) -> None:
        # 平台双保险：装饰器之外再显式校验（AGENTS.md §2）
        if not is_supported_platform(event):
            return
        if not self._config.enablebilianalysis:
            return

        chain = event.get_messages()
        # QQ JSON 分享卡片判定（原版 isJsonCard：消息链中存在 json 元素）。
        # 卡片是消息级来源：卡片消息中的全部目标按卡片来源处理（对齐原版）
        is_card = any(isinstance(comp, Json) for comp in chain)
        allowed = set(self._config.video_parse_mode)
        if is_card and "card" not in allowed:
            return

        # JSON 卡片内容不会进入 message_str，需单独提取后合并（原版行为）
        card_text = collect_json_card_text(chain)
        targets = parse_bilibili_content(
            f"{event.message_str}\n{card_text}", parse_standalone_ids=True
        )
        if not targets:
            return

        # 非卡片消息按各目标的出现形态逐一过滤：
        # link=完整链接 / short=短链 / id=独立 BV/AV 号
        if is_card:
            usable = targets
        else:
            usable = [t for t in targets if target_origin(t) in allowed]
        if not usable:
            return

        limited = usable[: self._config.parse_limit]
        reason = self._service.enqueue(event, limited, "card" if is_card else "link")
        if reason:
            self._logger.debug(f"频率限制：{reason}")
        # 不调用 stop_event，保持管线透传（原版 return next()）
