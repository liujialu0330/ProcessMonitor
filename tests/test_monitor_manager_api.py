"""
MonitorManager 新接口与线程释放语义：
stop_task 非阻塞、remove_task 按 isRunning 等待、start_task 拒绝已结束任务、
stop_all_tasks 先全部置标志再逐个 join、维护锁、标记、delete_task_data、运行计数、note
"""
import os
import threading
import time
from datetime import datetime

import pytest
from PyQt5.QtCore import Qt

from core.monitor_manager import MonitorManager
from core.monitor_task import MonitorTask
from data.database import Database
from tests.helpers import poll_until


@pytest.fixture
def manager(db, qapp):
    return MonitorManager(db=db)


class _SlowCollector:
    """可被门闩卡住的假采集器：卡住期间线程仍在运行，用来模拟"停止收尾中"""

    def __init__(self, gate: threading.Event = None):
        self.gate = gate
        self.last_unavailable = {}
        self.entered = threading.Event()

    def is_process_running(self):
        return True

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        return {"memory_rss": 1.0}


def _start_task_with_collector(manager, collector, interval=0.05, **kwargs):
    task_id = manager.create_task(os.getpid(), "t.exe", ["memory_rss"], interval, **kwargs)
    task = manager.get_task(task_id)
    task.collector = collector
    assert manager.start_task(task_id) is True
    return task_id, task


# ========== stop_task 非阻塞 / remove_task / start_task ==========

def test_stop_task_does_not_wait(manager, monkeypatch):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    waited = []
    with monkeypatch.context() as m:
        m.setattr(task, 'wait', lambda *a, **k: waited.append(1) or True)
        assert manager.stop_task(task_id) is True

    assert waited == [], "stop_task 不应 wait"
    assert task.is_running() is False
    assert task.wait(5000) is True   # 收尾由线程自行完成


def test_stop_task_returns_while_thread_still_running(manager):
    """线程卡在采集中：stop_task 仍立即返回（不会像旧实现那样被 wait 卡住）"""
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)

    t0 = time.perf_counter()
    assert manager.stop_task(task_id) is True
    assert time.perf_counter() - t0 < 0.2
    assert task.isRunning() is True      # 仍在收尾（卡在采集里）
    assert manager.stop_task(task_id) is False   # 已发出停止请求，再停无效

    gate.set()
    assert task.wait(5000) is True


def test_remove_task_waits_while_thread_running_even_if_flag_cleared(manager):
    """_running 已为 False 但线程仍在运行（停止收尾中）：remove_task 必须 wait 到线程结束"""
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)
    manager.stop_task(task_id)
    assert task.is_running() is False and task.isRunning() is True

    threading.Timer(0.3, gate.set).start()
    t0 = time.perf_counter()
    assert manager.remove_task(task_id) is True
    elapsed = time.perf_counter() - t0

    assert task.isFinished() is True, "remove_task 返回时线程必须已结束"
    assert elapsed >= 0.2
    assert manager.get_task(task_id) is None


def test_remove_task_unknown_returns_false(manager):
    assert manager.remove_task("nope") is False


def test_start_task_rejects_finished_task(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    manager.stop_task(task_id)
    assert task.wait(5000) is True
    assert task.isFinished() is True

    assert manager.start_task(task_id) is False


def test_start_task_rejects_stopping_task(manager):
    """停止收尾中（flag 已清、线程未结束）再 start 会冲掉停止标志，必须拒绝"""
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)
    manager.stop_task(task_id)

    assert manager.start_task(task_id) is False
    gate.set()
    assert task.wait(5000) is True
    assert task.is_running() is False


def test_start_task_rejects_running_and_unknown(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    assert manager.start_task(task_id) is False
    assert manager.start_task("nope") is False
    manager.stop_all_tasks()


# ========== stop_all_tasks ==========

class _FakeTask:
    def __init__(self, name, log, running=True):
        self.name = name
        self.log = log
        self._alive = running

    def isRunning(self):
        return self._alive

    def stop(self):
        self.log.append(("stop", self.name))

    def wait(self, *args):
        self.log.append(("wait", self.name, args))
        self._alive = False
        return True


def test_stop_all_tasks_flags_all_before_joining(manager):
    log = []
    manager._tasks = {n: _FakeTask(n, log) for n in ("a", "b", "c")}
    manager._tasks["idle"] = _FakeTask("idle", log, running=False)

    manager.stop_all_tasks()

    assert [e[:2] for e in log] == [
        ("stop", "a"), ("stop", "b"), ("stop", "c"),
        ("wait", "a"), ("wait", "b"), ("wait", "c"),
    ]
    # 无超时 join：wait 不带参数
    assert all(e[2] == () for e in log if e[0] == "wait")


def test_stop_all_tasks_real_tasks_all_finished(manager):
    ids = [_start_task_with_collector(manager, _SlowCollector())[0] for _ in range(3)]
    tasks = [manager.get_task(i) for i in ids]

    manager.stop_all_tasks()

    assert all(t.isFinished() for t in tasks)
    assert manager.running_task_count() == 0


# ========== 运行计数 / 维护锁 ==========

def test_running_task_count_and_has_running_tasks(manager):
    assert manager.running_task_count() == 0 and manager.has_running_tasks() is False
    pending = manager.create_task(os.getpid(), "p.exe", ["memory_rss"], 1.0)
    assert manager.running_task_count() == 0   # 只创建未启动不算运行

    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert manager.running_task_count() == 1 and manager.has_running_tasks() is True

    assert collector.entered.wait(5)
    manager.pause_task(task_id)
    assert manager.running_task_count() == 1   # 暂停中仍算运行

    manager.stop_task(task_id)
    assert manager.running_task_count() == 1   # 停止收尾中线程仍在
    gate.set()
    task.wait(5000)
    assert manager.running_task_count() == 0
    assert pending is not None


def test_maintenance_lock_blocks_create_and_emits_error(manager):
    errors = []
    manager.error_occurred.connect(lambda tid, msg: errors.append((tid, msg)))

    assert manager.is_maintenance_running() is False
    assert manager.begin_maintenance() is True
    assert manager.is_maintenance_running() is True

    assert manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0) is None
    assert errors == [('', '正在清理数据库，请稍后再试')]
    assert manager.get_task_count() == 0

    manager.end_maintenance()
    assert manager.is_maintenance_running() is False
    assert manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0) is not None


