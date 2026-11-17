"""B 站 wbi 签名与 buvid3 指纹。

B 站官方 API 的访问要求：无 cookie 的裸请求可能触发风控（返回 HTML 验证页），
带 buvid3 指纹可稳定通过；部分接口（如 playurl）还要求 wbi 签名。

签名算法参考 bilibili-API-collect 的 wbi 规范。指纹与签名 key 均带缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from urllib.parse import urlencode

from .config import PluginConfig
from .logger import PluginLogger, format_error
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

# wbi key 缓存有效期（官方约 1 天，保守取 12 小时）
WBI_KEY_TTL_S = 12 * 3600
# 获取失败后的重试冷却时长，避免上游不可达时每次解析重复阻塞 10s+10s
WBI_FAIL_COOLDOWN_S = 60.0


class BilibiliWbi:
    """buvid3 指纹获取与 wbi 参数签名（均走 RequestManager、带缓存与失败冷却）。"""

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
        self._buvid3_fail_ts: float = 0.0
        self._wbi_keys: tuple[str, str] | None = None  # (img_key, sub_key)
        self._wbi_keys_ts: float = 0.0
        self._wbi_fail_ts: float = 0.0
        self._lock = asyncio.Lock()

    async def cookie_header(self) -> dict[str, str]:
        """返回带 buvid3 的 Cookie 头；获取失败降级为空（请求仍可能成功）。"""
        buvid3 = await self._ensure_buvid3()
        return {"Cookie": f"buvid3={buvid3}"} if buvid3 else {}

    async def signed_params(self, params: dict[str, str]) -> dict[str, str]:
        """对参数做 wbi 签名；key 获取失败时返回原参数（部分接口仍可通行）。"""
        keys = await self._ensure_wbi_keys()
        if keys is None:
            return dict(params)
        img_key, sub_key = keys
        return sign_wbi(params, img_key, sub_key)

    # ---- 内部实现 ----

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._config.user_agent,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def _ensure_buvid3(self) -> str | None:
        if self._buvid3:
            return self._buvid3
        if time.time() - self._buvid3_fail_ts < WBI_FAIL_COOLDOWN_S:
            return None
        async with self._lock:
            if self._buvid3:
                return self._buvid3
            if time.time() - self._buvid3_fail_ts < WBI_FAIL_COOLDOWN_S:
                return None
            try:
                self._buvid3 = await self._request_manager.request(
                    FINGER_API,
                    self._headers(),
                    _parse_buvid3_response,
                    "buvid3 指纹 API",
                )
            except Exception as error:  # noqa: BLE001 —— 指纹失败不阻塞主流程
                self._buvid3_fail_ts = time.time()
                self._logger.debug(f"获取 buvid3 失败：{format_error(error)}")
            return self._buvid3

    async def _ensure_wbi_keys(self) -> tuple[str, str] | None:
        now = time.time()
        if self._wbi_keys and now - self._wbi_keys_ts < WBI_KEY_TTL_S:
            return self._wbi_keys
        if now - self._wbi_fail_ts < WBI_FAIL_COOLDOWN_S:
            return self._wbi_keys
        async with self._lock:
            now = time.time()
            if self._wbi_keys and now - self._wbi_keys_ts < WBI_KEY_TTL_S:
                return self._wbi_keys
            if now - self._wbi_fail_ts < WBI_FAIL_COOLDOWN_S:
                return self._wbi_keys
            try:
                keys = await self._request_manager.request(
                    NAV_API,
                    self._headers(),
                    _parse_wbi_nav_response,
                    "wbi 密钥 API",
                )
                self._wbi_keys = keys
                self._wbi_keys_ts = time.time()
            except Exception as error:  # noqa: BLE001 —— key 失败降级为不签名或沿用旧缓存
                self._wbi_fail_ts = time.time()
                self._logger.debug(f"获取 wbi key 失败：{format_error(error)}")
            return self._wbi_keys


def _parse_buvid3_response(content: str) -> str | None:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    b_3 = (payload.get("data") or {}).get("b_3")
    return str(b_3) if b_3 else None


def _parse_wbi_nav_response(content: str) -> tuple[str, str] | None:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
    img_key = _key_from_url(str(wbi_img.get("img_url") or ""))
    sub_key = _key_from_url(str(wbi_img.get("sub_url") or ""))
    return (img_key, sub_key) if img_key and sub_key else None


def _key_from_url(url: str) -> str:
    """从 https://…/xxx.key 形式的 URL 中提取 key（不含扩展名）。"""
    if not url:
        return ""
    stem = url.rsplit("/", 1)[-1]
    return stem.split(".")[0]


def mixin_key(original: str) -> str:
    return "".join(original[i] for i in MIXIN_KEY_ENC_TAB if i < len(original))[:32]


def sign_wbi(params: dict[str, str], img_key: str, sub_key: str) -> dict[str, str]:
    """wbi 签名：参数排序、加 wts、过滤特殊字符后取 md5 得 w_rid。"""
    mixed = mixin_key(img_key + sub_key)
    signed = dict(params)
    signed["wts"] = str(round(time.time()))
    ordered = {key: signed[key] for key in sorted(signed)}
    filtered = {
        key: "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in ordered.items()
    }
    signed["w_rid"] = hashlib.md5(
        (urlencode(filtered) + mixed).encode()
    ).hexdigest()
    return signed
