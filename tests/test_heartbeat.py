"""测试 5~60 分钟连续概率呼吸心跳模型（solve_heartbeat_alpha、边界条件与抽签复位）。"""

from __future__ import annotations

from irmia.heartbeat import (
    DEFAULT_HEARTBEAT_CEIL_MS,
    DEFAULT_HEARTBEAT_FLOOR_MS,
    DEFAULT_HEARTBEAT_TICK_MS,
    HeartbeatEngine,
    heartbeat_mean_ms,
    heartbeat_probability,
    solve_heartbeat_alpha,
)


def test_solve_heartbeat_alpha_matches_irmia_calibration() -> None:
    """验证默认 5~60m、tick=1m、目标均值 15m 下反解的 alpha 与 I.R.M.I.A 标定值 (1.103180) 一致。"""
    target_ms = 15 * 60 * 1000
    alpha = solve_heartbeat_alpha(
        target_ms,
        DEFAULT_HEARTBEAT_FLOOR_MS,
        DEFAULT_HEARTBEAT_CEIL_MS,
        DEFAULT_HEARTBEAT_TICK_MS,
    )
    assert abs(alpha - 1.103180) < 1e-4
    mean_min = (
        heartbeat_mean_ms(
            DEFAULT_HEARTBEAT_FLOOR_MS,
            DEFAULT_HEARTBEAT_CEIL_MS,
            DEFAULT_HEARTBEAT_TICK_MS,
            alpha,
        )
        / 60_000.0
    )
    assert abs(mean_min - 15.0) < 1e-3


def test_heartbeat_floor_and_ceil_bounds() -> None:
    """验证安静 < floor 时概率恒为 0，安静 >= ceil 时概率恒为 1，中间单调递增。"""
    floor_ms = 5 * 60 * 1000
    ceil_ms = 60 * 60 * 1000
    assert heartbeat_probability(0, floor_ms, ceil_ms, 1.1) == 0.0
    assert heartbeat_probability(4.99 * 60 * 1000, floor_ms, ceil_ms, 1.1) == 0.0
    assert heartbeat_probability(5.0 * 60 * 1000, floor_ms, ceil_ms, 1.1) == 0.0

    p10 = heartbeat_probability(10 * 60 * 1000, floor_ms, ceil_ms, 1.1)
    p30 = heartbeat_probability(30 * 60 * 1000, floor_ms, ceil_ms, 1.1)
    assert 0.0 < p10 < p30 < 1.0
    assert heartbeat_probability(60 * 60 * 1000, floor_ms, ceil_ms, 1.1) == 1.0
    assert heartbeat_probability(75 * 60 * 1000, floor_ms, ceil_ms, 1.1) == 1.0


def test_heartbeat_engine_sampling_and_reset() -> None:
    """验证抽签触发、周期复位与外部活动归零。"""
    now = 1_000_000.0
    rolls = [0.99, 0.01]

    def fake_rng() -> float:
        return rolls.pop(0) if rolls else 0.5

    engine = HeartbeatEngine(
        floor_ms=5 * 60 * 1000,
        ceil_ms=60 * 60 * 1000,
        tick_ms=60 * 1000,
        target_mean_ms=15 * 60 * 1000,
        rng=fake_rng,
        now_ms_fn=lambda: now,
    )
    # 3 分钟（不足 floor）绝不抽签触发
    now += 3 * 60 * 1000
    assert engine.sample_tick(now) is None

    # 15 分钟，第一签 roll=0.99 未命中
    now += 12 * 60 * 1000
    assert engine.sample_tick(now) is None

    # 16 分钟，第二签 roll=0.01 命中
    now += 1 * 60 * 1000
    firing = engine.sample_tick(now)
    assert firing is not None
    assert firing.idle_ticks == 1
    assert firing.roll == 0.01
    assert firing.probability > 0.01

    # 命中后单轮周期起点复位，下一分钟不会因为超过 floor 而连续触发
    now += 1 * 60 * 1000
    assert engine.sample_tick(now) is None

    # 外部消息到达，安静时长与 idle_ticks 全部归零
    engine.note_activity(now)
    assert engine.idle_ticks == 0
    assert engine.quiet_ms(now) == 0.0
