"""测试信箱分流判据、read_channel 5 分支防刷屏精选、已读水位线推进与游标翻页不改已读。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from irmia.inbox import evaluate_channel_message
from irmia.read_channel_tool import execute_read_channel
from irmia.render import render_mention_notice, render_session_list
from irmia.store import IrmiaStore


def test_inbox_routing_rules() -> None:
    """验证私聊无条件唤醒、群聊仅在 @ 或命中关键词时唤醒。"""
    # 私聊无条件唤醒
    c2c = evaluate_channel_message(
        is_private=True,
        is_platform_at=False,
        text="在吗",
        mention_keywords=["清漪", "弥亚"],
    )
    assert c2c.should_wake is True
    assert c2c.is_private is True

    # 群聊普通闲聊进信箱不唤醒
    grp_plain = evaluate_channel_message(
        is_private=False,
        is_platform_at=False,
        text="今晚吃火锅还是烤肉",
        mention_keywords=["清漪", "弥亚"],
    )
    assert grp_plain.should_wake is False

    # 群聊关键词命中唤醒
    grp_kw = evaluate_channel_message(
        is_private=False,
        is_platform_at=False,
        text="诶，清漪怎么看这件事？",
        mention_keywords=["清漪", "弥亚"],
    )
    assert grp_kw.should_wake is True
    assert grp_kw.is_keyword_mention is True


def test_read_channel_five_branches_and_paging(tmp_path: Path) -> None:
    """验证 read_channel 首次读取、刷屏精选（提及+前10条）、（我）发言合并、已读水位线推进及翻页不改已读。"""

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "irmia_data")
        await store.open()
        umo = "aiocqhttp:GroupMessage:888888"

        # 先写入 3 条初始消息并第一次读取（设定初始已读水位线 id=3）
        for i in range(1, 4):
            await store.ensure_message_recorded(
                umo=umo,
                platform_id="aiocqhttp",
                message_type="GroupMessage",
                session_id="888888",
                sender_id=f"1000{i}",
                sender_name=f"群友{i}",
                text=f"初始消息 {i}",
                ts=1700000000.0 + i,
                group_name="测试摸鱼群",
            )

        first_out = await execute_read_channel(
            store=store,
            sid=umo,
            limit=20,
            mention_keywords=["弥亚"],
            bot_self_ids={"999999"},
        )
        assert "最近 3 条" in first_out
        assert "已读到 id=3" in first_out
        st = await store.get_session_state(umo)
        assert st["read_up_to_id"] == 3

        # 紧接着再读一次（无新消息） -> 提示没有新消息并给翻页指路
        repeat_map: dict[str, int] = {}
        empty_out = await execute_read_channel(
            store=store,
            sid=umo,
            limit=20,
            repeat_counter=repeat_map,
        )
        assert "没有新消息，已经读到最新" in empty_out

        # 模拟水群刷屏：连续塞入 25 条新消息（id 4..28），其中第 18 条提到了『弥亚』，第 20 条是机器人自己的回复
        for i in range(4, 29):
            is_bot = i == 20
            txt = (
                "弥亚快来评评理！"
                if i == 18
                else ("我刚睡醒呢" if is_bot else f"水群废话 {i}")
            )
            await store.ensure_message_recorded(
                umo=umo,
                platform_id="aiocqhttp",
                message_type="GroupMessage",
                session_id="888888",
                sender_id="999999" if is_bot else f"200{i}",
                sender_name="Bot" if is_bot else f"路人{i}",
                text=txt,
                ts=1700000100.0 + i,
                is_bot=is_bot,
            )

        # 默认读取（limit=20，未读共 25 条超过一屏）：应触发提及精选（#18 及其前 10 条 #8..#18）
        digest_out = await execute_read_channel(
            store=store,
            sid=umo,
            limit=20,
            mention_keywords=["弥亚"],
            bot_self_ids={"999999"},
        )
        assert "▶ [" in digest_out and "弥亚快来评评理！" in digest_out
        # 已读水位线只按实际展示的最大非 bot 消息 #18 推进，#19..#28 中的 9 条非 bot 消息保持未读
        assert "已读到 id=18" in digest_out
        assert "还有 9 条未读没列出来，传 before=29 往前翻能看到" in digest_out
        assert "before=8" in digest_out
        st_after = await store.get_session_state(umo)
        assert st_after["read_up_to_id"] == 18

        # 测试按提示带上 before=29 翻页：能看到 #20 的（我）和 #21..#28 的未读消息，且翻页不修改 read_up_to_id（仍保持 18）
        page_out = await execute_read_channel(
            store=store,
            sid=umo,
            before=29,
            limit=10,
        )
        assert "before=29 之前的 10 条" in page_out
        assert "（我）: 我刚睡醒呢" in page_out
        assert "翻页不改已读" in page_out
        st_paged = await store.get_session_state(umo)
        assert st_paged["read_up_to_id"] == 18

        # 验证跨轮次场景：先把剩余未读读完推进到 28，随后 Bot 回复 #29，接着群友又发了 #30, #31
        await store.mark_session_all_read(umo)
        await store.ensure_message_recorded(
            umo=umo,
            sender_id="999999",
            text="上一轮结尾的回复",
            ts=1700000200.0,
            is_bot=True,
        )
        await store.ensure_message_recorded(
            umo=umo,
            sender_id="2001",
            sender_name="路人甲",
            text="新一轮群友消息 A",
            ts=1700000201.0,
            is_bot=False,
        )
        await store.ensure_message_recorded(
            umo=umo,
            sender_id="2002",
            sender_name="路人乙",
            text="新一轮群友消息 B",
            ts=1700000202.0,
            is_bot=False,
        )
        cross_turn_out = await execute_read_channel(store=store, sid=umo, limit=20)
        assert "（我）: 上一轮结尾的回复" in cross_turn_out
        assert "新一轮群友消息 A" in cross_turn_out
        assert "新一轮群友消息 B" in cross_turn_out
        st_final = await store.get_session_state(umo)
        assert st_final["read_up_to_id"] == 31

        await store.close()

    asyncio.run(_run())


def test_render_mention_notice_and_session_list(tmp_path: Path) -> None:
    """验证点名通知与外部会话清单的渲染格式。"""
    notice = render_mention_notice(
        umo="aiocqhttp:GroupMessage:888888",
        where_name="摸鱼群",
        caller_name="阿强",
        is_at=True,
        at_label="18:30",
        unread=12,
        topic="讨论周末聚餐地点",
    )
    assert "18:30，摸鱼群 里有人@了你（是 阿强）" in notice
    assert "那边一共有 12 条未读" in notice
    assert "那边在聊：讨论周末聚餐地点。" in notice
    assert "read_channel" in notice

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "irmia_list")
        await store.open()
        umo = "aiocqhttp:GroupMessage:888888"
        await store.ensure_message_recorded(
            umo=umo,
            platform_id="aiocqhttp",
            message_type="GroupMessage",
            session_id="888888",
            sender_id="12345",
            sender_name="阿强",
            text="大家晚上好",
            ts=1700000000.0,
            group_name="摸鱼群",
        )
        await store.update_topic(umo, "大家在打招呼", to_id=1)
        sessions = await store.list_sessions()
        rendered = render_session_list(sessions, lambda s: s.group_name)
        assert "摸鱼群｜sid aiocqhttp:GroupMessage:888888" in rendered
        assert "｜1 条未读｜在聊：大家在打招呼" in rendered
        await store.close()

    asyncio.run(_run())
