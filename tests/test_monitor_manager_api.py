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

import dataclasses

from core.contracts import StopReason, TaskInfo, TaskStatus
from core.monitor_manager import MonitorManager
from core.monitor_task import MonitorTask
from data.database import Database
from tests.helpers import poll_until, record_stop_reasons


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


# ========== 只读快照与状态查询（界面只用这一组） ==========

def test_task_info_snapshot_fields_and_status_lifecycle(manager):
    task_id = manager.create_task(
        os.getpid(), "snap.exe", ["memory_rss", "num_threads"], 0.05,
        note="快照", create_time=1234.5)

    info = manager.get_task_info(task_id)
    assert isinstance(info, TaskInfo)
    assert (info.task_id, info.pid, info.process_name) == (task_id, os.getpid(), "snap.exe")
    assert info.metric_types == ("memory_rss", "num_threads")
    assert (info.interval, info.note, info.create_time) == (0.05, "快照", 1234.5)
    assert info.status is TaskStatus.PENDING
    assert info.start_time is None and info.end_time is None
    assert info.is_paused is False and info.is_stopping is False
    # 快照不可变，也不暴露任务线程对象
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.note = "改"
    assert not any(isinstance(v, MonitorTask) for v in dataclasses.astuple(info))

    gate = threading.Event()
    collector = _SlowCollector(gate)
    collector.get_create_time = lambda: 1234.5   # 与所选进程一致，正常采集
    manager.get_task(task_id).collector = collector
    assert manager.start_task(task_id) is True
    assert collector.entered.wait(5)
    running = manager.get_task_info(task_id)
    assert running.status is TaskStatus.RUNNING and running.start_time is not None

    manager.stop_task(task_id)
    gate.set()
    assert manager.get_task(task_id).wait(5000)
    stopped = manager.get_task_info(task_id)
    assert stopped.status is TaskStatus.STOPPED and stopped.end_time is not None
    assert [i.task_id for i in manager.list_task_infos()] == [task_id]


def test_get_task_info_unknown_returns_none(manager):
    assert manager.get_task_info("nope") is None
    assert manager.list_task_infos() == []
    assert manager.is_task_active("nope") is False
    assert manager.is_task_paused("nope") is False


def test_is_task_active_false_while_stopping(manager):
    """停止收尾中（标志已清、线程未结束）：is_task_active 为 False，快照 is_stopping 为 True，
    status 仍是 RUNNING（与 running_task_count 口径一致）"""
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)
    assert manager.is_task_active(task_id) is True

    manager.stop_task(task_id)
    info = manager.get_task_info(task_id)
    assert manager.is_task_active(task_id) is False
    assert info.is_stopping is True and info.status is TaskStatus.RUNNING

    gate.set()
    assert task.wait(5000)
    assert manager.is_task_active(task_id) is False
    assert manager.get_task_info(task_id).is_stopping is False


def test_is_task_active_false_before_start(manager):
    task_id = manager.create_task(os.getpid(), "p.exe", ["memory_rss"], 1.0)
    assert manager.is_task_active(task_id) is False


def test_is_task_paused_follows_pause_resume(manager):
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)

    assert manager.pause_task(task_id) is True
    assert manager.is_task_paused(task_id) is True
    assert manager.get_task_info(task_id).is_paused is True
    assert manager.is_task_active(task_id) is True   # 暂停中仍算在采集任务

    assert manager.resume_task(task_id) is True
    assert manager.is_task_paused(task_id) is False
    gate.set()
    manager.stop_all_tasks()


def test_has_running_task_for_pid(manager):
    gate = threading.Event()
    collector = _SlowCollector(gate)
    assert manager.has_running_task_for_pid(os.getpid()) is False

    pending = manager.create_task(os.getpid(), "p.exe", ["memory_rss"], 1.0)
    assert manager.has_running_task_for_pid(os.getpid()) is False   # 未启动不算
    assert pending is not None

    task_id, task = _start_task_with_collector(manager, collector)
    assert manager.has_running_task_for_pid(os.getpid()) is True
    assert manager.has_running_task_for_pid(os.getpid() + 1) is False

    manager.stop_task(task_id)
    assert manager.has_running_task_for_pid(os.getpid()) is False   # 停止中不算
    gate.set()
    assert task.wait(5000)


def test_manager_task_stopped_carries_stop_reason(manager, qapp):
    reasons = record_stop_reasons(manager)
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    manager.stop_task(task_id)
    assert task.wait(5000)
    assert poll_until(lambda: (qapp.processEvents(), reasons)[1], timeout=5)
    assert reasons == [StopReason.USER]


# ========== join_all（退出路径兜底 join） ==========

class _JoinTask:
    """记录 wait 超时参数的假线程；hang=True 时 wait 超时返回 False"""

    def __init__(self, log, name, alive=True, hang=False):
        self.log, self.name, self._alive, self.hang = log, name, alive, hang

    def isRunning(self):
        return self._alive

    def stop(self):
        pass

    def wait(self, timeout_ms=None):
        if timeout_ms is None:   # 用例结束时 conftest 的 stop_all_tasks 无超时收尾
            self._alive = False
            return True
        self.log.append((self.name, timeout_ms))
        if self.hang:
            return False
        self._alive = False
        return True


def test_join_all_waits_running_tasks_with_timeout(manager):
    log = []
    manager._tasks = {
        "a": _JoinTask(log, "a"),
        "idle": _JoinTask(log, "idle", alive=False),
        "b": _JoinTask(log, "b"),
    }
    assert manager.join_all(timeout_ms=3000) is True
    assert log == [("a", 3000), ("b", 3000)]   # 已结束的线程不等


def test_join_all_reports_timeout_and_still_joins_rest(manager):
    log = []
    manager._tasks = {"stuck": _JoinTask(log, "stuck", hang=True), "ok": _JoinTask(log, "ok")}
    assert manager.join_all(timeout_ms=50) is False
    assert log == [("stuck", 50), ("ok", 50)]   # 一个超时不影响后面的线程


def test_join_all_real_stuck_thread_times_out(manager):
    """真实线程卡在收尾：join_all 按超时返回 False，放开后能正常结束"""
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)
    manager.stop_task(task_id)

    t0 = time.perf_counter()
    assert manager.join_all(timeout_ms=100) is False
    assert time.perf_counter() - t0 < 2.0

    gate.set()
    assert manager.join_all(timeout_ms=5000) is True
    assert task.isFinished()
