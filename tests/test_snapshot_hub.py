"""
SnapshotHub：缓存命中、并发只取一次、相位对齐、stats；MonitorWorker / MonitorManager
对 hub 的使用（截止时间对齐到网格、同周期任务共用快照）。
"""
import os
import statistics
import threading
import time
from datetime import timedelta

import pytest
from PyQt5.QtCore import Qt

from core.monitor_manager import MonitorManager
from core.monitor_task import MonitorWorker
from core.snapshot_hub import SnapshotHub, default_hub
from core.system_snapshot import SnapshotError
from data.repos.point_repo import PointQuery
from tests.doubles import FakeSnapshotSource, ManualMonotonic, fake_system_snapshot
from tests.test_monitor_worker_clock import (
    JOIN_TIMEOUT_MS, T0, ScriptedCollector, SteppingClock, stop_after,
)


def _hub(clock=None, **source_kwargs):
    source = FakeSnapshotSource(
        snapshots=[lambda: fake_system_snapshot()], **source_kwargs)
    return SnapshotHub(source, clock=clock or ManualMonotonic()), source


# ========== 缓存 ==========

def test_cache_hit_within_max_age_and_refetch_after():
    clock = ManualMonotonic()
    hub, source = _hub(clock)
    first = hub.get(0.5)
    assert hub.get(0.5) is first
    clock.advance(0.5)
    assert hub.get(0.5) is first              # 年龄恰为上限仍命中
    clock.advance(0.01)
    second = hub.get(0.5)
    assert second is not first
    assert source.calls == 2
    stats = hub.stats()
    assert (stats.samples, stats.hits, stats.failures) == (2, 2, 0)


def test_max_age_zero_always_refetches_unless_same_instant():
    clock = ManualMonotonic()
    hub, source = _hub(clock)
    hub.get(0.0)
    hub.get(0.0)                              # 同一时刻：年龄 0 ≤ 0
    clock.advance(0.001)
    hub.get(0.0)
    assert source.calls == 2


def test_refresh_always_takes_new_snapshot():
    hub, source = _hub()
    a = hub.get(10)
    b = hub.refresh()
    assert a is not b and hub.get(10) is b
    assert source.calls == 2


def test_failure_propagates_counts_and_keeps_cache():
    clock = ManualMonotonic()
    good = fake_system_snapshot()
    source = FakeSnapshotSource(snapshots=[good, SnapshotError("偶发")])
    hub = SnapshotHub(source, clock=clock)
    assert hub.get(0.5) is good
    clock.advance(1)
    with pytest.raises(SnapshotError):
        hub.get(0.5)
    assert hub.stats().failures == 1
    assert hub.get(5) is good                 # 失败不清掉旧缓存


def test_stats_average_ms():
    hub, _ = _hub(clock=ManualMonotonic(), delay=0.02)
    hub.refresh()
    hub.refresh()
    stats = hub.stats()
    assert stats.samples == 2
    assert stats.avg_ms >= 15 and stats.last_ms >= 15


def test_availability_passthrough():
    hub = SnapshotHub(FakeSnapshotSource(available=False, reason="测试原因"))
    assert hub.available() is False
    assert hub.unavailable_reason == "测试原因"


# ========== 并发 ==========

def test_concurrent_get_takes_single_snapshot():
    """两个线程同时 get：只取一次快照（锁内双检），拿到同一个对象"""
    # 真实时钟；取样耗时 0.2 秒，期间另一线程在锁上等
    source = FakeSnapshotSource(snapshots=[lambda: fake_system_snapshot()], delay=0.2)
    hub = SnapshotHub(source)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(hub.get(0.5))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 2 and results[0] is results[1]
    assert source.calls == 1
    stats = hub.stats()
    assert (stats.samples, stats.hits) == (1, 1)


# ========== 相位对齐 ==========

