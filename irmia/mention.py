"""群点名卡片在聊天回合取得主脑锁后生成，复用读群的精选与已读语义。"""

from __future__ import annotations

from typing import Any

from .config import IrmiaConfig
from .read_channel_tool import execute_read_channel
from .render import render_mention_notice, render_mention_notice_with_digest
from .store import IrmiaStore
from .vision_describe import Captioner


async def build_mention_notice(
    *,
    store: IrmiaStore,
    sid: str,
    meta: dict[str, Any],
    cfg: IrmiaConfig,
    bot_self_ids: set[str],
    tz_name: str,
    captioner: Captioner | None,
) -> str:
    """现算未读数；开启内嵌时读群并标记已读，图片仅取缓存、不等待识别。"""
    state = await store.get_session_state(sid)
    unread = store.count_unread(sid, int(state.get("read_up_to_id", 0) or 0))
    if not cfg.mention_inline_enabled:
        return render_mention_notice(umo=sid, unread=unread, **meta)

    digest = await execute_read_channel(
        store=store,
        sid=sid,
        before=None,
        limit=cfg.mention_inline_limit,
        contacts_map=cfg.contacts_map,
        mention_keywords=cfg.mention_keywords,
        bot_self_ids=bot_self_ids,
        called_sid_this_turn=sid,
        tz_name=tz_name,
        repeat_counter=None,
        captioner=captioner if cfg.read_channel_image_enabled else None,
        caption_wait_sec=0,
        caption_max_new=cfg.read_channel_image_max,
    )
    return render_mention_notice_with_digest(
        umo=sid, unread=unread, digest=digest, **meta
    )