def test_maintenance_blocks_start_task(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.begin_maintenance() is True
    assert manager.start_task(task_id) is False
    manager.end_maintenance()
    task = manager.get_task(task_id)
    task.collector = _SlowCollector()
    assert manager.start_task(task_id) is True
    manager.stop_all_tasks()


def test_begin_maintenance_refused_when_tasks_running(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())

    assert manager.begin_maintenance() is False
    assert manager.is_maintenance_running() is False

    manager.stop_all_tasks()
    assert manager.begin_maintenance() is True
    manager.end_maintenance()


# ========== 标记 / 删除 / note ==========

def test_add_marker_returns_timestamp_and_persists(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    before = datetime.now()

    ts = manager.add_marker(task_id, "  点击开始扫描  ")

    assert isinstance(ts, datetime) and ts >= before
    markers = manager.db.get_markers(task_id)
    assert [(m.label, m.timestamp) for m in markers] == [("点击开始扫描", ts)]
    manager.stop_all_tasks()


def test_add_marker_uses_given_click_time(manager):
    """界面传入点击时刻时按它落库，而不是调用（确认）时的当前时间"""
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    clicked = datetime(2026, 11, 6, 10, 0, 0)

    ts = manager.add_marker(task_id, "开始第 3 轮", clicked)

    assert ts == clicked
    assert [(m.label, m.timestamp) for m in manager.db.get_markers(task_id)] == [
        ("开始第 3 轮", clicked)]


def test_create_task_passes_create_time_to_task(manager):
    with_ct = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0,
                                  create_time=1234.5)
    without_ct = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.get_task(with_ct).expected_create_time == 1234.5
    assert manager.get_task(without_ct).expected_create_time is None


def test_add_marker_failures_return_none(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.add_marker("unknown-task", "x") is None
    assert manager.add_marker(task_id, "   ") is None
    assert manager.add_marker(task_id, "") is None
    assert manager.db.get_markers(task_id) == []


def test_delete_task_data_refused_while_running(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    assert poll_until(lambda: manager.db.get_task(task_id) is not None)

    assert manager.delete_task_data(task_id) is False
    assert manager.db.get_task(task_id) is not None

    manager.stop_all_tasks()
    assert manager.delete_task_data(task_id) is True
    assert manager.db.get_task(task_id) is None
    assert manager.db.get_data_point_count(task_id) == 0


def test_delete_task_data_for_historical_task_not_in_memory(manager):
    from data.models import MonitorTask as TaskModel
    old = TaskModel("old-1", 1, "old.exe", ["memory_rss"], 1.0, datetime(2026, 1, 1), None, "stopped")
    manager.db.save_task(old)
    manager.db.add_marker("old-1", datetime(2026, 1, 1), "m")

    assert manager.delete_task_data("old-1") is True
    assert manager.db.get_task("old-1") is None
    assert manager.db.get_markers("old-1") == []


def test_create_task_note_is_persisted(manager):
    task_id = manager.create_task(os.getpid(), "n.exe", ["memory_rss"], 0.05, note="基线对照")
    task = manager.get_task(task_id)
    assert task.note == "基线对照" and task.get_task_info().note == "基线对照"
    task.collector = _SlowCollector()
    manager.start_task(task_id)
    assert poll_until(lambda: manager.db.get_task(task_id) is not None)

    assert manager.db.get_task(task_id).note == "基线对照"
    manager.stop_all_tasks()


def test_monitor_task_constructor_keeps_keyword_compat(db):
    """旧的关键字/位置调用方式仍然可用（note 在末尾）"""
    t = MonitorTask(1, "a.exe", ["memory_rss"], 1.0, "tid-1", db)
    assert (t.task_id, t.db, t.note) == ("tid-1", db, '')


# ========== 维护与导出互斥 ==========

def test_begin_maintenance_refused_while_exporting(manager):
    """导出线程长时间读库，进行中时不能开始清理/压缩数据库"""
    assert manager.has_running_exports() is False
    assert manager.begin_export() is True
    assert manager.has_running_exports() is True

    assert manager.begin_maintenance() is False
    assert manager.is_maintenance_running() is False

    manager.end_export()
    assert manager.has_running_exports() is False
    assert manager.begin_maintenance() is True
    manager.end_maintenance()


def test_export_count_tracks_concurrent_exports(manager):
    assert manager.begin_export() is True
    assert manager.begin_export() is True
    manager.end_export()
    assert manager.begin_maintenance() is False   # 还有一个导出未结束
    manager.end_export()
    manager.end_export()                          # 多余的注销不会把计数减成负数
    assert manager.has_running_exports() is False
    assert manager.begin_export() is True
    assert manager.has_running_exports() is True
    manager.end_export()


def test_begin_export_refused_during_maintenance(manager):
    assert manager.begin_maintenance() is True
    assert manager.begin_export() is False
    assert manager.has_running_exports() is False
    manager.end_maintenance()
    assert manager.begin_export() is True
    manager.end_export()
