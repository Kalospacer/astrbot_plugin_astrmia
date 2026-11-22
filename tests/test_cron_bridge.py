"""CronJob 主脑接管：挂载与还原、透传、主脑绑定、请求钩子、并发写回与外发闸门。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from irmia.cron_bridge import (
    CRON_PATCH_FLAG,
    build_cron_user_prompt,
    install_cron_patch,
    is_cron_event,
    uninstall_cron_patch,
)
from irmia.runtime import IrmiaRuntime
from test_runtime_hooks import FakeContext, FakeConversation, FakeEvent

MASTER = "aiocqhttp:FriendMessage:114514"
TARGET = "aiocqhttp:GroupMessage:888888"
RUN_STARTED_AT = "2026-11-22T08:00:00+08:00"


class StoredConvManager:
    """每次读取都按存储内容返回新对象，模拟数据库里的主脑历史。"""

    def __init__(self) -> None:
        self.cid = "master_cid_001"
        self.history = json.dumps([{"role": "user", "content": "主脑历史第一句"}])
        self.on_update = None

    async def get_curr_conversation_id(self, umo: str) -> str:
        return self.cid

    async def new_conversation(self, umo: str, platform_id: str | None = None) -> str:
        return self.cid

    async def get_conversation(self, umo: str, cid: str, create_if_not_exists: bool = False):
        return FakeConversation(cid=self.cid, history=self.history)

    async def update_conversation(self, umo: str, conversation_id: str | None = None, history=None, **_: Any) -> None:
        if self.on_update is not None:
            self.on_update()
        self.history = json.dumps(history, ensure_ascii=False)


class FakeCronManager:
    def __init__(self) -> None:
        self.native_calls: list[dict[str, Any]] = []

    async def _woke_main_agent(self, *, message, session_str, extras, delivery_session_str=""):
        self.native_calls.append(
            {"message": message, "session_str": session_str, "extras": extras}
        )


def _extras() -> dict[str, Any]:
    payload = {"session": TARGET, "sender_id": "114514", "note": "提醒大家喝水"}
    return {
        "cron_job": {
            "id": "job-1",
            "name": "喝水提醒",
            "description": "每天提醒喝水",
            "note": "提醒大家喝水",
            "run_started_at": RUN_STARTED_AT,
            "session": TARGET,
        },
        "cron_payload": payload,
    }


def _make_runtime(tmp_path: Path, **cfg: Any) -> tuple[IrmiaRuntime, FakeContext]:
    from astrbot.core.tools.message_tools import SendMessageToUserTool

    ctx = FakeContext()
    ctx.conversation_manager = StoredConvManager()
    ctx.cron_manager = FakeCronManager()
    ctx.get_llm_tool_manager = lambda: SimpleNamespace(
        get_builtin_tool=lambda _cls: SendMessageToUserTool()
    )
    raw = {"master_umo": MASTER, "heartbeat_enabled": False, **cfg}
    rt = IrmiaRuntime(ctx, raw, logging.getLogger("test"), data_dir=tmp_path)
    return rt, ctx


def _patch_agent(monkeypatch, rt: IrmiaRuntime, captured: dict[str, Any], on_step=None) -> None:
    from astrbot.core.agent.runners.base import AgentState

    async def _steps(_max_step):
        if on_step is not None:
            await on_step()
        if False:
            yield None

    runner = SimpleNamespace(
        step_until_done=_steps,
        get_final_llm_resp=lambda: SimpleNamespace(role="assistant", completion_text="已在群里提醒"),
        state=AgentState.DONE,
    )

    async def _fake_build(**kwargs):
        captured["event"] = kwargs["event"]
        captured["req"] = kwargs["req"]
        captured["apply_reset"] = kwargs["apply_reset"]
        return SimpleNamespace(agent_runner=runner, reset_coro=None)

    async def _fake_hook(event, event_type, req):
        captured["hook_type"] = event_type
        await rt.on_llm_request(event, req)

    monkeypatch.setattr("astrbot.core.astr_main_agent.build_main_agent", _fake_build)
    monkeypatch.setattr("astrbot.core.pipeline.context.call_event_hook", _fake_hook)


@pytest.mark.asyncio
async def test_install_is_idempotent_and_uninstall_restores(tmp_path):
    """重复挂载不叠层，卸载后还原为原生方法。"""
    rt, ctx = _make_runtime(tmp_path)
    mgr = ctx.cron_manager
    native = mgr._woke_main_agent
    await rt.initialize()
    try:
        patched = mgr._woke_main_agent
        assert getattr(mgr, CRON_PATCH_FLAG) is True
        assert install_cron_patch(rt) is False
        assert mgr._woke_main_agent is patched
    finally:
        await rt.terminate()
    assert getattr(mgr, CRON_PATCH_FLAG) is False
    assert mgr._woke_main_agent == native
    uninstall_cron_patch(rt)
    assert mgr._woke_main_agent == native


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cfg",
    [{"enabled": False}, {"takeover_cron": False}, {"master_umo": ""}],
)
async def test_passthrough_when_takeover_inactive(tmp_path, cfg):
    """关闭插件、关闭接管或未绑定主脑时原样交给 AstrBot 原生流程。"""
    rt, ctx = _make_runtime(tmp_path, **cfg)
    await rt.initialize()
    try:
        await ctx.cron_manager._woke_main_agent(
            message="提醒大家喝水", session_str=TARGET, extras=_extras(), delivery_session_str=TARGET
        )
        assert len(ctx.cron_manager.native_calls) == 1
        assert ctx.cron_manager.native_calls[0]["extras"]["cron_job"]["id"] == "job-1"
    finally:
        await rt.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["CPA2/hb-test", "missing/provider"])
async def test_cron_runs_on_master_with_static_system_prompt(tmp_path, monkeypatch, provider_id):
    """接管后：复用心跳模型、绑定主脑、动态任务信息只在 prompt、补发请求钩子、extras 完整保留。"""
    from astrbot.core.star.star_handler import EventType

    rt, ctx = _make_runtime(tmp_path, heartbeat_provider_id=provider_id)
    captured: dict[str, Any] = {}
    _patch_agent(monkeypatch, rt, captured)
    await rt.initialize()
    try:
        await ctx.cron_manager._woke_main_agent(
            message="提醒大家喝水", session_str=TARGET, extras=_extras(), delivery_session_str=TARGET
        )
        assert ctx.cron_manager.native_calls == []
        event, req = captured["event"], captured["req"]

        assert event.__class__.__name__ == "CronMessageEvent" and is_cron_event(event)
        assert event.get_extra("cron_payload")["sender_id"] == "114514"
        assert event.get_extra("cron_job")["run_started_at"] == RUN_STARTED_AT
        assert event.role == "admin" and event.plugins_name is None
        if provider_id == "CPA2/hb-test":
            assert event.get_extra("selected_provider") == provider_id
        else:
            assert event.get_extra("selected_provider") is None

        assert captured["apply_reset"] is False
        assert captured["hook_type"] == EventType.OnLLMRequestEvent
        assert req.conversation.cid == "master_cid_001"
        assert req.contexts == [{"role": "user", "content": "主脑历史第一句"}]
        assert RUN_STARTED_AT not in req.system_prompt and "job-1" not in req.system_prompt
        assert "=== [IRMIA] 你现在的处境 ===" in req.system_prompt
        assert req.prompt.startswith("[定时任务] 喝水提醒") and RUN_STARTED_AT in req.prompt
        assert "[私聊" not in req.prompt
        assert req.func_tool.get_tool("send_message_to_user") is not None
        status = req.extra_user_content_parts[-1].text
        assert f"这次是定时任务唤醒（目标会话：{TARGET}）。" in status

        history = json.loads(ctx.conversation_manager.history)
        assert history[-1]["content"].startswith("[CronJob] 喝水提醒")
        assert "已在群里提醒" in history[-1]["content"]
    finally:
        await rt.terminate()


@pytest.mark.asyncio
async def test_cron_summary_rereads_latest_history_under_brain_lock(tmp_path, monkeypatch):
    """定时任务运行期间聊天写入了新历史，末尾持主脑锁重读后追加，聊天内容不丢。"""
    rt, ctx = _make_runtime(tmp_path)
    conv_mgr: StoredConvManager = ctx.conversation_manager
    lock_states: list[bool] = []
    conv_mgr.on_update = lambda: lock_states.append(rt._brain_lock.locked())

    async def _chat_meanwhile() -> None:
        history = json.loads(conv_mgr.history)
        history.append({"role": "user", "content": "运行期间的聊天"})
        conv_mgr.history = json.dumps(history, ensure_ascii=False)

    _patch_agent(monkeypatch, rt, {}, on_step=_chat_meanwhile)
    await rt.initialize()
    try:
        await ctx.cron_manager._woke_main_agent(
            message="提醒大家喝水", session_str=TARGET, extras=_extras(), delivery_session_str=TARGET
        )
        contents = [m["content"] for m in json.loads(conv_mgr.history)]
        assert "运行期间的聊天" in contents
        assert contents[-1].startswith("[CronJob]")
        assert lock_states == [True]
    finally:
        await rt.terminate()


@pytest.mark.asyncio
async def test_cron_event_send_gate(tmp_path):
    """定时任务轮外发 [CronJob] 日志被拦截，正常文本不进 intercept_direct_reply 缓冲。"""
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.message_session import MessageSession

    rt, ctx = _make_runtime(tmp_path, intercept_direct_reply=True)
    await rt.initialize()
    try:
        session = MessageSession.from_str(TARGET)
        event = CronMessageEvent(
            context=ctx, session=session, message="提醒", extras=_extras(),
            message_type=session.message_type,
        )
        rt._current_chat_event.set(event)
        with pytest.raises(RuntimeError, match="internal-log blocked"):
            await ctx.send_message(TARGET, MessageChain(chain=[Plain("[CronJob] 喝水提醒完成")]))
        assert await ctx.send_message(TARGET, MessageChain(chain=[Plain("大家记得喝水")]))
        assert [m[1].get_plain_text() for m in ctx.sent_messages] == ["大家记得喝水"]
        assert not event.get_extra("irmia_buffered_tool_chain")
    finally:
        await rt.terminate()


def test_cron_prompt_and_event_detection() -> None:
    """任务对象进 prompt；心跳事件与普通事件不算定时任务。"""
    prompt = build_cron_user_prompt("提醒大家喝水", _extras(), "")
    assert "目标会话：无（仅在后台执行）" in prompt and '"id": "job-1"' in prompt
    assert not is_cron_event(FakeEvent(TARGET, "普通消息"))
    cron_like = FakeEvent(TARGET, "x")
    cron_like.set_extra("cron_payload", {})
    assert is_cron_event(cron_like)
