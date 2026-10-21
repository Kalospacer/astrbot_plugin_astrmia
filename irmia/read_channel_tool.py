"""read_channel 工具：段目录、段展开、游标翻页。"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from .config import IrmiaConfig
from .inbox import mentions_keyword
from .render import format_short_time
from .store import ChannelMessageRow, IrmiaStore, is_group_umo, parse_umo
from .vision_describe import Captioner, captions_for, render_with_captions

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

SELF_SPEAK_LABEL = "（我）"
MENTION_ROW_MARK = "▶ "

READ_CHANNEL_DESCRIPTION = (
    "读某个群或私聊的消息，也包括你自己在那边说过的话（行首写着（我））。"
    "不带参数返回最近的段目录，一段是 {segment_msg_count} 条消息加一句总结；"
    "没攒够一段的群给最新原文。"
    "传 segment=<编号> 展开某段原文，传 before=<消息编号> 往前翻历史。"
    "读一次即清空该会话未读，翻页不改已读。"
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


async def execute_read_channel(
    *,
    store: IrmiaStore,
    sid: str,
    before: int | None = None,
    limit: int | None = None,
    segment: int | None = None,
    segment_page: int = READ_CHANNEL_SEGMENT_PAGE,
    contacts_map: dict[str, str] | None = None,
    mention_keywords: Sequence[str] = (),
    bot_self_ids: set[str] | None = None,
    called_sid_this_turn: str | None = None,
    digest_only: bool = False,
    tz_name: str = "Asia/Shanghai",
    repeat_counter: dict[str, int] | None = None,
    captioner: Captioner | None = None,
    caption_wait_sec: float = 8,
    caption_max_new: int = 6,
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
            raw_text = (r.text or "").replace("\r\n", "\n").strip()
            raw_text = render_with_captions(raw_text, captions_for(r, captions))
            body = raw_text or "[空消息]"
            lines.append(f"{prefix}[{stamp} #{r.id}] {who}: {body}")
        return lines

    async def _captions(
        rows: Sequence[ChannelMessageRow],
    ) -> dict[int, list[str | None]]:
        if captioner is None:
            return {}
        return await captioner.caption_rows(
            rows, wait_sec=caption_wait_sec, max_new=caption_max_new
        )

    def _mention_digest(
        pool: Sequence[ChannelMessageRow],
        own_trigger_idx: int | None,
        max_rows: int,
    ) -> tuple[list[ChannelMessageRow], set[int]]:
        """挑出叫了你的行，每条带上前面几条上下文。"""
        trigger_idx: list[int] = []
        for idx, m in enumerate(pool):
            if idx == own_trigger_idx:
                trigger_idx.append(idx)
            elif m.id > prev_read_id and _is_row_mentioning_me(
                m, self_ids, mention_keywords
            ):
                trigger_idx.append(idx)
        if not trigger_idx:
            shown = pool[-max_rows:]
            return shown, _mentioned_ids(shown)

        picked: set[int] = set()
        for t_idx in trigger_idx:
            start = max(0, t_idx - READ_CHANNEL_MENTION_CONTEXT)
            picked.update(range(start, t_idx + 1))
        shown = [pool[i] for i in sorted(picked)][-max_rows:]
        return shown, _mentioned_ids(shown)

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
    if segment is not None:
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

    # 点名摘要走自己的挑选规则，不进段目录
    if digest_only:
        shown, mentioned_set = _mention_digest(pool, own_trigger_idx, eff_limit)
        store.clear_pending_bot_speeches(target_sid)
        shown_up_to = max((m.id for m in shown if not m.is_bot), default=prev_read_id)
        await store.update_read_state(
            target_sid, read_up_to_id=max(prev_read_id, shown_up_to)
        )
        return await _render_block(shown, mentioned_set)

    is_group = is_group_umo(target_sid)
    latest_ext_id = max((m.id for m in pool if not m.is_bot), default=newest_id)
    new_read_up_to = max(prev_read_id, latest_ext_id)

    segments: list[dict[str, Any]] = []
    segment_total = 0
    if is_group:
        segments = store.list_topic_segments(
            target_sid, after_id=prev_read_id, limit=max(1, int(segment_page))
        )
        segment_total = store.count_topic_segments(target_sid, after_id=prev_read_id)

    store.clear_pending_bot_speeches(target_sid)
    await store.update_read_state(target_sid, read_up_to_id=new_read_up_to)

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
        return "\n".join(out)

    # 默认模式：无段兜底，给最新原文
    shown = pool[-eff_limit:]
    return await _render_block(shown, _mentioned_ids(shown))


@pydantic_dataclass
class ReadChannelTool(FunctionTool):
    """read_channel 工具。"""

    name: str = "read_channel"
    description: str = READ_CHANNEL_DESCRIPTION.format(
        segment_msg_count=IrmiaConfig.segment_msg_count
    )
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
                    "description": "段编号。展开该段全部原文，不动未读。",
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
                        f"原文条数上限，默认 {READ_CHANNEL_DEFAULT_LIMIT}，"
                        f"翻页 {READ_CHANNEL_PAGE_SIZE}，最多 {READ_CHANNEL_MAX_LIMIT}。"
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
