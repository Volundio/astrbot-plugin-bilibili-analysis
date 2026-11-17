"""视频缓存：下载到本地缓存目录（断线续传）与过期清理。

- download_video：对照原插件 old/src/bilibili-api.ts 的 downloadVideo / readVideoBody，
  保留大小上限（Content-Length 预检 + 流式超限中止）与 120 秒总时限；
  在此之上补充断线续传与完整性判定（差异登记见 docs/DESIGN.md 第 9 节）
- purge_expired_videos：缓存文件按修改时间保留固定时长，到期删除
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .config import PluginConfig
from .logger import PluginLogger, format_error
from .video_file import MIME_EXTENSIONS, create_video_file_name

# 缓存文件保留时长：协议端可能在发送调用返回后才经回调 URL 取文件，
# 不能发完即删；到期后无论发送成功与否一律清理
VIDEO_CACHE_TTL_S = 3600
# 后台巡检间隔（文件实际存活时间 ≤ 保留时长 + 巡检间隔）
VIDEO_CACHE_SWEEP_INTERVAL_S = 600

# 原版 downloadVideo 的 120 秒总时限（含全部续传尝试）
DOWNLOAD_TIMEOUT_S = 120
# 单次读超时：直链 CDN 停滞时尽早失败转续传，而非干等总时限
DOWNLOAD_READ_TIMEOUT_S = 30
# B 站 CDN 常在传输中途断开连接，断开后从已接收位置续传
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_S = 0.5
DOWNLOAD_CHUNK_SIZE = 64 * 1024

_CONTENT_RANGE_PATTERN = re.compile(
    r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE
)
_CONTENT_RANGE_TOTAL_PATTERN = re.compile(r"/(\d+)\s*$")


@dataclass
class _DownloadState:
    """跨续传尝试共享的下载进度。"""

    final_path: Path | None = None
    part_path: Path | None = None
    mime: str = "video/mp4"
    received: int = 0
    total: int | None = None  # 完整文件字节数；服务端未声明时为 None


def _header_int(value: str | None) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        return None


def _parse_content_range(value: str | None) -> tuple[int | None, int | None]:
    """从 Content-Range: bytes start-end/total 提取 (start, total)。"""
    if not value:
        return None, None
    match = _CONTENT_RANGE_PATTERN.search(value)
    if match:
        start = int(match.group(1))
        total_raw = match.group(3)
        total = int(total_raw) if total_raw.isdigit() else None
        return start, total
    total_match = _CONTENT_RANGE_TOTAL_PATTERN.search(value)
    return None, (int(total_match.group(1)) if total_match else None)


def _find_cached_video(
    dest_dir: Path, file_stem: str, max_bytes: int
) -> tuple[str, tuple[Path, str] | None]:
    """查找未过期的完整缓存文件。

    返回 ("hit", (path, mime)) / ("oversize", None) / ("miss", None)。
    """
    now = time.time()
    for mime in MIME_EXTENSIONS:
        candidate = dest_dir / create_video_file_name(file_stem, mime)
        try:
            if not candidate.is_file():
                continue
            stat = candidate.stat()
        except OSError:
            continue
        if stat.st_size <= 0 or now - stat.st_mtime >= VIDEO_CACHE_TTL_S:
            continue
        if max_bytes > 0 and stat.st_size > max_bytes:
            return "oversize", None
        return "hit", (candidate, mime)
    return "miss", None


async def download_video(
    session: aiohttp.ClientSession,
    config: PluginConfig,
    logger: PluginLogger,
    url: str,
    file_stem: str,
    dest_dir: Path,
) -> tuple[Path, str] | None:
    """按大小上限流式下载视频到 dest_dir，连接中断时自动续传。

    采用「命中有效缓存直接复用 + 临时 .part 文件原子替换」策略，避免并发/重复下载
    截断协议端正在读取的缓存文件，或下载失败时误删已有缓存。
    返回 (文件路径, MIME 类型)；超限或失败返回 None（不留半截文件）。
    """
    max_mb = config.max_file_size_mb
    max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0

    cache_status, cached = _find_cached_video(dest_dir, file_stem, max_bytes)
    if cache_status == "hit" and cached is not None:
        logger.debug(f"复用未过期的视频缓存：{cached[0].name}")
        return cached
    if cache_status == "oversize":
        logger.debug(f"缓存视频超过 {max_mb}MB 上限，跳过发送")
        return None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + DOWNLOAD_TIMEOUT_S
    state = _DownloadState()
    logger.debug(f"开始下载视频：{url}（上限 {max_mb}MB）")
    try:
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warn(f"下载视频失败：超过 {DOWNLOAD_TIMEOUT_S} 秒总时限（{url}）")
                return None
            try:
                completed = await _download_attempt(
                    session, config, logger, url, file_stem, dest_dir,
                    state, max_bytes, remaining,
                )
            except Exception as error:  # noqa: BLE001 —— 对齐原版 catch-all：网络/超时/磁盘 IO 异常均降级
                if state.total is not None and state.total > 0 and state.received >= state.total:
                    # 数据已按声明大小收齐，异常只发生在连接收尾阶段，文件可用
                    logger.debug(
                        f"连接收尾异常但数据已完整，按下载成功处理：{format_error(error)}"
                    )
                    completed = True
                elif attempt < DOWNLOAD_MAX_ATTEMPTS:
                    logger.warn(
                        f"下载视频中断（第 {attempt}/{DOWNLOAD_MAX_ATTEMPTS} 次，"
                        f"已接收 {state.received / 1024 / 1024:.2f}MB），"
                        f"续传重试：{format_error(error)}"
                    )
                    await asyncio.sleep(DOWNLOAD_RETRY_DELAY_S)
                    continue
                else:
                    logger.warn(f"下载视频失败：{format_error(error)}（{url}）")
                    logger.debug(f"下载视频失败堆栈：\n{traceback.format_exc()}")
                    return None
            if not completed or state.part_path is None or state.final_path is None:
                return None
            state.part_path.replace(state.final_path)
            logger.debug(
                f"视频下载完成，大小：{state.received / 1024 / 1024:.2f}MB"
            )
            return state.final_path, state.mime
        return None
    finally:
        # 含任务取消在内的一切出口均清理临时 .part 文件，不动已落盘的正式缓存
        if state.part_path is not None:
            state.part_path.unlink(missing_ok=True)


async def _download_attempt(
    session: aiohttp.ClientSession,
    config: PluginConfig,
    logger: PluginLogger,
    url: str,
    file_stem: str,
    dest_dir: Path,
    state: _DownloadState,
    max_bytes: int,
    remaining: float,
) -> bool:
    """单次下载/续传。True = 文件完整；False = 不可重试的放弃（已记日志）。

    网络或完整性异常向上抛出，由调用方按进度决定续传或放弃。
    """
    max_mb = config.max_file_size_mb
    headers = {
        "User-Agent": config.user_agent,
        "Referer": "https://www.bilibili.com/",
        "Accept": "video/*,*/*;q=0.8",
    }
    resuming = state.received > 0 and state.part_path is not None
    if resuming:
        headers["Range"] = f"bytes={state.received}-"
    timeout = aiohttp.ClientTimeout(total=remaining, sock_read=DOWNLOAD_READ_TIMEOUT_S)

    async with session.get(url, headers=headers, timeout=timeout) as response:
        if resuming and response.status == 416:
            # 续传起点已是文件末尾：上一次其实已收齐（服务端未声明大小时无从得知）
            return state.received > 0
        if response.status < 200 or response.status >= 300:
            logger.warn(f"下载视频失败：HTTP {response.status}（{url}）")
            return False
        if resuming and response.status != 206:
            # 服务端不支持 Range，返回的是完整文件：从头重下
            resuming = False
            state.received = 0

        if resuming:
            range_start, range_total = _parse_content_range(
                response.headers.get("Content-Range")
            )
            if range_start is not None and range_start != state.received:
                logger.warn(
                    f"下载视频失败：续传偏移不匹配（期望 {state.received}，实际 {range_start}）"
                )
                return False
            if state.total is None and range_total is not None:
                state.total = range_total
        else:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            state.mime = content_type if content_type.startswith("video/") else "video/mp4"
            state.total = _header_int(response.headers.get("Content-Length"))

        if max_bytes > 0 and state.total is not None and state.total > max_bytes:
            logger.debug(
                f"视频大小 {state.total / 1024 / 1024:.2f}MB 超过 {max_mb}MB，跳过下载"
            )
            return False

        if not resuming:
            dest_dir.mkdir(parents=True, exist_ok=True)
            file_name = create_video_file_name(file_stem, state.mime)
            state.final_path = dest_dir / file_name
            if state.part_path is None:
                state.part_path = dest_dir / f".{file_name}.{uuid.uuid4().hex[:8]}.part"

        assert state.part_path is not None
        # 流式写入临时 .part 文件；缺少 Content-Length 时也能及时中止超限文件
        with state.part_path.open("ab" if resuming else "wb") as file:
            async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue
                if max_bytes > 0 and state.received + len(chunk) > max_bytes:
                    logger.debug(f"视频大小超过 {max_mb}MB，跳过下载")
                    return False
                file.write(chunk)
                state.received += len(chunk)

        if state.total is not None and state.received < state.total:
            raise aiohttp.ClientPayloadError(
                f"视频数据未收齐（已接收 {state.received}/{state.total} 字节）"
            )
        if state.received <= 0:
            logger.warn(f"下载视频失败：响应内容为空（{url}）")
            return False
    return True


def purge_expired_videos(
    video_dir: Path, ttl_s: float = VIDEO_CACHE_TTL_S, now: float | None = None
) -> int:
    """删除 video_dir 下修改时间超过 ttl_s 的缓存文件，返回删除数量。

    按修改时间判定而非内存登记：插件重载、进程重启后遗留的文件同样会被清理。
    """
    current = time.time() if now is None else now
    removed = 0
    try:
        entries = list(video_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and current - entry.stat().st_mtime >= ttl_s:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            # 文件被占用等情况留待下次巡检
            continue
    return removed
