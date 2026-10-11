"""插件配置解析与动态读取封装。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    light_provider_id: str = ""
    topic_min_unread: int = 5
    topic_cooldown_sec: int = 180
    heartbeat_enabled: bool = True
    heartbeat_floor_min: int = 5
    heartbeat_ceil_min: int = 60
    heartbeat_target_mean_min: int = 15
    intercept_direct_reply: bool = False


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
        light_provider_id=str(cfg.get("light_provider_id", "") or "").strip(),
        topic_min_unread=_int("topic_min_unread", 5, minimum=1),
        topic_cooldown_sec=_int("topic_cooldown_sec", 180, minimum=10),
        heartbeat_enabled=bool(cfg.get("heartbeat_enabled", True)),
        heartbeat_floor_min=floor_min,
        heartbeat_ceil_min=ceil_min,
        heartbeat_target_mean_min=target_mean,
        intercept_direct_reply=bool(cfg.get("intercept_direct_reply", False)),
    )
