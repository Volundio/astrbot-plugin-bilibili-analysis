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
from urllib.parse import quote, urljoin

import aiohttp

from .config import PluginConfig
from .link_parser import VideoTarget
from .logger import PluginLogger, format_error
from .request_manager import RequestManager
from .video_file import create_video_file_name

# 原版固定的解析 API 端点
PARSE_API_BASE = "https://api.xingzhige.com/API/b_parse/"

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

    # ---- 视频信息解析 ----

    async def fetch_video_view(self, target: VideoTarget) -> BiliVideoView | None:
        """经外置 API 解析视频信息与视频直链（原版 fetchVideoView）。"""
        base_url = (
            f"https://www.bilibili.com/video/{target.bvid}"
            if target.bvid
            else f"https://www.bilibili.com/video/av{target.aid}"
        )
        video_url = f"{base_url}?p={target.page}" if target.page > 1 else base_url
        api_url = f"{PARSE_API_BASE}?url={quote(video_url, safe='')}"

        response = await self._request_manager.request(
            api_url,
            _json_headers(self._config.user_agent),
            self._parse_external_api_response,
            "视频解析 API",
        )
        data = response.get("data") or {}
        if not data:
            return None

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
                # 兼容不同拼写（原版 danmuku ?? danmaku，需区分「键缺失」与「值为 null」）
                danmaku=_to_number(
                    stat.get("danmuku")
                    if stat.get("danmuku") is not None
                    else stat.get("danmaku")
                ),
            ),
            pages=[],
            video_url=str(video_field.get("url") or ""),
        )

    @staticmethod
    def _parse_external_api_response(content: str) -> dict[str, Any] | None:
        """只有结构完整且可用的响应才能作为竞速的成功结果（原版同名方法）。"""
        try:
            response = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(response, dict) or response.get("code") != 0:
            return None
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        bvid = data.get("bvid")
        aid = _to_number(data.get("aid"))
        video_field = data.get("video") or {}
        title = data.get("title") or video_field.get("title")
        if not bvid or not aid or not title:
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
