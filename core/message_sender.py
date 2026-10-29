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
    Node,
    Nodes,
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


async def send_parsed_messages(
    event: AstrMessageEvent,
    messages: list[list[BaseMessageComponent]],
    config: PluginConfig,
    logger: PluginLogger,
    is_disposed: Callable[[], bool],
) -> bool:
    """按配置发送解析结果：合并转发（Nodes）或逐条发送。

    - 视频必须独占一条消息（OneBot 实现层硬性要求，与图文混链会被拒），
      合并转发时视频作为独立节点放在图文节点之后，仍属同一条转发
    - 合并转发发送失败时降级为逐条发送（DESIGN.md §9 第 3 条）
    """
    if is_disposed() or not messages:
        return False

    figure_messages, video_messages = _split_video_messages(messages)

    if config.use_figure and is_supported_platform(event):
        try:
            sender_id = event.get_sender_id() or "0"
            sender_name = event.get_sender_name() or sender_id
            node_contents = figure_messages + video_messages  # 视频节点置后
            nodes = Nodes(
                [
                    Node(uin=sender_id, name=sender_name, content=content)
                    for content in node_contents
                ]
            )
            await event.send(MessageChain(chain=[nodes]))
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
