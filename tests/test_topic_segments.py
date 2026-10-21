"""分段信箱：切段、未读口径、建档游标、段目录与段展开。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from irmia.config import load_irmia_config
from irmia.read_channel_tool import execute_read_channel
from irmia.store import IrmiaStore
from irmia.topic import TopicSummarizer

UMO = "aiocqhttp:GroupMessage:777"


async def _feed(store: IrmiaStore, start: int, end: int, *, is_bot: bool = False) -> None:
    """往 umo 里连写 start..end 号消息。"""
    for i in range(start, end + 1):
        await store.ensure_message_recorded(
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="777",
            sender_id="bot" if is_bot else f"u{i}",
            sender_name="Bot" if is_bot else f"群友{i}",
            text=f"第 {i} 条",
            ts=1700000000.0 + i,
            is_bot=is_bot,
        )


def test_segment_append_and_dedup(tmp_path: Path) -> None:
    """同一 end_msg_id 重复写只留一行，内容取最后一次。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "seg")
        await store.open()
        await store.append_topic_segment(
            UMO,
            start_msg_id=1,
            end_msg_id=25,
            start_ts=100.0,
            end_ts=200.0,
            summary="第一段在聊抽卡",
        )
        await store.append_topic_segment(
            UMO,
            start_msg_id=1,
            end_msg_id=25,
            start_ts=100.0,
            end_ts=200.0,
            summary="第一段在聊抽卡（改写）",
        )
        segs = store.list_topic_segments(UMO, after_id=0, limit=10)
        assert len(segs) == 1
        assert segs[0]["summary"] == "第一段在聊抽卡（改写）"
        assert store.count_topic_segments(UMO, after_id=0) == 1

        await store.append_topic_segment(
            UMO,
            start_msg_id=26,
            end_msg_id=50,
            start_ts=300.0,
            end_ts=400.0,
            summary="第二段在聊新赛季",
        )
        assert store.count_topic_segments(UMO, after_id=0) == 2
        assert len(store.list_topic_segments(UMO, after_id=25, limit=10)) == 1
        await store.close()

    asyncio.run(_run())


def test_unread_excludes_backfill(tmp_path: Path) -> None:
    """backfill 灌进来的历史不算未读。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "bf")
        await store.open()
        await _feed(store, 1, 5)
        conn = store._ensure_conn()
        conn.execute("UPDATE fallback_messages SET source='backfill'")
        conn.commit()
        assert store.count_unread(UMO, 0) == 0
        await store.close()

    asyncio.run(_run())


def test_new_session_cursor_at_max(tmp_path: Path) -> None:
    """建档即已读：新群第一条进来后未读为 0。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "new")
        await store.open()
        await _feed(store, 1, 1)
        st = await store.get_session_state(UMO)
        assert st["read_up_to_id"] == 1
        assert store.count_unread(UMO, st["read_up_to_id"]) == 0
        await store.close()

    asyncio.run(_run())


def test_fetch_messages_asc_from_gap(tmp_path: Path) -> None:
    """asc=True 时从 after_id 之后第一条往后取，积压再多也不跳空。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "asc")
        await store.open()
        await _feed(store, 1, 100)
        first = store.fetch_messages(UMO, after_id=0, limit=25, asc=True)
        assert [m.id for m in first] == list(range(1, 26))
        second = store.fetch_messages(UMO, after_id=25, limit=25, asc=True)
        assert [m.id for m in second] == list(range(26, 51))
        # 默认（非 asc）仍然取最近的一批
        tail = store.fetch_messages(UMO, limit=25)
        assert [m.id for m in tail] == list(range(76, 101))
        await store.close()

    asyncio.run(_run())


def test_read_channel_returns_segment_dir(tmp_path: Path) -> None:
    """有段时给目录，游标推到最新非 bot 消息。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "dir")
        await store.open()
        await _feed(store, 1, 30)
        await store.append_topic_segment(
            UMO,
            start_msg_id=1,
            end_msg_id=25,
            start_ts=1700000001.0,
            end_ts=1700000025.0,
            summary="在聊抽卡",
        )
        out = await execute_read_channel(store=store, sid=UMO)
        assert "[段 1]" in out
        assert "在聊抽卡" in out
        assert "传 segment=<编号> 展开某段的原文。" in out
        st = await store.get_session_state(UMO)
        assert st["read_up_to_id"] == 30
        await store.close()

    asyncio.run(_run())


