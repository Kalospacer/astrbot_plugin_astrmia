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

SENT_CREDENTIAL_NOTE = (
    "\n已送达凭据：这个会话的「我说过什么」里已经有这一篇了"
    "（`read_channel` 里那一行行首是 `（我）`），不必为了确认再发一遍。"
)


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
            f"—— 往上还有更早的记录：想接着往回翻就传 before={next_before} "
            f"（每页 {page_size} 条，翻页不改已读）。"
        )
    return "—— 已经到这个会话最早一条记录了，上面没有更早的。"


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
            f"sid 不是合法的会话标识：{target_sid}。"
            "它就在你上下文的外部会话清单里（形如 aiocqhttp:GroupMessage:群号）。"
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
                f"{target_sid} 里没有比 before={cursor_before} 更早的记录了。"
                "——要么已经翻到头，要么这个游标不属于该会话。"
                "想看当前未读，就不带 before 读一次。"
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
            f"{target_sid} 从 before={cursor_before} 往前 {len(batch)} 条"
            f"（本机时间，正序；**翻页不改已读**）· {where_label}"
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
            f"{target_sid} 里没有取到消息（这个会话可能还没有过记录，或者 sid 写错了）。"
            "请核对当前状态层外部会话清单里的 sid。"
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
        mine_note = (
            "；这个位置含你自己发出去的那些（行首 `（我）`）" if has_own else ""
        )
        way = (
            f"要看更早的（连没点你的那些）就翻页：带上 before={newest_id + 1} 重读一次"
            f"（每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读）。"
            if len(pool) > 0
            else "想看更早的没有了——这个会话能看到的就这些。"
        )
        if times <= 1:
            return (
                f"{target_sid} 没有新消息：你已经读到最新了（停在 id={newest_id}{mine_note}）。"
                f"不必再翻一遍——{way}想接着说就直接调 send_message_to_user，回不回、说什么都由你。"
            )
        return (
            f"{target_sid} 还是没有新消息：这一轮你已经读过它 {times} 次，再读返回的还是同一段"
            f"（停在 id={newest_id}{mine_note}）。{way}"
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
            f"{target_sid} 没有点了你的新消息"
            f"（这里攒着 {asked_cnt} 条没点你的新话，先不摆出来）。"
            f"想看就翻页：带上 before={newest_id + 1} 重读一次"
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
        comp_parts.append(f"其中点你的 {at_count} 条")
    if mine_count > 0:
        comp_parts.append(f"其中你自己的发言 {mine_count} 行")
    comp_str = ("，" + "，".join(comp_parts)) if comp_parts else ""

    pending_cursor = newest_id + 1
    pending_line = (
        f"另有 {not_shown} 条你还没看——往上翻页：带上 before={pending_cursor} 重读一次"
        f"（每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读）"
        if not_shown > 0 and (not_shown != prev_pending or called_this_turn)
        else None
    )

    out_lines = [
        f"{target_sid} 最近 {len(display)} 条{comp_str}（本机时间，正序；已标记读过 id={new_read_up_to}） · {where_label}",
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
        "看某个外部会话（QQ群或私聊）的消息（也包括你自己在这个会话说过的话，行首 `（我）`）。"
        "默认只给未读里点你的那些和它们之前十条（未读少于一屏则全给），看过的标记已读。"
        "想看更早的记录就传 before 游标往回翻（每页 50 条，翻页不改已读）。"
        "想接着说话请调用 send_message_to_user。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "sid": {
                    "type": "string",
                    "description": "会话标识（即 UMO，形如 aiocqhttp:GroupMessage:群号，见上下文的外部会话清单）",
                },
                "before": {
                    "type": "integer",
                    "description": (
                        f"可选翻页游标：取比该消息 id 更早的历史记录（从上一屏页脚抄）。"
                        f"传了就进入翻页模式：一页 {READ_CHANNEL_PAGE_SIZE} 条、不改已读；不传则按默认未读模式读取。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"这一屏最多返回几行（默认 {READ_CHANNEL_DEFAULT_LIMIT}，翻页默认 {READ_CHANNEL_PAGE_SIZE}，"
                        f"上限 {READ_CHANNEL_MAX_LIMIT}）；你自己的发言也占行。"
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
