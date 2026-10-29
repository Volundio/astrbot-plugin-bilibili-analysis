"""消息模板渲染。

对照移植原插件 old/src/video-formatter.ts：
- 占位符替换（与原版语义一致，见 docs/DESIGN.md 第 7 节）
- `${~~~}` 把模板拆分成多条消息
- 大数字缩写（万 / 亿）
- `${封面}` → Image 组件、`${视频}` → Video 组件

差异说明：原版把组件占位符替换为 koishi h 元素字符串再整体 h.parse，
AstrBot 的 Plain 为纯文本无需转义（DESIGN.md §9 第 8 条），这里直接
按组件占位符切分模板构建组件序列，行为等价。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrbot.api.message_components import BaseMessageComponent, Image, Plain, Video

from .config import PluginConfig
from .bilibili_api import BiliVideoView

if TYPE_CHECKING:
    from pathlib import Path

# ${~~~} 消息拆分符（原版 template.split(/\$\{~~~\}/g)）
_MESSAGE_SPLIT_PATTERN = re.compile(r"\$\{~~~}")

# 组件型占位符：${封面} / ${视频}（保留捕获组以便交替切分）
_COMPONENT_SPLIT_PATTERN = re.compile(r"(\$\{封面\}|\$\{视频\})")


def _to_https(url: str) -> str:
    return re.sub(r"^http://", "https://", url, flags=re.IGNORECASE)


def numeral(value: int, use_numeral: bool) -> str:
    """大数字缩写：≥1 亿用「x.x亿」、≥1 万用「x.x万」（原版 numeral）。"""
    if not use_numeral:
        return str(value)
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def _player_link(view: BiliVideoView, page: int) -> str:
    """网页播放器链接（原版 playerLink）。"""
    return (
        "https://www.bilibili.com/blackboard/webplayer/mbplayer.html"
        f"?bvid={view.bvid}&p={page}&autoplay=true"
    )


def _page_link(config: PluginConfig, view: BiliVideoView) -> str:
    """普通视频页链接，按配置选择 BV/AV 号（原版 pageLink）。"""
    if config.video_id_preference == "av":
        return f"https://www.bilibili.com/video/av{view.aid}"
    return f"https://www.bilibili.com/video/{view.bvid}"


def get_message_template(config: PluginConfig, source: str) -> str:
    """按消息来源取模板：卡片用卡片模板（空回落通用），其余用通用模板。"""
    if source == "card":
        return config.effective_card_template
    return config.link_template


def has_video_placeholder(config: PluginConfig, source: str) -> bool:
    """模板是否包含 ${视频}：决定是否需要下载视频（原版 hasVideoPlaceholder）。"""
    return "${视频}" in get_message_template(config, source)


def _truncate_desc(desc: str, max_length: int) -> str:
    if len(desc) > max_length:
        return f"{desc[:max_length]}..."
    return desc


def _apply_text_placeholders(
    template: str,
    config: PluginConfig,
    view: BiliVideoView,
    page: int,
) -> str:
    """替换全部纯文本占位符（组件占位符 ${封面}/${视频} 保持原样）。"""
    desc = _truncate_desc(view.desc, config.desc_max_len)
    stat = view.stat
    replacements: dict[str, str] = {
        "${标题}": view.title,
        "${UP主}": view.owner.name if view.owner else "",
        "${简介}": desc,
        "${点赞}": numeral(stat.like, config.use_numeral),
        "${投币}": numeral(stat.coin, config.use_numeral),
        "${收藏}": numeral(stat.favorite, config.use_numeral),
        "${转发}": numeral(stat.share, config.use_numeral),
        "${观看}": numeral(stat.view, config.use_numeral),
        "${弹幕}": numeral(stat.danmaku, config.use_numeral),
        "${播放链接}": _player_link(view, page),
        "${视频地址}": _page_link(config, view),
        "${tab}": "\t",
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def _build_components(
    rendered: str,
    view: BiliVideoView,
    video_path: Path | None,
) -> list[BaseMessageComponent]:
    """按 ${封面} / ${视频} 切分为组件序列；文本段 strip 后为空则跳过。"""
    components: list[BaseMessageComponent] = []
    for segment in _COMPONENT_SPLIT_PATTERN.split(rendered):
        if segment == "${封面}":
            if view.pic:
                components.append(Image.fromURL(_to_https(view.pic)))
        elif segment == "${视频}":
            if video_path is not None:
                components.append(Video.fromFileSystem(str(video_path)))
        elif segment.strip():
            components.append(Plain(text=segment.strip()))
    return components


def build_video_messages(
    config: PluginConfig,
    view: BiliVideoView,
    page: int,
    source: str = "link",
    video_path: Path | None = None,
) -> list[list[BaseMessageComponent]]:
    """渲染模板为多条消息（原版 buildVideoMessages，返回组件列表的列表）。"""
    template = get_message_template(config, source)
    messages: list[list[BaseMessageComponent]] = []
    for part in _MESSAGE_SPLIT_PATTERN.split(template):
        rendered = _apply_text_placeholders(part, config, view, page)
        components = _build_components(rendered, view, video_path)
        if components:
            messages.append(components)
    return messages
