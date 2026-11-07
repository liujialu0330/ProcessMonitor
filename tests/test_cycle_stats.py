"""
采集周期统计 core.cycle_stats：定长直方图与 p95 估算、活动段与计划周期数，以及
MonitorWorker 用假时钟制造延迟、跳过与暂停后的统计与收尾落库。
"""
from data.models import QualityStats

from core.cycle_stats import BUCKET_BOUNDS_MS, CycleStats, Histogram
from tests.test_monitor_worker_clock import (
    METRICS, ScriptedCollector, SteppingClock, make_clocked_worker, stop_after, _run_to_end,
    started,  # noqa: F401  fixture
)


# ---------------------------------------------------------------- 直方图


def test_histogram_has_fixed_buckets_and_memory():
    h = Histogram()
    for i in range(100_000):
        h.add((i % 700) / 1000.0)
    assert len(h.counts) == len(BUCKET_BOUNDS_MS) + 1 == 10
    assert h.total == 100_000 == sum(h.counts)


def test_bucket_upper_bound_is_inclusive_and_negative_is_zero():
    h = Histogram()
    h.add(0.001)      # 恰好 1 ms：0–1 桶
    h.add(-0.005)     # 提前醒来：按 0 计
    h.add(0.0011)     # 1–2 桶
    h.add(0.5)        # 恰好 500 ms：200–500 桶
    h.add(0.5001)     # 溢出桶
    assert h.counts == [2, 1, 0, 0, 0, 0, 0, 0, 1, 1]


def test_p95_takes_bucket_upper_bound():
    h = Histogram()
    assert h.p95_ms() is None
    for _ in range(95):
        h.add(0.0004)
    for _ in range(5):
        h.add(0.3)
    assert h.p95_ms() == 1           # 第 95 个样本仍在 0–1 桶
    h.add(0.3)
    assert h.p95_ms() == 500         # 101 个样本：第 96 个落在 200–500 桶


def test_p95_in_overflow_bucket_uses_observed_max():
    h = Histogram()
    for _ in range(10):
        h.add(2.0001)
    assert h.p95_ms() == 2001


# ---------------------------------------------------------------- 活动段


def test_planned_cycles_by_segments_excludes_pause():
    stats = CycleStats(interval=1.0)
    stats.start_segment(100.0)
    stats.end_segment(119.0)          # 100..119 共 20 个网格点
    stats.end_segment(130.0)          # 重复结束：忽略
    stats.start_segment(149.0)
    assert stats.planned_cycles(now=158.5) == 20 + 10
    stats.end_segment(178.0)
    assert stats.planned_cycles() == 20 + 30


def test_segment_ending_before_first_deadline_plans_nothing():
    stats = CycleStats(interval=2.0)
    assert not stats.has_data
    stats.start_segment(10.0)         # 含 CPU 指标：首个截止时间在未来
    stats.end_segment(9.0)            # 首采之前就停止
    assert stats.planned_cycles() == 0


def test_skip_counts_whole_cycles_and_records_residual_lag():
    stats = CycleStats(interval=60.0)
    assert stats.skip(300.0000001) < 1e-6       # 恰好 5 个周期（含浮点误差）：余量≈0
    assert stats.missed_cycles == 5 and stats.resets == 1
    assert stats.lag_hist.counts[0] == 1
    residual = stats.skip(90.0)       # 1.5 个周期：跳过 1，余量 30 秒
    assert stats.missed_cycles == 6 and stats.resets == 2
    assert abs(residual - 30.0) < 1e-9
    assert stats.lag_hist.counts[-1] == 1


def test_summary_fields():
    stats = CycleStats(interval=1.0)
    stats.start_segment(0.0)
    for _ in range(10):
        stats.lag(0.0005)
        stats.collected(0.004)
        stats.wrote(0.0015)
    stats.end_segment(9.0)
    assert stats.summary() == QualityStats(10, 10, 0, 1, 5, 2)
    assert stats.summary().completion_ratio == 1.0


# ---------------------------------------------------------------- MonitorWorker 驱动


