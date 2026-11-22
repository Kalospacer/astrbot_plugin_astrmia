"""生成群点名与活跃跟进通知：触发事实落盘，前情只在本轮可见。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import IrmiaConfig
from .read_channel_tool import collect_mention_digest
from .render import (
    TRIGGER_LABELS,
    render_mention_notice,
    render_mention_permanent_prompt,
    render_mention_temp_context,
)
from .store import IrmiaStore
from .vision_describe import Captioner


@dataclass
class MentionNoticeParts:
    """群唤醒通知的动静两部分。

    permanent_prompt 写进 req.prompt 随历史永久保存，只含群名、sid、时刻与触发原话；
    temp_context 放进 extra_user_content_parts 并标记为临时块，保存历史时被剥离。
    """

    permanent_prompt: str
    temp_context: str = ""


def _resolve_trigger_kind(meta: dict[str, Any]) -> str:
    """从通知元信息取规范唤醒来源（at / keyword / followup）。"""
    kind = str(meta.get("trigger_kind") or "")
    if kind in TRIGGER_LABELS:
        return kind
    if meta.get("is_active_followup"):
        return "followup"
    return "at" if meta.get("is_at") else "keyword"


async def build_mention_notice(
    *,
    store: IrmiaStore,
    sid: str,
    meta: dict[str, Any],
    cfg: IrmiaConfig,
    bot_self_ids: set[str],
    tz_name: str,
    captioner: Captioner | None,
) -> MentionNoticeParts:
    """现算未读数；开启内嵌时读群并标记已读，图片仅取缓存、不等待识别。"""
    trigger_kind = _resolve_trigger_kind(meta)
    if not cfg.mention_inline_enabled:
        state = await store.get_session_state(sid)
        unread = store.count_unread(sid, int(state.get("read_up_to_id", 0) or 0))
        return MentionNoticeParts(
            render_mention_notice(
                umo=sid,
                where_name=meta["where_name"],
                caller_name=meta["caller_name"],
                is_at=(trigger_kind == "at"),
                at_label=meta["at_label"],
                unread=unread,
                topic=meta["topic"],
                is_active_followup=(trigger_kind == "followup"),
            )
        )

    digest = await collect_mention_digest(
        store=store,
        sid=sid,
        trigger_pmids=meta.get("trigger_pmids") or (),
        max_rows=cfg.mention_inline_limit,
        contacts_map=cfg.contacts_map,
        mention_keywords=cfg.mention_keywords,
        bot_self_ids=bot_self_ids,
        tz_name=tz_name,
        captioner=captioner if cfg.read_channel_image_enabled else None,
        caption_max_new=cfg.read_channel_image_max,
        image_brief_max_chars=cfg.image_brief_max_chars,
        msg_max_chars=cfg.msg_max_chars,
    )
    permanent = render_mention_permanent_prompt(
        umo=sid,
        where_name=meta["where_name"],
        caller_name=meta["caller_name"],
        trigger_label=TRIGGER_LABELS[trigger_kind],
        at_label=meta["at_label"],
        trigger_lines=digest.trigger_lines if digest else [],
    )
    if digest is None:
        return MentionNoticeParts(permanent)
    return MentionNoticeParts(
        permanent_prompt=permanent,
        temp_context=render_mention_temp_context(
            umo=sid,
            unread=digest.unread,
            topic=meta["topic"],
            context_lines=digest.context_lines,
            oldest_shown_id=digest.oldest_shown_id,
            leftover=digest.leftover,
            has_more=digest.has_more,
        ),
    )
