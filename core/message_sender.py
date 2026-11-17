"""消息发送（平台特定模块，允许 aiocqhttp 专属调用）。

对照移植原插件 old/src/message-sender.ts：
- 支持合并转发的平台用 Nodes 一次发送，否则逐条发送
- 等待提示通过 OneBot API 发送以获取 message_id，解析完成后撤回

平台约束（AGENTS.md §2）：仅支持 aiocqhttp（QQ / OneBot v11）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    BaseMessageComponent,
    Image,
    Plain,
    Video,
)

from .config import PluginConfig
from .logger import PluginLogger, format_error

SUPPORTED_PLATFORM = "aiocqhttp"


class _ForwardCallTimeoutError(Exception):
    """合并转发请求已发往协议端，但等待 WebSocket 回包超时。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def is_supported_platform(event: AstrMessageEvent) -> bool:
    return event.get_platform_name() == SUPPORTED_PLATFORM


def _extract_self_id(event: AstrMessageEvent) -> int | str | None:
    """提取当前机器人自身账号 ID（用于多账号路由与自身消息判定）。"""
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is not None and callable(getattr(raw, "get", None)):
        self_id = raw.get("self_id")
        if self_id:
            return self_id
    get_self_id = getattr(event, "get_self_id", None)
    if callable(get_self_id):
        self_id = get_self_id()
        if self_id:
            text = str(self_id)
            return int(text) if text.isdigit() else self_id
    return None


def is_self_message(event: AstrMessageEvent) -> bool:
    """判断消息是否由机器人自身发出（防开启上报自身消息时回环重复解析）。"""
    sender_id = str(event.get_sender_id() or "")
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is not None and callable(getattr(raw, "get", None)):
        if raw.get("post_type") == "message_sent":
            return True
        raw_self_id = str(raw.get("self_id") or "")
        raw_user_id = str(raw.get("user_id") or "")
        if raw_self_id and (raw_self_id == sender_id or raw_self_id == raw_user_id):
            return True
    self_id = _extract_self_id(event)
    if sender_id and self_id is not None and sender_id == str(self_id):
        return True
    return False


def _is_call_action_timeout(error: BaseException) -> bool:
    """判断 OneBot call_action 异常是否为等待回包超时。

    请求已发往协议端后若仅因大视频上传耗时超过 aiocqhttp 的 api_timeout_sec
    而超时，协议端仍在后台继续上传并最终送达，此时降级重发会产生双份消息。
    """
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return True
    detail = f"{type(error).__name__}: {error}".lower()
    return "timeout" in detail or "timed out" in detail


def _onebot_target(event: AstrMessageEvent) -> tuple[str, dict[str, Any]]:
    """返回 (action, 参数字典)，供等待提示发送等 OneBot API 调用使用。"""
    params: dict[str, Any]
    group_id = event.get_group_id()
    if group_id:
        action = "send_group_msg"
        params = {"group_id": int(group_id) if group_id.isdigit() else group_id}
    else:
        action = "send_private_msg"
        sender_id = event.get_sender_id() or ""
        params = {"user_id": int(sender_id) if sender_id.isdigit() else sender_id}
    self_id = _extract_self_id(event)
    if self_id is not None:
        params["self_id"] = self_id
    return action, params


async def send_plain(event: AstrMessageEvent, text: str) -> None:
    """发送纯文本消息。"""
    try:
        await event.send(MessageChain(chain=[Plain(text=text)]))
    except Exception:
        pass


async def send_wait_tip(
    event: AstrMessageEvent, text: str, logger: PluginLogger
) -> int | None:
    """发送等待提示，aiocqhttp 下返回 message_id 以便解析完成后撤回。"""
    if not is_supported_platform(event):
        await send_plain(event, text)
        return None
    bot = getattr(event, "bot", None)
    if bot is None:
        await send_plain(event, text)
        return None
    try:
        action, params = _onebot_target(event)
        result = await bot.call_action(
            action,
            message=[{"type": "text", "data": {"text": text}}],
            **params,
        )
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return int(message_id) if message_id is not None else None
    except Exception as error:  # 平台调用失败时降级为普通发送
        logger.warn(f"等待提示经 OneBot API 发送失败：{format_error(error)}")
        await send_plain(event, text)
        return None


async def recall_message(event: AstrMessageEvent, message_id: int | None) -> None:
    """撤回消息，失败静默（原版 .catch(() => undefined)）。"""
    if message_id is None or not is_supported_platform(event):
        return
    bot = getattr(event, "bot", None)
    if bot is None:
        return
    try:
        params: dict[str, Any] = {"message_id": message_id}
        self_id = _extract_self_id(event)
        if self_id is not None:
            params["self_id"] = self_id
        await bot.call_action("delete_msg", **params)
    except Exception:
        pass


def _split_video_messages(
    messages: list[list[BaseMessageComponent]],
) -> tuple[list[list[BaseMessageComponent]], list[list[Video]]]:
    """把含视频的消息拆为「其余组件消息 + 纯视频消息」两组。

    OneBot 协议端（NapCat 等）要求 video 段必须是消息中唯一的段，
    与文本/图片混链会被整体拒绝（retcode=1404/1400）。
    """
    others: list[list[BaseMessageComponent]] = []
    videos: list[list[Video]] = []
    for message in messages:
        video_comps = [comp for comp in message if isinstance(comp, Video)]
        if not video_comps:
            others.append(message)
            continue
        rest = [comp for comp in message if not isinstance(comp, Video)]
        if rest:
            others.append(rest)
        videos.extend([video] for video in video_comps)
    return others, videos