class TimedCollector(ScriptedCollector):
    """每次采集让假时钟前进 cost 秒（模拟采集耗时）"""

    def __init__(self, clock, cost, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock = clock
        self.cost = cost

    def collect_metrics(self, metric_types):
        self.clock.advance(self.cost)
        return super().collect_metrics(metric_types)


def test_fake_clock_lag_skip_pause_statistics(db, started):  # noqa: F811
    """
    周期 1 秒、共采 30 次：第 2–11 次采集各晚 30 ms；第 15 次之后的等待多睡 3 个周期
    （跳过 3 个周期）；第 20 次之后暂停 2 秒再恢复。每次采集耗时 7 ms。

    时间线：采集 1..15 在 0..14（2..11 各 +30 ms），16 在 18，17..20 在 19..22，
    暂停 22→24，21..30 在 24..33。计划 = [0,22] 的 23 个 + [24,33] 的 10 个 = 33 =
    完成 30 + 跳过 3。
    """
    clock = SteppingClock()
    ref = []
    worker = make_clocked_worker(
        db, clock, interval=1.0,
        collector_factory=lambda w: TimedCollector(
            clock, 0.007, METRICS, after_collect=stop_after(ref, 30)))
    ref.append(worker)
    paused = []

    def on_wait(event, timeout):
        calls = worker.collector.calls
        if timeout is None:
            clock.advance(2.0)
            assert worker.resume() is True
        elif 1 <= calls <= 10:
            clock.advance(0.030)
        elif calls == 15:
            clock.advance(3.0)
        elif calls == 20 and not paused:
            paused.append(True)
            assert worker.pause() is True

    clock.on_wait = on_wait
    _run_to_end(worker, started)

    stats = worker.cycle_stats
    assert stats.done_cycles == 30
    assert stats.missed_cycles == 3 and stats.resets == 1
    assert stats.planned_cycles() == 33
    # 调度延迟：10 次 30 ms（20–50 桶），其余 20 次为 0（跳过那次的余量也为 0）
    assert stats.lag_hist.counts[0] == 20 and stats.lag_hist.counts[5] == 10
    assert stats.lag_hist.total == 30
    # 采集耗时 7 ms 落在 5–10 桶；写库在假时钟下耗时为 0
    assert stats.collect_hist.counts[3] == 30
    assert stats.write_hist.counts[0] == 30

    expected = QualityStats(planned_cycles=33, done_cycles=30, missed_cycles=3,
                            lag_p95_ms=50, collect_p95_ms=10, write_p95_ms=1)
    assert worker.task_model.quality == expected
    assert worker.quality_snapshot() == expected
    # 收尾落库：write_p95_ms 没有列，不存
    stored = db.tasks.load_quality(worker.task_id)
    assert stored == QualityStats(33, 30, 3, 50, 10, None)
    assert db.tasks.get(worker.task_id).quality == stored


def test_collect_exception_is_neither_done_nor_missed(db, started):  # noqa: F811
    """采集抛异常的周期不算完成也不算跳过：planned == done + missed + 异常次数"""
    clock = SteppingClock()
    ref = []

    class Flaky(ScriptedCollector):
        def collect_metrics(self, metric_types):
            result = super().collect_metrics(metric_types)
            if self.calls in (3, 7):
                raise RuntimeError("采集故障")
            return result

    worker = make_clocked_worker(
        db, clock, interval=1.0,
        collector_factory=lambda w: Flaky(METRICS, after_collect=stop_after(ref, 12)))
    ref.append(worker)
    _run_to_end(worker, started)

    quality = db.tasks.load_quality(worker.task_id)
    assert quality.planned_cycles == 12
    assert quality.done_cycles == 10
    assert quality.missed_cycles == 0


def test_task_that_never_scheduled_writes_no_quality(db, started):  # noqa: F811
    """启动即失败（进程不存在）：没有调度过，不写质量列"""
    clock = SteppingClock()

    class Gone(ScriptedCollector):
        def is_process_running(self):
            return False

    worker = make_clocked_worker(db, clock, collector_factory=lambda w: Gone(METRICS))
    _run_to_end(worker, started)

    assert worker.quality_snapshot() is None
    assert db.tasks.load_quality(worker.task_id) is None


def test_quality_save_failure_does_not_break_teardown(db, started, monkeypatch):  # noqa: F811
    """写质量失败只记日志：task_stopped 照常发出、状态照常落库"""
    clock = SteppingClock()
    ref = []
    worker = make_clocked_worker(
        db, clock, interval=1.0,
        collector_factory=lambda w: ScriptedCollector(METRICS, after_collect=stop_after(ref, 3)))
    ref.append(worker)

    def boom(*_a, **_k):
        raise RuntimeError("写入失败")

    monkeypatch.setattr(db.tasks, 'save_quality', boom)
    _run_to_end(worker, started)

    assert len(worker.stops) == 1
    assert db.tasks.get(worker.task_id).status == 'stopped'
