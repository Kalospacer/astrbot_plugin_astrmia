"""read_channel 工具实现（复刻 I.R.M.I.A src/tools/admin.ts 的 read_channel 5 分支防刷屏与游标翻页语义）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .inbox import mentions_keyword
from .render import format_short_time
from .store import ChannelMessageRow, IrmiaStore, parse_umo

try:
    from astrbot.api import FunctionTool
except Exception:  # pragma: no cover - 单元测试无 AstrBot 环境时的轻量基类兜底

    class FunctionTool:  # type: ignore[no-redef]
        pass


READ_CHANNEL_DEFAULT_LIMIT = 20
READ_CHANNEL_PAGE_SIZE = 50
READ_CHANNEL_MAX_LIMIT = 100
READ_CHANNEL_MENTION_CONTEXT = 10

SELF_SPEAK_LABEL = "（我）"
MENTION_ROW_MARK = "▶ "


def _is_row_mentioning_me(
    row: ChannelMessageRow,
    bot_self_ids: set[str],
    mention_keywords: Sequence[str],
) -> bool:
    """判断一条外部消息是否在点她（包含 [@bot_qq] 标记或命中称呼关键词）。"""
    if row.is_bot:
        return False
    text = row.text or ""
    for bid in bot_self_ids:
        if bid and (f"@{bid}" in text or f"[@{bid}]" in text):
            return True
    if "[@all]" in text.lower() or "@全体成员" in text:
        return True
    return mentions_keyword(text, mention_keywords)


@dataclass
class DigestSelection:
    """默认未读视图的精选结果。"""

    items: list[ChannelMessageRow] = field(default_factory=list)
    mentioned_ids: set[int] = field(default_factory=set)


def select_mention_digest(
    pool: Sequence[ChannelMessageRow],
    prev_read_id: int,
    limit: int,
    bot_self_ids: set[str],
    mention_keywords: Sequence[str],
    own_trigger_idx: int | None = None,
) -> DigestSelection:
    """从消息池中挑选未读里『点到她的那些行（及她刚发出的触发点）+ 每条之前 10 条上下文』。"""
    mentioned_ids: set[int] = set()
    trigger_indices: list[int] = []

    for idx, item in enumerate(pool):
        if own_trigger_idx is not None and idx == own_trigger_idx:
            trigger_indices.append(idx)
            continue
        if item.id <= prev_read_id:
            continue
        if _is_row_mentioning_me(item, bot_self_ids, mention_keywords):
            mentioned_ids.add(item.id)
            trigger_indices.append(idx)

    if not trigger_indices:
        return DigestSelection(items=[], mentioned_ids=mentioned_ids)

    selected_indices: set[int] = set()
    for t_idx in trigger_indices:
        start = max(0, t_idx - READ_CHANNEL_MENTION_CONTEXT)
        for i in range(start, t_idx + 1):
            selected_indices.add(i)

    ordered = [pool[i] for i in sorted(selected_indices)]
    if len(ordered) > limit:
        ordered = ordered[-limit:]
    return DigestSelection(items=ordered, mentioned_ids=mentioned_ids)


def _paging_footer(
    next_before: int, more: bool, page_size: int = READ_CHANNEL_PAGE_SIZE
) -> str:
    """生成页脚翻页指路说明。"""
    if more and next_before > 1:
        return (
            f"—— 更早的记录：传 before={next_before} 往前翻"
            f"（每页 {page_size} 条，翻页不改已读）。"
        )
    return "—— 已经是这个会话最早的记录了。"


async def execute_read_channel(
    *,
    store: IrmiaStore,
    sid: str,
    before: int | None = None,
    limit: int | None = None,
    contacts_map: dict[str, str] | None = None,
    mention_keywords: Sequence[str] = (),
    bot_self_ids: set[str] | None = None,
    called_sid_this_turn: str | None = None,
    tz_name: str = "Asia/Shanghai",
    repeat_counter: dict[str, int] | None = None,
) -> str:
    """执行 read_channel 核心逻辑（纯业务函数，便于直接单测与工具调用）。"""
    target_sid = str(sid or "").strip()
    pid, mtype, session_id = parse_umo(target_sid)
    if not pid or not mtype or not session_id:
        return (
            f"sid 格式不对：{target_sid}。"
            "会话清单里的 sid 长这样：aiocqhttp:GroupMessage:群号。"
        )

    is_paging = before is not None and int(before) > 0
    default_lim = (
        READ_CHANNEL_PAGE_SIZE if is_paging else READ_CHANNEL_DEFAULT_LIMIT
    )
    raw_lim = default_lim if limit is None else int(limit)
    eff_limit = max(1, min(READ_CHANNEL_MAX_LIMIT, raw_lim))
    self_ids = bot_self_ids or set()
    cmap = contacts_map or {}

    st = await store.get_session_state(target_sid)
    group_name = str(st.get("group_name", "") or "")

    def _mentioned_ids(rows: Sequence[ChannelMessageRow]) -> set[int]:
        return {
            r.id
            for r in rows
            if _is_row_mentioning_me(r, self_ids, mention_keywords)
        }

    def _render_rows(
        rows: Sequence[ChannelMessageRow], mentioned_set: set[int]
    ) -> list[str]:
        lines: list[str] = []
        for r in rows:
            stamp = format_short_time(r.ts, tz_name)
            who = (
                SELF_SPEAK_LABEL
                if r.is_bot
                else store.resolve_person_name(
                    r.sender_id, r.sender_name, r.sender_card, cmap
                )
            )
            prefix = (
                MENTION_ROW_MARK
                if ((not r.is_bot) and r.id in mentioned_set)
                else ""
            )
            body = (r.text or "").replace("\r\n", "\n").strip() or "[空消息]"
            lines.append(f"{prefix}[{stamp} #{r.id}] {who}: {body}")
        return lines

    # ── ① 翻页模式：给 before 游标 -> 读历史，一页默认 50 条，绝不修改已读水位线 ──
    if is_paging:
        cursor_before = int(before)  # type: ignore[arg-type]
        batch = store.fetch_messages(
            target_sid, before_id=cursor_before, limit=eff_limit
        )
        if not batch:
            return (
                f"{target_sid} 在 before={cursor_before} 之前没有记录了。"
                "可能已经翻到头，也可能这个游标不是这个会话的。"
                "想看未读就不带 before 再读一次。"
            )
        where_label = store.resolve_session_name(
            target_sid,
            group_name,
            batch[-1].sender_id,
            batch[-1].sender_name,
            cmap,
        )
        mentioned = _mentioned_ids(batch)
        header = (
            f"{target_sid} · {where_label}｜before={cursor_before} 之前的 {len(batch)} 条"
            "（本机时间，从旧到新；翻页不改已读）"
        )
        footer = _paging_footer(
            next_before=batch[0].id,
            more=len(batch) >= eff_limit,
            page_size=READ_CHANNEL_PAGE_SIZE,
        )
        return "\n".join([header, *_render_rows(batch, mentioned), footer])

    # ── 默认模式：读取最近消息池 ──
    pool_size = max(eff_limit * 4, 100)
    pool = store.fetch_messages(target_sid, limit=pool_size)
    if not pool:
        return (
            f"{target_sid} 里没有消息。可能这个会话还没有记录，也可能 sid 写错了，对一下会话清单。"
        )

    prev_read_id = int(st.get("read_up_to_id", 0) or 0)
    prev_pending = int(st.get("pending_not_shown", 0) or 0)
    fetch_floor = pool[0].id
    newest_id = pool[-1].id
    has_unseen_own = store.has_unseen_bot_speech(target_sid)

    # 未读外部消息只按非 bot 消息与 prev_read_id 比较，彻底避免已读过的 is_bot=1 消息卡在 prev_read_id 之上当钉子户
    ext_unread = (
        [m for m in pool if not m.is_bot]
        if prev_read_id == 0
        else [m for m in pool if (not m.is_bot) and m.id > prev_read_id]
    )
    # 仅当处于消息池最末尾的最新一条消息是尚未核对的自身发言时，才将其作为触发点（避免截断后续新到的外部消息）
    own_trigger_idx: int | None = (
        len(pool) - 1 if (has_unseen_own and pool[-1].is_bot) else None
    )

    if prev_read_id == 0:
        unread = pool
    elif ext_unread:
        # 存在未读外部消息时，定位最后一条已读外部消息的位置，取其后的全部外部与穿插的自身发言（严格保持时间轴）
        last_read_idx = -1
        for idx, m in enumerate(pool):
            if (not m.is_bot) and m.id <= prev_read_id:
                last_read_idx = idx
        unread = pool[last_read_idx + 1 :]
    elif own_trigger_idx is not None:
        unread = [pool[own_trigger_idx]]
    else:
        unread = []
    unread_fits_one_screen = len(unread) <= eff_limit

    called_this_turn = called_sid_this_turn == target_sid

    # ── ② 真的没有新东西 ──
    if not ext_unread and own_trigger_idx is None and not called_this_turn:
        times = 1
        if repeat_counter is not None:
            times = repeat_counter.get(target_sid, 0) + 1
            repeat_counter[target_sid] = times
        has_own = any(m.is_bot for m in pool)
        mine_note = "，包括你自己发的（行首（我））" if has_own else ""
        way = (
            f"要看更早的（包括没叫你的）就传 before={newest_id + 1} 往前翻"
            f"（每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读）。"
            if len(pool) > 0
            else "没有更早的了。"
        )
        if times <= 1:
            return (
                f"{target_sid} 没有新消息，已经读到最新（id={newest_id}{mine_note}）。"
                f"不用再读一遍。{way}想说话就用 send_message_to_user。"
            )
        return (
            f"{target_sid} 还是没有新消息。这一轮你已经读了 {times} 次，结果都一样"
            f"（id={newest_id}{mine_note}）。{way}"
        )

    # ── ③ 五分支防刷屏精选（合并分支 1/2，按需延迟调用 select_mention_digest） ──
    if prev_read_id == 0 or (not ext_unread and own_trigger_idx is None and called_this_turn):
        display = pool[-eff_limit:]
        mentioned_set = _mentioned_ids(display)
    else:
        # 仅当自身发言没有跨越未展示的外部未读消息时，才将其作为触发点，防止跳级吞掉中间未读
        can_use_own_trigger = own_trigger_idx is not None and (
            len(ext_unread) <= READ_CHANNEL_MENTION_CONTEXT
        )
        triggered = select_mention_digest(
            pool,
            prev_read_id,
            eff_limit,
            self_ids,
            mention_keywords,
            own_trigger_idx=own_trigger_idx if can_use_own_trigger else None,
        )
        if triggered.items:
            display = triggered.items
            mentioned_set = triggered.mentioned_ids
        elif unread_fits_one_screen or "group" not in mtype.lower():
            display = unread[-eff_limit:]
            mentioned_set = set()
        else:
            display = []
            mentioned_set = set()

    if repeat_counter is not None:
        repeat_counter[target_sid] = repeat_counter.get(target_sid, 0) + 1

    if not display:
        asked_cnt = store.count_unread(target_sid, prev_read_id)
        return (
            f"{target_sid} 没有叫你的新消息。另外有 {asked_cnt} 条没叫你的新消息，这次没列出来。"
            f"想看就传 before={newest_id + 1} 往前翻"
            f"（每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读）。"
        )

    # ── ④ 已读水位线只推进到『这一屏里真正展示给她的最大外部消息 ID』（自身发言不推进水位线） ──
    store.clear_pending_bot_speeches(target_sid)

    shown_up_to = max(
        (m.id for m in display if not m.is_bot), default=prev_read_id
    )
    new_read_up_to = max(prev_read_id, shown_up_to)
    not_shown = store.count_unread(target_sid, new_read_up_to)
    await store.update_read_state(
        target_sid,
        read_up_to_id=new_read_up_to,
        pending_not_shown=not_shown,
    )

    display_floor = display[0].id
    more_history_below = display_floor > fetch_floor or len(pool) >= pool_size

    anchor = next((m for m in reversed(display) if not m.is_bot), display[-1])
    where_label = store.resolve_session_name(
        target_sid, group_name, anchor.sender_id, anchor.sender_name, cmap
    )
    mine_count = sum(1 for m in display if m.is_bot)
    at_count = sum(
        1 for m in display if (not m.is_bot) and m.id in mentioned_set
    )
    comp_parts = []
    if at_count > 0:
        comp_parts.append(f"{at_count} 条叫了你")
    if mine_count > 0:
        comp_parts.append(f"{mine_count} 条是你自己发的")
    comp_str = ("，" + "，".join(comp_parts)) if comp_parts else ""

    pending_cursor = newest_id + 1
    pending_line = (
        f"还有 {not_shown} 条未读没列出来，传 before={pending_cursor} 往前翻能看到"
        f"（每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读）。"
        if not_shown > 0 and (not_shown != prev_pending or called_this_turn)
        else None
    )

    out_lines = [
        f"{target_sid} · {where_label}｜最近 {len(display)} 条{comp_str}（本机时间，从旧到新；已读到 id={new_read_up_to}）",
        *_render_rows(display, mentioned_set),
        _paging_footer(
            next_before=display_floor,
            more=more_history_below,
            page_size=READ_CHANNEL_PAGE_SIZE,
        ),
    ]
    if pending_line:
        out_lines.append(pending_line)
    return "\n".join(out_lines)


@pydantic_dataclass
class ReadChannelTool(FunctionTool):
    """AstrBot v4.5.7+ 标准 dataclass 形态的 read_channel 工具。"""

    name: str = "read_channel"
    description: str = (
        "读某个 QQ 群或私聊的消息，包括你自己在那边说过的话（行首（我））。"
        f"默认只给未读里叫了你的消息和每条之前 {READ_CHANNEL_MENTION_CONTEXT} 条，叫了你的行前面有 ▶；未读不到一屏就全给。读过的会标记已读。"
        f"传 before 可以往前翻历史，每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读。"
        "要说话用 send_message_to_user。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "sid": {
                    "type": "string",
                    "description": "会话的 sid，形如 aiocqhttp:GroupMessage:群号，在会话清单里。",
                },
                "before": {
                    "type": "integer",
                    "description": (
                        "可选。翻页游标，从上一次结果末尾的提示里抄。"
                        f"传了就读这个 id 之前的历史，每页 {READ_CHANNEL_PAGE_SIZE} 条，不改已读；不传就读未读。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"这次最多返回几条。默认 {READ_CHANNEL_DEFAULT_LIMIT}，翻页默认 {READ_CHANNEL_PAGE_SIZE}，"
                        f"最多 {READ_CHANNEL_MAX_LIMIT}。你自己的发言也算条数。"
                    ),
                },
            },
            "required": ["sid"],
        }
    )
    handler_fn: Callable[..., Any] | None = Field(default=None, repr=False)

    async def call(self, context: Any, **kwargs: Any) -> str:
        """执行 read_channel 工具调用。"""
        if self.handler_fn is None:
            return "error: read_channel 尚未绑定运行时宿主。"
        return await self.handler_fn(context, **kwargs)
