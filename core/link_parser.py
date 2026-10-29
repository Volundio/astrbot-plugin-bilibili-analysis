"""链接与 JSON 卡片解析（纯函数模块）。

逐条对照移植原插件 old/src/link-parser.ts：
- 从消息文本中提取 B 站视频目标（bvid / av 号、分 P、b23.tv 等短链）
- 递归收集 QQ JSON 分享卡片中的所有字符串
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrbot.api.message_components import BaseMessageComponent, Json

# ---- 原版正则（old/src/link-parser.ts），Python 语法等价改写 ----

_BV_PATTERN = re.compile(r"bv1[0-9A-Za-z]{9}", re.IGNORECASE)
_AV_PATTERN = re.compile(r"av\d+", re.IGNORECASE)

# 消息中任意 http(s) URL（原版 urlPattern）
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# 短链域名：b23.tv / bili22|23|33|2233.cn（原版 shortPattern）
_SHORT_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(b23\.tv|bili(?:22|23|33|2233)\.cn)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)

# 独立 BV / AV 号（原版 standalonePattern）
_STANDALONE_PATTERN = re.compile(
    r"(?:^|[\s,，。；;：:()（）\[\]【】])(BV1[0-9A-Za-z]{9}|av\d+)"
    r"(?=$|[\s,，。；;：:()（）\[\]【】])",
    re.IGNORECASE,
)

# 视频页路径中的 BV / AV 号（原版 parseBilibiliUrl 内的正则）
_VIDEO_PATH_PATTERN = re.compile(r"/(BV1[0-9A-Za-z]{9}|av\d+)(?:/|$)", re.IGNORECASE)

# 尾部标点清洗（原版 cleanUrl）
_TRAILING_PUNCT_PATTERN = re.compile(r"[)\]}>.,;:!?，。；：！？]+$")

# 短链后紧随的 query（原版 shortPattern 处理中的 tail 匹配）
_SHORT_QUERY_PATTERN = re.compile(r"^\?([^\s<>\"']*)")

# 仅当字符串以 scheme:// 开头才可能构成合法 URL（对齐 new URL 失败语义）
_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@dataclass(frozen=True)
class VideoTarget:
    """已确定视频号的目标（old/src/link-parser.ts ResolvedVideoTarget）。

    origin 记录目标在消息中的出现形态，供来源白名单过滤：
    - "link"：完整视频页链接（bilibili.com/video/…）
    - "short"：短链域名直带的 BV/AV 号（如 b23.tv/BV1xx…）
    - "id"：消息中独立出现的 BV/AV 号（不属于任何 URL）
    """

    bvid: str | None = None
    aid: str | None = None
    page: int = 1
    origin: str = "link"

    @property
    def key(self) -> str:
        video_id = self.bvid if self.bvid else f"av{self.aid}"
        return f"{video_id}:{self.page}"


@dataclass(frozen=True)
class ShortTarget:
    """待解析的短链目标（old/src/link-parser.ts ShortVideoTarget）。"""

    host: str
    code: str
    page: int = 1

    @property
    def key(self) -> str:
        return f"{self.host}/{self.code}:{self.page}"


BilibiliTarget = VideoTarget | ShortTarget


def target_origin(target: BilibiliTarget) -> str:
    """返回目标的来源形态：card 由消息级判定，其余为 link / short / id。"""
    if isinstance(target, ShortTarget):
        return "short"
    return target.origin


def _normalize_id(raw_id: str) -> str | None:
    """归一化视频号：BV 前缀统一大写、av 号统一小写（原版 normalizeId）。"""
    bv_match = _BV_PATTERN.search(raw_id)
    if bv_match:
        bvid = bv_match.group(0)
        return f"BV{bvid[2:]}"
    av_match = _AV_PATTERN.search(raw_id)
    if av_match:
        return av_match.group(0).lower()
    return None


def _parse_page(params: dict[str, list[str]]) -> int:
    """读取 ?p= / ?page= 分 P 参数，非法或缺失返回 1（原版 parsePage）。"""
    for name in ("p", "page"):
        values = params.get(name)
        if values:
            try:
                page = int(values[0])
            except (TypeError, ValueError):
                continue
            if page > 0:
                return page
    return 1


def _parse_page_from_query(query: str) -> int:
    if not query:
        return 1
    return _parse_page(parse_qs(query))


def _create_video_target(
    raw_id: str, page: int, origin: str = "link"
) -> VideoTarget | None:
    normalized = _normalize_id(raw_id)
    if not normalized:
        return None
    if normalized.startswith("av"):
        return VideoTarget(aid=normalized[2:], page=page, origin=origin)
    return VideoTarget(bvid=normalized, page=page, origin=origin)


def _parse_bilibili_url(raw_url: str) -> BilibiliTarget | None:
    """解析完整 URL，仅接受 bilibili.com/video/<BV|av>（原版 parseBilibiliUrl）。

    对齐 JS ``new URL`` 的失败语义：缺少 scheme:// 前缀（含被前导字符污染的
    匹配串）时直接返回 None。
    """
    raw_url = raw_url.strip()
    if not _SCHEME_PATTERN.match(raw_url):
        return None
    try:
        parsed = urlparse(raw_url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return None
    hostname = re.sub(r"^www\.", "", hostname)
    hostname = re.sub(r"^m\.", "", hostname)
    if hostname != "bilibili.com" or not parsed.path.startswith("/video/"):
        return None
    match = _VIDEO_PATH_PATTERN.search(parsed.path)
    if not match:
        return None
    return _create_video_target(match.group(1), _parse_page(parse_qs(parsed.query)))


def _clean_url(raw: str) -> str:
    return _TRAILING_PUNCT_PATTERN.sub("", raw.strip())


def _visit_json_value(value: Any, output: list[str]) -> None:
    """递归收集 JSON 卡片中的所有字符串；字符串本身是 JSON 时继续展开。

    对应原版 visitJsonValue：卡片链接可能被转义嵌套，收集所有字符串避免漏掉。
    """
    if isinstance(value, str):
        output.append(value)
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                _visit_json_value(json.loads(stripped), output)
            except (json.JSONDecodeError, RecursionError):
                # JSON 卡片里也可能混入普通文本，忽略解析失败
                pass
        return
    if isinstance(value, list):
        for item in value:
            _visit_json_value(item, output)
        return
    if isinstance(value, dict):
        for item in value.values():
            _visit_json_value(item, output)


def collect_json_card_text(chain: list[BaseMessageComponent]) -> str:
    """提取消息链中所有 QQ JSON 卡片组件内的字符串（原版 collectJsonCardText）。"""
    output: list[str] = []
    for component in chain:
        if isinstance(component, Json):
            _visit_json_value(component.data, output)
    return "\n".join(output)


def parse_bilibili_content(
    content: str,
    parse_standalone_ids: bool = True,
) -> list[BilibiliTarget]:
    """从消息文本提取全部 B 站视频目标，按出现顺序去重。

    对应原版 parseBilibiliContent 的多轮正则提取：
    任意 URL → 短链 → 独立 BV/AV 号。
    """
    # JSON 转义的 / 还原（原版 normalized）
    normalized = content.replace("\\/", "/")
    targets: dict[str, BilibiliTarget] = {}
    seen_video_ids: set[str] = set()

    def push_target(target: BilibiliTarget | None) -> None:
        if target is None:
            return
        if isinstance(target, VideoTarget):
            video_id = target.bvid if target.bvid else f"av{target.aid}"
            seen_video_ids.add(video_id)
        targets.setdefault(target.key, target)

    # 第一轮：任意 URL（含视频页与会被后续轮重复覆盖的链接，先到先得）
    for match in _URL_PATTERN.finditer(normalized):
        push_target(_parse_bilibili_url(_clean_url(match.group(0))))

    # 第二轮：短链；短链 code 本身形如 BV/av 号时直接当视频目标（原版行为），
    # 其来源形态仍按短链计（消息中出现的是短链域名）
    for match in _SHORT_PATTERN.finditer(normalized):
        tail = normalized[match.end():]
        query_match = _SHORT_QUERY_PATTERN.match(tail)
        page = _parse_page_from_query(query_match.group(1) if query_match else "")
        direct = _create_video_target(match.group(2), page, origin="short")
        if direct:
            push_target(direct)
        else:
            push_target(ShortTarget(host=match.group(1), code=match.group(2), page=page))

    # 第三轮：独立 BV / AV 号（排除已通过链接出现的视频号）
    if parse_standalone_ids:
        for match in _STANDALONE_PATTERN.finditer(normalized):
            normalized_id = _normalize_id(match.group(1))
            if normalized_id and normalized_id not in seen_video_ids:
                push_target(_create_video_target(normalized_id, 1, origin="id"))

    return list(targets.values())


def target_from_resolved_url(url: str) -> VideoTarget | None:
    """短链跳转后的完整 URL 转为视频目标（原版 targetFromResolvedUrl）。"""
    target = _parse_bilibili_url(url)
    return target if isinstance(target, VideoTarget) else None
