"""测试 IrmiaRuntime 与钩子全链路集成（分流、主脑绑定、半遮面替换、回执凭据、沉默开关与 SilentNightEvent 让路）。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from irmia.runtime import IrmiaRuntime


@dataclass
class FakeSender:
    user_id: str = "114514"
    nickname: str = "老哥"
    card: str = ""


@dataclass
class FakeGroup:
    group_id: str = "888888"
    group_name: str = "快乐开黑群"


@dataclass
class FakeMsgObj:
    message_id: str = "msg_1"
    sender: FakeSender = field(default_factory=FakeSender)
    group: FakeGroup = field(default_factory=FakeGroup)
    message: list[Any] = field(default_factory=list)


@dataclass
class FakeResult:
    chain: list[Any] = field(default_factory=lambda: ["plain_item"])
    text: str = "这是一句回复"
    model_res: bool = True

    def get_plain_text(self) -> str:
        return self.text

    def is_model_result(self) -> bool:
        return self.model_res

    def is_llm_result(self) -> bool:
        return self.model_res


class FakeEvent:
    def __init__(
        self,
        umo: str,
        message_str: str,
        *,
        private: bool = False,
        at_bot: bool = False,
        sender_id: str = "114514",
        sender_name: str = "老哥",
        role: str = "member",
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = message_str
        self._private = private
        self.is_at_or_wake_command = at_bot
        self.is_wake = at_bot or private
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.role = role
        self.stopped = False
        self._extras: dict[str, Any] = {}
        self.message_obj = FakeMsgObj(
            sender=FakeSender(user_id=sender_id, nickname=sender_name)
        )
        self._result: FakeResult | None = None

    def is_private_chat(self) -> bool:
        return self._private

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_self_id(self) -> str:
        return "999999"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def stop_event(self) -> None:
        self.stopped = True

    def set_extra(self, k: str, v: Any) -> None:
        self._extras[k] = v

    def get_extra(self, k: str, default: Any = None) -> Any:
        return self._extras.get(k, default)

    def get_result(self) -> FakeResult | None:
        return self._result


class SilentNightEvent(FakeEvent):
    """模拟 AstrHe 的夜间复盘静默事件。"""


@dataclass
class FakeConversation:
    cid: str = "master_cid_001"
    history: str = '[{"role": "user", "content": "主脑历史第一句"}]'


class FakeConvManager:
    def __init__(self) -> None:
        self.conv = FakeConversation()

    async def get_curr_conversation_id(self, umo: str) -> str:
        return self.conv.cid

    async def new_conversation(
        self, umo: str, platform_id: str | None = None
    ) -> str:
        return self.conv.cid

    async def get_conversation(
        self, umo: str, cid: str, create_if_not_exists: bool = False
    ) -> FakeConversation:
        return self.conv


class FakeContext:
    def __init__(self) -> None:
        self.conversation_manager = FakeConvManager()
        self.registered_tools: list[Any] = []
        self.sent_messages: list[tuple[str, Any]] = []

    def get_config(self, umo: str | None = None) -> dict[str, Any]:
        return {"timezone": "Asia/Shanghai", "admins_id": ["114514"]}

    def add_llm_tools(self, *tools: Any) -> None:
        self.registered_tools.extend(tools)

    async def send_message(self, session: Any, message_chain: Any) -> bool:
        self.sent_messages.append((str(session), message_chain))
        return True


@dataclass
class FakeReq:
    prompt: str = ""
    conversation: Any = None
    contexts: list[Any] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    extra_user_content_parts: list[Any] = field(default_factory=list)


def test_runtime_end_to_end_flow(tmp_path: Path) -> None:
    """验证完整运行时钩子流：主脑自动绑定、未点名群消息入信箱、点名群消息转半遮面通知、工具凭据追加与直出拦截开关。"""

    async def _run() -> None:
        ctx = FakeContext()
        cfg = {
            "enabled": True,
            "master_umo": "",
            "mention_keywords": ["清漪"],
            "contacts_map": ["114514 = 哥哥"],
            "group_batch_window_sec": 0,
            "intercept_direct_reply": False,
        }
        rt = IrmiaRuntime(
            context=ctx,
            config=cfg,
            logger=logging.getLogger("irmia_test"),
            data_dir=tmp_path / "irmia_rt",
        )
        await rt.initialize()
        assert len(ctx.registered_tools) == 1
        assert ctx.registered_tools[0].name == "read_channel"

        # 1. AstrHe 的 SilentNightEvent 必须完全让路，不拦不改
        night_ev = SilentNightEvent(
            "aiocqhttp:GroupMessage:888888", "夜间测试", private=False
        )
        await rt.on_incoming_event(night_ev)
        assert night_ev.stopped is False

        # 2. 管理员首次私聊：自动绑定 master_umo 并直接放行
        priv_ev = FakeEvent(
            "aiocqhttp:FriendMessage:114514",
            "早安清漪",
            private=True,
            sender_id="114514",
            role="admin",
        )
        await rt.on_incoming_event(priv_ev)
        assert priv_ev.stopped is False
        assert await rt.resolve_master_umo() == "aiocqhttp:FriendMessage:114514"

        # 3. 群聊普通消息（未点名）：被 stop_event() 静默放进信箱，未读 +1
        grp_umo = "aiocqhttp:GroupMessage:888888"
        grp_plain = FakeEvent(
            grp_umo,
            "今天天气真不错",
            private=False,
            at_bot=False,
            sender_id="222222",
            sender_name="路人乙",
        )
        await rt.on_incoming_event(grp_plain)
        assert grp_plain.stopped is True

        # 4. 群聊关键词提及（"清漪"）：不拦截，生成半遮面通知卡，并在 on_llm_request 中切换主脑历史与替换 prompt
        grp_mention = FakeEvent(
            grp_umo,
            "清漪快来帮我看看这道题",
            private=False,
            at_bot=False,
            sender_id="114514",
            sender_name="老哥",
        )
        await rt.on_incoming_event(grp_mention)
        assert grp_mention.stopped is False
        assert grp_mention.is_wake is True
        notice = grp_mention.get_extra("irmia_mention_notice")
        assert isinstance(notice, str) and "提到了你" in notice

        req = FakeReq(
            prompt="清漪快来帮我看看这道题", image_urls=["http://example.com/a.jpg"]
        )
        await rt.on_llm_request(grp_mention, req)
        # prompt 被替换为半遮面通知卡，原图被收起，conversation 切换到 master_cid_001，身份保持原生 member
        assert req.prompt == notice
        assert req.image_urls == []
        assert req.conversation.cid == "master_cid_001"
        assert req.contexts == [{"role": "user", "content": "主脑历史第一句"}]
        assert grp_mention.role == "member"
        assert len(req.extra_user_content_parts) == 1
        injected_text = req.extra_user_content_parts[0].text
        assert "外部会话" in injected_text
        assert "快乐开黑群" in injected_text

        # 5. 测试 send_message_to_user 工具回执增强与账本写入
        @dataclass
        class FakeTool:
            name: str = "send_message_to_user"

        @dataclass
        class FakeContentItem:
            text: str = f"Message sent to session {grp_umo}"

        @dataclass
        class FakeToolResult:
            content: list[FakeContentItem] = field(
                default_factory=lambda: [FakeContentItem()]
            )

        t_res = FakeToolResult()
        await rt.on_llm_tool_respond(
            grp_mention,
            FakeTool(),
            {"messages": [{"type": "plain", "text": "来啦来啦，哪道题呀？"}]},
            t_res,
        )
        assert grp_mention.get_extra("irmia_sent_current_session") is True

        # 6. 测试 [SILENT] 静默与 intercept_direct_reply 开关
        grp_mention._result = FakeResult(text="[SILENT]")
        await rt.on_decorating_result(grp_mention)
        assert grp_mention._result.chain == []

        cfg["intercept_direct_reply"] = True
        grp_mention._result = FakeResult(text="我在屋里自言自语一句")
        await rt.on_decorating_result(grp_mention)
        assert grp_mention._result.chain == []

        # 6b. 测试当前会话 send_message_to_user 暂存回流到 result.chain（供后续表情与分段插件处理）
        grp_mention2 = FakeEvent(
            grp_umo,
            "清漪在吗",
            private=False,
            at_bot=True,
            sender_id="114514",
            sender_name="老哥",
        )
        req2 = FakeReq(prompt="清漪在吗")
        await rt.on_llm_request(grp_mention2, req2)
        from types import SimpleNamespace as _SNS
        tool_mc = _SNS(chain=["真实回复第一句。真实回复第二句。"])
        sent_ok = await ctx.send_message(grp_umo, tool_mc)
        assert sent_ok is True
        assert ctx.sent_messages == []  # 尚未裸发，已暂存等流水线装饰
        grp_mention2._result = FakeResult(text="我回了老哥一句。")
        await rt.on_decorating_result(grp_mention2)
        assert grp_mention2._result.chain == ["真实回复第一句。真实回复第二句。"]
        # 装饰阶段内部（如 splitter 切分前段）再次调用 send_message 时直通底层
        await ctx.send_message(grp_umo, _SNS(chain=["分段1"]))
        assert len(ctx.sent_messages) == 1

        status_txt = await rt.status_report()
        assert "aiocqhttp:FriendMessage:114514" in status_txt
        assert "快乐开黑群" in status_txt

        await rt.terminate()

    asyncio.run(_run())