def test_align_and_next_slot_on_grid():
    hub, _ = _hub(clock=ManualMonotonic(1000.0))
    assert hub.phase_origin == 1000.0
    assert hub.align(1000.3, 1.0) == 1001.0
    assert hub.align(1001.0, 1.0) == 1001.0
    assert hub.align(999.2, 1.0) == 1000.0
    assert hub.next_slot(1004.0, 2.0) == 1006.0      # 在网格上：恰为 +周期
    assert hub.next_slot(1004.3, 2.0) == 1006.0      # 不在网格上：回到网格，间隔 ≥ 半周期
    assert hub.next_slot(1005.2, 2.0) == 1008.0
    # 反复推进不累积浮点误差
    t = 1000.0
    for _ in range(10000):
        t = hub.next_slot(t, 0.1)
    assert t == pytest.approx(2000.0, abs=1e-9)


def test_max_age_for_interval():
    assert SnapshotHub.max_age_for(1) == 0.5
    assert SnapshotHub.max_age_for(60) == 0.5
    assert SnapshotHub.max_age_for(0.2) == pytest.approx(0.1)


def test_default_hub_is_shared():
    assert default_hub() is default_hub()


# ========== MonitorWorker 的相位对齐（假时钟） ==========

def _clocked_worker_with_hub(db, clock, hub, metrics, interval, n):
    ref = []
    worker = MonitorWorker(pid=os.getpid(), process_name='hub.exe', metric_types=metrics,
                           interval=interval, db=db, clock=clock, hub=hub)
    worker.collector = ScriptedCollector(metrics, after_collect=stop_after(ref, n))
    ref.append(worker)
    return worker


def _run(worker):
    worker.start()
    assert worker.wait(JOIN_TIMEOUT_MS)


def _stamps(db, worker, metric):
    return [p.timestamp for p in db.points.fetch(PointQuery(worker.task_id, metric))]


def test_worker_with_hub_aligns_samples_to_grid(db, qapp):
    """hub 原点 1000.0，线程在 1000.3 启动：首采对齐到 1001.0，之后间隔恒为周期且都在网格上"""
    clock = SteppingClock(mono0=1000.0)
    hub = SnapshotHub(FakeSnapshotSource(), clock=clock)
    clock.advance(0.3)
    worker = _clocked_worker_with_hub(db, clock, hub, ['num_handles'], 2.0, 20)
    _run(worker)

    stamps = _stamps(db, worker, 'num_handles')
    assert len(stamps) == 20
    # 墙钟 = T0 + (单调 - 1000)：首采在单调 1001.0，即 T0 + 1 秒（最多晚 1 秒）
    assert stamps[0] == T0 + timedelta(seconds=1)
    # 第二次回到周期网格（原点 + 2k）：1002.0，之后每 2 秒
    assert stamps[1] == T0 + timedelta(seconds=2)
    assert {b - a for a, b in zip(stamps[1:], stamps[2:])} == {timedelta(seconds=2)}


def test_worker_with_hub_cpu_first_sample_at_least_one_interval(db, qapp):
    clock = SteppingClock(mono0=1000.0)
    hub = SnapshotHub(FakeSnapshotSource(), clock=clock)
    clock.advance(0.3)
    worker = _clocked_worker_with_hub(db, clock, hub, ['cpu_percent'], 5.0, 4)
    _run(worker)
    stamps = _stamps(db, worker, 'cpu_percent')
    # 预热在 1000.3，首采不早于 1005.3，对齐到整秒网格 1006.0；之后在 5 秒网格 1010、1015…
    assert stamps[0] == T0 + timedelta(seconds=6)
    assert stamps[1:] == [T0 + timedelta(seconds=s) for s in (10, 15, 20)]


def test_two_workers_started_apart_land_on_same_grid(db, qapp):
    """不同时刻启动的同周期任务：采样时刻落在同一网格上（假时钟下完全相同）"""
    clock = SteppingClock(mono0=1000.0)
    hub = SnapshotHub(FakeSnapshotSource(), clock=clock)
    clock.advance(0.7)
    a = _clocked_worker_with_hub(db, clock, hub, ['num_threads'], 3.0, 6)
    _run(a)
    clock.advance(1.45)    # 第二个任务在非整秒时刻启动
    b = _clocked_worker_with_hub(db, clock, hub, ['num_threads'], 3.0, 6)
    _run(b)
    grid = {T0 + timedelta(seconds=3 * k) for k in range(100)}
    assert set(_stamps(db, a, 'num_threads')[1:]) <= grid
    assert set(_stamps(db, b, 'num_threads')[1:]) <= grid


