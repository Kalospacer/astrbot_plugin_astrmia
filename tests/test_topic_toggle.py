"""话题总结开关：触发、展示、数据保留与唤醒小结模型归属。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from irmia.config import load_irmia_config
from irmia.read_channel_tool import execute_read_channel
from irmia.render import build_self_brief, render_session_list
from irmia.runtime import IrmiaRuntime
from irmia.store import IrmiaStore
from irmia.topic import TopicSummarizer
from test_runtime_hooks import FakeContext, FakeEvent, FakeReq

UMO = "aiocqhttp:GroupMessage:777"
MASTER_UMO = "aiocqhttp:FriendMessage:114514"


@pytest.mark.parametrize("value", [None, True, False])
def test_config_toggle_default_and_off(value: bool | None) -> None:
    """缺省保持开启，显式布尔值按配置生效。"""
    raw = {} if value is None else {"topic_summary_enabled": value}
    assert load_irmia_config(raw).topic_summary_enabled is (value is not False)


def test_render_session_list_hides_topic_when_disabled(tmp_path: Path) -> None:
    """关闭后隐藏话题与段数，但保留会话位置和最近说话时间。"""

    async def run() -> None:
        store = IrmiaStore(tmp_path)
        await store.open()
        try:
            await store.ensure_message_recorded(umo=UMO, text="原文内容")
            await store.update_topic(UMO, "旧话题", 1)
            sessions = await store.list_sessions(0)
            enabled = render_session_list(sessions, lambda row: "测试群")
            disabled = render_session_list(
                sessions, lambda row: "测试群", show_topic=False
            )
            assert "在聊：旧话题" in enabled and "段" in enabled
            assert "在聊" not in disabled and "段" not in disabled
            assert UMO in disabled and "我还没在这说过话" in disabled
        finally:
            await store.close()

    asyncio.run(run())


def test_self_brief_variants() -> None:
    """关闭后的说明书只引导读原文，不再宣称有话题或段目录。"""
    enabled = build_self_brief(segment_msg_count=50)
    disabled = build_self_brief(topic_summary_enabled=False)
    assert "每 50 条消息为一段" in enabled and "话题摘要" in enabled
    assert "分段" not in disabled and "话题" not in disabled
    assert "read_channel" in disabled and "原文" in disabled


@pytest.mark.parametrize("segment", [None, 1])
def test_read_channel_skips_segment_dir_when_disabled(
    tmp_path: Path, segment: int | None
) -> None:
    """即使有旧段或传了旧段编号，关闭时也读最新原文；重开可展开旧段。"""

    async def run() -> None:
        store = IrmiaStore(tmp_path)
        await store.open()
        try:
            for index in range(1, 31):
                await store.ensure_message_recorded(
                    umo=UMO, text=f"原文第 {index} 条", ts=1700000000 + index
                )
            await store.append_topic_segment(
                UMO,
                start_msg_id=1,
                end_msg_id=25,
                start_ts=1700000001,
                end_ts=1700000025,
                summary="旧段总结",
            )
            await store.update_topic(UMO, "旧话题", 25)
            old_segments = store.list_topic_segments(UMO)
            output = await execute_read_channel(
                store=store, sid=UMO, summary_enabled=False, segment=segment
            )
            assert "[段 " not in output and "旧段总结" not in output
            assert "原文第 30 条" in output
            state = await store.get_session_state(UMO)
            assert state["read_up_to_id"] == 30
            assert state["topic_to_id"] == 25 and state["topic"] == "旧话题"
            assert store.list_topic_segments(UMO) == old_segments

            await store.close()
            await store.open()
            assert store.list_topic_segments(UMO) == old_segments
            assert (await store.get_session_state(UMO))["topic_to_id"] == 25
            restored = await execute_read_channel(store=store, sid=UMO, segment=1)
            assert "段 1，25 条" in restored and "原文第 1 条" in restored
            assert "原文第 30 条" not in restored

            for index in range(31, 51):
                await store.ensure_message_recorded(umo=UMO, text=f"新增 {index}")
            provider = SimpleNamespace(
                text_chat=AsyncMock(
                    return_value=SimpleNamespace(completion_text='{"topic": "新话题"}')
                )
            )
            summarizer = TopicSummarizer(store, None, logging.getLogger("test"))
            summarizer.resolve_provider = AsyncMock(return_value=provider)
            await summarizer.maybe_summarize(
                UMO, load_irmia_config({}), force_for_mention=True
            )
            assert (await store.get_session_state(UMO))["topic_to_id"] == 50
            assert store.count_topic_segments(UMO) == 2
            assert store.get_topic_segment(1) == old_segments[0]
        finally:
            await store.close()

    asyncio.run(run())


def test_read_channel_description_variants() -> None:
    """工具描述随开关切换，开启时沿用配置的每段条数。"""
    from irmia.read_channel_tool import read_channel_description

    enabled = read_channel_description(True, segment_msg_count=50)
    disabled = read_channel_description(False)
    assert "段目录" in enabled and "50 条" in enabled
    assert "段" not in disabled and "原文" in disabled
    assert "before" in disabled


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("at_bot", [True, False])
@pytest.mark.parametrize("inline", [True, False])
def test_incoming_skips_summarize_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    at_bot: bool,
    inline: bool,
) -> None:
    """开关同时控制两条触发路径、通知、状态、请求注入与已注册工具。"""

    async def run() -> None:
        ctx = FakeContext()
        runtime = IrmiaRuntime(
            context=ctx,
            config={
                "topic_summary_enabled": enabled,
                "master_umo": MASTER_UMO,
                "group_batch_window_sec": 0,
                "heartbeat_enabled": False,
                "image_keep_days": 0,
                "read_channel_image_enabled": False,
                "mention_inline_enabled": inline,
            },
            logger=logging.getLogger("test"),
            data_dir=tmp_path,
        )
        await runtime.initialize()
        try:
            summarize = AsyncMock(return_value="旧话题")
            monkeypatch.setattr(runtime.topic_summarizer, "maybe_summarize", summarize)
            await runtime.store.update_topic(UMO, "旧话题", 25)
            event = FakeEvent(UMO, "这是一条群消息", at_bot=at_bot)
            await runtime.on_incoming_event(event)
            if runtime._bg_tasks:
                await asyncio.gather(*list(runtime._bg_tasks))
            assert summarize.await_count == int(enabled)
            assert event.stopped is (not at_bot)

            description = ctx.registered_tools[0].description
            status = await runtime.status_report()
            assert ("段目录" in description) is enabled
            assert f"话题概括：{'开启' if enabled else '关闭'}" in status
            assert ("旧话题" in status) is enabled
            if not enabled:
                assert " 段" not in status

            if at_bot:
                req = FakeReq(prompt=event.message_str)
                await runtime.on_llm_request(event, req)
                assert ("那边在聊：旧话题" in req.prompt) is enabled
                injected = req.extra_user_content_parts[-1].text
                assert ("在聊：旧话题" in injected) is enabled
                if not enabled:
                    assert " 段" not in injected

            await runtime.store.append_topic_segment(
                UMO,
                start_msg_id=1,
                end_msg_id=1,
                start_ts=1,
                end_ts=1,
                summary="不应泄露的旧段总结",
            )
            await runtime.store.ensure_message_recorded(umo=UMO, text="工具读取新原文")
            output = await ctx.registered_tools[0].call(
                SimpleNamespace(context=SimpleNamespace(event=event)),
                sid=UMO,
                segment=1,
            )
            if enabled:
                assert "段 1，1 条" in output
            else:
                assert "段 1" not in output and "旧段总结" not in output
                assert "工具读取新原文" in output
            assert runtime.store.get_topic_segment(1) is not None
        finally:
            await runtime.terminate()

    asyncio.run(run())


@pytest.mark.parametrize("provider_id", ["heartbeat/model", ""])
def test_wake_summary_uses_heartbeat_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    """唤醒小结跟随心跳配置，留空时走相同的主会话模型回退。"""

    async def run() -> None:
        ctx = FakeContext()
        runtime = IrmiaRuntime(
            context=ctx,
            config={
                "heartbeat_provider_id": provider_id,
                "light_provider_id": "topic/only",
            },
            logger=logging.getLogger("test"),
            data_dir=tmp_path,
        )
        provider = object()
        resolve = AsyncMock(return_value=provider)
        monkeypatch.setattr(runtime.topic_summarizer, "resolve_provider", resolve)
        summarize = AsyncMock(return_value="看过群聊")
        monkeypatch.setattr("irmia.runtime.summarize_wake", summarize)
        persist = AsyncMock()
        monkeypatch.setattr(
            "astrbot.core.utils.history_saver.persist_agent_history", persist
        )
        try:
            req = SimpleNamespace(
                conversation=ctx.conversation_manager.conv, tool_calls_result=[]
            )
            event = FakeEvent(MASTER_UMO, "心跳", private=True)
            await runtime._append_wake_summary(
                MASTER_UMO, event, req, "心跳提示", "", runtime.cfg()
            )
            resolve.assert_awaited_once_with(MASTER_UMO, provider_id)
            assert summarize.await_args.args[0] is provider
            persist.assert_awaited_once()
        finally:
            await runtime.terminate()

    asyncio.run(run())
