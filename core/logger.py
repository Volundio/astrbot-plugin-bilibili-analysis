"""日志封装。

对应原插件 old/src/logger.ts：debug 级别日志仅在配置开启时以 info 级别输出，
warn/error 始终输出。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger as _root_logger


def format_error(error: BaseException) -> str:
    """异常描述统一为「类型名: 消息」。

    str(TimeoutError()) 等异常的消息为空字符串，只打 str(error) 会得到
    空白日志，无法定位失败原因。
    """
    message = str(error).strip()
    type_name = type(error).__name__
    return f"{type_name}: {message}" if message else type_name


class PluginLogger:
    """带调试开关的日志器，统一加插件前缀便于过滤。"""

    def __init__(self, debug_enabled: bool, prefix: str = "bilibili-analysis") -> None:
        self._debug_enabled = debug_enabled
        self._prefix = f"[{prefix}]"

    def debug(self, message: str, *params: Any) -> None:
        # 对齐原版：调试日志开启时用 info 级别输出，保证默认日志级别下可见
        if self._debug_enabled:
            _root_logger.info(f"{self._prefix} {message}", *params)

    def info(self, message: str, *params: Any) -> None:
        _root_logger.info(f"{self._prefix} {message}", *params)

    def warn(self, message: str, *params: Any) -> None:
        _root_logger.warning(f"{self._prefix} {message}", *params)

    def error(self, message: str, *params: Any) -> None:
        _root_logger.error(f"{self._prefix} {message}", *params)