def test_resume_samples_immediately_then_returns_to_grid(db, qapp):
    clock = SteppingClock(mono0=1000.0)
    hub = SnapshotHub(FakeSnapshotSource(), clock=clock)
    worker = _clocked_worker_with_hub(db, clock, hub, ['num_handles'], 2.0, 12)

    def on_wait(event, timeout):
        if timeout is None:
            clock.advance(7.3)                 # 暂停 7.3 秒后恢复（不在网格上）
            assert worker.resume() is True
        elif worker.collector.calls == 5 and not worker.is_paused():
            assert worker.pause() is True
    clock.on_wait = on_wait
    _run(worker)

    stamps = _stamps(db, worker, 'num_handles')
    offsets = [(s - T0).total_seconds() for s in stamps]
    resume_at = next(o for o in offsets if o % 2)       # 恢复后立即采的那一次
    after = [o for o in offsets if o > resume_at]
    assert after and all(o % 2 == 0 for o in after)     # 之后回到 2 秒网格
    assert after[0] - resume_at >= 1.0                  # 不会紧挨着再采


# ========== 真实时钟：同周期任务共用快照 ==========

class _TimedCollector(ScriptedCollector):
    """记录每次采集的真实单调时刻，并经 hub 取一次快照（模拟快照路径的取样）"""

    def __init__(self, metrics, hub, max_age):
        super().__init__(metrics)
        self.hub = hub
        self.max_age = max_age
        self.times = []

    def collect_metrics(self, metric_types):
        self.times.append(time.monotonic())
        self.hub.get(self.max_age)
        return super().collect_metrics(metric_types)


def test_same_interval_workers_sample_together_and_share_snapshots(db, qapp):
    interval = 0.25
    source = FakeSnapshotSource(snapshots=[lambda: fake_system_snapshot()])
    hub = SnapshotHub(source)
    workers = []
    for _ in range(2):
        w = MonitorWorker(pid=os.getpid(), process_name='pair.exe', metric_types=['num_handles'],
                          interval=interval, db=db, hub=hub)
        w.collector = _TimedCollector(['num_handles'], hub, SnapshotHub.max_age_for(interval))
        workers.append(w)
    try:
        workers[0].start()
        time.sleep(0.11)                     # 错开启动
        workers[1].start()
        time.sleep(2.2)
    finally:
        for w in workers:
            w.stop()
        for w in workers:
            assert w.wait(5000)

    a, b = (w.collector.times[1:] for w in workers)   # 去掉对齐前可能不同步的首采
    assert len(a) >= 5 and len(b) >= 5
    diffs = [min(abs(t - u) for u in b) for t in a]
    assert statistics.median(diffs) <= 0.05, diffs
    # 一次快照服务两个任务：取样次数明显少于两者采集次数之和
    total = len(workers[0].collector.times) + len(workers[1].collector.times)
    assert source.calls <= total * 0.75, (source.calls, total)


# ========== MonitorManager 注入 ==========

def test_manager_injects_shared_hub_into_workers(db, qapp):
    hub = SnapshotHub(FakeSnapshotSource())
    manager = MonitorManager(db=db, snapshot_hub=hub)
    task_id = manager.create_task(os.getpid(), 'x.exe', ['num_handles'], interval=5)
    worker = manager.get_task(task_id)
    assert manager.snapshot_hub is hub
    assert worker._hub is hub and worker.collector._hub is hub
    assert worker.collector._max_age_s == 0.5
    manager.remove_task(task_id)


def test_manager_defaults_to_process_wide_hub(db, qapp):
    manager = MonitorManager(db=db)
    assert manager.snapshot_hub is default_hub()
