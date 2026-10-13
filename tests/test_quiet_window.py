"""测试心跳夜间静默窗口：跨零点判断与配置解析。"""

from __future__ import annotations

from irmia.config import is_in_quiet_window, load_irmia_config


def test_quiet_window_same_day() -> None:
    """start < end：同一天内的区间，左闭右开。"""
    assert is_in_quiet_window(1, 1, 6) is True
    assert is_in_quiet_window(5, 1, 6) is True
    assert is_in_quiet_window(6, 1, 6) is False
    assert is_in_quiet_window(0, 1, 6) is False
    assert is_in_quiet_window(12, 1, 6) is False


def test_quiet_window_across_midnight() -> None:
    """start > end：跨零点，22 -> 8 表示 22:00 到次日 08:00。"""
    assert is_in_quiet_window(22, 22, 8) is True
    assert is_in_quiet_window(23, 22, 8) is True
    assert is_in_quiet_window(0, 22, 8) is True
    assert is_in_quiet_window(7, 22, 8) is True
    assert is_in_quiet_window(8, 22, 8) is False
    assert is_in_quiet_window(21, 22, 8) is False
    assert is_in_quiet_window(12, 22, 8) is False


def test_quiet_window_empty_when_same() -> None:
    """start == end 视为空窗口，全天不静默。"""
    assert is_in_quiet_window(0, 8, 8) is False
    assert is_in_quiet_window(8, 8, 8) is False
    assert is_in_quiet_window(23, 8, 8) is False


def test_quiet_window_wraps_out_of_range_hours() -> None:
    """非 0~23 的小时值按 24 取模处理，不越界。"""
    assert is_in_quiet_window(23, 22, 32) is True
    assert is_in_quiet_window(8, 22, 32) is False
    assert is_in_quiet_window(23, -2, 8) is True


def test_load_config_quiet_defaults_off() -> None:
    """默认关闭静默窗口，不影响现有行为。"""
    cfg = load_irmia_config({})
    assert cfg.heartbeat_quiet_enabled is False
    assert cfg.heartbeat_quiet_start_hour == 22
    assert cfg.heartbeat_quiet_end_hour == 8


def test_load_config_quiet_parsing() -> None:
    """开启后按配置解析，并对越界小时取模。"""
    cfg = load_irmia_config(
        {
            "heartbeat_quiet_enabled": True,
            "heartbeat_quiet_start_hour": 23,
            "heartbeat_quiet_end_hour": 7,
        }
    )
    assert cfg.heartbeat_quiet_enabled is True
    assert cfg.heartbeat_quiet_start_hour == 23
    assert cfg.heartbeat_quiet_end_hour == 7

    wrapped = load_irmia_config(
        {
            "heartbeat_quiet_enabled": True,
            "heartbeat_quiet_start_hour": 25,
            "heartbeat_quiet_end_hour": 8,
        }
    )
    assert wrapped.heartbeat_quiet_start_hour == 1
