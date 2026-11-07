"""
磁盘写满：采集线程保留缓冲、通知暂停、空间回来后先补写保留的数据。

用 tests/storage_faults.simulate_disk_full 让真实的 SQLite 报 SQLITE_FULL（不真写满磁盘）。
每个周期写很多指标行，保证需要新页而触发写满。

覆盖：
- 工作线程：写满时缓冲不丢、只报告一次；恢复后补写，库里的行数与时间戳逐条一致（无缺无重）；
- 约一小时数据的缓冲上限：超出丢最旧的并记日志，报告仍只发一次；
- 管理器 + 存储守卫：任务被暂停并记入"因空间不足暂停"，用户恢复后数据完整。
"""
import contextlib
import logging
import os
from datetime import timedelta

import pytest
from PyQt5.QtCore import Qt

from core import monitor_task
from core.monitor_manager import MonitorManager
from core.monitor_task import MonitorWorker
from core.maintenance_service import MaintenanceService
from core.shutdown import ThreadRegistry
from core.storage_guard import StorageGuard
from data.database import Database
from data.models import DataPoint
from data.repos.point_repo import PointQuery
from tests.helpers import poll_until, process_events_until, stop_and_join
from tests.soak_doubles import T0, SegmentCollector, StepClock
from tests.storage_faults import simulate_disk_full

# 每个周期 N 个指标：几千行才会逼出新页
METRICS = [f"m{i:04d}" for i in range(1500)]
FIRST_METRIC = METRICS[0]


def _worker(db, clock, collector, interval=1.0):
    from core.exit_forensics import NullExitCodeProbe
    worker = MonitorWorker(pid=os.getpid(), process_name='full.exe', metric_types=list(METRICS),
                           interval=interval, db=db, clock=clock,
                           collector_factory=lambda pid, spec: collector,
                           exit_probe_factory=lambda pid: NullExitCodeProbe())
    return worker


def _stamps(db, task_id, metric=FIRST_METRIC):
    return [p.timestamp for p in db.points.fetch(PointQuery(task_id, metric))]


def test_full_disk_keeps_buffer_pauses_and_flushes_in_order_after_recovery(db, qapp):
    clock = StepClock()
    stack = contextlib.ExitStack()
    full_signals = []
    flow = {'resumed_at': None}

    def on_collect(n):
        if n == 4:
            stack.enter_context(simulate_disk_full(db))     # 第 4 个周期写库时磁盘"满了"
        if n == 9:
            worker.stop()

    collector = SegmentCollector(METRICS, [float(i) for i in range(1, 40)], on_collect=on_collect)
    worker = _worker(db, clock, collector)

    def on_full(_task_id):
        full_signals.append(clock.now())
        # 管理器 / 守卫的职责：收到报告后暂停任务
        assert worker.pause() is True

    worker.storage_full.connect(on_full, Qt.DirectConnection)

    def on_wait(event, timeout):
        if timeout is None:
            # 任务已暂停：模拟用户腾出空间、30 秒后点"恢复"
            stack.close()
            clock.advance(30)
            flow['resumed_at'] = clock.now()
            assert worker.resume() is True

    clock.on_wait = on_wait
    worker.start()
    assert worker.wait(30000)
    stack.close()

    assert len(full_signals) == 1                    # 只报告一次
    assert flow['resumed_at'] == T0 + timedelta(seconds=3 + 30)
    stamps = _stamps(db, worker.task_id)
    # 周期 1..4 在暂停前采到（第 4 个因写满被保留，恢复后第一个补写）；恢复后立即采第 5 个
    expected = ([T0 + timedelta(seconds=k) for k in range(4)]
                + [T0 + timedelta(seconds=33 + k) for k in range(5)])
    assert stamps == expected
    # 每个指标的行数一致：没有半截周期，没有重复
    for metric in (METRICS[0], METRICS[700], METRICS[-1]):
        values = [p.value for p in db.points.fetch(PointQuery(worker.task_id, metric))]
        assert values == [float(i) for i in range(1, 10)]


def test_held_point_count_counts_unwritten_points_only_while_the_disk_is_full(db, qapp):
    """退出确认据此提示"有 N 条因磁盘已满未写入的数据，退出后将丢失"：只在写满锁存期间
    有值，补写完成后为 0；没写满过的任务始终为 0"""
    clock = StepClock()
    stack = contextlib.ExitStack()
    seen = {'while_full': None, 'before': None}

    def on_collect(n):
        if n == 3:
            seen['before'] = worker.held_point_count()
            stack.enter_context(simulate_disk_full(db))
        if n == 6:
            worker.stop()

    collector = SegmentCollector(METRICS, [float(i) for i in range(1, 40)], on_collect=on_collect)
    worker = _worker(db, clock, collector)

    def on_full(_task_id):
        seen['while_full'] = worker.held_point_count()
        worker.pause()

    worker.storage_full.connect(on_full, Qt.DirectConnection)

    def on_wait(event, timeout):
        if timeout is None:
            stack.close()
            worker.resume()

    clock.on_wait = on_wait
    worker.start()
    assert worker.wait(30000)
    stack.close()

    assert seen['before'] == 0
    assert seen['while_full'] == len(METRICS)       # 写满的那个周期的数据留在内存
    assert worker.held_point_count() == 0           # 补写完成后没有未写入的数据


