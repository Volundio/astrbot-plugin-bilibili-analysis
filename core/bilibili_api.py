"""B 站相关 API 封装。

对照移植原插件 old/src/bilibili-api.ts：
- fetch_video_view：经外置 API（api.xingzhige.com/API/b_parse/）解析视频信息与直链
- resolve_short_link：解析 b23.tv 等短链的真实地址
- download_video：流式下载视频到文件，支持大小上限与超时
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import aiohttp

from .config import PluginConfig
from .link_parser import VideoTarget
from .logger import PluginLogger, format_error
from .request_manager import RequestManager
from .video_file import create_video_file_name
from .wbi import BilibiliWbi

# B 站官方 API（v1.3.0 起为兜底源：主源失败时接管，信息全、含真实分 P；
# 直链需另走 playurl。数据中心 IP 可能触发 B 站风控，故不作主源）
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_API = "https://api.bilibili.com/x/player/wbi/playurl"

# 第三方解析 API（原版数据源，v1.3.0 起仍为主源：信息与视频直链一次返回。
# 该服务存在线路不稳、响应慢的间歇性故障，由官方 API 兜底）
XINGZHIGE_API = "https://api.xingzhige.com/API/b_parse/"

# playurl 请求参数：html5 平台 + fnval=1 取 MP4 直链（durl），
# 无登录态最高 480p，发群聊场景够用
PLAYURL_PARAMS = {"qn": "64", "fnval": "1", "platform": "html5", "high_quality": "1"}

# 原版 headers()
def _json_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }


def _to_number(value: Any) -> int:
    """字符串/数字容错转换（原版 toNumber）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value == value else 0  # NaN 判定
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


@dataclass
class BiliVideoStat:
    view: int = 0
    danmaku: int = 0
    favorite: int = 0
    coin: int = 0
    share: int = 0
    like: int = 0


@dataclass
class BiliVideoOwner:
    name: str = ""
    face: str = ""
    mid: int = 0


@dataclass
class BiliVideoView:
    """解析结果（对应原版 BiliVideoView，pages 原版即为空数组保留字段）。"""

    bvid: str = ""
    aid: int = 0
    title: str = ""
    desc: str = ""
    pic: str = ""
    duration: int = 0
    owner: BiliVideoOwner = field(default_factory=BiliVideoOwner)
    stat: BiliVideoStat = field(default_factory=BiliVideoStat)
    pages: list[dict[str, Any]] = field(default_factory=list)
    video_url: str = ""


