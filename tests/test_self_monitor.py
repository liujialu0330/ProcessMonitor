"""
自身监控 core.self_monitor.SelfMonitor：环形缓冲定长、私有字节趋势判定（上涨 / 平稳 /
数据不足）、只看最近 N 小时、采样失败不抛出；默认采样器能读到本进程。
"""
import math
from datetime import datetime, timedelta

from core.analysis.trend import Verdict
from core.self_monitor import DEFAULT_CAPACITY, SelfMonitor, default_sampler
from utils.metrics import MetricType

T0 = datetime(2026, 11, 7, 8, 0, 0)


class MinuteClock:
    """每次 now() 之后前进 60 秒的假时钟（tick 先读 now 再读 monotonic）"""

    def __init__(self):
        self.mono = 0.0

    def monotonic(self) -> float:
        return self.mono

    def now(self) -> datetime:
        value = T0 + timedelta(seconds=self.mono)
        return value

    def step(self) -> None:
        self.mono += 60.0


def _drive(values, capacity=DEFAULT_CAPACITY):
    """按 values（私有字节 KB 序列）逐分钟 tick，返回 SelfMonitor"""
    clock = MinuteClock()
    series = iter(values)

    def sampler():
        return {MetricType.MEMORY_PRIVATE: next(series), MetricType.NUM_HANDLES: 400.0,
                MetricType.NUM_THREADS: 20.0, MetricType.NUM_GDI_OBJECTS: 50.0,
                MetricType.NUM_USER_OBJECTS: 30.0}

    monitor = SelfMonitor(clock=clock, sampler=sampler, capacity=capacity)
    for _ in values:
        monitor.tick()
        clock.step()
    return monitor


def test_default_capacity_is_24_hours_of_minutes():
    assert DEFAULT_CAPACITY == 1440
    assert SelfMonitor(sampler=lambda: {}).capacity == 1440


def test_ring_buffer_keeps_only_latest_samples():
    monitor = _drive([float(i) for i in range(8)], capacity=5)
    samples = monitor.snapshot()
    assert len(samples) == 5
    assert [s.private_kb for s in samples] == [3.0, 4.0, 5.0, 6.0, 7.0]
    assert monitor.latest().private_kb == 7.0
    assert samples[0].timestamp == T0 + timedelta(minutes=3)
    assert samples[-1].handles == 400.0 and samples[-1].gdi == 50.0


def test_rising_private_bytes_judged_rising():
    # 2 小时，每分钟 +50 KB，叠加 ±5 KB 的规则抖动
    monitor = _drive([100_000 + 50 * i + 5 * math.sin(i) for i in range(120)])
    assert monitor.verdict().verdict is Verdict.RISING


def test_flat_private_bytes_judged_stable():
    constant = _drive([100_000.0] * 120)
    assert constant.verdict().verdict is Verdict.STABLE
    noisy = _drive([100_000 + 200 * math.sin(i * 1.7) for i in range(120)])
    assert noisy.verdict().verdict is Verdict.STABLE


def test_too_few_samples_is_insufficient():
    assert _drive([100_000 + i for i in range(10)]).verdict().verdict is Verdict.INSUFFICIENT
    assert SelfMonitor(sampler=lambda: {}).verdict().verdict is Verdict.INSUFFICIENT


def test_verdict_only_looks_at_recent_window():
    """前 3 小时上涨、最近 2 小时持平：看最近 1 小时为平稳，看全部为上涨"""
    values = [100_000 + 100 * i for i in range(180)] + [118_000.0] * 120
    monitor = _drive(values)
    assert monitor.verdict(hours=1).verdict is Verdict.STABLE
    assert monitor.verdict(hours=24).verdict is Verdict.RISING


def test_sampler_failure_returns_none_and_counts():
    def broken():
        raise OSError("拒绝访问")

    monitor = SelfMonitor(clock=MinuteClock(), sampler=broken)
    assert monitor.tick() is None
    assert monitor.tick() is None
    assert monitor.failures == 2
    assert monitor.snapshot() == []


def test_missing_metric_is_none():
    monitor = SelfMonitor(clock=MinuteClock(),
                          sampler=lambda: {MetricType.MEMORY_PRIVATE: 1.0})
    sample = monitor.tick()
    assert sample.private_kb == 1.0 and sample.handles is None and sample.gdi is None


def test_default_sampler_reads_own_process():
    values = default_sampler()()
    assert values[MetricType.MEMORY_PRIVATE] > 0
    assert values[MetricType.NUM_THREADS] >= 1
