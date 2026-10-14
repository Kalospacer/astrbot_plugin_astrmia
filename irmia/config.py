"""插件配置解析与动态读取封装。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_HEARTBEAT_PROMPT = """[心跳] 已经 {quiet_min} 分钟没人私聊你或在群里叫你了，这是这段时间你第 {idle_ticks} 次自己醒来。

趁空下来，打理一下自己的一天：
1. 对一遍待办和今天的规划（规划不清楚就用 astrhe_read_plan 看全文，待办的备注和日期用 astrhe_read_todos 看）。答应过别人、或者自己说过要做却还没做的事，挑一件现在就能动手的去做，做了就用 astrhe_todo_update 记一笔进展或完成；已经过时、不用再做的，标成放弃。
2. 想起新的要做的事、聊天里冒出来的约定还没记下，用 astrhe_todo_add 记成待办，挂到用得上它的会话；计划变了就用 astrhe_todo_edit 或 astrhe_plan_edit 改过来。
3. 现在做不了、得晚点办的事，用 astrhe_schedule 给自己定个时间，到点再醒来处理，别只记在心里。
4. 看看会话清单，在意的人或群可以用 read_channel 翻一翻，想说话就用 send_message_to_user。
5. 待办都理过了、眼下也确实没事，就安静结束这一拍，不用为了说话而说话。"""


def _coerce_int(
    value: Any,
    *,
    default: int,
    min_value: int | None = None,
) -> int:
    """安全将配置值转换为整数（排除 bool 子类干扰并执行下界保护）。"""
    if isinstance(value, bool):
        res = default
    else:
        try:
            res = int(value)
        except (TypeError, ValueError):
            res = default
    if min_value is not None and res < min_value:
        return default if default >= min_value else min_value
    return res


@dataclass
class IrmiaConfig:
    """运行期归一化后的插件配置快照。"""

    enabled: bool = True
    master_umo: str = ""
    mention_keywords: list[str] = field(default_factory=list)
    contacts_map: dict[str, str] = field(default_factory=dict)
    group_batch_window_sec: int = 3
    mention_inline_enabled: bool = True
    mention_inline_limit: int = 20
    light_provider_id: str = ""
    topic_min_unread: int = 5
    topic_cooldown_sec: int = 180
    heartbeat_enabled: bool = True
    heartbeat_provider_id: str = ""
    heartbeat_prompt: str = DEFAULT_HEARTBEAT_PROMPT
    heartbeat_floor_min: int = 5
    heartbeat_ceil_min: int = 60
    heartbeat_target_mean_min: int = 15
    heartbeat_quiet_enabled: bool = False
    heartbeat_quiet_start_hour: int = 22
    heartbeat_quiet_end_hour: int = 8
    intercept_direct_reply: bool = False
    topic_image_caption_enabled: bool = True
    image_keep_days: int = 3
    read_channel_image_enabled: bool = True
    read_channel_image_max: int = 6
    read_channel_image_wait_sec: int = 8
    image_brief_max_chars: int = 80
    read_image_max_chars: int = 500
    vision_provider_ids: list[str] = field(default_factory=list)
    vision_timeout_sec: int = 60
    vision_cache_ttl_days: int = 30
    topic_max_chars: int = 24
    topic_context_messages: int = 25
    topic_line_max_chars: int = 120
    light_timeout_sec: int = 10
    wake_summary_max_chars: int = 60
    heartbeat_tick_sec: int = 60
    wake_max_steps: int = 32
    session_list_max: int = 8
    quote_preview_chars: int = 40


def parse_contacts_map(raw_list: Any) -> dict[str, str]:
    """解析形如 ['123456 = 老哥', 'aiocqhttp:GroupMessage:999 = 摸鱼群'] 的备注列表。

    Args:
        raw_list: 原始配置中的列表或字典。

    Returns:
        键为归一化的 UMO / 群号 / QQ 号，值为显示名的字典。
    """
    if isinstance(raw_list, dict):
        return {
            str(k).strip(): str(v).strip()
            for k, v in raw_list.items()
            if str(k).strip() and str(v).strip()
        }
    if not isinstance(raw_list, list):
        return {}
    result: dict[str, str] = {}
    for item in raw_list:
        line = str(item or "").strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key and val:
            result[key] = val
    return result


def normalize_heartbeat_mins(
    floor_min: int, ceil_min: int, target_mean_min: int
) -> tuple[int, int, int]:
    """统一归一化心跳的 floor / ceil / target_mean 分钟数，确保落在合法开区间内。"""
    f_min = max(1, int(floor_min))
    c_min = max(f_min + 2, int(ceil_min))
    t_min = max(f_min + 1, int(target_mean_min))
    if t_min >= c_min:
        t_min = max(f_min + 1, (f_min + c_min) // 2)
    return f_min, c_min, t_min


def normalize_hour(value: Any, default: int = 0) -> int:
    """把小时值规整到 0~23；非法值回默认，越界值按 24 取模。"""
    if isinstance(value, bool):
        return default
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return hour % 24


def is_in_quiet_window(current_hour: int, start_hour: int, end_hour: int) -> bool:
    """判断当前小时是否落在静默窗口内（左闭右开）。

    支持跨零点：start > end 表示「今晚 start 点到次日 end 点」，
    例如 (22, 8) 覆盖 22:00~23:59 与 00:00~07:59。
    start == end 视为空窗口，全天不静默。

    Args:
        current_hour: 当前小时（0~23，超出范围按 24 取模）。
        start_hour: 窗口开始小时。
        end_hour: 窗口结束小时。

    Returns:
        在窗口内返回 True，否则 False。
    """
    cur = int(current_hour) % 24
    start = int(start_hour) % 24
    end = int(end_hour) % 24
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def load_irmia_config(raw: dict[str, Any] | None) -> IrmiaConfig:
    """从插件原始配置字典生成安全的 IrmiaConfig 对象，支持热读取。

    Args:
        raw: AstrBot 传入的插件配置字典。

    Returns:
        校验并填充默认值后的 IrmiaConfig 实例。
    """
    cfg = raw if isinstance(raw, dict) else {}

    def _int(key: str, default: int, minimum: int = 0) -> int:
        return _coerce_int(
            cfg.get(key, default),
            default=default,
            min_value=minimum,
        )

    def _group_int(group: str, key: str, default: int) -> int:
        section = cfg.get(group)
        value = section.get(key, default) if isinstance(section, dict) else default
        return _coerce_int(value, default=default, min_value=1)

    raw_keywords = cfg.get("mention_keywords", [])
    keywords = (
        [str(w).strip() for w in raw_keywords if str(w).strip()]
        if isinstance(raw_keywords, list)
        else []
    )

    floor_min, ceil_min, target_mean = normalize_heartbeat_mins(
        _int("heartbeat_floor_min", 5, minimum=1),
        _int("heartbeat_ceil_min", 60, minimum=3),
        _int("heartbeat_target_mean_min", 15, minimum=2),
    )

    return IrmiaConfig(
        enabled=bool(cfg.get("enabled", True)),
        master_umo=str(cfg.get("master_umo", "") or "").strip(),
        mention_keywords=keywords,
        contacts_map=parse_contacts_map(cfg.get("contacts_map", [])),
        group_batch_window_sec=_int("group_batch_window_sec", 3, minimum=0),
        mention_inline_enabled=bool(cfg.get("mention_inline_enabled", True)),
        mention_inline_limit=_int("mention_inline_limit", 20, minimum=1),
        light_provider_id=str(cfg.get("light_provider_id", "") or "").strip(),
        topic_min_unread=_int("topic_min_unread", 5, minimum=1),
        topic_cooldown_sec=_int("topic_cooldown_sec", 180),
        heartbeat_enabled=bool(cfg.get("heartbeat_enabled", True)),
        heartbeat_provider_id=str(cfg.get("heartbeat_provider_id", "") or "").strip(),
        heartbeat_prompt=(
            str(cfg.get("heartbeat_prompt", "") or "").strip() or DEFAULT_HEARTBEAT_PROMPT
        ),
        heartbeat_floor_min=floor_min,
        heartbeat_ceil_min=ceil_min,
        heartbeat_target_mean_min=target_mean,
        heartbeat_quiet_enabled=bool(cfg.get("heartbeat_quiet_enabled", False)),
        heartbeat_quiet_start_hour=normalize_hour(
            cfg.get("heartbeat_quiet_start_hour", 22), 22
        ),
        heartbeat_quiet_end_hour=normalize_hour(
            cfg.get("heartbeat_quiet_end_hour", 8), 8
        ),
        intercept_direct_reply=bool(cfg.get("intercept_direct_reply", False)),
        topic_image_caption_enabled=bool(
            cfg.get("topic_image_caption_enabled", True)
        ),
        image_keep_days=_int("image_keep_days", 3),
        read_channel_image_enabled=bool(cfg.get("read_channel_image_enabled", True)),
        read_channel_image_max=_int("read_channel_image_max", 6),
        read_channel_image_wait_sec=_int("read_channel_image_wait_sec", 8),
        image_brief_max_chars=_int("image_brief_max_chars", 80, minimum=1),
        read_image_max_chars=_int("read_image_max_chars", 500, minimum=1),
        vision_provider_ids=[
            str(p).strip()
            for p in cfg.get("vision_provider_ids", [])
            if str(p).strip()
        ],
        vision_timeout_sec=_int("vision_timeout_sec", 60, minimum=1),
        vision_cache_ttl_days=_int("vision_cache_ttl_days", 30),
        topic_max_chars=_group_int("topic_options", "max_chars", 24),
        topic_context_messages=_group_int("topic_options", "context_messages", 25),
        topic_line_max_chars=_group_int("topic_options", "line_max_chars", 120),
        light_timeout_sec=_group_int("topic_options", "timeout_sec", 10),
        wake_summary_max_chars=_group_int("wake_options", "summary_max_chars", 60),
        heartbeat_tick_sec=_group_int("wake_options", "tick_sec", 60),
        wake_max_steps=_group_int("wake_options", "max_steps", 32),
        session_list_max=_group_int("display_options", "session_list_max", 8),
        quote_preview_chars=_group_int("display_options", "quote_preview_chars", 40),
    )