class BilibiliApi:
    def __init__(
        self,
        config: PluginConfig,
        request_manager: RequestManager,
        logger: PluginLogger,
    ) -> None:
        self._config = config
        self._request_manager = request_manager
        self._logger = logger
        self._wbi = BilibiliWbi(config, request_manager, logger)

    # ---- 视频信息解析（原 API 为主，官方 API 兜底）----

    async def fetch_video_view(self, target: VideoTarget) -> BiliVideoView | None:
        """解析视频信息：原版 API（xingzhige）优先，失败降级 B 站官方 API。"""
        try:
            view = await self._fetch_view_xingzhige(target)
            if view is not None:
                return view
        except Exception as error:  # noqa: BLE001 —— 主源失败转兜底源
            self._logger.warn(f"主源解析失败，转官方 API 兜底：{format_error(error)}")

        try:
            return await self._fetch_view_official(target)
        except Exception as error:  # noqa: BLE001 —— 双源全败由上层统一提示
            self._logger.warn(
                f"官方兜底亦失败，主源与官方 API 均不可用：{format_error(error)}"
            )
            return None

    async def _fetch_view_official(self, target: VideoTarget) -> BiliVideoView | None:
        """B 站官方 view API（兜底源）。"""
        id_query = f"bvid={target.bvid}" if target.bvid else f"aid={target.aid}"
        api_url = f"{VIEW_API}?{id_query}"
        headers = {
            **_json_headers(self._config.user_agent),
            **await self._wbi.cookie_header(),
        }

        def parser(content: str) -> dict[str, Any] | None:
            result = self._parse_view_response(content)
            if result is None:
                # B 站风控会返回 HTML 验证页，记录片段便于确认
                self._logger.debug(f"官方 API 响应不可用，内容片段：{content[:200]!r}")
            return result

        # view 接口对分 P 返回全部 pages，分 P 号在构造消息与取直链时使用
        data = await self._request_manager.request(
            api_url,
            headers,
            parser,
            "视频信息 API(兜底)",
        )
        return self._view_from_data(data)

    @staticmethod
    def _parse_view_response(content: str) -> dict[str, Any] | None:
        """校验官方 view 响应：code==0 且含 bvid/title 才算成功。

        B 站风控（数据中心 IP 等）会返回 HTML 验证页而非 JSON，
        此处按解析失败处理，由请求竞速/代理模式兜底。
        """
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        if not data.get("bvid") or not data.get("title"):
            return None
        return data

    def _view_from_data(self, data: dict[str, Any]) -> BiliVideoView:
        """把官方 view 响应 data 构造为 BiliVideoView。"""
        stat = data.get("stat") or {}
        owner = data.get("owner") or {}
        pages = [
            {
                "page": _to_number(page.get("page")),
                "cid": _to_number(page.get("cid")),
                "part": str(page.get("part") or ""),
            }
            for page in (data.get("pages") or [])
            if isinstance(page, dict)
        ]
        return BiliVideoView(
            bvid=str(data.get("bvid") or ""),
            aid=_to_number(data.get("aid")),
            title=str(data.get("title") or ""),
            desc=str(data.get("desc") or ""),
            pic=str(data.get("pic") or ""),
            duration=_to_number(data.get("duration")),
            owner=BiliVideoOwner(
                name=str(owner.get("name") or ""),
                face=str(owner.get("face") or ""),
                mid=_to_number(owner.get("mid")),
            ),
            stat=BiliVideoStat(
                view=_to_number(stat.get("view")),
                like=_to_number(stat.get("like")),
                coin=_to_number(stat.get("coin")),
                favorite=_to_number(stat.get("favorite")),
                share=_to_number(stat.get("share")),
                danmaku=_to_number(stat.get("danmaku")),
            ),
            pages=pages,
            video_url="",
        )

    # ---- 视频直链（优先复用兜底源带回的直链，否则官方 playurl）----

    async def fetch_video_url(self, view: BiliVideoView, page: int) -> str | None:
        """获取视频 MP4 直链：主源（xingzhige）已带回直链则直接用，
        否则走官方 playurl（html5 平台，无登录最高 480p）。失败返回 None。"""
        if view.video_url:
            return view.video_url
        cid = self._pick_cid(view, page)
        if not cid:
            return None
        params = await self._wbi.signed_params(
            {"bvid": view.bvid, "cid": str(cid), **PLAYURL_PARAMS}
        )
        api_url = f"{PLAYURL_API}?{urlencode(params)}"
        headers = {
            **_json_headers(self._config.user_agent),
            **await self._wbi.cookie_header(),
        }
        try:
            return await self._request_manager.request(
                api_url,
                headers,
                self._parse_playurl_response,
                "视频直链 API",
            )
        except Exception as error:  # noqa: BLE001 —— 直链失败仅降级为不发视频
            self._logger.warn(f"获取视频直链失败：{format_error(error)}")
            return None

    @staticmethod
    def _parse_playurl_response(content: str) -> str | None:
        """校验 playurl 响应并取出第一条 MP4 直链（durl[0].url）。"""
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return None
        data = payload.get("data") or {}
        durl = data.get("durl")
        if isinstance(durl, list) and durl:
            url = str(durl[0].get("url") or "")
            # 原样使用（durl 直链可能为 http://，B 站 CDN 均可下载，
            # 与原版行为一致；不做协议假设）
            if url:
                return url
        return None

    @staticmethod
    def _pick_cid(view: BiliVideoView, page: int) -> int:
        """按分 P 号取 cid；越界或缺失时回落第一 P。"""
        for item in view.pages:
            if item.get("page") == page and item.get("cid"):
                return int(item["cid"])
        if view.pages and view.pages[0].get("cid"):
            return int(view.pages[0]["cid"])
        return 0

    # ---- 主源：xingzhige b_parse（信息与直链一次返回）----

    async def _fetch_view_xingzhige(self, target: VideoTarget) -> BiliVideoView | None:
        """经 xingzhige 解析（原版数据源，主源）。响应自带视频直链。"""
        base_url = (
            f"https://www.bilibili.com/video/{target.bvid}"
            if target.bvid
            else f"https://www.bilibili.com/video/av{target.aid}"
        )
        video_url = f"{base_url}?p={target.page}" if target.page > 1 else base_url
        api_url = f"{XINGZHIGE_API}?url={quote(video_url, safe='')}"

        def parser(content: str) -> dict[str, Any] | None:
            result = self._parse_xingzhige_response(content)
            if result is None:
                # 记录响应片段，便于定位「返回结果不可用」的具体内容
                self._logger.debug(f"主源响应不可用，内容片段：{content[:200]!r}")
            return result

        response = await self._request_manager.request(
            api_url,
            _json_headers(self._config.user_agent),
            parser,
            "视频解析 API(主源)",
        )
        data = response.get("data") or {}
        video_field = data.get("video") or {}
        stat = data.get("stat") or {}
        owner = data.get("owner") or {}
        return BiliVideoView(
            bvid=str(data.get("bvid") or ""),
            aid=_to_number(data.get("aid")),
            title=str(data.get("title") or video_field.get("title") or ""),
            desc=str(data.get("desc") or video_field.get("desc") or ""),
            pic=str(data.get("pic") or video_field.get("fm") or ""),
            duration=0,
            owner=BiliVideoOwner(
                name=str(owner.get("name") or ""),
                face=str(owner.get("face") or ""),
                mid=_to_number(owner.get("mid")),
            ),
            stat=BiliVideoStat(
                view=_to_number(stat.get("view")),
                like=_to_number(stat.get("like")),
                coin=_to_number(stat.get("coin")),
                favorite=_to_number(stat.get("favorite")),
                share=_to_number(stat.get("share")),
                # 兼容不同拼写（原版 danmuku ?? danmaku）
                danmaku=_to_number(
                    stat.get("danmuku")
                    if stat.get("danmuku") is not None
                    else stat.get("danmaku")
                ),
            ),
            pages=[],
            # 兜底源自带直链，fetch_video_url 直接复用，免去一次 playurl 请求
            video_url=str(video_field.get("url") or ""),
        )

    @staticmethod
    def _parse_xingzhige_response(content: str) -> dict[str, Any] | None:
        """只有结构完整且可用的响应才能作为竞速的成功结果（原版校验逻辑）。"""
        try:
            response = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(response, dict) or response.get("code") != 0:
            return None
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        video_field = data.get("video") or {}
        if not data.get("bvid") or not data.get("title"):
            return None
        if not video_field.get("url"):
            return None
        return response

    # ---- 短链解析 ----

    async def resolve_short_link(self, host: str, code: str) -> str | None:
        """手动跟随短链重定向，避免引入额外依赖（原版 resolveShortLink）。"""
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._request_manager.session.get(
                f"https://{host}/{code}",
                allow_redirects=False,  # redirect: manual
                headers={
                    **_json_headers(self._config.user_agent),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=timeout,
            ) as response:
                if 300 <= response.status < 400:
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    return urljoin(str(response.url), location)
                text = await response.text()
                match = re.search(r"""https?://[^"'<\s]+""", text, re.IGNORECASE)
                return match.group(0) if match else None
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self._logger.debug(f"短链解析失败：{format_error(error)}")
            return None

    # ---- 视频下载 ----

    async def download_video(
        self, url: str, title: str, dest_dir: Path
    ) -> tuple[Path, str] | None:
        """按大小上限流式下载视频到 dest_dir（原版 downloadVideo）。

        返回 (文件路径, MIME 类型)；超限或失败返回 None。
        """
        max_mb = self._config.max_file_size_mb
        max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0
        # sock_read：单次读超时 30s，直链 CDN 停滞时尽早失败而非干等总超时
        timeout = aiohttp.ClientTimeout(total=120, sock_read=30)
        self._logger.debug(f"开始下载视频：{url}（上限 {max_mb}MB）")
        try:
            async with self._request_manager.session.get(
                url,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "video/*,*/*;q=0.8",
                },
                timeout=timeout,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    self._logger.warn(
                        f"下载视频失败：HTTP {response.status}（{url}）"
                    )
                    return None

                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                mime = content_type if content_type.startswith("video/") else "video/mp4"

                declared_size: int | None = None
                try:
                    declared_size = int(response.headers.get("Content-Length") or "")
                except ValueError:
                    declared_size = None
                if max_bytes > 0 and declared_size is not None and declared_size > max_bytes:
                    self._logger.debug(
                        f"视频大小 {declared_size / 1024 / 1024:.2f}MB 超过 {max_mb}MB，跳过下载"
                    )
                    return None

                return await self._read_video_body(
                    response, max_bytes, max_mb, title, mime, dest_dir
                )
        except Exception as error:  # noqa: BLE001 —— 对齐原版 catch-all：网络/超时/磁盘 IO 异常均降级为跳过
            import traceback

            self._logger.warn(f"下载视频失败：{format_error(error)}（{url}）")
            self._logger.debug(f"下载视频失败堆栈：\n{traceback.format_exc()}")
            return None

    async def _read_video_body(
        self,
        response: aiohttp.ClientResponse,
        max_bytes: int,
        max_mb: int,
        title: str,
        mime: str,
        dest_dir: Path,
    ) -> tuple[Path, str] | None:
        """流式写入文件；缺少 Content-Length 时也能及时中止超限文件（原版 readVideoBody）。"""
        dest_dir.mkdir(parents=True, exist_ok=True)
        # 以标题命名，协议端展示的文件名即视频标题
        path = dest_dir / create_video_file_name(title, mime)
        total_bytes = 0
        try:
            with path.open("wb") as file:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if max_bytes > 0 and total_bytes > max_bytes:
                        self._logger.debug(f"视频大小超过 {max_mb}MB，跳过下载")
                        path.unlink(missing_ok=True)
                        return None
                    file.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._logger.debug(f"视频下载完成，大小：{total_bytes / 1024 / 1024:.2f}MB")
        return path, mime
