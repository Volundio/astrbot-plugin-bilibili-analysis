"""冒烟测试：在无 AstrBot 环境下验证插件模块导入与核心纯函数逻辑。

用法：python3 scripts/smoke_test.py
退出码 0 = 全部通过。

通过 importlib 以「包」形式加载插件根目录，与 AstrBot 真实加载方式
（data.plugins.<目录名>.main）保持一致的相对导入语义。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. 构造 astrbot 的最小 mock（仅覆盖本插件用到的符号）
# ---------------------------------------------------------------------------


def _build_astrbot_mock() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    # astrbot.api.AstrBotConfig（dict 子类的简化 stub）
    class AstrBotConfig(dict):
        pass

    astrbot.AstrBotConfig = AstrBotConfig
    api.AstrBotConfig = AstrBotConfig

    # astrbot.api.logger
    api_logger = types.ModuleType("astrbot.api.logger")
    logging.basicConfig(level=logging.DEBUG, format="[mock] %(message)s")
    api_logger.logger = logging.getLogger("astrbot")
    astrbot.logger = api_logger.logger
    api.logger = api_logger.logger

    # astrbot.api.event
    api_event = types.ModuleType("astrbot.api.event")

    class MessageChain:  # 简化 stub
        def __init__(self, chain=None):
            self.chain = chain or []

    class AstrMessageEvent:  # 简化 stub
        pass

    class _EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    class _PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    filter_mod = types.ModuleType("astrbot.api.event.filter")

    def _passthrough_decorator(*_args, **_kwargs):
        def deco(func):
            return func
        return deco

    filter_mod.EventMessageType = _EventMessageType
    filter_mod.PlatformAdapterType = _PlatformAdapterType
    filter_mod.command = _passthrough_decorator
    filter_mod.event_message_type = _passthrough_decorator
    filter_mod.platform_adapter_type = _passthrough_decorator

    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.MessageChain = MessageChain
    api_event.MessageEventResult = object
    api_event.filter = filter_mod

    # astrbot.api.message_components
    mc = types.ModuleType("astrbot.api.message_components")

    class BaseMessageComponent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def toDict(self):  # 简化 stub：非核心组件的回退序列化
            return {"type": "unknown", "data": {}}

    class Plain(BaseMessageComponent):
        def __init__(self, text="", **_):
            super().__init__()
            self.text = text

        async def to_dict(self):
            return {"type": "text", "data": {"text": self.text}}

    class Image(BaseMessageComponent):
        def __init__(self, file="", **_):
            super().__init__()
            self.file = file

        @staticmethod
        def fromURL(url, **_):
            return Image(file=url)

        async def convert_to_base64(self):
            return "ZmFrZV9pbWFnZQ=="  # fake_base64_image

    class Video(BaseMessageComponent):
        def __init__(self, file="", **_):
            super().__init__()
            self.file = file

        @staticmethod
        def fromFileSystem(path, **_):
            return Video(file=f"file://{path}")

        async def to_dict(self):
            # 模拟 AstrBot 文件服务：本地路径 → 回调 URL
            return {"type": "video", "data": {"file": "https://astrbot.mock/api/file/fake-token"}}

    class File(BaseMessageComponent):
        pass

    class Node(BaseMessageComponent):
        def __init__(self, uin="0", name="", content=None, **_):
            super().__init__()
            self.uin, self.name, self.content = uin, name, content or []

    class Nodes(BaseMessageComponent):
        def __init__(self, nodes=None, **_):
            super().__init__()
            self.nodes = nodes or []

    class Json(BaseMessageComponent):
        def __init__(self, data=None, **_):
            super().__init__()
            self.data = data if isinstance(data, dict) else {}

    for name, obj in {
        "BaseMessageComponent": BaseMessageComponent,
        "Plain": Plain,
        "Image": Image,
        "Video": Video,
        "File": File,
        "Node": Node,
        "Nodes": Nodes,
        "Json": Json,
    }.items():
        setattr(mc, name, obj)

    # astrbot.api.star
    api_star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        """对齐真实基类 astrbot/core/star/base.py：
        接受 config 参数但【不】存储 —— 插件必须自己声明并赋值 self.config。
        这样若插件代码直接读 self.config 而未存储，测试会像真实环境一样报错。
        """

        def __init__(self, context=None, config=None):
            self.context = context  # noqa: ARG002 —— 故意不存储 config

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            return Path("/tmp/astrbot_test_data") / (plugin_name or "plugin")

    def register(*_args, **_kwargs):
        def deco(cls):
            return cls
        return deco

    api_star.Context = Context
    api_star.Star = Star
    api_star.StarTools = StarTools
    api_star.register = register

    # astrbot.api.util
    api_util = types.ModuleType("astrbot.api.util")

    class SessionController:
        pass

    def session_waiter(*_args, **_kwargs):
        def deco(func):
            return func
        return deco

    api_util.SessionController = SessionController
    api_util.session_waiter = session_waiter

    # astrbot.core.star.filter.command（GreedyStr）
    core = types.ModuleType("astrbot.core")
    star = types.ModuleType("astrbot.core.star")
    star_filter = types.ModuleType("astrbot.core.star.filter")
    cmd = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        pass

    cmd.GreedyStr = GreedyStr

    # 注册到 sys.modules
    for name, module in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": api_event,
        "astrbot.api.event.filter": filter_mod,
        "astrbot.api.message_components": mc,
        "astrbot.api.star": api_star,
        "astrbot.api.util": api_util,
        "astrbot.core": core,
        "astrbot.core.star": star,
        "astrbot.core.star.filter": star_filter,
        "astrbot.core.star.filter.command": cmd,
    }.items():
        sys.modules.setdefault(name, module)


# ---------------------------------------------------------------------------
# 2. 以包形式加载插件（相对导入与 AstrBot 真实加载语义一致）
# ---------------------------------------------------------------------------


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "plugin_under_test",
        PLUGIN_ROOT / "main.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugin_under_test"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 3. 断言工具
# ---------------------------------------------------------------------------

_passed = 0
_failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


# ---------------------------------------------------------------------------
# 4. 各模块测试
# ---------------------------------------------------------------------------


def test_config(core):
    print("[config]")
    cfg = core.config.PluginConfig.from_user_config({})
    check("默认开启解析", cfg.enablebilianalysis)
    check("默认模板含视频占位符", "${视频}" in cfg.link_template)
    check("默认模板为 emoji 风格", "👍" in cfg.link_template and "🪙" in cfg.link_template)
    check("默认模板含视频地址跳转", "🔗 ${视频地址}" in cfg.link_template)
    check("默认卡片模板与通用同布局",
          cfg.card_template == cfg.link_template)
    check("缺省卡片模板取 schema 默认",
          cfg.effective_card_template == core.config.DEFAULT_CARD_TEMPLATE.strip())
    check("parse_limit 默认 3", cfg.parse_limit == 3)
    check("四种来源默认全开",
          cfg.video_parse_mode == ("card", "link", "short", "id"))

    cfg2 = core.config.PluginConfig.from_user_config(
        {"bVideoCard_area": "", "requestMode": "hacker", "videoParseMode": "card"}
    )
    check("空卡片模板回落通用模板", cfg2.effective_card_template == cfg2.link_template)
    check("非法请求模式回退 parallel", cfg2.request_mode == "parallel")
    check("字符串解析来源归一", cfg2.video_parse_mode == ("card",))

    cfg3 = core.config.PluginConfig.from_user_config({"parseLimit": "abc"})
    check("非法数字容错", cfg3.parse_limit == 3)

    check("默认发送视频", cfg.send_video is True)
    cfg5 = core.config.PluginConfig.from_user_config({"sendVideo": False})
    check("sendVideo 关闭归一", cfg5.send_video is False)

    # 旧版本配置迁移：link/card 旧值按新语义（link 仅完整链接）解释
    cfg4 = core.config.PluginConfig.from_user_config({"videoParseMode": ["link", "card"]})
    check("旧配置来源按新语义解释",
          cfg4.video_parse_mode == ("link", "card"))


def test_link_parser(core):
    print("[link_parser]")
    lp = core.link_parser

    t = lp.parse_bilibili_content("看这个 https://www.bilibili.com/video/BV1GJ411x7h7")
    check("普通 BV 链接", len(t) == 1 and isinstance(t[0], lp.VideoTarget)
          and t[0].bvid == "BV1GJ411x7h7" and t[0].page == 1)
    check("完整链接来源标记", t and lp.target_origin(t[0]) == "link")

    t = lp.parse_bilibili_content("https://www.bilibili.com/video/BV1GJ411x7h7?p=3")
    check("分 P 参数", t and t[0].page == 3)

    t = lp.parse_bilibili_content("https://www.bilibili.com/video/av170001?page=2")
    check("AV 链接与 page 参数", t and isinstance(t[0], lp.VideoTarget)
          and t[0].aid == "170001" and t[0].page == 2)

    t = lp.parse_bilibili_content("https://m.bilibili.com/video/BV1GJ411x7h7")
    check("m. 域名", t and t[0].bvid == "BV1GJ411x7h7")

    t = lp.parse_bilibili_content("https://www.bilibili.com/video/BV1GJ411x7h7。")
    check("尾部中文标点清洗", t and t[0].bvid == "BV1GJ411x7h7")

    t = lp.parse_bilibili_content("分享 https://b23.tv/abcd1234 给你")
    check("b23.tv 短链", t and isinstance(t[0], lp.ShortTarget)
          and t[0].host == "b23.tv" and t[0].code == "abcd1234")
    check("短链来源标记", t and lp.target_origin(t[0]) == "short")

    t = lp.parse_bilibili_content("https://b23.tv/BV1GJ411x7h7")
    check("短链 code 为 BV 号直接解析", t and isinstance(t[0], lp.VideoTarget))
    check("短链直带 BV 的来源仍为 short", t and lp.target_origin(t[0]) == "short")

    t = lp.parse_bilibili_content("哈喽 BV1GJ411x7h7 好看")
    check("独立 BV 号", t and t[0].bvid == "BV1GJ411x7h7")
    check("独立 BV 号来源标记", t and lp.target_origin(t[0]) == "id")

    t = lp.parse_bilibili_content("推荐 av170001。")
    check("独立 av 号", t and t[0].aid == "170001")
    check("独立 av 号来源标记", t and lp.target_origin(t[0]) == "id")
    # 原版 standalone 断言字符集不含全角感叹号，保真：不应匹配
    t = lp.parse_bilibili_content("推荐 av170001！")
    check("av 号后跟！不匹配（保真）", len(t) == 0)

    t = lp.parse_bilibili_content(
        "https://www.bilibili.com/video/BV1GJ411x7h7 和 https://www.bilibili.com/video/BV1GJ411x7h7?p=2"
    )
    check("同视频不同分 P 不合并", len(t) == 2)

    t = lp.parse_bilibili_content("https://example.com/video/BV1GJ411x7h7")
    check("非 B 站域名忽略", len(t) == 0)

    t = lp.parse_bilibili_content("看 BV1GJ411x7h7 和 https://www.bilibili.com/video/BV1GJ411x7h7")
    check("独立号与链接同视频去重", len(t) == 1)

    resolved = lp.target_from_resolved_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=5")
    check("短链解析结果转换", resolved is not None and resolved.page == 5)

    json_comp = core.message_components.Json(data={
        "meta": {
            "detail": "看视频 https://b23.tv/x1y2z3w",
            "qqdocurl": "https://www.bilibili.com/video/BV1GJ411x7h7",
        }
    })
    text = lp.collect_json_card_text([json_comp])
    check("JSON 卡片文本提取", "BV1GJ411x7h7" in text and "b23.tv" in text)

    t = lp.parse_bilibili_content('{"url":"https:\\/\\/www.bilibili.com\\/video\\/BV1GJ411x7h7"}')
    check("JSON 转义斜杠还原", t and t[0].bvid == "BV1GJ411x7h7")


def test_video_formatter(core):
    print("[video_formatter]")
    vf = core.video_formatter

    cfg = core.config.PluginConfig.from_user_config({})
    view = core.bilibili_api.BiliVideoView(
        bvid="BV1GJ411x7h7",
        aid=170001,
        title="测试<标题>",
        desc="简介" * 60,
        pic="http://i0.hdslb.com/bfs/archive/xxx.jpg",
        owner=core.bilibili_api.BiliVideoOwner(name="UP主", face="f", mid=1),
        stat=core.bilibili_api.BiliVideoStat(
            view=123456, like=23333, coin=666, favorite=777, share=888, danmaku=999
        ),
        video_url="https://example.com/v.mp4",
    )

    messages = vf.build_video_messages(cfg, view, 1, "link", video_path=None)
    check("默认模板渲染出消息", len(messages) >= 1)
    all_text = "\n".join(
        c.text for m in messages for c in m if isinstance(c, core.message_components.Plain)
    )
    check("标题渲染", "测试<标题>" in all_text)
    check("数字缩写（万）", "12.3万" in all_text)
    check("播放链接渲染", "mbplayer.html?bvid=BV1GJ411x7h7&p=1" in all_text)
    check("简介截断", len("简介" * 50 + "...") == 103 and "简介" + "..." in all_text)
    has_image = any(isinstance(c, core.message_components.Image) for m in messages for c in m)
    check("封面转 https Image", has_image)
    has_video = any(isinstance(c, core.message_components.Video) for m in messages for c in m)
    check("无文件时不产生 Video 组件", not has_video)

    check("模板含视频占位符判定", vf.has_video_placeholder(cfg, "link"))
    cfg_no_video = core.config.PluginConfig.from_user_config({"bVideo_area": "${标题}"})
    check("无视频占位符判定", not vf.has_video_placeholder(cfg_no_video, "link"))

    cfg_split = core.config.PluginConfig.from_user_config(
        {"bVideo_area": "${标题}${~~~}${UP主}${~~~}${观看}"}
    )
    messages = vf.build_video_messages(cfg_split, view, 1, "link")
    check("${~~~} 拆分多条消息", len(messages) == 3)

    check("亿缩写", vf.numeral(123456789, True) == "1.2亿")
    check("万缩写", vf.numeral(23456, True) == "2.3万")
    check("不缩写", vf.numeral(23456, False) == "23456")


def test_video_file(core):
    print("[video_file]")
    vf = core.video_file
    check("非法字符清洗", vf.sanitize_file_name('a<b>:"c/\\d|e?f*g') == "a_b___c__d_e_f_g")
    check("超长截断 80", len(vf.sanitize_file_name("哈" * 100)) == 80)
    check("MIME 扩展名", vf.create_video_file_name("标题", "video/webm") == "标题.webm")
    check("未知 MIME 默认 mp4", vf.create_video_file_name("标题", "text/html") == "标题.mp4")
    check("去尾部已有扩展名", vf.create_video_file_name("视频.mp4", "video/mp4") == "视频.mp4")
    # 原版行为：非法字符替换为 _，"???" 得到 "___" 作为文件名（保真）
    check("纯非法字符标题保真", vf.create_video_file_name("???", "video/mp4") == "___.mp4")


def test_bilibili_api_parse(core):
    print("[bilibili_api]")
    api = core.bilibili_api.BilibiliApi
    parse_view = api._parse_view_response
    view_ok = parse_view(
        '{"code":0,"data":{"bvid":"BV1xx","aid":1,"title":"t","pic":"p"}}'
    )
    check("view 合法响应通过", view_ok is not None and view_ok["bvid"] == "BV1xx")
    check("view code 非 0 拒绝", parse_view('{"code":-404,"message":"啥都木有"}') is None)
    check("view 缺标题拒绝", parse_view('{"code":0,"data":{"bvid":"x","aid":1}}') is None)
    check("view 风控 HTML 拒绝", parse_view("<html>出错啦</html>") is None)

    parse_url = api._parse_playurl_response
    url_ok = parse_url('{"code":0,"data":{"durl":[{"url":"http://upos.example/v.mp4"}]}}')
    check("playurl 原样取直链", url_ok == "http://upos.example/v.mp4")
    check("playurl 无 durl 拒绝", parse_url('{"code":0,"data":{}}') is None)
    check("playurl code 非 0 拒绝", parse_url('{"code":-404}') is None)

    parse_fallback = api._parse_xingzhige_response
    fb_ok = parse_fallback(
        '{"code":0,"data":{"bvid":"BV1xx","aid":1,"title":"t",'
        '"video":{"url":"https://cdn/v.mp4"}}}'
    )
    check("兜底源合法响应通过", fb_ok is not None)
    check("兜底源缺直链拒绝", parse_fallback(
        '{"code":0,"data":{"bvid":"BV1xx","aid":1,"title":"t"}}') is None)
    check("兜底源 code 非 0 拒绝", parse_fallback('{"code":-404}') is None)

    # 分 P → cid 选择
    view = core.bilibili_api.BiliVideoView(
        bvid="BV1xx",
        pages=[{"page": 1, "cid": 111}, {"page": 2, "cid": 222}],
    )
    check("按分 P 取 cid", api._pick_cid(view, 2) == 222)
    check("越界分 P 回落第一 P", api._pick_cid(view, 9) == 111)
    check("无分 P 返回 0", api._pick_cid(core.bilibili_api.BiliVideoView(bvid="x"), 1) == 0)


def test_wbi_sign():
    print("[wbi]")
    wbi_mod = sys.modules["plugin_under_test.core.wbi"]
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    signed = wbi_mod.sign_wbi({"bvid": "BV1xx", "cid": "111"}, img_key, sub_key)
    check("签名含 32 位 w_rid", len(signed.get("w_rid", "")) == 32)
    check("签名含 wts", "wts" in signed)
    check("mixin key 32 位", len(wbi_mod.mixin_key(img_key + sub_key)) == 32)
    check("从 URL 提取 key", wbi_mod._key_from_url(
        f"https://i0.hdslb.com/bfs/wbi/{img_key}.png") == img_key)


def test_search_removed(core):
    print("[search 已移除]")
    import os
    check("core/search.py 文件已删除",
          not os.path.exists(PLUGIN_ROOT / "core" / "search.py"))


def test_logger_format_error():
    """回归：TimeoutError 等空消息异常不得再产生空白日志。"""
    print("[logger]")
    logger_mod = sys.modules["plugin_under_test.core.logger"]
    fmt = logger_mod.format_error
    check("空消息异常输出类型名", fmt(TimeoutError()) == "TimeoutError")
    check("带消息异常输出 类型: 消息",
          fmt(RuntimeError("boom")) == "RuntimeError: boom")
    check("空字符串消息异常输出类型名", fmt(ValueError("")) == "ValueError")


def test_rate_limiter_sync():
    """rate_limiter 需要事件循环（call_later），用 asyncio 驱动。"""
    print("[rate_limiter]")

    async def run():
        # 延迟导入：模块以插件包路径加载
        rl_mod = sys.modules["plugin_under_test.core.rate_limiter"]
        cfg_mod = sys.modules["plugin_under_test.core.config"]
        cfg = cfg_mod.PluginConfig.from_user_config({})
        rl = rl_mod.VideoRateLimiter(cfg)
        try:
            channel, user = "ch1", "u1"
            # 原版行为：单链接消息预占计数，5 个后拦截
            for i in range(4):
                check(f"第 {i + 1} 条单链接消息放行",
                      rl.check_new_message(channel, user, 1) is None)
                rl.reserve_message(channel, user, 1)
            check("第 5 条触发用户限制",
                  rl.check_new_message(channel, user, 1) == "user-video-limit")
            # 对齐 enqueue 流程：收到 user-video-limit 后调用 block_user（原版行为）
            if rl.check_new_message(channel, user, 1) == "user-video-limit":
                rl.block_user(channel, user)
            check("block_user 后进入拉黑", rl.is_user_blocked(channel, user))
            check("其它用户不受影响", not rl.is_user_blocked(channel, user + "x"))
            # 多链接消息：count>=4 时拦截
            rl2 = rl_mod.VideoRateLimiter(cfg)
            check("多链接首条放行", rl2.check_new_message("c", "u", 3) is None)
            # 频道并发
            rl2.start_channel_video("c")
            rl2.start_channel_video("c")
            rl2.start_channel_video("c")
            check("频道并发达 3 后禁止", not rl2.can_start_channel_video("c"))
            rl2.end_channel_video("c")
            check("释放后可继续", rl2.can_start_channel_video("c"))
        finally:
            rl.dispose()
            if "rl2" in locals():
                rl2.dispose()

    asyncio.run(run())


def test_main_import(plugin):
    print("[main]")
    check("插件类已注册", hasattr(plugin, "BilibiliAnalysisPlugin"))
    check("插件类含监听器", hasattr(plugin.BilibiliAnalysisPlugin, "on_message"))
    check("点播指令已移除", not hasattr(plugin.BilibiliAnalysisPlugin, "bilibili_point"))

    # 复现 AstrBot 实例化路径：plugin_cls(context=..., config=...)；
    # 回归验证 'BilibiliAnalysisPlugin' object has no attribute 'config' 的问题
    try:
        obj = plugin.BilibiliAnalysisPlugin(
            context=None,
            config={"enablebilianalysis": False},
        )
        ok = obj._config.enablebilianalysis is False
        check("带 config 实例化成功且配置生效", ok)
    except Exception as error:  # noqa: BLE001
        check("带 config 实例化成功且配置生效", False, str(error))

    try:
        obj_fallback = plugin.BilibiliAnalysisPlugin(context=None)
        check("无 config 回退实例化成功（空配置兜底）",
              obj_fallback._config.enablebilianalysis is True)
        # mock Star 基类不存储 config，无参实例化时 self.config 必须仍是插件自存的空 dict
        check("config 兜底为空 dict 而非基类属性", obj_fallback.config == {})
    except Exception as error:  # noqa: BLE001
        check("无 config 回退实例化成功（空配置兜底）", False, str(error))


def main() -> int:
    _build_astrbot_mock()
    plugin = _load_plugin()
    core = types.SimpleNamespace(
        config=sys.modules["plugin_under_test.core.config"],
        link_parser=sys.modules["plugin_under_test.core.link_parser"],
        video_formatter=sys.modules["plugin_under_test.core.video_formatter"],
        video_file=sys.modules["plugin_under_test.core.video_file"],
        bilibili_api=sys.modules["plugin_under_test.core.bilibili_api"],
        wbi=sys.modules["plugin_under_test.core.wbi"],
        message_components=sys.modules["astrbot.api.message_components"],
    )

    print("=== astrbot_plugin_bilibili_analysis 冒烟测试 ===\n")
    test_config(core)
    test_link_parser(core)
    test_video_formatter(core)
    test_video_file(core)
    test_bilibili_api_parse(core)
    test_wbi_sign()
    test_search_removed(core)
    test_logger_format_error()
    test_rate_limiter_sync()
    test_main_import(plugin)

    print(f"\n通过 {_passed} 项，失败 {len(_failed)} 项")
    if _failed:
        print("失败项：")
        for item in _failed:
            print(f"  - {item}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
