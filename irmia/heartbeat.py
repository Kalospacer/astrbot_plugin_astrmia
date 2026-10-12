"""连续概率呼吸心跳模型（复刻 I.R.M.I.A src/wake/heartbeat.ts）。

模型定义：
    q = clamp01((quiet_ms - floor_ms) / (ceil_ms - floor_ms))
    p = q ** alpha
    - quiet_ms < floor_ms: p = 0（绝不触发）
    - quiet_ms >= ceil_ms: p = 1（必然触发）
    - alpha 由目标均值 target_mean_ms 通过固定 200 步二分法解析反解得出。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from .config import normalize_heartbeat_mins

DEFAULT_HEARTBEAT_FLOOR_MS = 5 * 60 * 1000
DEFAULT_HEARTBEAT_CEIL_MS = 60 * 60 * 1000
DEFAULT_HEARTBEAT_TICK_MS = 60 * 1000
HEARTBEAT_SHAPE_ALPHA_FALLBACK = 1.1

ALPHA_SEARCH_MIN = 1e-6
ALPHA_SEARCH_MAX = 256.0
ALPHA_SEARCH_STEPS = 200


def clamp01(value: float) -> float:
    """将浮点数裁剪至 [0.0, 1.0] 闭区间。"""
    return 0.0 if value <= 0.0 else (1.0 if value >= 1.0 else float(value))


def heartbeat_probability(
    quiet_ms: float,
    floor_ms: float = DEFAULT_HEARTBEAT_FLOOR_MS,
    ceil_ms: float = DEFAULT_HEARTBEAT_CEIL_MS,
    alpha: float = HEARTBEAT_SHAPE_ALPHA_FALLBACK,
) -> float:
    """计算给定安静时长下的单次 tick 命中概率 p = q^alpha。

    Args:
        quiet_ms: 当前安静时长（毫秒）。
        floor_ms: 安静下限（毫秒），低于或等于此值概率恒为 0。
        ceil_ms: 安静上限（毫秒），达到此值概率恒为 1。
        alpha: 概率曲线形状指数。

    Returns:
        [0.0, 1.0] 之间的命中概率。
    """
    if quiet_ms <= floor_ms:
        return 0.0
    if ceil_ms <= floor_ms or quiet_ms >= ceil_ms:
        return 1.0
    q = (quiet_ms - floor_ms) / (ceil_ms - floor_ms)
    return float(q ** max(ALPHA_SEARCH_MIN, alpha))


def heartbeat_mean_ms(
    floor_ms: float = DEFAULT_HEARTBEAT_FLOOR_MS,
    ceil_ms: float = DEFAULT_HEARTBEAT_CEIL_MS,
    tick_ms: float = DEFAULT_HEARTBEAT_TICK_MS,
    alpha: float = HEARTBEAT_SHAPE_ALPHA_FALLBACK,
) -> float:
    """用离散 PMF 求和计算单轮安静时长的解析期望值 E[T]（毫秒）。

    抽签只发生在 tick 的整数倍时刻；tick 不整除 ceil 时，剩余概率落在第一个越过 ceil 的抽签时刻。

    Args:
        floor_ms: 安静下限（毫秒）。
        ceil_ms: 安静上限（毫秒）。
        tick_ms: 抽签间隔（毫秒）。
        alpha: 形状指数。

    Returns:
        期望安静时长（毫秒）。
    """
    step = float(tick_ms)
    survival = 1.0
    expected = 0.0
    t = step
    while True:
        p = heartbeat_probability(t, floor_ms, ceil_ms, alpha)
        expected += t * survival * p
        survival *= 1.0 - p
        if p >= 1.0 or survival <= 1e-12:
            break
        t += step
    return expected


def solve_heartbeat_alpha(
    target_mean_ms: float,
    floor_ms: float = DEFAULT_HEARTBEAT_FLOOR_MS,
    ceil_ms: float = DEFAULT_HEARTBEAT_CEIL_MS,
    tick_ms: float = DEFAULT_HEARTBEAT_TICK_MS,
) -> float:
    """按目标平均间隔（毫秒）通过固定 200 步二分法反解形状指数 alpha。

    Args:
        target_mean_ms: 目标平均唤醒间隔（毫秒）。
        floor_ms: 安静下限（毫秒）。
        ceil_ms: 安静上限（毫秒）。
        tick_ms: 抽签步长（毫秒）。

    Returns:
        收敛后的 alpha 值。
    """
    low = ALPHA_SEARCH_MIN
    high = ALPHA_SEARCH_MAX
    for _ in range(ALPHA_SEARCH_STEPS):
        mid = 0.5 * (low + high)
        if heartbeat_mean_ms(floor_ms, ceil_ms, tick_ms, mid) < target_mean_ms:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


@dataclass
class HeartbeatFiring:
    """一次心跳触发的完整可审计事实。"""

    quiet_seconds: float
    idle_ticks: int
    probability: float
    roll: float
    alpha: float


@dataclass
class HeartbeatDistribution:
    """当前心跳参数与解析分布摘要。"""

    floor_ms: float
    ceil_ms: float
    tick_ms: float
    alpha: float
    mean_ms: float


class HeartbeatEngine:
    """管理安静计时、概率抽签与后台呼吸循环。"""

    def __init__(
        self,
        *,
        floor_ms: float = DEFAULT_HEARTBEAT_FLOOR_MS,
        ceil_ms: float = DEFAULT_HEARTBEAT_CEIL_MS,
        tick_ms: float = DEFAULT_HEARTBEAT_TICK_MS,
        target_mean_ms: float | None = 15 * 60 * 1000,
        rng: Callable[[], float] | None = None,
        now_ms_fn: Callable[[], float] | None = None,
    ) -> None:
        self.floor_ms = float(floor_ms)
        self.ceil_ms = float(ceil_ms)
        self.tick_ms = float(tick_ms)
        self._target_mean_ms = (
            float(target_mean_ms) if target_mean_ms is not None else None
        )
        self.alpha = (
            solve_heartbeat_alpha(
                self._target_mean_ms, self.floor_ms, self.ceil_ms, self.tick_ms
            )
            if self._target_mean_ms is not None
            else HEARTBEAT_SHAPE_ALPHA_FALLBACK
        )
        self.mean_ms = heartbeat_mean_ms(
            self.floor_ms, self.ceil_ms, self.tick_ms, self.alpha
        )
        self._rng = rng or random.random
        self._now_ms = now_ms_fn or (lambda: time.time() * 1000.0)
        now = self._now_ms()
        self.last_external_ms: float = now
        self.cycle_start_ms: float = now
        self.idle_ticks: int = 0

    def reconfigure(
        self,
        *,
        floor_min: int,
        ceil_min: int,
        target_mean_min: int,
        tick_sec: int = 60,
    ) -> None:
        """当心跳配置参数实际发生变化时，热更新并重算 alpha。"""
        f_min, c_min, t_min = normalize_heartbeat_mins(
            floor_min, ceil_min, target_mean_min
        )
        floor_ms = float(f_min * 60_000)
        ceil_ms = float(c_min * 60_000)
        tick_ms = float(tick_sec * 1000)
        target_ms = float(t_min * 60_000)
        if (
            floor_ms == self.floor_ms
            and ceil_ms == self.ceil_ms
            and tick_ms == self.tick_ms
            and target_ms == self._target_mean_ms
        ):
            return
        self.floor_ms = floor_ms
        self.ceil_ms = ceil_ms
        self.tick_ms = tick_ms
        self._target_mean_ms = target_ms
        self.alpha = solve_heartbeat_alpha(
            target_ms, self.floor_ms, self.ceil_ms, self.tick_ms
        )
        self.mean_ms = heartbeat_mean_ms(
            self.floor_ms, self.ceil_ms, self.tick_ms, self.alpha
        )

    def note_activity(self, at_ms: float | None = None) -> None:
        """外部活动（私聊或群聊唤醒）到达时将安静计时与空拍计数归零。"""
        now = at_ms if at_ms is not None else self._now_ms()
        self.last_external_ms = now
        self.cycle_start_ms = now
        self.idle_ticks = 0

    def quiet_ms(self, now_ms: float | None = None) -> float:
        """距离上次外部活动的真实安静毫秒数。"""
        now = now_ms if now_ms is not None else self._now_ms()
        return max(0.0, now - self.last_external_ms)

    def cycle_elapsed_ms(self, now_ms: float | None = None) -> float:
        """当前抽签周期已走过的毫秒数。"""
        now = now_ms if now_ms is not None else self._now_ms()
        return max(0.0, now - self.cycle_start_ms)

    def current_probability(self, now_ms: float | None = None) -> float:
        """当前时刻若抽签的命中概率。"""
        return heartbeat_probability(
            self.cycle_elapsed_ms(now_ms),
            self.floor_ms,
            self.ceil_ms,
            self.alpha,
        )

    def distribution(self) -> HeartbeatDistribution:
        """返回当前分布摘要。"""
        return HeartbeatDistribution(
            floor_ms=self.floor_ms,
            ceil_ms=self.ceil_ms,
            tick_ms=self.tick_ms,
            alpha=self.alpha,
            mean_ms=self.mean_ms,
        )

    def sample_tick(self, now_ms: float | None = None) -> HeartbeatFiring | None:
        """执行一次 tick 抽签：命中则推进周期起点并返回 HeartbeatFiring，未命中返回 None。"""
        now = now_ms if now_ms is not None else self._now_ms()
        elapsed = max(0.0, now - self.cycle_start_ms)
        prob = heartbeat_probability(
            elapsed, self.floor_ms, self.ceil_ms, self.alpha
        )
        if prob <= 0.0:
            return None
        roll = float(self._rng())
        if roll < prob:
            self.idle_ticks += 1
            quiet_sec = max(0.0, (now - self.last_external_ms) / 1000.0)
            self.cycle_start_ms = now
            return HeartbeatFiring(
                quiet_seconds=round(quiet_sec, 1),
                idle_ticks=self.idle_ticks,
                probability=round(prob, 6),
                roll=round(roll, 6),
                alpha=round(self.alpha, 6),
            )
        return None
