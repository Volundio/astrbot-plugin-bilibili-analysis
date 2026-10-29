"""频率限制器。

逐方法对照移植原插件 old/src/rate-limiter.ts：
- 单用户 30 秒窗口内最多 5 个视频，超限拉黑并清空其队列任务
- 同一会话同时处理的视频数上限 3（频道并发保护）
"""

from __future__ import annotations

import asyncio

from .config import PluginConfig

# 单用户累计 5 个视频后触发暂停（原版 MAX_VIDEO_COUNT）
MAX_VIDEO_COUNT = 5

# 同一会话同时处理的视频数上限（原版 CHANNEL_ACTIVE_LIMIT）
CHANNEL_ACTIVE_LIMIT = 3

# 用户短时间频率的统计窗口（原版 USER_FREQUENCY_WINDOW）
USER_FREQUENCY_WINDOW_S = 30.0

BlockReason = str  # "user-video-limit" | "channel-video-limit"


class _UserRecord:
    __slots__ = ("count", "timer_handle")

    def __init__(self) -> None:
        self.count = 0
        self.timer_handle: asyncio.TimerHandle | None = None


class VideoRateLimiter:
    """会话/用户两级频率限制。所有键均为「会话标识:用户标识」。"""

    def __init__(self, config: PluginConfig) -> None:
        self._config = config
        self._user_records: dict[str, _UserRecord] = {}
        self._active_channel_counts: dict[str, int] = {}
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for record in self._user_records.values():
            if record.timer_handle:
                record.timer_handle.cancel()
        self._user_records.clear()
        self._active_channel_counts.clear()

    # ---- 入队前检查（原版 checkNewMessage）----
    # 单链接消息提前计数；多链接消息交给处理时逐条判断
    def check_new_message(
        self, channel_id: str, user_id: str, link_count: int
    ) -> BlockReason | None:
        if self._disposed or not self._config.user_rate_limit:
            return None
        count = self._get_user_count(channel_id, user_id)
        if count >= MAX_VIDEO_COUNT:
            return "user-video-limit"
        if link_count > 1 and count >= MAX_VIDEO_COUNT - 1:
            return "user-video-limit"
        if link_count == 1 and count + 1 >= MAX_VIDEO_COUNT:
            return "user-video-limit"
        return None

    def reserve_message(self, channel_id: str, user_id: str, link_count: int) -> None:
        """入队时为单链接消息预占计数（原版 reserveMessage）。"""
        if self._disposed or not self._config.user_rate_limit:
            return
        if link_count == 1:
            self._add_user_count(channel_id, user_id, 1)

    def block_user(self, channel_id: str, user_id: str) -> None:
        """触发限制时累加计数使用户进入拉黑状态（原版 blockUser）。"""
        if self._disposed or not self._config.user_rate_limit:
            return
        self._add_user_count(channel_id, user_id, 1)

    def is_user_blocked(self, channel_id: str, user_id: str) -> bool:
        if self._disposed or not self._config.user_rate_limit:
            return False
        return self._get_user_count(channel_id, user_id) >= MAX_VIDEO_COUNT

    def can_process_user_video(
        self, channel_id: str, user_id: str, is_multi_link_message: bool
    ) -> bool:
        """处理每个链接前的用户配额校验（原版 canProcessUserVideo）。"""
        if self._disposed or not self._config.user_rate_limit:
            return True
        limit = MAX_VIDEO_COUNT - 1 if is_multi_link_message else MAX_VIDEO_COUNT
        return self._get_user_count(channel_id, user_id) < limit

    def record_processed_video(self, channel_id: str, user_id: str) -> None:
        """多链接消息中每处理完一个链接计数（原版 recordProcessedVideo）。"""
        if self._disposed or not self._config.user_rate_limit:
            return
        self._add_user_count(channel_id, user_id, 1)

    # ---- 会话并发（原版 canStartChannelVideo / start / end）----
    def can_start_channel_video(self, channel_id: str) -> bool:
        if self._disposed or not self._config.user_rate_limit:
            return True
        return self._active_channel_counts.get(channel_id, 0) < CHANNEL_ACTIVE_LIMIT

    def start_channel_video(self, channel_id: str) -> None:
        if self._disposed:
            return
        self._active_channel_counts[channel_id] = (
            self._active_channel_counts.get(channel_id, 0) + 1
        )

    def end_channel_video(self, channel_id: str) -> None:
        if self._disposed:
            return
        count = max(0, self._active_channel_counts.get(channel_id, 0) - 1)
        if count == 0:
            self._active_channel_counts.pop(channel_id, None)
        else:
            self._active_channel_counts[channel_id] = count

    # ---- 内部 ----
    def _add_user_count(self, channel_id: str, user_id: str, count: int) -> None:
        key = self._user_key(channel_id, user_id)
        record = self._user_records.get(key)
        if record is None:
            record = _UserRecord()
            self._user_records[key] = record

        record.count += count
        if record.timer_handle:
            record.timer_handle.cancel()
        # 窗口结束自动清除记录，避免内存持续增长（原版 ctx.setTimeout 自清）
        loop = asyncio.get_running_loop()
        record.timer_handle = loop.call_later(
            USER_FREQUENCY_WINDOW_S, self._user_records.pop, key, None
        )

    def _get_user_count(self, channel_id: str, user_id: str) -> int:
        record = self._user_records.get(self._user_key(channel_id, user_id))
        return record.count if record else 0

    @staticmethod
    def _user_key(channel_id: str, user_id: str) -> str:
        return f"{channel_id}:{user_id}"
