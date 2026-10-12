"""测试与 AstrHe 数据库（messages 聊天账本与 persons 关系本）的无锁联动读取。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from irmia.read_channel_tool import execute_read_channel
from irmia.store import IrmiaStore


def test_astrhe_database_bridge(tmp_path: Path) -> None:
    """构造模拟的 AstrHe 数据库，验证 IrmiaStore 直接读取其 messages 与 persons 别名并剔除撤回消息。"""
    astrhe_dir = tmp_path / "astrbot_plugin_astrhe"
    astrhe_dir.mkdir(parents=True, exist_ok=True)
    astrhe_db = astrhe_dir / "astrhe.db"

    conn = sqlite3.connect(str(astrhe_db))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            umo TEXT NOT NULL,
            platform_id TEXT NOT NULL,
            message_type TEXT NOT NULL,
            session_id TEXT NOT NULL,
            platform_message_id TEXT,
            sender_id TEXT NOT NULL DEFAULT '',
            sender_name TEXT NOT NULL DEFAULT '',
            sender_card TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            recalled INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'live'
        );
        CREATE TABLE persons (
            qq TEXT PRIMARY KEY,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            relation_text TEXT NOT NULL DEFAULT '',
            recent_text TEXT NOT NULL DEFAULT '',
            anniversaries_json TEXT NOT NULL DEFAULT '[]',
            expected_friend_until REAL,
            expected_reason TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            evidence TEXT NOT NULL DEFAULT ''
        );
        """
    )
    # 写入关系本别名：114514 -> "笨蛋老哥"
    conn.execute(
        "INSERT INTO persons(qq, aliases_json, updated_at) VALUES(?, ?, ?)",
        ("114514", json.dumps(["笨蛋老哥", "哥哥"], ensure_ascii=False), 1700000000.0),
    )
    umo = "aiocqhttp:GroupMessage:777879783"
    # 写入 3 条消息：1 条正常、1 条已撤回（recalled=1）、1 条 Bot 经 send_message_to_user 发出的记录（is_bot=1）
    conn.execute(
        "INSERT INTO messages(umo, platform_id, message_type, session_id, sender_id, sender_name, text, ts, is_bot, recalled) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (umo, "aiocqhttp", "GroupMessage", "777879783", "114514", "路人甲网名", "清漪今天真棒", 1700000010.0, 0, 0),
    )
    conn.execute(
        "INSERT INTO messages(umo, platform_id, message_type, session_id, sender_id, sender_name, text, ts, is_bot, recalled) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (umo, "aiocqhttp", "GroupMessage", "777879783", "114514", "路人甲网名", "这条撤回了不该看见", 1700000020.0, 0, 1),
    )
    conn.execute(
        "INSERT INTO messages(umo, platform_id, message_type, session_id, sender_id, sender_name, text, ts, is_bot, recalled) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (umo, "aiocqhttp", "GroupMessage", "777879783", "999999", "Bot", "哼哼那当然啦", 1700000030.0, 1, 0),
    )
    # 模拟 AstrHe 启动时绕过白名单管线拉取的纯 backfill 野群，不应出现在信箱列表中
    conn.execute(
        "INSERT INTO messages(umo, platform_id, message_type, session_id, sender_id, sender_name, text, ts, is_bot, recalled, source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("aiocqhttp:GroupMessage:534017924", "aiocqhttp", "GroupMessage", "534017924", "888888", "野群路人", "不在白名单的历史消息", 1700000005.0, 0, 0, "backfill"),
    )
    conn.commit()
    conn.close()

    async def _run() -> None:
        store = IrmiaStore(tmp_path / "astrbot_plugin_astrmia", astrhe_db_path=astrhe_db)
        await store.open()

        # 验证从 AstrHe persons 表解析别名
        resolved_name = store.resolve_person_name("114514", "路人甲网名")
        assert resolved_name == "笨蛋老哥"

        # 验证外部会话清单聚合（自动排除已撤回消息，未读数只算非 bot 消息 = 1 条）
        sessions = await store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].umo == umo
        assert sessions[0].total_messages == 2
        assert sessions[0].unread == 1

        # 验证 read_channel 读取：显示关系本别名『笨蛋老哥』与『（我）』，且不包含撤回消息
        out = await execute_read_channel(store=store, sid=umo, limit=20)
        assert "笨蛋老哥: 清漪今天真棒" in out
        assert "（我）: 哼哼那当然啦" in out
        assert "这条撤回了不该看见" not in out

        # 读完后未读数归零，且 read_up_to_id 停在最后一条非 bot 消息 id=1（不被 id=3 的 bot 消息垫高）
        sessions_after = await store.list_sessions()
        assert sessions_after[0].unread == 0
        st_after = await store.get_session_state(umo)
        assert st_after["read_up_to_id"] == 1

        # 验证当主脑通过 send_message_to_user 发送了一条尚未落入 astrhe.db 的消息时，
        # read_channel 能立即看到该条（我）发言，且不会垫高 read_up_to_id 吞掉下一条群友真消息
        await store.ensure_message_recorded(
            umo=umo,
            sender_id="999999",
            text="这是刚通过工具发出的未落盘消息",
            ts=1700000040.0,
            is_bot=True,
        )
        out_pending = await execute_read_channel(store=store, sid=umo, limit=20)
        assert "（我）: 这是刚通过工具发出的未落盘消息" in out_pending
        # 验证前面已读过的上下文（笨蛋老哥: 清漪今天真棒）也被作为前 10 条上下文一起带出
        assert "笨蛋老哥: 清漪今天真棒" in out_pending
        st_pending = await store.get_session_state(umo)
        assert st_pending["read_up_to_id"] == 1

        # 紧接着再读一次（无新消息）：必须正常进入『没有新消息』分支，不会因为 #3 是 bot 消息而死锁
        out_again = await execute_read_channel(store=store, sid=umo, limit=20)
        assert "没有新消息，已经读到最新" in out_again

        # 随后群友在 astrhe.db 发来第 4 条新消息，未读数必须正常变为 1（绝不被吞）
        # 同时再模拟上一轮未读过的暂存 Bot 发言（ts=1700000045.0）排在新群友消息（ts=1700000050.0）之前
        await store.ensure_message_recorded(
            umo=umo,
            sender_id="999999",
            text="穿插在中间的暂存Bot发言",
            ts=1700000045.0,
            is_bot=True,
        )
        c2 = sqlite3.connect(str(astrhe_db))
        c2.execute(
            "INSERT INTO messages(umo, platform_id, message_type, session_id, sender_id, sender_name, text, ts, is_bot, recalled) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (umo, "aiocqhttp", "GroupMessage", "777879783", "114514", "路人甲网名", "群友的新消息不能被吞", 1700000050.0, 0, 0),
        )
        c2.commit()
        c2.close()
        assert store.count_unread(umo, st_pending["read_up_to_id"]) == 1
        out_interleaved = await execute_read_channel(store=store, sid=umo, limit=20)
        assert out_interleaved.index("穿插在中间的暂存Bot发言") < out_interleaved.index("群友的新消息不能被吞")
        await store.close()

    asyncio.run(_run())
