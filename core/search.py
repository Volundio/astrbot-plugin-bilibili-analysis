"""B 站视频搜索（点播功能数据源）。

原版点播用 koishi-puppeteer 截图搜索页；AstrBot 无浏览器服务，
改为直接调用 B 站官方搜索 API（需要 wbi 签名与 buvid3 指纹），
差异已登记于 docs/DESIGN.md 第 9 节。

签名算法参考 bilibili-API-collect 的 wbi 规范。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .config import PluginConfig
from .logger import PluginLogger
from .request_manager import RequestManager

# wbi key 重排表（bilibili-API-collect / mixinKeyEncTab）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

FINGER_API = "https://api.bilibili.com/x/frontend/finger/spi"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
SEARCH_API = "https://api.bilibili.com/x/web-interface/wbi/search/type"

# 风控/签名失效时刷新缓存并重试的次数
MAX_RETRY = 1

# wbi key 缓存有效期（官方约 1 天，保守取 12 小时）
WBI_KEY_TTL_S = 12 * 3600

# 搜索结果标题中的高亮标签
_HIGHLIGHT_TAG_PATTERN = re.compile(r"<em class=\"keyword\">|</em>")

# search/type 返回 code 为 -412 时表示触发了风控
RISK_CONTROL_CODES = {-412, -799}


@dataclass(frozen=True)
class SearchResultItem:
    index: int  # 展示序号（从 1 开始）
    bvid: str
    title: str
    author: str
    play: int
    duration: str


class BilibiliSearchApi:
    """带 buvid3 指纹与 wbi 签名的 B 站搜索。"""

    def __init__(
        self,
        config: PluginConfig,
        request_manager: RequestManager,
        logger: PluginLogger,
    ) -> None:
        self._config = config
        self._request_manager = request_manager
        self._logger = logger
        self._buvid3: str | None = None
        self._wbi_keys: tuple[str, str] | None = None  # (img_key, sub_key)
        self._wbi_keys_ts: float = 0.0
        self._refresh_lock = asyncio.Lock()

    # ---- 对外入口 ----

    async def search_video(self, keyword: str, page: int = 1) -> list[SearchResultItem] | None:
        """搜索视频。成功返回结果列表（可为空列表），失败返回 None。"""
        for attempt in range(MAX_RETRY + 1):
            try:
                return await self._search_once(keyword, page, refresh=attempt > 0)
            except (aiohttp.ClientError, asyncio.TimeoutError, _SearchError) as error:
                self._logger.warn(f"B 站搜索失败（第 {attempt + 1} 次）：{error}")
        return None

    # ---- 内部实现 ----

    async def _search_once(
        self, keyword: str, page: int, refresh: bool
    ) -> list[SearchResultItem]:
        if refresh:
            self._wbi_keys = None
            self._buvid3 = None

        buvid3 = await self._ensure_buvid3()
        img_key, sub_key = await self._ensure_wbi_keys()

        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": str(page),
        }
        signed = self._sign_wbi(params, img_key, sub_key)

        timeout = aiohttp.ClientTimeout(total=10)
        async with self._request_manager.session.get(
            SEARCH_API,
            params=signed,
            headers={
                "User-Agent": self._config.user_agent,
                "Referer": "https://search.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Cookie": f"buvid3={buvid3}",
            },
            timeout=timeout,
        ) as response:
            if response.status != 200:
                raise _SearchError(f"HTTP {response.status}")
            payload: dict[str, Any] = await response.json(content_type=None)

        code = payload.get("code")
        if code in RISK_CONTROL_CODES:
            raise _SearchError(f"触发风控（code={code}）")
        if code != 0:
            raise _SearchError(f"接口返回 code={code}, {payload.get('message', '')}")

        results = (payload.get("data") or {}).get("result") or []
        items: list[SearchResultItem] = []
        for index, entry in enumerate(results, start=1):
            bvid = str(entry.get("bvid") or "")
            if not bvid:
                continue
            play = entry.get("play")
            items.append(
                SearchResultItem(
                    index=index,
                    bvid=bvid,
                    title=self._strip_highlight(str(entry.get("title") or "")),
                    author=str(entry.get("author") or ""),
                    play=int(play) if isinstance(play, (int, float)) else 0,
                    duration=str(entry.get("duration") or ""),
                )
            )
        return items

    async def _ensure_buvid3(self) -> str:
        if self._buvid3:
            return self._buvid3
        async with self._refresh_lock:
            if self._buvid3:
                return self._buvid3
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._request_manager.session.get(
                FINGER_API,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Referer": "https://www.bilibili.com/",
                },
                timeout=timeout,
            ) as response:
                payload: dict[str, Any] = await response.json(content_type=None)
            b_3 = (payload.get("data") or {}).get("b_3")
            if not b_3:
                raise _SearchError(f"获取 buvid3 失败：{payload.get('message', payload)}")
            self._buvid3 = str(b_3)
            return self._buvid3

    async def _ensure_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys and time.time() - self._wbi_keys_ts < WBI_KEY_TTL_S:
            return self._wbi_keys
        async with self._refresh_lock:
            if self._wbi_keys and time.time() - self._wbi_keys_ts < WBI_KEY_TTL_S:
                return self._wbi_keys
            timeout = aiohttp.ClientTimeout(total=10)
            async with self._request_manager.session.get(
                NAV_API,
                headers={
                    "User-Agent": self._config.user_agent,
                    "Referer": "https://www.bilibili.com/",
                },
                timeout=timeout,
            ) as response:
                payload: dict[str, Any] = await response.json(content_type=None)
            wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
            img_key = self._key_from_url(str(wbi_img.get("img_url") or ""))
            sub_key = self._key_from_url(str(wbi_img.get("sub_url") or ""))
            if not img_key or not sub_key:
                raise _SearchError("获取 wbi key 失败")
            self._wbi_keys = (img_key, sub_key)
            self._wbi_keys_ts = time.time()
            return self._wbi_keys

    # ---- wbi 签名（纯函数）----

    @staticmethod
    def _key_from_url(url: str) -> str:
        """从 https://…/xxx.key 形式的 URL 中提取 key（不含扩展名）。"""
        if not url:
            return ""
        stem = url.rsplit("/", 1)[-1]
        return stem.split(".")[0]

    @staticmethod
    def _mixin_key(original: str) -> str:
        return "".join(original[i] for i in MIXIN_KEY_ENC_TAB if i < len(original))[:32]

    @classmethod
    def _sign_wbi(cls, params: dict[str, str], img_key: str, sub_key: str) -> dict[str, str]:
        mixin = cls._mixin_key(img_key + sub_key)
        signed = dict(params)
        signed["wts"] = str(round(time.time()))
        # 按 key 排序并过滤特殊字符
        ordered = {key: signed[key] for key in sorted(signed)}
        filtered = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in ordered.items()
        }
        query = urlencode(filtered)
        signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return signed

    @staticmethod
    def _strip_highlight(title: str) -> str:
        return _HIGHLIGHT_TAG_PATTERN.sub("", title)


class _SearchError(RuntimeError):
    """搜索流程中可识别的业务错误（风控/接口异常）。"""
