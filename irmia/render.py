"""装置自述、此刻层会话清单与群点名通知卡的渲染。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Sequence
from zoneinfo import ZoneInfo

from .store import SessionSummary, is_group_umo, parse_umo

SELF_BRIEF_HEADER = "=== [IRMIA] 你现在的处境 ==="

CHAT_TYPE_LABELS: dict[str, str] = {
    "FriendMessage": "单聊",
    "GroupMessage": "群聊",
    "c2c": "单聊",
    "group": "群聊",
    "group-at": "群聊",
}

_CN_TZ_NAMES = {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "Asia/Taipei", "Asia/Hong_Kong"}
_CST_FALLBACK = timezone(timedelta(hours=8), "CST")


def resolve_tz(tz_name: str = "Asia/Shanghai") -> tuple[tzinfo, str]:
    """解析时区；缺 tzdata 时东八区回退 UTC+08:00。

    Args:
        tz_name: IANA 时区名称。

    Returns:
        (tzinfo 实例, 实际生效的时区显示名) 二元组。
    """
    name = str(tz_name or "").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name), name
    except Exception:
        if name in _CN_TZ_NAMES:
            return _CST_FALLBACK, name
        return timezone.utc, "UTC"


def format_local_stamp(ts: float, tz_name: str = "Asia/Shanghai") -> str:
    """将 UTC 秒级时间戳格式化为本机时区的 MM-DD HH:MM。"""
    if ts <= 0:
        return "未知时刻"
    tz, _ = resolve_tz(tz_name)
    return datetime.fromtimestamp(ts, tz=tz).strftime("%m-%d %H:%M")


def format_short_time(
    ts: float, tz_name: str = "Asia/Shanghai", always_hm_only: bool = False
) -> str:
    """格式化为 HH:MM；跨天时带 MM-DD。"""
    if ts <= 0:
        return "--:--"
    tz, _ = resolve_tz(tz_name)
    dt = datetime.fromtimestamp(ts, tz=tz)
    if always_hm_only or dt.date() == datetime.now(tz=tz).date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def format_ago(seconds: float) -> str:
    """把距今秒数写成 N 分钟前 / N 小时前 / N 天前。"""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def format_clock_line(now_ts: float, tz_name: str = "Asia/Shanghai") -> str:
    """生成此刻层顶部的『时刻：』行。"""
    tz, effective_label = resolve_tz(tz_name)
    dt_local = datetime.fromtimestamp(now_ts, tz=tz)
    dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    offset_raw = dt_local.strftime("%z")
    offset_fmt = (
        f"UTC{offset_raw[:3]}:{offset_raw[3:]}" if len(offset_raw) == 5 else "UTC"
    )
    return (
        f"时刻：{dt_local.strftime('%Y-%m-%d %H:%M:%S')} "
        f"（{effective_label} · {offset_fmt}）｜UTC {dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


def build_self_brief(
    intercept_direct_reply: bool = False,
    topic_max_chars: int = 24,
    mention_inline: bool = True,
    segment_msg_count: int = 25,
    topic_summary_enabled: bool = True,
) -> str:
    """拼接注入主脑的静态装置自述，放在 system_prompt 稳定前缀里。

    Args:
        intercept_direct_reply: 已发到当前窗口时是否收起末尾回复。
        topic_max_chars: 清单里话题的字数上限。
        mention_inline: 点名通知是否已内嵌消息与前后文。
        segment_msg_count: 群消息切成一段需要多少条。
        topic_summary_enabled: 是否展示话题概括与段目录说明。

    Returns:
        装置自述文本。
    """
    reply_rules = [
        "- 直接输出的文本回复给唤醒你的会话。",
        "- 不回复时仅输出 [SILENT]，该输出不会被发送。",
        "- 向其他会话发送消息使用 send_message_to_user，session 填目标 sid。",
        "- 自主唤醒轮次没有等待回复的对象，直接输出的文本不会被发送，需要发言时使用 send_message_to_user。",
    ]
    if intercept_direct_reply:
        reply_rules.append(
            "- 本轮已用 send_message_to_user 发送到唤醒你的会话时，末尾直接输出的文本不再发送。"
        )

    group_rules = (
        [
            f"- 会话清单显示分段数量，点名通知显示未读条数。每 {segment_msg_count} 条消息为一段，附一句摘要。",
            f"- 未唤醒你的群消息仅记为未读，不触发唤醒。清单中显示分段数与 {topic_max_chars} 字以内的话题摘要。",
            "- 使用 read_channel(sid=...) 查看。",
        ]
        if topic_summary_enabled
        else [
            "- 未唤醒你的群消息仅记为未读，不触发唤醒。",
            "- 使用 read_channel(sid=...) 查看最近原文，传 before=<消息编号> 向前翻页。",
        ]
    )

    return "\n".join(
        [
            SELF_BRIEF_HEADER,
            "所有私聊与群聊共享同一段记忆。每个会话有一个 sid，见下方会话清单。",
            "",
            "回复规则：",
            *reply_rules,
            "",
            "回复义务：",
            "- 管理员私聊原则上均需回复。",
            "- 其他会话消息不承担必须回复的义务，与己无关或无意参与时可以不回复。",
            "- 群聊被 @ 或命中关键词时，通知内已附带相关消息及前后文，据此判断是否回应；信息不足时用 read_channel 查看更多。"
            if mention_inline
            else "- 群聊被 @ 或命中关键词时，通知仅提示有人唤醒，先用 read_channel 查看上下文再决定是否回应。",
            "",
            "群消息：",
            *group_rules,
        ]
    )


def render_session_list(
    sessions: Sequence[SessionSummary],
    resolve_where_fn,
    tz_name: str = "Asia/Shanghai",
    max_items: int = 8,
    now_ts: float | None = None,
    show_topic: bool = True,
) -> str:
    """渲染会话清单，按开关显示话题和段数，并保留最近说话时间。"""
    if not sessions:
        return "外部会话：还没收到过任何外部消息。"
    now = now_ts if now_ts is not None else datetime.now(tz=timezone.utc).timestamp()
    shown = sessions[:max_items]
    lines: list[str] = []
    for s in shown:
        _, mtype, _ = parse_umo(s.umo)
        kind_label = CHAT_TYPE_LABELS.get(mtype, mtype or "会话")
        where = resolve_where_fn(s)
        when = format_local_stamp(s.last_ts, tz_name)
        segment_part = (
            f"｜今天 {s.today_segments} 段 / 历史 {s.history_segments} 段"
            if show_topic and is_group_umo(s.umo)
            else ""
        )
        topic_part = f"｜在聊：{s.topic}" if show_topic and s.topic.strip() else ""
        my_last_part = (
            f"｜我上次在这说话：{format_ago(now - s.my_last_ts)}"
            if s.my_last_ts > 0
            else "｜我还没在这说过话"
        )
        lines.append(
            f"- {kind_label} · {where}｜sid {s.umo}｜最后 {when}{segment_part}{topic_part}{my_last_part}"
        )
    rest = (
        f"\n（还有 {len(sessions) - max_items} 个更早的会话没列出来。）"
        if len(sessions) > max_items
        else ""
    )
    return (
        "外部会话（sid 可以填进 send_message_to_user 的 session，或者 read_channel 的 sid）：\n"
        + "\n".join(lines)
        + rest
    )


def _mention_head(
    where_name: str, caller_name: str, is_at: bool, at_label: str
) -> str:
    """点名通知的开头：几点、在哪、谁叫了你。"""
    called = "@了你" if is_at else "提到了你"
    who = f"（是 {caller_name}）" if caller_name and caller_name != where_name else ""
    stamp = f"{at_label}，" if at_label else ""
    return f"{stamp}{where_name} 里有人{called}{who}"


def _talking(topic: str) -> str:
    return f"那边在聊：{topic.strip()}。" if topic and topic.strip() else ""


def render_mention_notice(
    *,
    umo: str,
    where_name: str,
    caller_name: str,
    is_at: bool,
    at_label: str,
    unread: int,
    topic: str,
) -> str:
    """生成群点名的半遮面通知。"""
    head = _mention_head(where_name, caller_name, is_at, at_label)
    talking = _talking(topic)

    if unread <= 0:
        return (
            f"{head}，该消息尚未查看。{talking}"
            f"使用 read_channel(sid={umo}) 查看。是否回复由你决定。"
        )
    return (
        f"{head}，该会话共 {unread} 条未读。{talking}"
        f"使用 read_channel(sid={umo}) 查看，limit 参数控制返回条数。是否回复由你决定。"
    )


def render_mention_notice_with_digest(
    *,
    umo: str,
    where_name: str,
    caller_name: str,
    is_at: bool,
    at_label: str,
    unread: int,
    topic: str,
    digest: str,
) -> str:
    """生成已附读群结果的点名通知，保留消息编号与翻页指路。"""
    head = _mention_head(where_name, caller_name, is_at, at_label)
    return (
        f"{head}，该会话共 {unread} 条未读。{_talking(topic)}\n"
        f"以下为命中的消息及前后文，已读并更新水位，无需再次 read_channel 该会话（sid={umo}）：\n"
        f"{digest}\n"
        "需要更早的记录时按上方提示传 before 参数。是否回复由你决定。"
    )


def render_heartbeat_prompt(template: str, quiet_seconds: float, idle_ticks: int) -> str:
    """只替换心跳占位符，保留模板中的其他大括号。"""
    quiet_min = round(quiet_seconds / 60.0, 1)
    return template.replace("{quiet_min}", str(quiet_min)).replace(
        "{idle_ticks}", str(int(idle_ticks))
    )
