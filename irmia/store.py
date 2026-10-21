"""信箱状态存储，只读联动 AstrHe 的账本与关系本。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROTO_PREFIX_RE = re.compile(r"^[\s\S]*[\x00-\x1f]")
_CTRL_CHARS_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁯﻿]+"
)


def sanitize_display_name(raw: str) -> str:
    """剥离 QQ 协议炫彩昵称残留的二进制头与不可见控制字符。"""
    s = str(raw or "")
    if not s:
        return ""
    s = re.sub(r"[\x00-\x1f⁠-⁯]+$", "", s)
    if any(ord(ch) < 32 for ch in s):
        s = _PROTO_PREFIX_RE.sub("", s)
    s = _CTRL_CHARS_RE.sub("", s)
    return " ".join(s.split())


IRMIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_state (
    umo TEXT PRIMARY KEY,
    read_up_to_id INTEGER NOT NULL DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '',
    topic_to_id INTEGER NOT NULL DEFAULT 0,
    topic_updated_at REAL NOT NULL DEFAULT 0,
    group_name TEXT NOT NULL DEFAULT '',
    pending_not_shown INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fallback_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    platform_id TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    platform_message_id TEXT,
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    sender_card TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0,
    recalled INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live',
    media TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fb_messages_umo_pmid
    ON fallback_messages(umo, platform_message_id)
    WHERE platform_message_id IS NOT NULL AND platform_message_id != '';
CREATE INDEX IF NOT EXISTS idx_fb_messages_umo_recalled_id
    ON fallback_messages(umo, recalled, id DESC);

CREATE TABLE IF NOT EXISTS topic_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    start_msg_id INTEGER NOT NULL DEFAULT 0,
    end_msg_id INTEGER NOT NULL DEFAULT 0,
    start_ts REAL NOT NULL DEFAULT 0,
    end_ts REAL NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    msg_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_topic_segments_umo_end
    ON topic_segments(umo, end_msg_id DESC);
"""

ALIAS_CACHE_TTL_SEC = 60.0


@dataclass
class SessionSummary:
    """外部会话清单单行摘要（对应 I.R.M.I.A 的 SessionEntry）。"""

    umo: str
    platform_id: str
    message_type: str
    session_id: str
    last_sender_id: str
    last_sender_name: str
    last_text: str
    last_ts: float
    total_messages: int
    read_up_to_id: int
    unread: int
    topic: str
    group_name: str
    my_last_ts: float = 0.0
    today_segments: int = 0
    history_segments: int = 0


@dataclass
class ChannelMessageRow:
    """信箱单条消息视图（合并外部发言与 is_bot=1 的自身发言）。"""

    id: int
    umo: str
    platform_id: str
    message_type: str
    session_id: str
    platform_message_id: str
    sender_id: str
    sender_name: str
    sender_card: str
    text: str
    ts: float
    is_bot: bool


#: 账本两张消息表（AstrHe messages / 兜底 fallback_messages）共有的读取列
MESSAGE_COLUMNS = (
    "id, umo, platform_id, message_type, session_id, platform_message_id, "
    "sender_id, sender_name, sender_card, text, ts, is_bot"
)


def _row_to_message(r: sqlite3.Row) -> ChannelMessageRow:
    """把按 MESSAGE_COLUMNS 查出的一行转成信箱消息视图。"""
    return ChannelMessageRow(
        id=int(r["id"]),
        umo=str(r["umo"]),
        platform_id=str(r["platform_id"] or ""),
        message_type=str(r["message_type"] or ""),
        session_id=str(r["session_id"] or ""),
        platform_message_id=str(r["platform_message_id"] or ""),
        sender_id=str(r["sender_id"] or ""),
        sender_name=str(r["sender_name"] or ""),
        sender_card=str(r["sender_card"] or ""),
        text=str(r["text"] or ""),
        ts=float(r["ts"] or 0.0),
        is_bot=bool(r["is_bot"]),
    )


def parse_umo(umo: str) -> tuple[str, str, str]:
    """拆三段式 UMO；格式不合法返回空串三元组。

    Args:
        umo: 统一消息来源字符串。

    Returns:
        (platform_id, message_type, session_id) 三元组；格式不合法时返回空串三元组。
    """
    parts = str(umo or "").split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
        return "", "", ""
    return parts[0], parts[1], parts[2]


