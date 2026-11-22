"""read_channel 工具：段目录、段展开、游标翻页，以及点名通知共用的读群摘要。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .config import IrmiaConfig
from .inbox import mentions_keyword
from .render import format_short_time
from .store import ChannelMessageRow, IrmiaStore, is_group_umo, parse_umo
from .vision_describe import (
    Captioner,
    captions_for,
    render_with_captions,
    truncate_message_body as truncate_message_body,
)

try:
    from astrbot.api import FunctionTool
except Exception:  # pragma: no cover - 单元测试无 AstrBot 环境时的轻量基类兜底

    class FunctionTool:  # type: ignore[no-redef]
        pass


READ_CHANNEL_DEFAULT_LIMIT = 20
READ_CHANNEL_PAGE_SIZE = 50
READ_CHANNEL_MAX_LIMIT = 100
READ_CHANNEL_SEGMENT_PAGE = IrmiaConfig.segment_rows_per_page
READ_CHANNEL_MENTION_CONTEXT = 10
#: 默认读未读时，紧挨第一条未读之前带上的已读前文条数
READ_CHANNEL_CONTEXT_BRIDGE = 2

SELF_SPEAK_LABEL = "（我）"
MENTION_ROW_MARK = "▶ "


def read_channel_description(
    summary_enabled: bool,
    *,
    segment_msg_count: int = IrmiaConfig.segment_msg_count,
) -> str:
    """按话题开关生成读群工具描述，开启时保留段目录说明。"""
    reading_mode = (
        f"不带参数返回未读的段目录，一段是 {segment_msg_count} 条消息加一句总结，"
        "外加全部尚未成段的原文。"
        "传 segment=<编号> 展开某段原文，传 before=<消息编号> 往前翻历史。"
        if summary_enabled
        else "不带参数返回全部未读原文和紧挨着的两条前文，传 before=<消息编号> 往前翻历史。"
    )
    return (
        "读某个群或私聊的消息，也包括你自己在那边说过的话（行首写着（我））。"
        f"{reading_mode}"
        "读一次即清空该会话未读，翻页不改已读。单条过长的消息会截断显示。"
        "要说话用 send_message_to_user。"
        "图片带一句描述，细节用 read_image，传 sid 和行首的 #编号。"
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


def _paging_hint(next_before: int, more: bool) -> str:
    """往前翻的指路句；翻到头就直说。"""
    if more and next_before > 1:
        return (
            f"传 before={next_before} 往前翻"
            f"（每页 {READ_CHANNEL_PAGE_SIZE} 条，翻页不改已读）"
        )
    return "已经到最早的记录了"


def render_channel_rows(
    rows: Sequence[ChannelMessageRow],
    *,
    store: IrmiaStore,
    mentioned_set: set[int],
    captions: dict[int, list[str | None]],
    tz_name: str,
    contacts_map: dict[str, str],
    image_brief_max_chars: int,
    msg_max_chars: int,
) -> list[str]:
    """把消息渲染成 `[时刻 #编号] 发言人: 正文` 行；先截纯文本，再替换图片描述。"""
    lines: list[str] = []
    for r in rows:
        stamp = format_short_time(r.ts, tz_name)
        who = (
            SELF_SPEAK_LABEL
            if r.is_bot
            else store.resolve_person_name(
                r.sender_id, r.sender_name, r.sender_card, contacts_map
            )
        )
        prefix = (
            MENTION_ROW_MARK if ((not r.is_bot) and r.id in mentioned_set) else ""
        )

        raw_text = (r.text or "").replace("\r\n", "\n").strip()
        raw_text = render_with_captions(
            raw_text,
            captions_for(r, captions),
            image_brief_max_chars,
            text_max_chars=msg_max_chars,
        )
        body = raw_text or "[空消息]"
        lines.append(f"{prefix}[{stamp} #{r.id}] {who}: {body}")
    return lines


@dataclass
class MentionDigest:
    """点名通知用的读群结果：触发原话与前情分开渲染。"""

    trigger_lines: list[str]
    context_lines: list[str]
    oldest_shown_id: int
    leftover: int
    unread: int = 0
    has_more: bool = False


async def collect_mention_digest(
    *,
    store: IrmiaStore,
    sid: str,
    trigger_pmids: Sequence[str] = (),
    max_rows: int = READ_CHANNEL_DEFAULT_LIMIT,
    contacts_map: dict[str, str] | None = None,
    mention_keywords: Sequence[str] = (),
    bot_self_ids: set[str] | None = None,
    tz_name: str = "Asia/Shanghai",
    captioner: Captioner | None = None,
    caption_max_new: int = 6,
    image_brief_max_chars: int = IrmiaConfig.image_brief_max_chars,
    msg_max_chars: int = IrmiaConfig.msg_max_chars,
) -> MentionDigest | None:
    """挑出触发消息与其前情并推进已读；图片只取缓存、不等待识别。

    触发消息是平台消息编号命中 trigger_pmids 的行，或未读里点到她的行；
    都找不到时（账本稍慢）退回最新一条外部消息。每条触发消息带前面
    READ_CHANNEL_MENTION_CONTEXT 条前情，总数不超过 max_rows。
    """
    self_ids = bot_self_ids or set()
    cmap = contacts_map or {}
    st = await store.get_session_state(sid)
    prev_read_id = int(st.get("read_up_to_id", 0) or 0)
    pool_limit = max(max_rows * 4, 100)
    pool = store.fetch_messages(sid, limit=pool_limit)
    if not pool:
        return None

    pmids = {str(p) for p in trigger_pmids if p}
    trigger_idx = [
        idx
        for idx, m in enumerate(pool)
        if not m.is_bot
        and (
            (m.platform_message_id and m.platform_message_id in pmids)
            or (m.id > prev_read_id and _is_row_mentioning_me(m, self_ids, mention_keywords))
        )
    ]
    if not trigger_idx:
        latest = next(
            (idx for idx in range(len(pool) - 1, -1, -1) if not pool[idx].is_bot),
            len(pool) - 1,
        )
        trigger_idx = [latest]

    picked: set[int] = set()
    for t_idx in trigger_idx:
        picked.update(range(max(0, t_idx - READ_CHANNEL_MENTION_CONTEXT), t_idx + 1))
    shown_idx = sorted(picked)[-max(1, max_rows) :]
    trigger_set = set(trigger_idx)
    shown = [pool[i] for i in shown_idx]
    triggers = [pool[i] for i in shown_idx if i in trigger_set]
    context = [pool[i] for i in shown_idx if i not in trigger_set]

    captions = (
        await captioner.caption_rows(shown, wait_sec=0, max_new=caption_max_new)
        if captioner is not None
        else {}
    )
    render_opts = dict(
        store=store,
        captions=captions,
        tz_name=tz_name,
        contacts_map=cmap,
        image_brief_max_chars=image_brief_max_chars,
        msg_max_chars=msg_max_chars,
    )
    context_mentions = {
        r.id for r in context if _is_row_mentioning_me(r, self_ids, mention_keywords)
    }
    unread_total = store.count_unread(sid, prev_read_id)
    if shown_idx[0] > 0:
        has_more = True
    elif len(pool) < pool_limit:
        has_more = False
    else:
        has_more = bool(store.fetch_messages(sid, before_id=shown[0].id, limit=1))

    digest = MentionDigest(
        trigger_lines=render_channel_rows(triggers, mentioned_set=set(), **render_opts),
        context_lines=render_channel_rows(
            context, mentioned_set=context_mentions, **render_opts
        ),
        oldest_shown_id=shown[0].id,
        leftover=max(
            0,
            unread_total
            - sum(1 for m in shown if not m.is_bot and m.id > prev_read_id),
        ),
        unread=unread_total,
        has_more=has_more,
    )

    store.clear_pending_bot_speeches(sid)
    shown_up_to = max((m.id for m in shown if not m.is_bot), default=prev_read_id)
    await store.update_read_state(sid, read_up_to_id=max(prev_read_id, shown_up_to))
    return digest


async def execute_read_channel(
    *,
    store: IrmiaStore,
    sid: str,
    before: int | None = None,
    limit: int | None = None,
    segment: int | None = None,
    segment_page: int = READ_CHANNEL_SEGMENT_PAGE,
    summary_enabled: bool = True,
    contacts_map: dict[str, str] | None = None,
    mention_keywords: Sequence[str] = (),
    bot_self_ids: set[str] | None = None,
    called_sid_this_turn: str | None = None,
    tz_name: str = "Asia/Shanghai",
    repeat_counter: dict[str, int] | None = None,
    captioner: Captioner | None = None,
    caption_wait_sec: float = 8,
    caption_max_new: int = 6,
    image_brief_max_chars: int = IrmiaConfig.image_brief_max_chars,
    msg_max_chars: int = IrmiaConfig.msg_max_chars,
    attention_window: int = IrmiaConfig.attention_window,
    segment_msg_count: int = IrmiaConfig.segment_msg_count,
) -> str:
    """执行 read_channel 核心逻辑（纯业务函数，便于直接单测与工具调用）。"""
    target_sid = str(sid or "").strip()
    pid, mtype, session_id = parse_umo(target_sid)
    if not pid or not mtype or not session_id:
        return (
            f"sid 写得不对：{target_sid}。"
            "会话清单里的 sid 长这样：aiocqhttp:GroupMessage:群号。"
        )

    is_paging = before is not None and int(before) > 0
    default_lim = (
        READ_CHANNEL_PAGE_SIZE if is_paging else READ_CHANNEL_DEFAULT_LIMIT
    )
    raw_lim = default_lim if limit is None else int(limit)
    eff_limit = max(1, min(READ_CHANNEL_MAX_LIMIT, raw_lim))
    # 未显式传 limit 时放宽到「保留窗口 + 一整段」，保证未成段的未读一条不漏
    unread_cap = (
        eff_limit
        if limit is not None
        else max(eff_limit, max(0, attention_window) + max(1, segment_msg_count))
    )
    self_ids = bot_self_ids or set()
    cmap = contacts_map or {}

    st = await store.get_session_state(target_sid)
    group_name = str(st.get("group_name", "") or "")
    prev_read_id = int(st.get("read_up_to_id", 0) or 0)
    plain_label = store.resolve_session_name(target_sid, group_name, "", "", cmap)

    def _mentioned_ids(rows: Sequence[ChannelMessageRow]) -> set[int]:
        return {
            r.id
            for r in rows
            if _is_row_mentioning_me(r, self_ids, mention_keywords)
        }

    def _render_rows(
        rows: Sequence[ChannelMessageRow],
        mentioned_set: set[int],
        captions: dict[int, list[str | None]],
    ) -> list[str]:
        return render_channel_rows(
            rows,
            store=store,
            mentioned_set=mentioned_set,
            captions=captions,
            tz_name=tz_name,
            contacts_map=cmap,
            image_brief_max_chars=image_brief_max_chars,
            msg_max_chars=msg_max_chars,
        )

    async def _captions(
        rows: Sequence[ChannelMessageRow],
    ) -> dict[int, list[str | None]]:
        if captioner is None:
            return {}
        return await captioner.caption_rows(
            rows, wait_sec=caption_wait_sec, max_new=caption_max_new
        )

    async def _render_block(
        shown: Sequence[ChannelMessageRow], mentioned_set: set[int]
    ) -> str:
        """渲染一块原文：抬头、消息行、以及没列出来的条数。"""
        anchor = next((m for m in reversed(shown) if not m.is_bot), shown[-1])
        where_label = store.resolve_session_name(
            target_sid, group_name, anchor.sender_id, anchor.sender_name, cmap
        )
        mine_count = sum(1 for m in shown if m.is_bot)
        at_count = sum(1 for m in shown if (not m.is_bot) and m.id in mentioned_set)
        comp_parts = []
        if at_count > 0:
            comp_parts.append(f"{at_count} 条叫了你")
        if mine_count > 0:
            comp_parts.append(f"{mine_count} 条是你自己发的")
        comp_str = ("，" + "，".join(comp_parts)) if comp_parts else ""

        leftover = store.count_unread(target_sid, prev_read_id) - sum(
            1 for m in shown if not m.is_bot and m.id > prev_read_id
        )
        out = [
            f"{target_sid} · {where_label}｜最近 {len(shown)} 条{comp_str}",
            *_render_rows(shown, mentioned_set, await _captions(shown)),
        ]
        if leftover > 0:
            out.append("")
            out.append(
                f"更早 {leftover} 条没有总结，{_paging_hint(shown[0].id, True)}。"
            )
        return "\n".join(out)

    # 展开某段，不动已读
    if summary_enabled and segment is not None:
        seg_id = int(segment)
        seg = store.get_topic_segment(seg_id)
        if seg is None:
            return f"没有段 {seg_id}。检查编号，或者不带 segment 再读一次看目录。"
        if str(seg["umo"]) != target_sid:
            return f"段 {seg_id} 属于 {seg['umo']}，不是这个会话。"
        start_id = int(seg["start_msg_id"])
        end_id = int(seg["end_msg_id"])
        rows = store.fetch_messages(
            target_sid,
            after_id=start_id - 1,
            before_id=end_id + 1,
            limit=max(1, end_id - start_id + 1),
            asc=True,
        )
        if not rows:
            return f"段 {seg_id} 的原文不在了。"
        return "\n".join(
            [
                f"{target_sid} · {plain_label}",
                f"段 {seg_id}，{len(rows)} 条",
                "",
                *_render_rows(rows, _mentioned_ids(rows), await _captions(rows)),
            ]
        )

    # 翻页，不改已读
    if is_paging:
        cursor_before = int(before)  # type: ignore[arg-type]
        batch = store.fetch_messages(
            target_sid, before_id=cursor_before, limit=eff_limit
        )
        if not batch:
            return (
                f"{target_sid} 在 before={cursor_before} 之前没有记录了。"
                "可能已经翻到头，也可能这个 before 是别的会话的。"
                "想看未读，就不带 before 再读一次。"
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
        footer = f"—— {_paging_hint(batch[0].id, len(batch) >= eff_limit)}。"
        captions = await _captions(batch)
        return "\n".join([header, *_render_rows(batch, mentioned, captions), footer])

    # 默认模式：读消息池
    pool_size = max(eff_limit * 4, 100)
    pool = store.fetch_messages(target_sid, limit=pool_size)
    if not pool:
        return (
            f"{target_sid} 里没有消息。可能这个会话还没有记录，也可能 sid 写错了，跟会话清单对一下。"
        )

    prev_read_id = int(st.get("read_up_to_id", 0) or 0)
    newest_id = pool[-1].id
    has_unseen_own = store.has_unseen_bot_speech(target_sid)

    # 未读外部消息只按非 bot 消息与 prev_read_id 比较，避开已读过的 is_bot=1 消息卡在游标之上
    ext_unread = (
        [m for m in pool if not m.is_bot]
        if prev_read_id == 0
        else [m for m in pool if (not m.is_bot) and m.id > prev_read_id]
    )
    # 池尾是未核对的自身发言时当作触发点，否则会截断后面新到的外部消息
    own_trigger_idx: int | None = (
        len(pool) - 1 if (has_unseen_own and pool[-1].is_bot) else None
    )

    called_this_turn = called_sid_this_turn == target_sid

    # 默认模式：没有新消息
    if not ext_unread and own_trigger_idx is None and not called_this_turn:
        times = 1
        if repeat_counter is not None:
            times = repeat_counter.get(target_sid, 0) + 1
            repeat_counter[target_sid] = times
        has_own = any(m.is_bot for m in pool)
        mine_note = "，包括你自己发的（行首（我））" if has_own else ""
        way = (
            f"想看更早的（包括没叫你的），就{_paging_hint(newest_id + 1, True)}。"
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

    # 默认模式：段目录，读一次即视为点开
    if repeat_counter is not None:
        repeat_counter[target_sid] = repeat_counter.get(target_sid, 0) + 1

    def _unread_window() -> list[ChannelMessageRow]:
        """全部未读加紧挨着的两条前文；积压超过 unread_cap 时只留最新的。"""
        start = next(
            (i for i, m in enumerate(pool) if not m.is_bot and m.id > prev_read_id),
            None,
        )
        if own_trigger_idx is not None:
            start = own_trigger_idx if start is None else min(start, own_trigger_idx)
        if start is None:
            return pool[-eff_limit:]
        if len(pool) - start > unread_cap:
            return pool[-unread_cap:]
        return pool[max(0, start - READ_CHANNEL_CONTEXT_BRIDGE) :]

    is_group = is_group_umo(target_sid)
    latest_ext_id = max((m.id for m in pool if not m.is_bot), default=newest_id)
    new_read_up_to = max(prev_read_id, latest_ext_id)

    segments: list[dict[str, Any]] = []
    segment_total = 0
    if is_group and summary_enabled:
        segments = store.list_topic_segments(
            target_sid, after_id=prev_read_id, limit=max(1, int(segment_page))
        )
        segment_total = store.count_topic_segments(target_sid, after_id=prev_read_id)

    if segments:
        older_cnt = segment_total - len(segments)
        head = f"{target_sid} · {plain_label}"
        if older_cnt > 0:
            head = f"{head}\n{len(segments)} 段，更早还有 {older_cnt} 段"
        out = [head, ""]
        for seg in segments:
            out.append(
                f"[段 {seg['id']}] "
                f"{format_short_time(seg['start_ts'], tz_name)}-"
                f"{format_short_time(seg['end_ts'], tz_name)} "
                f"#{seg['start_msg_id']}-#{seg['end_msg_id']} {seg['summary']}"
            )
        out.append("")
        out.append("传 segment=<编号> 展开某段的原文。")
        if older_cnt > 0:
            oldest_start = int(segments[-1]["start_msg_id"])
            out.append(
                f"再往前 {older_cnt} 段不在这一屏，{_paging_hint(oldest_start, True)}。"
            )
        latest_segment_end = max(int(seg["end_msg_id"]) for seg in segments)
        tail = [row for row in pool if row.id > latest_segment_end]
        if tail:
            shown = tail[-unread_cap:]
            out.extend(["", f"尚未成段的最近 {len(shown)} 条："])
            out.extend(
                _render_rows(shown, _mentioned_ids(shown), await _captions(shown))
            )
            if len(shown) == unread_cap and store.fetch_messages(
                target_sid, after_id=latest_segment_end, before_id=shown[0].id, limit=1
            ):
                out.append(f"还有更早的未成段消息，{_paging_hint(shown[0].id, True)}。")
        output = "\n".join(out)
    else:
        # 默认模式：无段时未传 limit 只给未读窗口，传了 limit 给最新一屏原文
        shown = _unread_window() if limit is None else pool[-eff_limit:]
        output = await _render_block(shown, _mentioned_ids(shown))

    store.clear_pending_bot_speeches(target_sid)
    await store.update_read_state(target_sid, read_up_to_id=new_read_up_to)
    return output


@pydantic_dataclass
class ReadChannelTool(FunctionTool):
    """read_channel 工具。"""

    name: str = "read_channel"
    description: str = read_channel_description(True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "sid": {
                    "type": "string",
                    "description": "会话的 sid，长得像 aiocqhttp:GroupMessage:群号，从会话清单里抄。",
                },
                "segment": {
                    "type": "integer",
                    "description": "段编号，仅话题概括开启时生效。展开该段全部原文，不动未读。",
                },
                "before": {
                    "type": "integer",
                    "description": (
                        "消息编号。从这条往前翻，不改未读。和 segment 二选一。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "原文条数上限。不传时展示全部未读（积压过多才截断），"
                        f"翻页默认 {READ_CHANNEL_PAGE_SIZE}，最多 {READ_CHANNEL_MAX_LIMIT}。"
                        "目录模式不受它影响。"
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
