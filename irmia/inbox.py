"""通道消息分流器与群聊防大喘气攒批门（复刻 I.R.M.I.A src/channel/inbox.ts & src/runtime/group-batch.ts）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Sequence


def mentions_keyword(text: str, keywords: Sequence[str]) -> bool:
    """判断消息正文中是否包含她的称呼关键词（大小写不敏感，忽略空词）。

    Args:
        text: 消息纯文本正文。
        keywords: 配置的称呼关键词列表。

    Returns:
        命中任一非空关键词返回 True，否则返回 False。
    """
    if not text or not keywords:
        return False
    haystack = text.lower()
    for word in keywords:
        needle = str(word or "").strip().lower()
        if needle and needle in haystack:
            return True
    return False


@dataclass
class WakeCheckResult:
    """单条通道消息的分流结论。"""

    should_wake: bool
    is_private: bool
    is_platform_at: bool
    is_keyword_mention: bool


def evaluate_channel_message(
    *,
    is_private: bool,
    is_platform_at: bool,
    text: str,
    mention_keywords: Sequence[str],
) -> WakeCheckResult:
    """判断一条通道消息该唤醒主脑还是静默归入信箱（纯函数，复刻 shouldWakeForChannelMessage）。

    规则：
    1. 私聊（is_private=True）-> 无条件唤醒（对方就是在直接跟她说话）。
    2. 群聊 -> 只有平台 @/引用了她（is_platform_at=True）或正文命中称呼关键词才唤醒；
       其余一律返回 should_wake=False（进信箱，只计未读）。

    Args:
        is_private: 是否为私聊消息。
        is_platform_at: 是否在平台层 @ 了机器人或引用回复了机器人。
        text: 消息纯文本。
        mention_keywords: 她的称呼关键词列表。

    Returns:
        WakeCheckResult 分流判定结果。
    """
    if is_private:
        return WakeCheckResult(
            should_wake=True,
            is_private=True,
            is_platform_at=False,
            is_keyword_mention=False,
        )
    by_kw = (not is_platform_at) and mentions_keyword(text, mention_keywords)
    should_wake = bool(is_platform_at or by_kw)
    return WakeCheckResult(
        should_wake=should_wake,
        is_private=False,
        is_platform_at=bool(is_platform_at),
        is_keyword_mention=by_kw,
    )


class GroupBatcher:
    """群聊点名防大喘气攒批器。

    当群友连续发送多条 @ 或提及消息时，等待 window_sec 秒：
    若窗口期内同一群又有新的唤醒消息到达，则前一条让位（返回 False 被拦截入信箱），
    由窗口期内的最后一条消息统一唤醒主脑。
    """

    def __init__(self) -> None:
        self._seq_by_umo: dict[str, int] = {}

    async def wait_my_turn(self, umo: str, window_sec: float) -> bool:
        """等待攒批窗口结束，判断当前消息是否仍是该群本批次的最后一条代表。

        Args:
            umo: 群会话 UMO。
            window_sec: 攒批窗口秒数；<= 0 时不等待直接放行。

        Returns:
            True 表示窗口结束且没有更新的同群唤醒消息抢占，由当前消息唤醒主脑；
            False 表示等待期间同群又有新唤醒消息到达，当前消息应静默让位。
        """
        if window_sec <= 0:
            return True
        my_seq = self._seq_by_umo.get(umo, 0) + 1
        self._seq_by_umo[umo] = my_seq
        await asyncio.sleep(window_sec)
        return self._seq_by_umo.get(umo, 0) == my_seq