class IrmiaStore:
    """管理本插件的信箱水位线/话题状态，并联动读取 AstrHe 的账本与关系本。"""

    def __init__(self, data_dir: Path, astrhe_db_path: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "irmia_state.db"
        self.astrhe_db_path = (
            Path(astrhe_db_path)
            if astrhe_db_path is not None
            else self.data_dir.parent / "astrbot_plugin_astrhe" / "astrhe.db"
        )
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._astrhe_conn: sqlite3.Connection | None = None
        self._astrhe_mtime: float = -1.0
        self._group_names: dict[str, str] = {}
        self._known_umos: set[str] = set()
        self._alias_cache: dict[str, tuple[str | None, float]] = {}
        self._pending_bot_speeches: dict[str, list[tuple[float, float, str, str]]] = {}
        self._unseen_bot_umos: set[str] = set()

    async def open(self) -> None:
        """初始化并打开本插件的 SQLite 状态库。"""
        async with self._lock:
            self._ensure_conn()

    async def close(self) -> None:
        """关闭数据库连接（含本插件状态库与复用的 AstrHe 只读连接）。"""
        async with self._lock:
            if self._astrhe_conn is not None:
                try:
                    self._astrhe_conn.close()
                except Exception:
                    pass
                self._astrhe_conn = None
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(IRMIA_SCHEMA)
            # 老库迁移：fallback_messages 补 media 列（新库建表时已带上）
            try:
                cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(fallback_messages)")
                }
                if "media" not in cols:
                    conn.execute(
                        "ALTER TABLE fallback_messages ADD COLUMN media TEXT NOT NULL DEFAULT ''"
                    )
            except Exception:
                pass
            conn.commit()
            for r in conn.execute("SELECT umo, group_name FROM session_state").fetchall():
                u = str(r["umo"] or "")
                if u:
                    self._known_umos.add(u)
                    gn = str(r["group_name"] or "")
                    if gn:
                        self._group_names[u] = gn
            self._conn = conn
        return self._conn

    def _get_astrhe_conn(self) -> sqlite3.Connection | None:
        """复用 AstrHe 的只读连接，按 mtime 失效。"""
        if not self.astrhe_db_path or not self.astrhe_db_path.is_file():
            if self._astrhe_conn is not None:
                try:
                    self._astrhe_conn.close()
                except Exception:
                    pass
                self._astrhe_conn = None
            return None
        try:
            stat = self.astrhe_db_path.stat()
            if self._astrhe_conn is not None and stat.st_mtime == self._astrhe_mtime:
                return self._astrhe_conn
            if self._astrhe_conn is not None:
                try:
                    self._astrhe_conn.close()
                except Exception:
                    pass
            uri = f"{self.astrhe_db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._astrhe_conn = conn
            self._astrhe_mtime = stat.st_mtime
            return conn
        except Exception:
            return None

    def _select_source_for_umo(self, umo: str) -> tuple[sqlite3.Connection, str]:
        """选消息表来源：AstrHe 有记录就读它，否则读 fallback_messages。"""
        aconn = self._get_astrhe_conn()
        if aconn is not None:
            try:
                row = aconn.execute(
                    "SELECT 1 FROM messages WHERE umo=? LIMIT 1", (umo,)
                ).fetchone()
                if row is not None:
                    return aconn, "messages"
            except Exception:
                pass
        return self._ensure_conn(), "fallback_messages"

    # KV 状态

    async def get_kv(self, key: str, default: str = "") -> str:
        """读取持久化键值。"""
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT value FROM kv_state WHERE key=?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else default

    async def set_kv(self, key: str, value: str) -> None:
        """写入持久化键值。"""
        async with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "INSERT INTO kv_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            conn.commit()

    # 关系本与名称解析

    def get_astrhe_person_alias(self, sender_id: str) -> str | None:
        """取关系本里这个 QQ 的第一个别名，60 秒缓存。"""
        sid = str(sender_id or "").strip()
        if not sid:
            return None
        now = time.time()
        cached = self._alias_cache.get(sid)
        if cached is not None and (now - cached[1]) < ALIAS_CACHE_TTL_SEC:
            return cached[0]

        aconn = self._get_astrhe_conn()
        if aconn is None:
            return None
        alias_val: str | None = None
        try:
            row = aconn.execute(
                "SELECT aliases_json FROM persons WHERE qq=?", (sid,)
            ).fetchone()
            if row and row["aliases_json"]:
                aliases = json.loads(row["aliases_json"])
                if isinstance(aliases, list):
                    for item in aliases:
                        s = str(item or "").strip()
                        if s:
                            alias_val = s
                            break
        except Exception:
            alias_val = None
        self._alias_cache[sid] = (alias_val, now)
        return alias_val

    def resolve_person_name(
        self,
        sender_id: str,
        sender_name: str = "",
        sender_card: str = "",
        contacts_map: dict[str, str] | None = None,
    ) -> str:
        """按 contacts_map、关系本、群名片、尾号的顺序取显示名。"""
        sid = str(sender_id or "").strip()
        cmap = contacts_map or {}
        if sid and sid in cmap:
            return cmap[sid]
        alias = self.get_astrhe_person_alias(sid)
        if alias:
            return alias
        card = sanitize_display_name(sender_card)
        if card:
            return card
        name = sanitize_display_name(sender_name)
        if name:
            return name
        if sid:
            tail = sid[-4:] if len(sid) > 4 else sid
            return f"用户(…{tail})"
        return "某人"

    def resolve_session_name(
        self,
        umo: str,
        group_name: str = "",
        last_sender_id: str = "",
        last_sender_name: str = "",
        contacts_map: dict[str, str] | None = None,
        *,
        with_last_sender: bool = False,
    ) -> str:
        """解析会话显示名（用于外部会话清单与通知卡）。

        Args:
            umo: 会话 UMO。
            group_name: 缓存的群名称。
            last_sender_id: 最近发言人 ID。
            last_sender_name: 最近发言人昵称。
            contacts_map: 插件配置中的备注字典。
            with_last_sender: 群聊时是否拼成「群名 · 最近发言人」。

        Returns:
            可读的会话名称。
        """
        cmap = contacts_map or {}
        _, msg_type, session_id = parse_umo(umo)
        is_group = "group" in msg_type.lower()

        if not is_group:
            if umo in cmap:
                return cmap[umo]
            if session_id in cmap:
                return cmap[session_id]
            return self.resolve_person_name(
                last_sender_id or session_id,
                last_sender_name,
                "",
                cmap,
            )

        if umo in cmap:
            gname = cmap[umo]
        elif session_id in cmap:
            gname = cmap[session_id]
        elif str(group_name or "").strip():
            gname = str(group_name).strip()
        else:
            tail = session_id[-6:] if len(session_id) > 6 else session_id
            gname = f"群聊(…{tail})"

        if not with_last_sender:
            return gname
        person = self.resolve_person_name(
            last_sender_id, last_sender_name, "", cmap
        )
        return gname if gname == person else f"{gname} · {person}"

    # 兜底消息记账

    async def ensure_message_recorded(
        self,
        *,
        umo: str,
        platform_id: str = "",
        message_type: str = "",
        session_id: str = "",
        sender_id: str = "",
        sender_name: str = "",
        sender_card: str = "",
        text: str = "",
        ts: float | None = None,
        platform_message_id: str | None = None,
        is_bot: bool = False,
        group_name: str = "",
    ) -> int:
        """把消息记进账本；AstrHe 已记账的会话只同步群名。"""
        now_ts = ts if ts is not None else time.time()
        pid, mtype, sid = parse_umo(umo)
        eff_pid = platform_id or pid
        eff_mtype = message_type or mtype
        eff_sid = session_id or sid
        if is_bot and text:
            self._unseen_bot_umos.add(umo)
        elif not is_bot:
            self._unseen_bot_umos.discard(umo)

        # 若该会话由 AstrHe 记账，只需在群名有更新时同步一下 group_name
        aconn = self._get_astrhe_conn()
        if aconn is not None:
            try:
                has_in_astrhe = (
                    aconn.execute(
                        "SELECT 1 FROM messages WHERE umo=? LIMIT 1", (umo,)
                    ).fetchone()
                    is not None
                )
                if has_in_astrhe:
                    if is_bot and text:
                        wall_now = time.time()
                        bucket = [
                            item
                            for item in self._pending_bot_speeches.get(umo, [])
                            if wall_now - item[0] < 60.0
                        ]
                        bucket.append((wall_now, now_ts, str(sender_id or "bot"), text))
                        self._pending_bot_speeches[umo] = bucket[-10:]
                    need_state_upsert = umo not in self._known_umos or (
                        bool(group_name) and self._group_names.get(umo) != group_name
                    )
                    if need_state_upsert:
                        first_seen = umo not in self._known_umos
                        self._known_umos.add(umo)
                        if group_name:
                            self._group_names[umo] = group_name
                        init_read_id = (
                            self.get_latest_message_id(umo) if first_seen else 0
                        )
                        async with self._lock:
                            conn = self._ensure_conn()
                            conn.execute(
                                """
                                INSERT INTO session_state(umo, read_up_to_id, group_name, updated_at) VALUES(?, ?, ?, ?)
                                ON CONFLICT(umo) DO UPDATE SET
                                    group_name = CASE WHEN excluded.group_name != '' THEN excluded.group_name ELSE session_state.group_name END,
                                    updated_at = excluded.updated_at
                                """,
                                (umo, init_read_id, group_name, now_ts),
                            )
                            conn.commit()
                    return 0
            except Exception:
                pass

        # 没有 AstrHe 记账时，消息写 fallback_messages，首见会话当场建档并把游标对齐到这条
        async with self._lock:
            conn = self._ensure_conn()
            first_seen = umo not in self._known_umos
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO fallback_messages(
                    umo, platform_id, message_type, session_id, platform_message_id,
                    sender_id, sender_name, sender_card, text, ts, is_bot, source
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    umo,
                    eff_pid,
                    eff_mtype,
                    eff_sid,
                    platform_message_id or None,
                    str(sender_id or ""),
                    str(sender_name or ""),
                    str(sender_card or ""),
                    str(text or ""),
                    now_ts,
                    1 if is_bot else 0,
                    "live",
                ),
            )
            if first_seen or (
                group_name and self._group_names.get(umo) != group_name
            ):
                self._known_umos.add(umo)
                if group_name:
                    self._group_names[umo] = group_name
                init_read_id = int(cur.lastrowid) if first_seen and cur.lastrowid else 0
                conn.execute(
                    """
                    INSERT INTO session_state(umo, read_up_to_id, group_name, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(umo) DO UPDATE SET
                        group_name = CASE WHEN excluded.group_name != '' THEN excluded.group_name ELSE session_state.group_name END,
                        updated_at = excluded.updated_at
                    """,
                    (umo, init_read_id, group_name, now_ts),
                )
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else 0

    # 信箱状态与消息查询

    async def get_session_state(self, umo: str) -> dict[str, Any]:
        """获取指定会话的已读水位线与话题状态。"""
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT umo, read_up_to_id, topic, topic_to_id, topic_updated_at, group_name, pending_not_shown "
                "FROM session_state WHERE umo=?",
                (umo,),
            ).fetchone()
            if row is None:
                return {
                    "umo": umo,
                    "read_up_to_id": 0,
                    "topic": "",
                    "topic_to_id": 0,
                    "topic_updated_at": 0.0,
                    "group_name": "",
                    "pending_not_shown": 0,
                }
            return dict(row)

    async def update_read_state(
        self,
        umo: str,
        *,
        read_up_to_id: int | None = None,
        pending_not_shown: int | None = None,
    ) -> None:
        """推进会话的已读水位线（只增不减）及未展示条数状态。"""
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT read_up_to_id, pending_not_shown FROM session_state WHERE umo=?",
                (umo,),
            ).fetchone()
            cur_read = int(row["read_up_to_id"]) if row else 0
            cur_pending = int(row["pending_not_shown"]) if row else 0

            new_read = (
                max(cur_read, int(read_up_to_id))
                if read_up_to_id is not None
                else cur_read
            )
            new_pending = (
                int(pending_not_shown)
                if pending_not_shown is not None
                else cur_pending
            )
            now_ts = time.time()
            self._known_umos.add(umo)
            conn.execute(
                """
                INSERT INTO session_state(umo, read_up_to_id, pending_not_shown, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(umo) DO UPDATE SET
                    read_up_to_id=excluded.read_up_to_id,
                    pending_not_shown=excluded.pending_not_shown,
                    updated_at=excluded.updated_at
                """,
                (umo, new_read, new_pending, now_ts),
            )
            conn.commit()

    async def update_topic(
        self, umo: str, topic: str, to_id: int, updated_at: float | None = None
    ) -> None:
        """更新会话的话题概括。"""
        now_ts = updated_at if updated_at is not None else time.time()
        async with self._lock:
            conn = self._ensure_conn()
            self._known_umos.add(umo)
            conn.execute(
                """
                INSERT INTO session_state(umo, topic, topic_to_id, topic_updated_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(umo) DO UPDATE SET
                    topic=excluded.topic,
                    topic_to_id=excluded.topic_to_id,
                    topic_updated_at=excluded.topic_updated_at,
                    updated_at=excluded.updated_at
                """,
                (umo, topic.strip(), int(to_id), now_ts, now_ts),
            )
            conn.commit()

    async def append_topic_segment(
        self,
        umo: str,
        *,
        start_msg_id: int,
        end_msg_id: int,
        start_ts: float,
        end_ts: float,
        summary: str,
        msg_count: int = 0,
    ) -> None:
        """写入一段话题概括；同 umo 同 end_msg_id 覆盖不新增。"""
        text = str(summary or "").strip()
        if not umo or int(end_msg_id) <= 0 or not text:
            return
        now_ts = time.time()
        async with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT id FROM topic_segments WHERE umo=? AND end_msg_id=? "
                "ORDER BY id DESC LIMIT 1",
                (umo, int(end_msg_id)),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE topic_segments SET start_msg_id=?, start_ts=?, end_ts=?, "
                    "summary=?, msg_count=?, created_at=? WHERE id=?",
                    (
                        int(start_msg_id),
                        float(start_ts),
                        float(end_ts),
                        text,
                        int(msg_count),
                        now_ts,
                        int(row["id"]),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO topic_segments(umo, start_msg_id, end_msg_id, "
                    "start_ts, end_ts, summary, msg_count, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        umo,
                        int(start_msg_id),
                        int(end_msg_id),
                        float(start_ts),
                        float(end_ts),
                        text,
                        int(msg_count),
                        now_ts,
                    ),
                )
            conn.commit()

    def list_topic_segments(
        self, umo: str, *, after_id: int = 0, limit: int = 10
    ) -> list[dict[str, Any]]:
        """取 end_msg_id > after_id 的段，最新的在前。"""
        conn = self._ensure_conn()
        rows = conn.execute(
            "SELECT id, umo, start_msg_id, end_msg_id, start_ts, end_ts, summary, msg_count "
            "FROM topic_segments WHERE umo=? AND end_msg_id>? "
            "ORDER BY end_msg_id DESC LIMIT ?",
            (umo, int(after_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_topic_segments(self, umo: str, *, after_id: int = 0) -> int:
        """数 end_msg_id > after_id 的段数。"""
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM topic_segments WHERE umo=? AND end_msg_id>?",
            (umo, int(after_id)),
        ).fetchone()
        return int(row["cnt"] or 0) if row else 0

    def get_topic_segment(self, segment_id: int) -> dict[str, Any] | None:
        """按段编号取一段，不存在返回 None。"""
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT id, umo, start_msg_id, end_msg_id, start_ts, end_ts, summary, msg_count "
            "FROM topic_segments WHERE id=?",
            (int(segment_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    async def mark_session_all_read(self, umo: str) -> None:
        """将某个会话（如私聊直接唤醒）标记为已读到当前最新一条消息。"""
        self._unseen_bot_umos.discard(umo)
        latest_id = self.get_latest_message_id(umo)
        if latest_id > 0:
            await self.update_read_state(
                umo, read_up_to_id=latest_id, pending_not_shown=0
            )

    def get_latest_message_id(self, umo: str) -> int:
        """获取某会话当前最新一条消息的行 ID。"""
        db_conn, table = self._select_source_for_umo(umo)
        row = db_conn.execute(
            f"SELECT MAX(id) AS m FROM {table} WHERE umo=? AND recalled=0",
            (umo,),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def count_unread(self, umo: str, read_up_to_id: int) -> int:
        """数游标之后实时抓到的非 bot 消息，补账不算。"""
        _, mtype, _ = parse_umo(umo)
        if mtype and "group" not in mtype.lower():
            return 0
        db_conn, table = self._select_source_for_umo(umo)
        row = db_conn.execute(
            f"SELECT COUNT(*) AS cnt FROM {table} "
            "WHERE umo=? AND recalled=0 AND is_bot=0 AND source='live' AND id>?",
            (umo, int(read_up_to_id)),
        ).fetchone()
        return int(row["cnt"] or 0) if row else 0

    def fetch_messages(
        self,
        umo: str,
        *,
        before_id: int | None = None,
        after_id: int | None = None,
        limit: int = 100,
        asc: bool = False,
    ) -> list[ChannelMessageRow]:
        """读消息；默认取最近 limit 条，asc=True 时从 after_id 之后往后取。"""
        db_conn, table = self._select_source_for_umo(umo)
        clauses = ["umo=?", "recalled=0"]
        params: list[Any] = [umo]
        if before_id is not None:
            clauses.append("id<?")
            params.append(int(before_id))
        if after_id is not None:
            clauses.append("id>?")
            params.append(int(after_id))
        where_sql = " AND ".join(clauses)
        order_sql = "ASC" if asc else "DESC"
        sql = (
            f"SELECT {MESSAGE_COLUMNS} "
            f"FROM {table} WHERE {where_sql} ORDER BY id {order_sql} LIMIT ?"
        )
        params.append(int(limit))
        rows = db_conn.execute(sql, tuple(params)).fetchall()
        result = [_row_to_message(r) for r in (rows if asc else reversed(rows))]
        if table == "messages" and before_id is None and umo in self._pending_bot_speeches:
            wall_now = time.time()
            pid, mtype, sid = parse_umo(umo)
            for recorded_at, sp_ts, sp_sender, sp_text in self._pending_bot_speeches.get(umo, []):
                if wall_now - recorded_at >= 60.0:
                    continue
                already = any(
                    m.is_bot
                    and "".join(sp_text.split()) in "".join(m.text.split())
                    and abs(m.ts - sp_ts) < 10.0
                    for m in result
                )
                if not already:
                    insert_idx = len(result)
                    for idx, existing in enumerate(result):
                        if existing.ts > sp_ts:
                            insert_idx = idx
                            break
                    anchor_id = (
                        result[insert_idx - 1].id
                        if insert_idx > 0
                        else (result[0].id if result else 0)
                    )
                    result.insert(
                        insert_idx,
                        ChannelMessageRow(
                            id=anchor_id,
                            umo=umo,
                            platform_id=pid,
                            message_type=mtype,
                            session_id=sid,
                            platform_message_id="",
                            sender_id=sp_sender,
                            sender_name="Bot",
                            sender_card="",
                            text=sp_text,
                            ts=sp_ts,
                            is_bot=True,
                        ),
                    )
        return result

    def get_message(self, umo: str, msg_id: int) -> ChannelMessageRow | None:
        """按会话和行号从同一账本来源查询消息，不混用两张表的编号。"""
        conn, table = self._select_source_for_umo(umo)
        row = conn.execute(
            f"SELECT {MESSAGE_COLUMNS} FROM {table} "
            "WHERE umo=? AND id=? AND recalled=0",
            (umo, msg_id),
        ).fetchone()
        return _row_to_message(row) if row is not None else None

    def has_unseen_bot_speech(self, umo: str) -> bool:
        """这个会话有没有还没被展示过的自身发言。"""
        return umo in self._unseen_bot_umos

    def clear_pending_bot_speeches(self, umo: str) -> None:
        """展示过就清掉暂存的自身发言。"""
        self._pending_bot_speeches.pop(umo, None)
        self._unseen_bot_umos.discard(umo)

    async def list_sessions(self, day_start_ts: float | None = None) -> list[SessionSummary]:
        """聚合会话摘要；给 day_start_ts 时顺带算今天与历史段数。"""
        async with self._lock:
            conn = self._ensure_conn()
            state_rows = conn.execute("SELECT * FROM session_state").fetchall()
            states = {str(r["umo"]): dict(r) for r in state_rows}

        def _collect_from_table(
            dbc: sqlite3.Connection, table: str, seen_umos: set[str]
        ) -> list[SessionSummary]:
            out: list[SessionSummary] = []
            try:
                agg_rows = dbc.execute(
                    f"""
                    SELECT m.umo, m.platform_id, m.message_type, m.session_id,
                           m.sender_id, m.sender_name, m.sender_card, m.text, m.ts,
                           stats.total_cnt, stats.live_cnt, stats.my_last_ts
                    FROM {table} m
                    INNER JOIN (
                        SELECT umo, MAX(id) AS max_id, COUNT(*) AS total_cnt,
                               SUM(CASE WHEN source = 'live' THEN 1 ELSE 0 END) AS live_cnt,
                               MAX(CASE WHEN is_bot = 1 THEN ts END) AS my_last_ts
                        FROM {table}
                        WHERE recalled=0
                        GROUP BY umo
                    ) stats ON m.id = stats.max_id
                    """
                ).fetchall()
            except Exception:
                return out

            for r in agg_rows:
                umo = str(r["umo"] or "")
                if (
                    not umo
                    or umo in seen_umos
                    or umo.startswith(("astrhe:", "cron:", "irmia:"))
                ):
                    continue
                # 排除 AstrHe 绕过管线大门拉取的纯 backfill 且从未在本插件建档的非白名单野群
                if (
                    table == "messages"
                    and umo not in states
                    and int(r["live_cnt"] or 0) <= 0
                ):
                    continue
                seen_umos.add(umo)
                st = states.get(umo, {})
                read_up_to_id = int(st.get("read_up_to_id", 0) or 0)
                topic = str(st.get("topic", "") or "")
                group_name = str(st.get("group_name", "") or "")
                pid, mtype, sid = parse_umo(umo)
                eff_mtype = mtype or str(r["message_type"] or "")

                if "group" in eff_mtype.lower():
                    u_row = dbc.execute(
                        f"SELECT COUNT(*) AS unread FROM {table} "
                        "WHERE umo=? AND recalled=0 AND is_bot=0 AND source='live' AND id>?",
                        (umo, read_up_to_id),
                    ).fetchone()
                    unread = int(u_row["unread"] or 0) if u_row else 0
                else:
                    unread = 0

                today_segments = 0
                history_segments = 0
                if "group" in eff_mtype.lower() and day_start_ts is not None:
                    seg_row = conn.execute(
                        "SELECT "
                        "SUM(CASE WHEN end_ts>=? AND end_msg_id>? THEN 1 ELSE 0 END) AS today_cnt, "
                        "SUM(CASE WHEN end_msg_id<=? THEN 1 ELSE 0 END) AS hist_cnt "
                        "FROM topic_segments WHERE umo=?",
                        (day_start_ts, read_up_to_id, read_up_to_id, umo),
                    ).fetchone()
                    today_segments = int(seg_row["today_cnt"] or 0) if seg_row else 0
                    history_segments = int(seg_row["hist_cnt"] or 0) if seg_row else 0

                out.append(
                    SessionSummary(
                        umo=umo,
                        platform_id=pid or str(r["platform_id"] or ""),
                        message_type=eff_mtype,
                        session_id=sid or str(r["session_id"] or ""),
                        last_sender_id=str(r["sender_id"] or ""),
                        last_sender_name=str(
                            r["sender_card"] or r["sender_name"] or ""
                        ),
                        last_text=str(r["text"] or "")
                        .replace("\n", " ")
                        .strip()[:60],
                        last_ts=float(r["ts"] or 0.0),
                        total_messages=int(r["total_cnt"] or 0),
                        read_up_to_id=read_up_to_id,
                        unread=unread,
                        topic=topic,
                        group_name=group_name,
                        my_last_ts=float(r["my_last_ts"] or 0.0),
                        today_segments=today_segments,
                        history_segments=history_segments,
                    )
                )
            return out

        seen: set[str] = set()
        summaries: list[SessionSummary] = []
        aconn = self._get_astrhe_conn()
        if aconn is not None:
            summaries.extend(_collect_from_table(aconn, "messages", seen))
        summaries.extend(
            _collect_from_table(self._ensure_conn(), "fallback_messages", seen)
        )
        summaries.sort(key=lambda s: s.last_ts, reverse=True)
        return summaries
