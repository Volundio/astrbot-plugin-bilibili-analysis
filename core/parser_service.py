"""解析编排服务（核心模块）。

对照移植原插件 old/src/parser-service.ts：
- 全局串行队列处理同一条消息里的多个链接
- 频率限制在入队前与处理时双重校验，超限清空对应用户/会话任务
- 频道级「同链接最小间隔」去重
- 单个目标处理流程：等待提示 → 解析 API → 撤回提示 → 按模板下载视频 → 渲染 → 发送
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api.event import AstrMessageEvent

from .bilibili_api import BilibiliApi, BiliVideoView
from .config import PluginConfig
from .link_parser import ShortTarget, VideoTarget, target_from_resolved_url
from .logger import PluginLogger, format_error
from . import message_sender
from .message_sender import send_parsed_messages, send_wait_tip, recall_message
from .rate_limiter import VideoRateLimiter
from .video_formatter import build_video_messages, has_video_placeholder


@dataclass
class _SessionTask:
    """一条待处理消息的解析任务（原版 SessionTask）。"""

    event: AstrMessageEvent
    targets: list[VideoTarget | ShortTarget]
    source: str  # "link" | "card"
    timestamp: float = field(default_factory=time.time)


class VideoParseService:
    def __init__(
        self,
        config: PluginConfig,
        api: BilibiliApi,
        logger: PluginLogger,
        rate_limiter: VideoRateLimiter,
        video_dir: Path,
    ) -> None:
        self._config = config
        self._api = api
        self._logger = logger
        self._rate_limiter = rate_limiter
        self._video_dir = video_dir

        # 原版 lastProcessedUrls：会话+目标 → 上次处理时间
        self._last_processed: dict[str, float] = {}
        # 原版 sessionQueue + isProcessingSession → asyncio.Queue + 常驻 worker
        self._queue: asyncio.Queue[_SessionTask | None] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._disposed = False

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动串行 worker（插件 initialize 时调用）。"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(
                self._worker_loop(), name="bilibili-analysis:parser"
            )

    async def close(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._worker and not self._worker.done():
            self._queue.put_nowait(None)  # sentinel 唤醒 worker 尽快退出
            try:
                await asyncio.wait_for(self._worker, timeout=5)
            except asyncio.TimeoutError:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._last_processed.clear()

    @property
    def is_disposed(self) -> bool:
        return self._disposed

    # ---- 入队（原版 enqueue，监听器调用）----

    def enqueue(
        self,
        event: AstrMessageEvent,
        targets: list[VideoTarget | ShortTarget],
        source: str,
    ) -> str | None:
        """消息入队。返回拦截原因（user-video-limit / channel-video-limit）或 None。"""
        if self._disposed or not targets:
            return None

        channel_id = event.unified_msg_origin
        user_id = event.get_sender_id() or ""

        reason = self._rate_limiter.check_new_message(channel_id, user_id, len(targets))
        if reason:
            if reason == "user-video-limit" and not self._rate_limiter.is_user_blocked(
                channel_id, user_id
            ):
                self._rate_limiter.block_user(channel_id, user_id)
            self._clear_queue_by_user(channel_id, user_id)
            return reason

        self._rate_limiter.reserve_message(channel_id, user_id, len(targets))
        self._queue.put_nowait(_SessionTask(event=event, targets=targets, source=source))
        self._logger.debug(f"收到解析消息，队列长度：{self._queue.qsize()}")
        self.start()
        return None

    # ---- 点播入口（原版 processTarget：绕过队列直接处理单个目标）----

    async def process_target(self, event: AstrMessageEvent, target: VideoTarget) -> bool:
        if self._disposed:
            return False
        return await self._process_one_target(event, target, "link")

    # ---- 串行 worker（原版 processSessionQueue / processSessionTask）----

    async def _worker_loop(self) -> None:
        while not self._disposed:
            task = await self._queue.get()
            try:
                if task is None or self._disposed:
                    continue
                if self._rate_limiter.is_user_blocked(
                    task.event.unified_msg_origin, task.event.get_sender_id() or ""
                ):
                    self._logger.debug("用户已触发频率限制，跳过剩余解析")
                    continue
                await self._process_session_task(task)
            except Exception as error:  # noqa: BLE001 —— worker 不得因单任务失败退出
                self._logger.error(f"处理解析任务失败：{error}")
            finally:
                self._queue.task_done()
        self._logger.debug("解析队列处理完成")

    async def _process_session_task(self, task: _SessionTask) -> None:
        if self._disposed:
            return
        channel_id = task.event.unified_msg_origin
        user_id = task.event.get_sender_id() or ""
        is_multi_link_message = len(task.targets) > 1

        for index, target in enumerate(task.targets):
            if self._disposed:
                return
            if not self._rate_limiter.can_process_user_video(
                channel_id, user_id, is_multi_link_message
            ):
                self._clear_queue_by_user(channel_id, user_id)
                break
            if not self._rate_limiter.can_start_channel_video(channel_id):
                self._clear_queue_by_channel(channel_id)
                break

            self._logger.debug(f"处理第 {index + 1}/{len(task.targets)} 个链接")
            self._rate_limiter.start_channel_video(channel_id)
            try:
                if isinstance(target, VideoTarget):
                    await self._process_one_target(task.event, target, task.source)
                else:
                    await self._process_short_target(task.event, target, task.source)
            finally:
                self._rate_limiter.end_channel_video(channel_id)

            if is_multi_link_message:
                self._rate_limiter.record_processed_video(channel_id, user_id)

    async def _process_short_target(
        self, event: AstrMessageEvent, target: ShortTarget, source: str
    ) -> bool:
        url = await self._api.resolve_short_link(target.host, target.code)
        if not url:
            self._logger.debug(f"短链解析失败：{target.host}/{target.code}")
            return False
        resolved = target_from_resolved_url(url)
        if resolved is None:
            self._logger.debug(f"短链目标不是普通视频：{url}")
            return False
        # 原版 resolved.page = resolved.page || target.page（parse_page 恒 ≥1，保真照搬）；
        # 短链跳转所得目标保留短链来源标记
        resolved = VideoTarget(
            bvid=resolved.bvid,
            aid=resolved.aid,
            page=resolved.page or target.page,
            origin="short",
        )
        return await self._process_one_target(event, resolved, source)

    # ---- 单目标处理（原版 processOneTarget）----

    def _is_processed_recently(self, target: VideoTarget, channel_id: str) -> bool:
        """同会话同目标在最小间隔内已处理过则跳过（原版 isProcessedRecently）。"""
        interval_ms = self._config.min_interval_s * 1000
        current = time.time() * 1000
        expired = [
            key
            for key, ts in self._last_processed.items()
            if current - ts >= interval_ms
        ]
        for key in expired:
            del self._last_processed[key]

        channel_key = f"{channel_id}:{target.key}"
        last_time = self._last_processed.get(channel_key)
        if last_time is not None and current - last_time < interval_ms:
            self._logger.debug(f"相同视频跳过：{target.key}，会话：{channel_id}")
            return True
        self._last_processed[channel_key] = current
        return False

    async def _process_one_target(
        self,
        event: AstrMessageEvent,
        target: VideoTarget,
        source: str,
    ) -> bool:
        if self._disposed or self._is_processed_recently(
            target, event.unified_msg_origin
        ):
            return False

        wait_tip_id = await self._send_wait_tip(event)
        view: BiliVideoView | None = None
        try:
            view = await self._api.fetch_video_view(target)
        except Exception as error:
            self._logger.warn(f"请求视频解析 API 失败：{format_error(error)}")
        finally:
            await recall_message(event, wait_tip_id)

        if self._disposed or view is None:
            if self._config.show_error and view is None:
                await message_sender.send_plain(
                    event, "无法解析该链接，可能视频不存在或暂不可见"
                )
            return False

        self._logger.debug(f"解析结果：{view.title} {view.bvid} p={target.page}")

        video_path: Path | None = None
        # sendVideo 开关关闭时跳过下载，${视频} 占位符渲染为空，仅发送消息模板
        if (
            self._config.send_video
            and has_video_placeholder(self._config, source)
            and view.video_url
        ):
            downloaded = await self._api.download_video(
                view.video_url, view.title, self._video_dir
            )
            if downloaded:
                video_path, _mime = downloaded
        if self._disposed:
            return False

        messages = build_video_messages(
            self._config, view, target.page, source, video_path
        )
        if not messages:
            return False
        if self._disposed:
            return False
        if self._config.debug_full:
            outline = "\n".join(
                " | ".join(type(comp).__name__ for comp in message)
                for message in messages
            )
            self._logger.debug(f"渲染消息结构：{outline}")

        sent = await send_parsed_messages(
            event, messages, self._config, self._logger, lambda: self._disposed
        )
        if not sent:
            return False
        self._logger.debug(f"发送完成：{view.bvid}")

        # 临时视频文件清理：优先交给事件生命周期兜底，否则立即删除
        if video_path is not None:
            track = getattr(event, "track_temporary_local_file", None)
            if callable(track):
                try:
                    track(str(video_path))
                    video_path = None
                except Exception:
                    pass
            if video_path is not None:
                video_path.unlink(missing_ok=True)
        return True

    async def _send_wait_tip(self, event: AstrMessageEvent) -> int | None:
        if self._disposed or not self._config.wait_tip:
            return None
        try:
            return await send_wait_tip(event, self._config.wait_tip, self._logger)
        except Exception:
            return None

    # ---- 队列清理（原版 clearSessionQueueByChannel / ByUser）----

    def _clear_queue_by_channel(self, channel_id: str) -> None:
        self._filter_queue(
            lambda task: task.event.unified_msg_origin != channel_id
        )

    def _clear_queue_by_user(self, channel_id: str, user_id: str) -> None:
        self._filter_queue(
            lambda task: task.event.unified_msg_origin != channel_id
            or (task.event.get_sender_id() or "") != user_id
        )

    def _filter_queue(self, keep) -> None:
        """重排 asyncio.Queue：保留 keep(task) 为 True 的任务。"""
        remaining = []
        while not self._queue.empty():
            try:
                task = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if task is not None and keep(task):
                remaining.append(task)
        for task in remaining:
            self._queue.put_nowait(task)
