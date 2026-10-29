"""视频文件命名工具。

对照移植原插件 old/src/video-file.ts：
- 清理文件名中的非法字符，避免协议端使用平台不接受的名称
- 根据视频 MIME 类型生成带后缀的文件名
"""

from __future__ import annotations

import re

# 原版 MIME_EXTENSIONS
MIME_EXTENSIONS: dict[str, str] = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-flv": "flv",
    "video/x-msvideo": "avi",
    "video/mpeg": "mpg",
    "video/mp2t": "ts",
}

# Windows 文件名非法字符与控制字符（原版 sanitizeFileName）
_ILLEGAL_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING_DOTS_PATTERN = re.compile(r"[. ]+$")
_KNOWN_VIDEO_EXT_PATTERN = re.compile(
    r"\.(mp4|m4v|webm|mov|flv|avi|mkv|mpeg|mpg|ts)$", re.IGNORECASE
)


def sanitize_file_name(value: str) -> str:
    """清理文件名非法字符：替换为下划线、压缩空白、截断到 80 字符。"""
    cleaned = _ILLEGAL_CHARS_PATTERN.sub("_", value)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = _TRAILING_DOTS_PATTERN.sub("", cleaned).strip()
    return cleaned[:80]


def create_video_file_name(title: str, mime_type: str) -> str:
    """根据视频标题与 MIME 类型生成「标题.扩展名」文件名。"""
    mime = mime_type.split(";")[0].strip().lower()
    extension = MIME_EXTENSIONS.get(mime, "mp4")
    stem = sanitize_file_name(title)
    stem = _KNOWN_VIDEO_EXT_PATTERN.sub("", stem)
    stem = _TRAILING_DOTS_PATTERN.sub("", stem).strip()
    return f"{stem or 'video'}.{extension}"
