"""插件配置模型。

对应原插件 old/src/config.ts：默认模板常量与配置归一。
AstrBot 将 _conf_schema.json 的用户配置以 dict 注入 self.config，
本模块负责把原始 dict（键名沿用原插件命名以便老用户对照迁移）
转换为类型安全的只读配置对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 默认通用模板（emoji 风格；占位符语义见 docs/DESIGN.md 第 7 节）
DEFAULT_LINK_TEMPLATE = (
    "📺 ${标题} ${tab} 👤 ${UP主}\n"
    "📝 ${简介}\n"
    "👍 ${点赞} ${tab} 🪙 ${投币}\n"
    "⭐ ${收藏} ${tab} 🔁 ${转发}\n"
    "👀 ${观看} ${tab} 💬 ${弹幕}\n"
    "▶️ ${播放链接}\n"
    "${封面}\n"
    "${视频}"
)

# 默认卡片模板（emoji 风格）
DEFAULT_CARD_TEMPLATE = (
    "📺 ${标题}\n"
    "📝 ${简介}\n"
    "🪙 ${投币} ${tab} ⭐ ${收藏} ${tab} 🔁 ${转发}\n"
    "▶️ ${播放链接}\n"
    "🔗 ${视频地址}\n"
    "${封面}\n"
    "${视频}"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 允许触发的来源（videoParseMode）：
# - card：QQ 分享的 B 站 JSON 卡片（消息级判定）
# - link：完整视频页链接 bilibili.com/video/…
# - short：b23.tv 等短链
# - id：消息中独立出现的 BV/AV 号（不属于任何 URL）
VALID_PARSE_MODES = ("card", "link", "short", "id")
VALID_REQUEST_MODES = ("proxy", "direct", "parallel")
VALID_ID_PREFERENCES = ("bv", "av")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    return result


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


@dataclass(frozen=True)
class PluginConfig:
    """规整后的插件配置（只读快照，配置热更新时随插件重载重建）。"""

    # 解析开关与触发设置
    enablebilianalysis: bool = True
    wait_tip: str = ""  # 等待提示语；空 = 不发送
    video_parse_mode: tuple[str, ...] = VALID_PARSE_MODES  # 四种来源默认全开
    parse_limit: int = 3

    # 消息模板
    link_template: str = DEFAULT_LINK_TEMPLATE
    card_template: str = DEFAULT_CARD_TEMPLATE  # 空串时回落 link_template
    desc_max_len: int = 50
    use_numeral: bool = True
    video_id_preference: str = "bv"

    # 视频下载限制
    max_file_size_mb: int = 50  # 0 表示不限制

    # 合并转发
    use_figure: bool = True

    # 频率限制
    min_interval_s: int = 180
    user_rate_limit: bool = True

    # 网络与调试
    request_mode: str = "parallel"
    user_agent: str = DEFAULT_USER_AGENT
    show_error: bool = False
    debug_log: bool = False
    debug_full: bool = False

    @property
    def effective_card_template(self) -> str:
        """卡片模板为空时回落通用模板（对齐原版 bVideoCard_area ?? bVideo_area）。"""
        return self.card_template.strip() or self.link_template

    def allows_source(self, source: str) -> bool:
        return source in self.video_parse_mode

    @classmethod
    def from_user_config(cls, raw: dict[str, Any] | None) -> "PluginConfig":
        """把 AstrBot 注入的原始配置 dict 归一为 PluginConfig。"""
        raw = raw or {}

        parse_mode_raw = raw.get("videoParseMode") or list(VALID_PARSE_MODES)
        if isinstance(parse_mode_raw, str):
            parse_mode_raw = [parse_mode_raw]
        parse_modes = tuple(
            mode for mode in parse_mode_raw if mode in VALID_PARSE_MODES
        ) or VALID_PARSE_MODES

        request_mode = _as_str(raw.get("requestMode"), "parallel").strip().lower()
        if request_mode not in VALID_REQUEST_MODES:
            request_mode = "parallel"

        id_pref = _as_str(raw.get("bVideoIDPreference"), "bv").strip().lower()
        if id_pref not in VALID_ID_PREFERENCES:
            id_pref = "bv"

        link_template = _as_str(raw.get("bVideo_area"), DEFAULT_LINK_TEMPLATE)
        if not link_template.strip():
            link_template = DEFAULT_LINK_TEMPLATE

        # 卡片模板：键缺失时用 schema 默认值；用户显式清空则由
        # effective_card_template 回落通用模板（对齐原版 ?? 语义并兜底）
        card_raw = raw.get("bVideoCard_area")
        card_template = DEFAULT_CARD_TEMPLATE if card_raw is None else str(card_raw)

        return cls(
            enablebilianalysis=_as_bool(raw.get("enablebilianalysis"), True),
            wait_tip=_as_str(raw.get("waitTip_Switch") or "", "").strip(),
            video_parse_mode=parse_modes,
            parse_limit=_as_int(raw.get("parseLimit"), 3, minimum=1),
            link_template=link_template,
            card_template=card_template,
            desc_max_len=_as_int(raw.get("bVideoShowIntroductionTofixed"), 50),
            use_numeral=_as_bool(raw.get("useNumeral"), True),
            video_id_preference=id_pref,
            max_file_size_mb=_as_int(raw.get("MaximumFileSizeMB"), 50, minimum=0),
            use_figure=_as_bool(raw.get("isfigure"), True),
            min_interval_s=_as_int(raw.get("MinimumTimeInterval"), 180, minimum=1),
            user_rate_limit=_as_bool(raw.get("preventSingleUserListAttack"), True),
            request_mode=request_mode,
            user_agent=_as_str(raw.get("userAgent"), DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
            show_error=_as_bool(raw.get("showError"), False),
            debug_log=_as_bool(raw.get("loggerinfo"), False),
            debug_full=_as_bool(raw.get("loggerinfofulljson"), False),
        )