def test_manager_sums_held_points_over_all_tasks(qapp, tmp_path):
    from types import SimpleNamespace
    manager = MonitorManager(db=Database(str(tmp_path / 'held.db')))
    try:
        assert manager.held_point_count() == 0
        manager._tasks['a'] = SimpleNamespace(held_point_count=lambda: 30)
        manager._tasks['b'] = SimpleNamespace(held_point_count=lambda: 12)
        manager._tasks['c'] = SimpleNamespace()         # 没有该接口的（替身）按 0 算
        assert manager.held_point_count() == 42
    finally:
        manager._tasks.clear()
        stop_and_join(manager)
        manager.db.close()


def test_buffer_is_capped_to_about_one_hour_and_drops_oldest(db, qapp, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    worker = _worker(db, StepClock(), SegmentCollector(METRICS, []), interval=1.0)
    full = []
    worker.storage_full.connect(lambda t: full.append(t), Qt.DirectConnection)
    worker._points_per_cycle = 2
    # 1 秒周期、每周期 2 行：一小时 = 7200 行
    worker._data_buffer = [DataPoint(worker.task_id, T0 + timedelta(seconds=i), float(i), 'a')
                           for i in range(10000)]
    worker._hold_for_storage_full()
    assert len(worker._data_buffer) == 7200
    assert worker._data_buffer[0].value == 10000 - 7200      # 丢的是最旧的
    assert worker._data_buffer[-1].value == 9999.0
    assert any("丢弃最旧" in r.getMessage() for r in caplog.records)
    worker._hold_for_storage_full()
    assert len(full) == 1                                    # 仍锁存：只报告一次

    # 绝对上限：再大的周期点数也不超过 FULL_HOLD_MAX_POINTS
    monkeypatch.setattr(monitor_task, 'FULL_HOLD_MAX_POINTS', 50)
    worker._points_per_cycle = 1000
    worker._data_buffer = [DataPoint(worker.task_id, T0, float(i), 'a') for i in range(200)]
    worker._storage_full_latched = False
    worker._hold_for_storage_full()
    assert len(worker._data_buffer) == 50
    assert worker._data_buffer[-1].value == 199.0


def test_resume_still_full_reports_again_and_keeps_data(db, qapp):
    """恢复时空间仍没回来：补写失败，数据继续保留，再次报告（守卫会再暂停）"""
    clock = StepClock()
    stack = contextlib.ExitStack()
    reports = []
    step = {'resumes': 0}

    def on_collect(n):
        if n == 3:
            stack.enter_context(simulate_disk_full(db))
        if n == 5:
            worker.stop()

    collector = SegmentCollector(METRICS, [float(i) for i in range(1, 40)], on_collect=on_collect)
    worker = _worker(db, clock, collector)

    def on_full(_t):
        reports.append(clock.now())
        assert worker.pause()

    worker.storage_full.connect(on_full, Qt.DirectConnection)

    def on_wait(event, timeout):
        if timeout is None:
            step['resumes'] += 1
            if step['resumes'] == 2:
                stack.close()                       # 第二次恢复时空间才回来
            clock.advance(10)
            worker.resume()

    clock.on_wait = on_wait
    worker.start()
    assert worker.wait(30000)
    stack.close()

    assert len(reports) == 2
    values = [p.value for p in db.points.fetch(PointQuery(worker.task_id, FIRST_METRIC))]
    assert values == [float(i) for i in range(1, 6)]     # 一个周期都没丢


# ========== 管理器 + 存储守卫 ==========

def test_manager_and_guard_pause_task_and_data_complete_after_resume(tmp_path, qapp):
    db = Database(str(tmp_path / "full_guard.db"))
    manager = MonitorManager(db=db)
    registry = ThreadRegistry()
    guard = StorageGuard(manager, MaintenanceService(db, manager), registry, db.db_path,
                         read_only=False, quick_check_delay_s=None)
    stack = contextlib.ExitStack()
    detected = []
    manager.storage_full_detected.connect(lambda t: detected.append(t))
    state = {}

    def on_collect(n):
        if n == 4:
            stack.enter_context(simulate_disk_full(db))

    collector = SegmentCollector(METRICS, [float(i) for i in range(1, 100000)],
                                 on_collect=on_collect)
    manager.set_worker_options(collector_factory=lambda pid, spec: collector)
    try:
        tid = manager.create_task(os.getpid(), 'full.exe', list(METRICS), interval=0.05)
        state['tid'] = tid
        manager.start_task(tid)
        # 工作线程的报告经队列信号到界面线程，要处理事件才会暂停
        assert process_events_until(lambda: manager.is_task_paused(tid), timeout=15)
        assert process_events_until(lambda: tid in guard.paused_task_ids(), timeout=5)
        assert detected == [tid]
        calls_when_paused = collector.calls
        assert poll_until(lambda: collector.calls == calls_when_paused, timeout=1, interval=0.2)

        stack.close()                               # 空间回来了
        assert manager.resume_task(tid)
        assert poll_until(lambda: collector.calls >= calls_when_paused + 3, timeout=15)
        stop_and_join(manager)
        n = collector.calls
        values = [p.value for p in db.points.fetch(PointQuery(tid, FIRST_METRIC))]
        # 采到的每个周期都落库了：数值 1..n 连续、无缺无重
        assert values == [float(i) for i in range(1, n + 1)]
    finally:
        stack.close()
        guard.stop()
        stop_and_join(manager)
        registry.cancel_all()
        registry.shutdown_all()
        db.close()
