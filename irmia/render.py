"""装置自述（SELF_BRIEF）、此刻层外部会话清单与群聊半遮面通知卡渲染（复刻 I.R.M.I.A src/model/self-brief.ts & render.ts）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Sequence
from zoneinfo import ZoneInfo

from .heartbeat import HeartbeatFiring
from .store import SessionSummary, parse_umo

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
    """统一解析时区名称，当环境缺少 tzdata 时对东八区安全回退至 UTC+08:00，杜绝时区标签与实际偏移不一致。

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
    """将时间戳格式化为本机时区的短时间（当天或 always_hm_only=True 时返回 HH:MM，跨天返回 MM-DD HH:MM）。"""
    if ts <= 0:
        return "--:--"
    tz, _ = resolve_tz(tz_name)
    dt = datetime.fromtimestamp(ts, tz=tz)
    if always_hm_only or dt.date() == datetime.now(tz=tz).date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


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
) -> str:
    """生成注入给主脑的 IRMIA 静态装置自述说明（放入 system_prompt 稳定前缀以保护 Prompt Cache）。

    Args:
        intercept_direct_reply: 是否开启了『工具已发到当前窗口时收起末尾回复』开关。
        topic_max_chars: 信箱清单里话题简报的字数上限。
        mention_inline: 群点名通知是否已内嵌消息与前后文。

    Returns:
        装置自述文本块。
    """
    reply_rules = [
        "- 谁叫醒你，你直接写的文字就回给谁。",
        "- 不想回就只输出 [SILENT]，这句不会发出去。",
        "- 要给别的群或别的人发消息，用 send_message_to_user，session 填对方的 sid。",
        "- 心跳醒来时没有人在等你，直接写的文字谁也收不到。想说话只能用 send_message_to_user。",
    ]
    if intercept_direct_reply:
        reply_rules.append(
            "- 这一轮已经用 send_message_to_user 发到了当前窗口的话，后面直接写的文字就不再发出去，免得同一句说两遍。"
        )

    return "\n".join(
        [
            SELF_BRIEF_HEADER,
            "你只有一个连续的意识。QQ 上的私聊和群聊都接到同一个你，记忆是连着的。每个会话有一个 sid，在下面的会话清单里。",
            "",
            "怎么说话：",
            *reply_rules,
            "",
            "谁需要回：",
            "- 管理员私聊你，基本都要回。",
            "- 其他人和群里的消息你看得到，但不欠谁回复。和你无关或者不想理，可以不回。",
            "- 群里被 @ 或被叫到名字，通知里已经附上了叫你的那几句和前后文，看完直接决定接不接；觉得不够再用 read_channel 往前翻。"
            if mention_inline
            else "- 群里被 @ 或被叫到名字，只说明有人在叫你。先用 read_channel 看前后在聊什么，再决定接不接。",
            "",
            "群消息：",
            f"- 群里没叫你的消息不会叫醒你，只记成未读。会话清单里能看到未读数和一句 {topic_max_chars} 字以内的话题。",
            "- 想看就用 read_channel(sid=...)，消息不会丢。",
            "",
            "管理员不在线很正常，一般是在忙或者在睡。心跳醒来想找人说话，或者看到哪个群有意思，可以用 send_message_to_user 过去说一句。",
        ]
    )


def render_session_list(
    sessions: Sequence[SessionSummary],
    resolve_where_fn,
    tz_name: str = "Asia/Shanghai",
    max_items: int = 8,
) -> str:
    """渲染此刻层的外部会话信箱清单（复刻 self-brief.ts renderSessionList）。"""
    if not sessions:
        return "外部会话：还没收到过任何外部消息。"
    shown = sessions[:max_items]
    lines: list[str] = []
    for s in shown:
        _, mtype, _ = parse_umo(s.umo)
        kind_label = CHAT_TYPE_LABELS.get(mtype, mtype or "会话")
        where = resolve_where_fn(s)
        when = format_local_stamp(s.last_ts, tz_name)
        unread_part = f"｜{s.unread} 条未读" if s.unread > 0 else ""
        topic_part = (
            f"｜在聊：{s.topic}" if (s.unread > 0 and s.topic.strip()) else ""
        )
        lines.append(
            f"- {kind_label} · {where}｜sid {s.umo}｜最后 {when}{unread_part}{topic_part}"
        )
    rest = (
        f"\n（还有 {len(sessions) - max_items} 个更早的会话没列出来）"
        if len(sessions) > max_items
        else ""
    )
    return (
        "外部会话（sid 填进 send_message_to_user 的 session 或 read_channel 的 sid）：\n"
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
    """生成群聊被 @ 或被提及时的点名通知（复刻 self-brief.ts renderMentionNote）。"""
    head = _mention_head(where_name, caller_name, is_at, at_label)
    talking = _talking(topic)

    if unread <= 0:
        return (
            f"{head}，这条你还没看。{talking}"
            f"要看就用 read_channel(sid={umo})。回不回你自己定。"
        )
    return (
        f"{head}，那边一共有 {unread} 条未读。{talking}"
        f"要看就用 read_channel(sid={umo})：只想看叫你的那几句就把 limit 调小，想全看就调大。回不回你自己定。"
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
        f"{head}，那边一共有 {unread} 条未读。{_talking(topic)}\n"
        f"下面就是刚才叫你的消息和前后文，已经帮你读过了（标记已读），不用再 read_channel 这个群（sid={umo}）：\n"
        f"{digest}\n"
        "想往前翻更早的，照上面的提示传 before 用 read_channel。回不回你自己定。"
    )


def render_heartbeat_prompt(firing: HeartbeatFiring) -> str:
    """生成呼吸心跳唤醒时的主脑输入提示。"""
    quiet_min = round(firing.quiet_seconds / 60.0, 1)
    return (
        f"[心跳] 外面已经安静了 {quiet_min} 分钟，没有人在叫你。"
        f"这是连续第 {firing.idle_ticks} 次自己醒来，中间没人找过你。\n"
        "可以看看下面的会话清单和今天的待办：\n"
        "- 想看哪个会话就用 read_channel(sid=...)。\n"
        "- 想找人说话就用 send_message_to_user(session=..., messages=...)。\n"
        "- 没什么要做的就直接结束，不用硬找话说。"
    )
