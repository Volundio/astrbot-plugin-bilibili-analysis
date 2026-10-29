"""HTTP 请求管理：直连 / Apifox 代理 / 并行竞速。

对照移植原插件 old/src/request-manager.ts：
- 三种请求模式（proxy / direct / parallel），并行模式下同时发起
  直连与代理，取首个「结构有效」的结果，成功后取消其余请求
- 每个请求独立超时；全部失败时抛出聚合错误
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp

from .config import PluginConfig
from .logger import PluginLogger

# 原版 APIFOX_PROXY_URL
APIFOX_PROXY_URL = "https://web-proxy.apifox.cn/api/v1/request"

T = TypeVar("T")

Parser = Callable[[str], T | None]


def _error_message(error: BaseException) -> str:
    return str(error) or type(error).__name__


class RequestManager:
    """共享 aiohttp 会话的竞速请求器。"""

    def __init__(self, config: PluginConfig, logger: PluginLogger) -> None:
        self._config = config
        self._logger = logger
        self._session: aiohttp.ClientSession | None = None
        self._disposed = False

    @property
    def session(self) -> aiohttp.ClientSession:
        """懒加载共享会话；插件卸载时由 close() 统一关闭。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._config.user_agent}
            )
        return self._session

    async def close(self) -> None:
        self._disposed = True
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def plain_get_text(self, url: str, headers: dict[str, str]) -> str:
        """直连 GET，返回响应文本。非 2xx 抛异常（原版 requestText）。"""
        timeout = aiohttp.ClientTimeout(total=10)
        async with self.session.get(url, headers=headers, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}")
            return await response.text()

    async def request(
        self,
        target_url: str,
        headers: dict[str, str],
        parser: Parser[T],
        description: str,
    ) -> T:
        """按配置的请求模式发起请求，返回首个通过 parser 校验的结果。

        parser 返回 None 视为「响应不可用」（结构不完整），该候选失败；
        对应原版 RequestManager.request + race。
        """
        if self._disposed:
            raise RuntimeError(f"{description}请求已销毁")

        candidates: list[tuple[str, Callable[[], Awaitable[T]]]] = []
        mode = self._config.request_mode

        if mode in ("parallel", "direct"):
            candidates.append(("直连", self._make_candidate(
                lambda: self.plain_get_text(target_url, headers), parser, "直连"
            )))

        if mode in ("parallel", "proxy"):
            candidates.append(("代理", self._make_candidate(
                lambda: self._proxy_get_text(target_url), parser, "代理"
            )))

        if not candidates:
            raise RuntimeError(f"{description}没有可用请求")

        return await self._race(candidates, description)

    def _make_candidate(
        self,
        fetch: Callable[[], Awaitable[str]],
        parser: Parser[T],
        label: str,
    ) -> Callable[[], Awaitable[T]]:
        async def run() -> T:
            content = await fetch()
            result = parser(content)
            if result is None:
                raise RuntimeError(f"{label}返回结果不可用")
            return result

        return run

    async def _proxy_get_text(self, target_url: str) -> str:
        """通过 Apifox 代理访问目标 URL（原版 requestTextByProxy）。"""
        timeout = aiohttp.ClientTimeout(total=10)
        async with self.session.post(
            APIFOX_PROXY_URL,
            headers={
                "api-u": target_url,
                "api-o0": "method=GET, timings=true, timeout=10000",
                "Content-Type": "application/json",
            },
            data="{}",
            timeout=timeout,
        ) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}")
            return await response.text()

    async def _race(
        self,
        candidates: list[tuple[str, Callable[[], Awaitable[T]]]],
        description: str,
    ) -> T:
        """竞速：首个成功者胜出并取消其余；全部失败抛聚合错误（原版 race）。"""
        loop = asyncio.get_running_loop()
        tasks: dict[asyncio.Task[T], str] = {
            loop.create_task(run(), name=f"{description}:{label}"): label
            for label, run in candidates
        }

        self._logger.debug(f"{description}开始请求：{'、'.join(tasks.values())}")

        failures: list[str] = []
        try:
            while tasks:
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    label = tasks.pop(task)
                    if task.cancelled():
                        failures.append(f"{label}：请求已取消")
                        continue
                    exc = task.exception()
                    if exc is None:
                        # 首个有效结果命中，取消其余请求
                        self._logger.debug(f"{description}命中：{label}")
                        return task.result()
                    failures.append(f"{label}：{_error_message(exc)}")
            detail = "；".join(failures)
            raise RuntimeError(f"{description}全部失败：{detail}")
        finally:
            for task in tasks:
                task.cancel()
            # 等待取消完成，避免悬挂任务告警
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
