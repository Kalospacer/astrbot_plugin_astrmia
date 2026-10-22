"""测试 IrmiaRuntime 与钩子全链路集成（分流、主脑绑定、半遮面替换、回执凭据、沉默开关与 SilentNightEvent 让路）。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

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
        self.provider_ids: set[str] = {"CPA2/hb-test"}

    def get_config(self, umo: str | None = None) -> dict[str, Any]:
        return {"timezone": "Asia/Shanghai", "admins_id": ["114514"]}

    def get_provider_by_id(self, pid: str) -> Any:
        if pid in self.provider_ids:
            return object()
        return None

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
            "heartbeat_provider_id": "CPA2/hb-test",
        }
        rt = IrmiaRuntime(
            context=ctx,
            config=cfg,
            logger=logging.getLogger("irmia_test"),
            data_dir=tmp_path / "irmia_rt",
        )
        await rt.initialize()
        assert len(ctx.registered_tools) == 2
        assert ctx.registered_tools[0].name == "read_channel"
        assert ctx.registered_tools[1].name == "read_image"

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

        # 4. 群聊关键词提及（"清漪"）：不拦截，先存通知元信息，在 on_llm_request 中生成内嵌卡片并切换主脑历史
        grp_mention = FakeEvent(
            grp_umo,
            "清漪快来帮我看看这道题",
            private=False,
            at_bot=False,
            sender_id="114514",
            sender_name="老哥",
        )
        grp_mention.message_obj.message_id = "msg_mention"
        await rt.on_incoming_event(grp_mention)
        assert grp_mention.stopped is False
        assert grp_mention.is_wake is True
        assert grp_mention.get_extra("irmia_mention_meta")["caller_name"] == "哥哥"
        assert grp_mention.get_extra("irmia_mention_notice") is None

        req = FakeReq(
            prompt="清漪快来帮我看看这道题", image_urls=["http://example.com/a.jpg"]
        )
        await rt.on_llm_request(grp_mention, req)
        # prompt 被替换为内嵌通知卡，原图被收起，conversation 切换到 master_cid_001，身份保持原生 member
        notice = grp_mention.get_extra("irmia_mention_notice")
        assert isinstance(notice, str) and "提到了你" in notice
        assert "清漪快来帮我看看这道题" in notice
        assert "标记已读" in notice
        assert grp_mention.get_extra("irmia_repeat_reads") == {}
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
        await rt.on_llm_response(grp_mention)
        grp_mention._result = FakeResult(text="[SILENT]")
        await rt.on_decorating_result(grp_mention)
        assert grp_mention._result.chain == []

        cfg["intercept_direct_reply"] = True
        grp_mention._result = FakeResult(text="我在屋里自言自语一句")
        await rt.on_decorating_result(grp_mention)
        assert grp_mention._result.chain == []

        # 6b. 测试工具调用途中的过渡话被收起、不提前开启 irmia_in_decorating，且最终步回流到 result.chain
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
        # Step 1: 调 read_channel 前模型顺嘴吐出过渡话 -> 被收起且不置 irmia_in_decorating
        grp_mention2._result = FakeResult(text="我先看看上下文。")
        await rt.on_decorating_result(grp_mention2)
        assert grp_mention2._result.chain == []
        assert grp_mention2.get_extra("irmia_in_decorating") is False
        # Step 2: 调 send_message_to_user 发当前会话 -> 成功暂存回流
        from types import SimpleNamespace as _SNS
        tool_mc = _SNS(chain=["真实回复第一句。真实回复第二句。"])
        sent_ok = await ctx.send_message(grp_umo, tool_mc)
        assert sent_ok is True
        assert ctx.sent_messages == []  # 尚未裸发，已暂存等流水线装饰
        # Step 3: 最终步触发 on_llm_response 后进入 on_decorating_result -> 替换末尾总结为暂存链
        await rt.on_llm_response(grp_mention2)
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


def test_runtime_batch_debounce_switches(tmp_path: Path) -> None:
    """验证私聊与群聊防抖攒批独立开关、窗口共用与连续私聊消息完整聚合。"""

    async def _run() -> None:
        ctx = FakeContext()
        # 1. 开启私聊防抖与群聊防抖，窗口设置为 0.05 秒以便快速测试
        cfg = {
            "enabled": True,
            "master_umo": "aiocqhttp:FriendMessage:114514",
            "mention_keywords": ["清漪"],
            "contacts_map": ["114514 = 哥哥"],
            "group_batch_enabled": True,
            "private_batch_enabled": True,
            "group_batch_window_sec": 0.05,
            "intercept_direct_reply": False,
        }
        rt = IrmiaRuntime(
            context=ctx,
            config=cfg,
            logger=logging.getLogger("irmia_test"),
            data_dir=tmp_path / "irmia_batch_test",
        )
        await rt.initialize()

        priv_umo = "aiocqhttp:FriendMessage:114514"

        # 构造连续两条私聊消息
        ev1 = FakeEvent(
            priv_umo,
            "第一句：帮我想想",
            private=True,
            sender_id="114514",
            sender_name="老哥",
        )
        # ev1 模拟带有一张图片
        class FakeImageComp:
            def __init__(self, url: str) -> None:
                self.url = url

        FakeImageComp.__name__ = "Image"
        ev1.message_obj.message = [FakeImageComp("http://example.com/item1.png")]

        ev2 = FakeEvent(
            priv_umo,
            "第二句：都垫点啥",
            private=True,
            sender_id="114514",
            sender_name="老哥",
        )

        async def _send_ev1():
            await rt.on_incoming_event(ev1)

        async def _send_ev2():
            await asyncio.sleep(0.01)
            await rt.on_incoming_event(ev2)

        await asyncio.gather(_send_ev1(), _send_ev2())

        # ev1 应该被 stop_event 拦截让位
        assert ev1.stopped is True
        # ev2 应该作为 leader 放行
        assert ev2.stopped is False

        # 检查 ev2 进入 on_llm_request 时 prompt 与 image_urls 聚合
        req = FakeReq(prompt="第二句：都垫点啥")
        await rt.on_llm_request(ev2, req)

        # 验证提示词中包含全部两句私聊内容
        assert "第一句：帮我想想" in req.prompt
        assert "第二句：都垫点啥" in req.prompt
        assert "[私聊 · 哥哥 · sid aiocqhttp:FriendMessage:114514]" in req.prompt
        # 验证前序事件的图片被合并到 req.image_urls
        assert "http://example.com/item1.png" in req.image_urls

        # 2. 测试关闭私聊防抖开关 (private_batch_enabled = False)
        cfg["private_batch_enabled"] = False
        ev_no_batch = FakeEvent(
            priv_umo,
            "单发不等待",
            private=True,
            sender_id="114514",
            sender_name="老哥",
        )
        await rt.on_incoming_event(ev_no_batch)
        assert ev_no_batch.stopped is False

        # 3. 测试群聊防抖开关开启时，两条连续点名合并
        cfg["group_batch_enabled"] = True
        grp_umo = "aiocqhttp:GroupMessage:888888"
        grp_ev1 = FakeEvent(
            grp_umo,
            "清漪1",
            private=False,
            at_bot=True,
            sender_id="114514",
            sender_name="老哥",
        )
        grp_ev2 = FakeEvent(
            grp_umo,
            "清漪2",
            private=False,
            at_bot=True,
            sender_id="114514",
            sender_name="老哥",
        )

        async def _send_grp1():
            await rt.on_incoming_event(grp_ev1)

        async def _send_grp2():
            await asyncio.sleep(0.01)
            await rt.on_incoming_event(grp_ev2)

        await asyncio.gather(_send_grp1(), _send_grp2())
        assert grp_ev1.stopped is True
        assert grp_ev2.stopped is False

        # 4. 测试群聊防抖开关关闭 (group_batch_enabled = False)
        cfg["group_batch_enabled"] = False
        grp_ev3 = FakeEvent(
            grp_umo,
            "清漪3立即响应",
            private=False,
            at_bot=True,
            sender_id="114514",
            sender_name="老哥",
        )
        await rt.on_incoming_event(grp_ev3)
        assert grp_ev3.stopped is False

        await rt.terminate()

    asyncio.run(_run())


@pytest.mark.parametrize("intercept", [False, True])
@pytest.mark.parametrize(
    "target", ["aiocqhttp:FriendMessage:114514", "aiocqhttp:GroupMessage:777"]
)
def test_wake_tool_message_sends_immediately(
    tmp_path: Path, intercept: bool, target: str
) -> None:
    """心跳工具消息直接送达，不等待普通聊天的最终回复步骤。"""
    from types import SimpleNamespace
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.tools.message_tools import SendMessageToUserTool
    from irmia.runtime import HeartbeatWakeEvent

    async def _run() -> None:
        master = "aiocqhttp:FriendMessage:114514"
        ctx = FakeContext()
        rt = IrmiaRuntime(
            ctx,
            {
                "master_umo": master,
                "heartbeat_enabled": False,
                "intercept_direct_reply": intercept,
            },
            logging.getLogger("test"),
            data_dir=tmp_path,
        )
        await rt.initialize()
        try:
            session = MessageSession.from_str(master)
            event = HeartbeatWakeEvent(
                context=ctx,
                session=session,
                message="heartbeat",
                extras={"irmia_wake": True},
                message_type=session.message_type,
            )
            await rt.on_llm_request(event, FakeReq(prompt="heartbeat"))
            wrapper = SimpleNamespace(context=SimpleNamespace(event=event, context=ctx))
            result = await SendMessageToUserTool().call(
                wrapper,
                messages=[{"type": "plain", "text": "主动问候"}],
                session=target,
            )

            assert result == f"Message sent to session {target}"
            assert len(ctx.sent_messages) == 1
            assert ctx.sent_messages[0][0] == target
            assert ctx.sent_messages[0][1].get_plain_text() == "主动问候"
            assert not event.get_extra("irmia_buffered_tool_chain")
        finally:
            await rt.terminate()

    asyncio.run(_run())


def test_buffered_reply_flushes_when_turn_finishes(tmp_path: Path) -> None:
    """聊天未进入最终装饰步骤时，由任务结束回调补发一次。"""
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain

    async def _run() -> None:
        umo = "aiocqhttp:FriendMessage:114514"
        ctx = FakeContext()
        rt = IrmiaRuntime(
            ctx,
            {
                "master_umo": umo,
                "heartbeat_enabled": False,
                "intercept_direct_reply": True,
            },
            logging.getLogger("test"),
            data_dir=tmp_path,
        )
        await rt.initialize()
        event = FakeEvent(umo, "问题", private=True)
        try:

            async def _turn() -> None:
                await rt.on_waiting_llm_request(event)
                await rt.on_llm_request(event, FakeReq(prompt="问题"))
                assert await ctx.send_message(
                    umo, MessageChain(chain=[Plain("需要补发的回复")])
                )
                assert ctx.sent_messages == []

            await asyncio.create_task(_turn())
            await asyncio.sleep(0)
            await asyncio.gather(*list(rt._bg_tasks))

            assert len(ctx.sent_messages) == 1
            assert ctx.sent_messages[0][1].get_plain_text() == "需要补发的回复"
            assert not event.get_extra("irmia_buffered_tool_chain")
            assert not rt._brain_lock.locked()
        finally:
            await rt.terminate()

    asyncio.run(_run())


@pytest.mark.parametrize("failure", [False, RuntimeError("transport failed")])
def test_buffered_reply_flush_failure_preserves_buffer(
    tmp_path: Path, failure: Any
) -> None:
    """发送未成功时保留暂存消息，不能先清空再丢失。"""
    from unittest.mock import AsyncMock
    from astrbot.core.message.components import Plain

    async def _run() -> None:
        umo = "aiocqhttp:FriendMessage:114514"
        ctx = FakeContext()
        transport = (
            AsyncMock(side_effect=failure)
            if isinstance(failure, Exception)
            else AsyncMock(return_value=False)
        )
        ctx.send_message = transport
        rt = IrmiaRuntime(
            ctx,
            {"heartbeat_enabled": False, "intercept_direct_reply": True},
            logging.getLogger("test"),
            data_dir=tmp_path,
        )
        await rt.initialize()
        event = FakeEvent(umo, "问题", private=True)
        chain = [Plain("不能丢的回复")]
        event.set_extra("irmia_buffered_tool_chain", chain)
        try:
            await rt._flush_unconsumed_buffered_chain(event)
            assert event.get_extra("irmia_buffered_tool_chain") == chain
            transport.assert_awaited_once()
        finally:
            await rt.terminate()

    asyncio.run(_run())


@pytest.mark.parametrize("intercept", [False, True])
@pytest.mark.parametrize(
    "target", ["aiocqhttp:FriendMessage:114514", "aiocqhttp:GroupMessage:777"]
)
def test_tool_reply_is_recorded_once(
    tmp_path: Path, intercept: bool, target: str
) -> None:
    """工具回执与最终发送完整串联后，一条回复只记账一次。"""
    from types import SimpleNamespace
    from astrbot.core.message.message_event_result import (
        MessageEventResult,
        ResultContentType,
    )
    from astrbot.core.tools.message_tools import SendMessageToUserTool

    async def _run() -> None:
        umo = "aiocqhttp:FriendMessage:114514"
        ctx = FakeContext()
        rt = IrmiaRuntime(
            ctx,
            {
                "master_umo": umo,
                "heartbeat_enabled": False,
                "intercept_direct_reply": intercept,
            },
            logging.getLogger("test"),
            data_dir=tmp_path,
        )
        await rt.initialize()
        event = FakeEvent(umo, "问题", private=True, role="admin")
        try:
            await rt.on_llm_request(event, FakeReq(prompt="问题"))
            wrapper = SimpleNamespace(context=SimpleNamespace(event=event, context=ctx))
            tool = SendMessageToUserTool()
            args = {
                "messages": [{"type": "plain", "text": "唯一的工具回复"}],
                "session": target,
            }
            result = await tool.call(wrapper, **args)
            await rt.on_llm_tool_respond(event, tool, args, result)
            await rt.on_llm_response(event)
            event._result = (
                MessageEventResult()
                .message("普通最终回复")
                .set_result_content_type(ResultContentType.LLM_RESULT)
            )
            await rt.on_decorating_result(event)
            if event._result.chain:
                await ctx.send_message(umo, event._result)
                await rt.on_after_message_sent(event)

            rows = rt.store.fetch_messages(target)
            assert sum(row.is_bot and row.text == "唯一的工具回复" for row in rows) == 1
            if not (intercept and target == umo):
                direct = rt.store.fetch_messages(umo)
                assert (
                    sum(row.is_bot and row.text == "普通最终回复" for row in direct)
                    == 1
                )
        finally:
            await rt.terminate()

    asyncio.run(_run())


def test_batch_order_survives_slow_image_keep(tmp_path: Path, monkeypatch) -> None:
    """先到消息的图片留底较慢，也不能抢走后到消息的攒批代表位置。"""

    async def _run() -> None:
        umo = "aiocqhttp:FriendMessage:114514"
        ctx = FakeContext()
        rt = IrmiaRuntime(
            ctx,
            {
                "master_umo": umo,
                "heartbeat_enabled": False,
                "group_batch_window_sec": 0.02,
            },
            logging.getLogger("test"),
            data_dir=tmp_path,
        )
        await rt.initialize()
        first = FakeEvent(umo, "先到的消息", private=True)
        second = FakeEvent(umo, "后到的消息", private=True)
        first.message_obj.message_id = "first"
        second.message_obj.message_id = "second"
        keep_started = asyncio.Event()
        release_keep = asyncio.Event()

        async def _delayed_keep(event, *args):
            if event is first:
                keep_started.set()
                await release_keep.wait()
            return 0

        monkeypatch.setattr("irmia.runtime.keep_event_images", _delayed_keep)
        first_task = asyncio.create_task(rt.on_incoming_event(first))
        try:
            await keep_started.wait()
            await rt.on_incoming_event(second)
            release_keep.set()
            await first_task

            assert first.stopped is True
            assert second.stopped is False
            assert second.get_extra("irmia_batched_private_texts") == [
                "先到的消息",
                "后到的消息",
            ]
        finally:
            release_keep.set()
            await first_task
            await rt.terminate()

    asyncio.run(_run())
