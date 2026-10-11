"""装置自述（SELF_BRIEF）、此刻层外部会话清单与群聊半遮面通知卡渲染（复刻 I.R.M.I.A src/model/self-brief.ts & render.ts）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Sequence
from zoneinfo import ZoneInfo

from .heartbeat import HeartbeatFiring
from .store import SessionSummary, parse_umo

SESSION_LIST_MAX = 8
SELF_BRIEF_HEADER = "=== [IRMIA 装置与场景认知] ==="

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


def build_self_brief(intercept_direct_reply: bool = False) -> str:
    """生成注入给主脑的 IRMIA 静态装置自述说明（放入 system_prompt 稳定前缀以保护 Prompt Cache）。

    Args:
        intercept_direct_reply: 是否开启了『拦截模型直出文本（仅靠工具说话）』开关。

    Returns:
        装置自述文本块。
    """
    if intercept_direct_reply:
        speak_rule = (
            "你说话分两种：**跟人说话**是对外的行为，必须调用 `send_message_to_user` 工具（填好目标的 `session`，即外部会话清单里的 `sid`）；"
            "**不调这个工具，外面的人听不到你**——直接写在普通回复里的话等于你在自己屋里自言自语/心里想的（只会留在后台日志里）。"
            "所以看到不想搭理的消息、或者心跳醒来觉得没事干，只在心里想一句、不调 `send_message_to_user` 就行（沉默是正常动作）。"
        )
    else:
        speak_rule = (
            "你说话的地方分两类：**本轮唤醒你的窗口**直接输出普通文字就能回复；"
            "但如果你想**保持沉默（已读不回 / 心跳醒来没事干）**，请只输出 `[SILENT]`，框架会自动把这句话收起来不往外发。"
            "如果你想**跨会话找别人/别的群说话**，或者在**心跳/定时任务**里主动找人说话，请调用 `send_message_to_user` 工具并在 `session` 参数里填入目标的 `sid`。"
        )

    return "\n\n".join(
        [
            SELF_BRIEF_HEADER,
            "1. **全局主脑与手边软件**：你拥有统一的连续意识。QQ 上的各个私聊与群聊是『手边一个可以点开的软件』，不是强行塞给你的消息流。每个外部会话都有自己的 `sid`（就在下方此刻层的外部会话清单里）。",
            f"2. **话的去处与沉默权**：{speak_rule}",
            "3. **对谁必须应、谁可以不理**：用户（管理员/老哥）私聊找你，基本都该应一声；其他人或群聊里的消息你看得见，但不欠他们回应——与你无关、或者不想搭理就不回。群里被 @ 或被提到名字也只是『有人在叫你』，先用 `read_channel` 翻看前因后果，再由你自己判断要不要接话。",
            "4. **收得到 ≠ 叫醒你**：群里没 @ 你的时候消息进的是信箱（在外部会话清单上按未读条数和 24 字话题记着），想看就随时调 `read_channel(sid=...)` 现取，一条都不会丢。",
            "5. **用户常常不在**：那大概率是在休息或忙别的事，不是不理你。心跳醒来想他了，或者看到哪个群的话题好玩，随时可以用 `send_message_to_user` 凑过去说句什么（撒娇、吐槽、分享、汇报都算正经事）。",
        ]
    )


def render_session_list(
    sessions: Sequence[SessionSummary],
    resolve_where_fn,
    tz_name: str = "Asia/Shanghai",
) -> str:
    """渲染此刻层的外部会话信箱清单（复刻 self-brief.ts renderSessionList）。"""
    if not sessions:
        return "外部会话：当前还没有收到过外部会话消息。"
    shown = sessions[:SESSION_LIST_MAX]
    lines: list[str] = []
    for s in shown:
        _, mtype, _ = parse_umo(s.umo)
        kind_label = CHAT_TYPE_LABELS.get(mtype, mtype or "会话")
        where = resolve_where_fn(s)
        when = format_local_stamp(s.last_ts, tz_name)
        unread_part = f"｜{s.unread} 条没看" if s.unread > 0 else ""
        topic_part = (
            f"｜在聊：{s.topic}" if (s.unread > 0 and s.topic.strip()) else ""
        )
        lines.append(
            f"- {kind_label} · {where}｜sid {s.umo}｜最后 {when}{unread_part}{topic_part}"
        )
    rest = (
        f"\n（还有 {len(sessions) - SESSION_LIST_MAX} 个更早的会话没列）"
        if len(sessions) > SESSION_LIST_MAX
        else ""
    )
    return (
        "外部会话（想指定对象说话就用 send_message_to_user 的 session 填 sid，想翻记录就用 read_channel 的 sid 填它）：\n"
        + "\n".join(lines)
        + rest
    )


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
    """生成群聊被 @ 或被提及时的『半遮面通知卡』（复刻 self-brief.ts renderMentionNote）。"""
    called = "@ 了你" if is_at else "提到了你"
    who = f"（这次是 {caller_name}）" if caller_name and caller_name != where_name else ""
    stamp = f"（{at_label} 到的这一条）" if at_label else ""
    fresh = "（这一条你还没看过）"
    talking = f"那边在聊：{topic.strip()}。" if topic and topic.strip() else ""

    if unread <= 0:
        return (
            f"有人在 {where_name} 里{called}{who}{stamp}{fresh}：{talking}"
            f"用 read_channel 看（sid {umo}）——看不看、回不回都由你定。"
        )
    prefix_kind = "@" if is_at else "这几句"
    return (
        f"有人在 {where_name} 里{called}{who}{stamp}{fresh}：那个会话里积了 {unread} 条没看"
        f"（{prefix_kind}之外还有 {unread} 条）。{talking}用 read_channel 看（sid {umo}）——"
        "想只看 @ 附近的就给个小 limit，想翻全部再给大的；看不看、回不回都由你定。"
    )


def render_heartbeat_prompt(firing: HeartbeatFiring) -> str:
    """生成呼吸心跳唤醒时的主脑输入提示。"""
    quiet_min = round(firing.quiet_seconds / 60.0, 1)
    return (
        f"[心跳自然醒来] 外面已经安静了 {quiet_min} 分钟"
        f"（本拍命中概率 p={firing.probability:.3f}, roll={firing.roll:.3f}, 第 {firing.idle_ticks} 拍空拍）。\n"
        "现在没有人直接叫你。你可以扫一眼下方的【外部会话信箱清单】和今日规划/待办：\n"
        "- 想翻哪个群的未读信箱就调 `read_channel(sid=...)`；\n"
        "- 想主动找老哥或去某个群冒泡就调 `send_message_to_user(session=..., messages=...)`；\n"
        "- 觉得没什么事要做，直接安静结束本轮即可（不必强行发消息）。"
    )