def test_read_channel_expand_segment(tmp_path: Path) -> None:
    """segment=N 返回该段原文，不动游标。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "expand")
        await store.open()
        await _feed(store, 1, 30)
        await store.append_topic_segment(
            UMO,
            start_msg_id=1,
            end_msg_id=25,
            start_ts=1700000001.0,
            end_ts=1700000025.0,
            summary="在聊抽卡",
        )
        before = (await store.get_session_state(UMO))["read_up_to_id"]
        out = await execute_read_channel(store=store, sid=UMO, segment=1)
        assert "段 1，25 条" in out
        assert "第 1 条" in out and "第 25 条" in out
        assert "第 26 条" not in out
        st = await store.get_session_state(UMO)
        assert st["read_up_to_id"] == before

        missing = await execute_read_channel(store=store, sid=UMO, segment=999)
        assert "没有段 999" in missing

        other = await execute_read_channel(
            store=store, sid="aiocqhttp:GroupMessage:888", segment=1
        )
        assert "不是这个会话" in other
        await store.close()

    asyncio.run(_run())


def test_maybe_summarize_fills_gap(tmp_path: Path) -> None:
    """积压 120 条时一次最多切 5 段，首尾相接；少于一整窗的尾巴留着下次。"""

    class _FakeProvider:
        async def text_chat(self, prompt: str, session_id: str, persist: bool) -> Any:
            return SimpleNamespace(completion_text='{"topic": "在聊抽卡"}')

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "fill")
        await store.open()
        await _feed(store, 1, 120)

        cfg = load_irmia_config({})
        cfg.topic_cooldown_sec = 0
        cfg.topic_min_unread = 1

        summarizer = TopicSummarizer(store, None, logging.getLogger("test"))

        async def _fake_resolve(umo: str, provider_id: str) -> Any:
            return _FakeProvider()

        summarizer.resolve_provider = _fake_resolve  # type: ignore[method-assign]

        await summarizer.maybe_summarize(UMO, cfg, force_for_mention=True)

        segs = store.list_topic_segments(UMO, after_id=0, limit=100)
        assert len(segs) == 4
        ordered = sorted(segs, key=lambda s: s["end_msg_id"])
        assert ordered[0]["start_msg_id"] == 1
        assert ordered[0]["end_msg_id"] == 25
        for prev, cur in zip(ordered, ordered[1:]):
            assert cur["start_msg_id"] == prev["end_msg_id"] + 1
        assert ordered[-1]["end_msg_id"] == 100

        st = await store.get_session_state(UMO)
        assert st["topic_to_id"] == 100
        assert st["topic"] == "在聊抽卡"
        await store.close()

    asyncio.run(_run())


def test_tail_updates_topic_without_advancing_cursor(tmp_path: Path) -> None:
    """不足一段的尾巴也更新「在聊」，但总结游标不动，下次还能凑满整段。"""

    class _FakeProvider:
        async def text_chat(self, prompt: str, session_id: str, persist: bool) -> Any:
            return SimpleNamespace(completion_text='{"topic": "在聊抽卡"}')

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "tail")
        await store.open()
        await _feed(store, 1, 10)

        cfg = load_irmia_config({})
        cfg.topic_cooldown_sec = 0
        cfg.topic_min_unread = 1

        summarizer = TopicSummarizer(store, None, logging.getLogger("test"))

        async def _fake_resolve(umo: str, provider_id: str) -> Any:
            return _FakeProvider()

        summarizer.resolve_provider = _fake_resolve  # type: ignore[method-assign]

        await summarizer.maybe_summarize(UMO, cfg, force_for_mention=True)

        assert store.list_topic_segments(UMO, after_id=0, limit=10) == []
        st = await store.get_session_state(UMO)
        assert st["topic"] == "在聊抽卡"
        assert st["topic_to_id"] == 0
        await store.close()

    asyncio.run(_run())


def test_today_history_segments_split_by_time(tmp_path: Path) -> None:
    """今天段与历史段按产生时间分区，跟读没读过无关。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "split")
        await store.open()
        await _feed(store, 1, 30)
        await store.append_topic_segment(
            UMO,
            start_msg_id=1,
            end_msg_id=25,
            start_ts=1700000001.0,
            end_ts=1700000025.0,
            summary="昨天那一段",
        )
        day_start = 1700000026.0
        await store.append_topic_segment(
            UMO,
            start_msg_id=26,
            end_msg_id=30,
            start_ts=day_start + 1.0,
            end_ts=day_start + 5.0,
            summary="今天这一段",
        )

        sessions = await store.list_sessions(day_start)
        assert sessions[0].today_segments == 1
        assert sessions[0].history_segments == 1
        await store.close()

    asyncio.run(_run())


def test_segment_directory_offers_before_hint(tmp_path: Path) -> None:
    """目录只列一屏，更早的段给出往前翻的入口。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "dir")
        await store.open()
        await _feed(store, 1, 300)
        for i in range(12):
            start = i * 25 + 1
            await store.append_topic_segment(
                UMO,
                start_msg_id=start,
                end_msg_id=start + 24,
                start_ts=1700000000.0 + start,
                end_ts=1700000000.0 + start + 24,
                summary=f"第 {i + 1} 段",
            )

        out = await execute_read_channel(store=store, sid=UMO, segment_page=10)
        assert "[段 12]" in out
        assert "10 段，更早还有 2 段" in out
        assert "再往前 2 段不在这一屏" in out
        assert "before=51" in out
        await store.close()

    asyncio.run(_run())


def test_mention_digest_skips_segment_directory(tmp_path: Path) -> None:
    """点名摘要给的是叫你的那几句，不是段目录。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "digest")
        await store.open()
        await _feed(store, 1, 25)
        await store.append_topic_segment(
            UMO,
            start_msg_id=1,
            end_msg_id=25,
            start_ts=1700000001.0,
            end_ts=1700000025.0,
            summary="在聊抽卡",
        )
        await store.ensure_message_recorded(
            umo=UMO,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="777",
            sender_id="u99",
            sender_name="群友99",
            text="@123456 清漪在吗",
            ts=1700000100.0,
        )

        out = await execute_read_channel(
            store=store, sid=UMO, digest_only=True, bot_self_ids={"123456"}
        )
        assert "[段 " not in out
        assert "清漪在吗" in out
        await store.close()

    asyncio.run(_run())