async def _to_onebot_segment(comp: BaseMessageComponent) -> dict | None:
    """把 AstrBot 消息组件转为 OneBot 消息段 dict（用于手工构造转发节点）。

    关键差异：Video 走组件自身的异步 to_dict()——AstrBot 会先把本地文件
    注册到文件服务并返回回调 URL（协议端经 URL 取文件），而不是 Nodes
    组件序列化时的 file:// 本地路径（协议端分离部署时读不到本地盘）。
    """
    if isinstance(comp, Plain):
        if not comp.text.strip():
            return None
        return {"type": "text", "data": {"text": comp.text}}
    if isinstance(comp, Image):
        bs64 = await comp.convert_to_base64()
        return {"type": "image", "data": {"file": f"base64://{bs64}"}}
    if isinstance(comp, Video):
        return await comp.to_dict()
    return comp.toDict()


async def _send_forward_message(
    event: AstrMessageEvent,
    node_contents: list[list[BaseMessageComponent]],
) -> None:
    """手工调用 OneBot 合并转发 API（绕过 AstrBot Nodes 组件序列化）。

    Nodes 组件对节点内视频不做上传转换（file:// 直传协议端），在协议端
    与 AstrBot 分离部署时必然 ENOENT；此处在插件层完成「上传转 URL」
    后再拼转发消息，任何部署形态均可送达。
    """
    bot = getattr(event, "bot", None)
    if bot is None:
        raise RuntimeError("当前平台无 bot 实例，无法发送合并转发")

    sender_id = event.get_sender_id() or ""
    self_id = _extract_self_id(event)
    # OneBot 转发节点要求合法 QQ 号；发送者非纯数字时回退为机器人自身或系统号 10000
    uin = sender_id if sender_id.isdigit() else (str(self_id) if self_id else "10000")
    nickname = event.get_sender_name() or sender_id or uin
    messages: list[dict] = []
    for content in node_contents:
        segments = []
        for comp in content:
            segment = await _to_onebot_segment(comp)
            if segment is not None:
                segments.append(segment)
        if not segments:
            continue
        messages.append(
            {
                "type": "node",
                "data": {
                    "user_id": str(uin),
                    "nickname": nickname,
                    "content": segments,
                },
            }
        )
    if not messages:
        return

    group_id = event.get_group_id()
    if group_id:
        action = "send_group_forward_msg"
        params: dict[str, Any] = {
            "group_id": int(group_id) if group_id.isdigit() else group_id
        }
    else:
        action = "send_private_forward_msg"
        params = {"user_id": int(sender_id) if sender_id.isdigit() else sender_id}

    if self_id is not None:
        params["self_id"] = self_id

    try:
        await bot.call_action(action, messages=messages, **params)
    except Exception as error:
        if _is_call_action_timeout(error):
            raise _ForwardCallTimeoutError(error) from error
        raise


async def _send_chain_safely(
    event: AstrMessageEvent,
    chain: list[BaseMessageComponent],
    logger: PluginLogger,
    label: str,
) -> bool:
    """安全发送单条消息链，捕获异常记日志，避免单条失败中断整个任务。"""
    try:
        await event.send(MessageChain(chain=chain))
        return True
    except Exception as error:  # noqa: BLE001
        logger.warn(f"{label}发送失败：{format_error(error)}")
        return False


async def send_parsed_messages(
    event: AstrMessageEvent,
    messages: list[list[BaseMessageComponent]],
    config: PluginConfig,
    logger: PluginLogger,
    is_disposed: Callable[[], bool],
) -> bool:
    """按配置发送解析结果：合并转发或逐条发送。

    - 合并转发手工调用 OneBot 转发 API：节点内视频先经 AstrBot 文件服务转回调 URL，
      图文节点在前、视频独占节点在后
    - 若 call_action 等待回包超时（常见于大视频上传耗时较长），协议端仍在后台继续上传
      并最终送达，跳过降级重发以免群内出现双份消息
    - 若发生非超时失败（如 ActionFailed 或回调不可达）：含视频时先尝试仅图文合并转发
      + 独立补发视频；仍失败再降级为逐条发送
    """
    if is_disposed() or not messages:
        return False

    figure_messages, video_messages = _split_video_messages(messages)

    if config.use_figure and is_supported_platform(event):
        try:
            await _send_forward_message(event, figure_messages + video_messages)
            return True
        except _ForwardCallTimeoutError as error:
            logger.warn(
                "合并转发等待协议端响应超时（协议端可能仍在后台上传发送，"
                f"跳过降级重发以免重复）：{format_error(error.cause)}"
            )
            return True
        except Exception as error:  # noqa: BLE001
            logger.warn(f"合并转发发送失败，尝试降级发送：{format_error(error)}")
            if video_messages and figure_messages and not is_disposed():
                try:
                    await _send_forward_message(event, figure_messages)
                    for video_message in video_messages:
                        if is_disposed():
                            return True
                        await _send_chain_safely(
                            event, video_message, logger, "降级补发视频"
                        )
                    return True
                except _ForwardCallTimeoutError as timeout_err:
                    logger.warn(
                        "仅图文合并转发等待协议端响应超时，跳过逐条重发："
                        f"{format_error(timeout_err.cause)}"
                    )
                    return True
                except Exception as retry_err:  # noqa: BLE001
                    logger.warn(
                        f"仅图文合并转发亦失败，降级为逐条发送：{format_error(retry_err)}"
                    )

    # 降级路径 / 未开启合并转发：图文逐条，视频独占逐条（独立捕获异常）
    any_sent = False
    for message in figure_messages:
        if is_disposed():
            return any_sent
        if await _send_chain_safely(event, message, logger, "解析图文消息"):
            any_sent = True
    for video_message in video_messages:
        if is_disposed():
            return any_sent
        if await _send_chain_safely(event, video_message, logger, "解析视频消息"):
            any_sent = True
    return any_sent
