"""消息发送（平台特定模块，允许 aiocqhttp 专属调用）。

对照移植原插件 old/src/message-sender.ts：
- 支持合并转发的平台用 Nodes 一次发送，否则逐条发送
- 等待提示通过 OneBot API 发送以获取 message_id，解析完成后撤回

平台约束（AGENTS.md §2）：仅支持 aiocqhttp（QQ / OneBot v11）。
"""

from __future__ import annotations

from collections.abc import Callable

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


def is_supported_platform(event: AstrMessageEvent) -> bool:
    return event.get_platform_name() == SUPPORTED_PLATFORM


def _onebot_target(event: AstrMessageEvent) -> tuple[str, dict[str, int | str]]:
    """返回 (action, 参数字典)，供 send / 撤回等 OneBot API 调用使用。"""
    group_id = event.get_group_id()
    if group_id:
        return "send_group_msg", {"group_id": int(group_id) if group_id.isdigit() else group_id}
    sender_id = event.get_sender_id()
    return "send_private_msg", {"user_id": int(sender_id) if sender_id.isdigit() else sender_id}


async def send_plain(event: AstrMessageEvent, text: str) -> None:
    """发送纯文本消息。"""
    await event.send(MessageChain(chain=[Plain(text=text)]))


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
        await bot.call_action("delete_msg", message_id=message_id)
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

    uin = event.get_sender_id() or "0"
    nickname = event.get_sender_name() or uin
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
        params: dict = {
            "group_id": int(group_id) if group_id.isdigit() else group_id
        }
    else:
        action = "send_private_forward_msg"
        sender_id = event.get_sender_id()
        params = {"user_id": int(sender_id) if sender_id.isdigit() else sender_id}

    # 多账号路由（对齐 aiocqhttp 适配器的 self_id 传递）
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if raw is not None and callable(getattr(raw, "get", None)):
        self_id = raw.get("self_id")
        if self_id:
            params["self_id"] = self_id

    await bot.call_action(action, messages=messages, **params)


async def send_parsed_messages(
    event: AstrMessageEvent,
    messages: list[list[BaseMessageComponent]],
    config: PluginConfig,
    logger: PluginLogger,
    is_disposed: Callable[[], bool],
) -> bool:
    """按配置发送解析结果：合并转发或逐条发送。

    - 合并转发不走 AstrBot 的 Nodes 组件，而是手工调用 OneBot 转发 API：
      节点内视频先经 AstrBot 文件服务上传转回调 URL（协议端分离部署时
      读不到本地盘，file:// 直传必败）；图文节点在前、视频独占节点在后
    - 合并转发失败时降级为逐条发送（视频走同一上传通道），保证送达
    """
    if is_disposed() or not messages:
        return False

    figure_messages, video_messages = _split_video_messages(messages)

    if config.use_figure and is_supported_platform(event):
        try:
            await _send_forward_message(event, figure_messages + video_messages)
            return True
        except Exception as error:
            logger.warn(f"合并转发发送失败，降级为逐条发送：{format_error(error)}")

    # 降级路径 / 未开启合并转发：图文逐条，视频独占逐条
    for message in figure_messages:
        if is_disposed():
            return False
        await event.send(MessageChain(chain=message))
    for video_message in video_messages:
        if is_disposed():
            return False
        await event.send(MessageChain(chain=video_message))
    return True
