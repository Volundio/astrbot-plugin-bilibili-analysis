"""astrbot_plugin_bilibili_analysis 插件入口。

移植自 koishi-plugin-bilibili-videolink-analysis（./old/src/index.ts）：
- 全量消息监听解析 B 站链接（old/src/middleware.ts）
- B 站点播指令（old/src/commands.ts，搜索改为官方 API 实现）
- 生命周期装配（old/src/index.ts apply / dispose）

平台约束：仅支持 QQ（aiocqhttp / OneBot v11），见 AGENTS.md §2。
"""

from __future__ import annotations

import re

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Json
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.util import SessionController, session_waiter

try:  # GreedyStr 未在 api 层导出，此处兼容导入，失败则降级为普通 str
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # pragma: no cover
    GreedyStr = str  # type: ignore[assignment,misc]

from .core.bilibili_api import BilibiliApi
from .core.config import PluginConfig
from .core.link_parser import (
    collect_json_card_text,
    parse_bilibili_content,
    VideoTarget,
)
from .core.logger import PluginLogger
from .core.message_sender import is_supported_platform, send_plain
from .core.parser_service import VideoParseService
from .core.rate_limiter import VideoRateLimiter
from .core.request_manager import RequestManager
from .core.search import BilibiliSearchApi
from .core.video_formatter import numeral

# 点播参数中的页码/分 P 选项（原版 -p <page:number>）
_PAGE_OPTION_PATTERN = re.compile(r"(?:^|\s)(?:-p|--page)\s+(\d+)(?=\s|$)")

# 询问关键词时等待用户回复的时长（原版 session.prompt(30 * 1000)）
KEYWORD_PROMPT_TIMEOUT_S = 30

# 单次搜索展示的结果条数（对齐原版截图单屏可见数量）
SEARCH_RESULT_LIMIT = 10


def _parse_point_args(raw: str) -> tuple[str, int]:
    """从指令参数中拆出关键词与页码（-p/--page，默认 1）。"""
    page = 1
    page_match = _PAGE_OPTION_PATTERN.search(raw)
    if page_match:
        try:
            page = max(1, int(page_match.group(1)))
        except ValueError:
            page = 1
        raw = _PAGE_OPTION_PATTERN.sub(" ", raw, count=1)
    keyword = raw.strip()
    return keyword, page


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
        self._search_api = BilibiliSearchApi(self._config, request_manager, self._logger)
        self._rate_limiter = VideoRateLimiter(self._config)
        video_dir = StarTools.get_data_dir("astrbot_plugin_bilibili_analysis") / "video"
        self._service = VideoParseService(
            self._config, self._api, self._logger, self._rate_limiter, video_dir
        )
        self._service.start()
        self._logger.info(
            f"插件已加载：解析={'开' if self._config.enablebilianalysis else '关'}，"
            f"点播={'开' if self._config.demand else '关'}，"
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
        # QQ JSON 分享卡片判定（原版 isJsonCard：消息链中存在 json 元素）
        is_card = any(isinstance(comp, Json) for comp in chain)
        if is_card and not self._config.allows_source("card"):
            return
        if not is_card and not self._config.allows_source("link"):
            return

        # JSON 卡片内容不会进入 message_str，需单独提取后合并（原版行为）
        card_text = collect_json_card_text(chain)
        targets = parse_bilibili_content(
            f"{event.message_str}\n{card_text}", parse_standalone_ids=True
        )
        if not targets:
            return

        limited = targets[: self._config.parse_limit]
        reason = self._service.enqueue(event, limited, "card" if is_card else "link")
        if reason:
            self._logger.debug(f"频率限制：{reason}")
        # 不调用 stop_event，保持管线透传（原版 return next()）

    # ---- B 站点播指令（原版 commands.ts）----

    @filter.command("B站点播", alias={"bili点播"})
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def bilibili_point(self, event: AstrMessageEvent, raw_args: GreedyStr):
        if not is_supported_platform(event):
            return
        if not self._config.demand:
            return

        keyword, page = _parse_point_args(str(raw_args))
        sender_id = event.get_sender_id() or ""

        if not keyword:
            await send_plain(event, "告诉我你想点播的关键词吧~")
            keyword = await self._wait_text(event, sender_id, KEYWORD_PROMPT_TIMEOUT_S)
            if not keyword:
                return

        items = await self._search_api.search_video(keyword, page)
        if items is None:
            await send_plain(event, "搜索失败，请稍后再试。")
            return
        if not items:
            await send_plain(event, "未找到相关视频。")
            return
        items = items[:SEARCH_RESULT_LIMIT]

        lines = []
        for item in items:
            meta = " · ".join(
                part for part in (
                    item.author,
                    f"播放 {numeral(item.play, self._config.use_numeral)}" if item.play else "",
                    item.duration,
                ) if part
            )
            lines.append(f"{item.index}. {item.title}\n    {meta}".rstrip())
        await send_plain(event, "\n".join(lines) + "\n请选择视频的序号：")

        choice = await self._wait_choice(event, sender_id, self._config.timeout, len(items))
        if choice is None:
            await send_plain(event, "输入无效，请重新点播。")
            return

        if self._config.enable:
            await self._service.process_target(
                event, VideoTarget(bvid=items[choice].bvid, page=page)
            )

    # ---- session_waiter 封装 ----

    async def _wait_text(
        self, event: AstrMessageEvent, sender_id: str, timeout_s: int
    ) -> str:
        """等待点播者回复一句话作为关键词（原版 session.prompt）。"""
        answer: dict[str, str] = {}

        @session_waiter(timeout=timeout_s, record_history_chains=False)
        async def _waiter(controller: SessionController, ev: AstrMessageEvent) -> None:
            if ev.get_sender_id() != sender_id:
                controller.keep(timeout_s, reset_timeout=True)
                return
            text = ev.message_str.strip()
            if text:
                answer["text"] = text
            controller.stop()

        try:
            await _waiter(event)
        except TimeoutError:
            return ""
        return answer.get("text", "")

    async def _wait_choice(
        self, event: AstrMessageEvent, sender_id: str, timeout_s: int, total: int
    ) -> int | None:
        """等待点播者回复序号；返回下标（0 起），无效输入返回 None（原版一次性机会）。"""
        choice: dict[str, int] = {}

        @session_waiter(timeout=timeout_s, record_history_chains=False)
        async def _waiter(controller: SessionController, ev: AstrMessageEvent) -> None:
            if ev.get_sender_id() != sender_id:
                controller.keep(timeout_s, reset_timeout=True)
                return
            try:
                index = int(ev.message_str.strip()) - 1
            except ValueError:
                controller.stop()  # 非数字：放弃（原版 parseInt 失败行为）
                return
            if 0 <= index < total:
                choice["index"] = index
            controller.stop()

        try:
            await _waiter(event)
        except TimeoutError:
            return None
        return choice.get("index")
