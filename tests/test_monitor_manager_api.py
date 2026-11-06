"""
MonitorManager 接口与线程释放语义：
去单例（普通注入）、stop_task / remove_task / stop_all_tasks 都不阻塞、线程 finished 后
manager 自行移除并释放对象、start_task 只接受未启动任务、计数只看 WorkerState、
join_all 带超时、维护锁、打点（默认标签序列、基线唯一、增删改、markers_changed、
基线样本）、delete_task_data、note
"""
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

import dataclasses

from core.contracts import StopReason, TaskInfo, TaskStatus
from core.monitor_manager import (
    BASELINE_LABEL, BASELINE_SAMPLE_COUNT, MonitorManager, default_marker_kind, median_of,
)
from core.monitor_task import MonitorTask, MonitorWorker, WorkerState
from data.database import Database
from data.errors import StorageError
from data.models import DataPoint, Marker, TaskRecord
from tests.helpers import (
    poll_until, process_events_until, record_stop_reasons, stop_and_join,
)


@pytest.fixture
def manager(db, qapp):
    mgr = MonitorManager(db=db)
    yield mgr
    stop_and_join(mgr)


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


def test_remove_task_does_not_wait_and_finished_callback_removes(manager, qapp):
    """线程卡在收尾中：remove_task 立即返回 True、不 wait；任务留在 manager 里直到
    线程 finished，再由 manager 自己移除、释放对象并发出一次 task_removed"""
    removed = []
    manager.task_removed.connect(removed.append)
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)

    t0 = time.perf_counter()
    assert manager.remove_task(task_id) is True
    assert time.perf_counter() - t0 < 0.2, "remove_task 不应等待线程"
    assert task.state is WorkerState.STOPPING and task.isRunning() is True
    assert manager.get_task(task_id) is task
    qapp.processEvents()
    assert removed == []

    gate.set()
    assert task.wait(5000) is True
    assert process_events_until(lambda: manager.get_task(task_id) is None)
    assert removed == [task_id]


def test_finished_worker_is_removed_and_deleted_without_remove_call(manager, qapp):
    """无人调用 remove_task：线程结束（finished）后 manager 自行移除并 deleteLater"""
    from PyQt5 import sip

    removed = []
    manager.task_removed.connect(removed.append)
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    manager.stop_task(task_id)
    assert task.wait(5000) is True
    assert manager.get_task(task_id) is task   # finished 经队列送达前仍在

    assert process_events_until(lambda: sip.isdeleted(task))
    assert manager.get_task(task_id) is None
    assert manager.get_task_count() == 0
    assert removed == [task_id]


def test_remove_task_not_started_removes_immediately(manager):
    """未启动的任务没有线程可等：立即移除，且对象已置 STOPPED 不会再被启动"""
    removed = []
    manager.task_removed.connect(removed.append)
    task_id = manager.create_task(os.getpid(), "p.exe", ["memory_rss"], 1.0)
    task = manager.get_task(task_id)

    assert manager.remove_task(task_id) is True
    assert manager.get_task(task_id) is None
    assert removed == [task_id]
    assert task.state is WorkerState.STOPPED
    assert manager.remove_task(task_id) is False


def test_remove_task_after_finished_before_callback(manager):
    """线程已结束但 finished 尚未送达：remove_task 直接移除，之后的回调不会重复发信号"""
    removed = []
    manager.task_removed.connect(removed.append)
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    manager.stop_task(task_id)
    assert task.wait(5000) is True

    assert manager.remove_task(task_id) is True
    assert manager.get_task(task_id) is None
    manager._on_worker_finished()
    assert removed == [task_id]


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


# ========== 去单例 ==========

def test_manager_is_not_a_singleton(tmp_path, qapp):
    """每次构造都是独立实例，各自持有注入的数据库与任务表"""
    db_a = Database(str(tmp_path / "a.db"))
    db_b = Database(str(tmp_path / "b.db"))
    a, b = MonitorManager(db=db_a), MonitorManager(db=db_b)

    assert a is not b
    assert (a.db, b.db) == (db_a, db_b)
    a.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert (a.get_task_count(), b.get_task_count()) == (1, 0)
    assert not hasattr(MonitorManager, '_instance')


def test_manager_requires_db():
    with pytest.raises(TypeError):
        MonitorManager()


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


def test_stop_all_tasks_only_requests_stop_without_waiting(manager):
    """退出路径的等待交给 join_all（带超时）：stop_all_tasks 只改状态并唤醒"""
    log = []
    manager._tasks = {n: _FakeTask(n, log) for n in ("a", "b", "c")}
    manager._tasks["idle"] = _FakeTask("idle", log, running=False)

    manager.stop_all_tasks()

    assert log == [("stop", "a"), ("stop", "b"), ("stop", "c"), ("stop", "idle")]
    manager._tasks = {}


def test_stop_all_tasks_returns_while_thread_stuck(manager):
    gate = threading.Event()
    collector = _SlowCollector(gate)
    task_id, task = _start_task_with_collector(manager, collector)
    assert collector.entered.wait(5)

    t0 = time.perf_counter()
    manager.stop_all_tasks()
    assert time.perf_counter() - t0 < 0.2
    assert task.state is WorkerState.STOPPING and task.isRunning()
    gate.set()
    assert manager.join_all(timeout_ms=5000) is True


def test_stop_all_then_join_all_real_tasks_all_finished(manager):
    ids = [_start_task_with_collector(manager, _SlowCollector())[0] for _ in range(3)]
    pending = manager.create_task(os.getpid(), "p.exe", ["memory_rss"], 1.0)
    tasks = [manager.get_task(i) for i in ids]

    manager.stop_all_tasks()
    assert manager.join_all(timeout_ms=5000) is True

    assert all(t.isFinished() for t in tasks)
    assert all(t.state is WorkerState.STOPPED for t in tasks)
    assert manager.get_task(pending).state is WorkerState.STOPPED   # 未启动的也不能再启动
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


class _StateOnlyTask:
    """只有 state 可信、isRunning 故意给反值：验证计数不看 QThread 状态"""

    def __init__(self, state, pid=1):
        self.state = state
        self.pid = pid

    def isRunning(self):
        return self.state is WorkerState.STOPPED


@pytest.mark.parametrize("state, live, active", [
    (WorkerState.WAITING, False, False),
    (WorkerState.RUNNING, True, True),
    (WorkerState.PAUSED, True, True),
    (WorkerState.STOPPING, True, False),
    (WorkerState.STOPPED, False, False),
])
def test_counts_follow_worker_state_only(manager, state, live, active):
    manager._tasks = {"t": _StateOnlyTask(state)}
    try:
        assert manager.running_task_count() == int(live)
        assert manager.has_running_tasks() is live
        assert manager.is_task_active("t") is active
        assert manager.has_running_task_for_pid(1) is active
        assert manager.is_task_paused("t") is (state is WorkerState.PAUSED)
    finally:
        manager._tasks = {}


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

    stop_and_join(manager)
    assert manager.begin_maintenance() is True
    manager.end_maintenance()


# ========== 标记 / 删除 / note ==========

def test_add_marker_returns_marker_and_persists(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    before = datetime.now()

    marker = manager.add_marker(task_id, "  点击开始扫描  ")

    assert isinstance(marker, Marker) and marker.timestamp >= before
    assert (marker.task_id, marker.label, marker.kind) == (task_id, "点击开始扫描", "user")
    markers = manager.db.get_markers(task_id)
    assert markers == [marker]
    manager.stop_all_tasks()


def test_add_marker_uses_given_click_time(manager):
    """界面传入点击时刻时按它落库，而不是调用（确认）时的当前时间"""
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    clicked = datetime(2026, 11, 6, 10, 0, 0)

    marker = manager.add_marker(task_id, "开始第 3 轮", clicked)

    assert marker.timestamp == clicked
    assert [(m.label, m.timestamp) for m in manager.db.get_markers(task_id)] == [
        ("开始第 3 轮", clicked)]


def test_create_task_passes_create_time_to_task(manager):
    with_ct = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0,
                                  create_time=1234.5)
    without_ct = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.get_task(with_ct).expected_create_time == 1234.5
    assert manager.get_task(without_ct).expected_create_time is None


def test_add_marker_persists_with_id_via_markers_repo(manager, monkeypatch):
    """manager 经 db.markers.add 写入：落库的打点带自增 ID、类型为用户打点"""
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    seen = []
    real = manager.db.markers.add
    monkeypatch.setattr(manager.db.markers, 'add',
                        lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])

    marker = manager.add_marker(task_id, "第 1 轮", datetime(2026, 11, 6, 9, 0, 0))

    assert marker.timestamp == datetime(2026, 11, 6, 9, 0, 0)
    markers = manager.db.markers.list(task_id)
    assert len(seen) == 1 and isinstance(seen[0], int) and seen[0] > 0
    assert marker.id == seen[0]
    assert [(m.id, m.label, m.kind) for m in markers] == [(seen[0], "第 1 轮", "user")]


def test_add_marker_storage_error_returns_none(manager, monkeypatch):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)

    def boom(*a, **k):
        raise StorageError("添加打点失败: database is locked")

    monkeypatch.setattr(manager.db.markers, 'add', boom)
    assert manager.add_marker(task_id, "x") is None


def test_create_time_is_written_to_task_row(manager):
    """create_task 的 create_time 随任务行落库（任务线程启动时 save_task 写入）"""
    create_time = 1_700_000_000.25

    class _Collector(_SlowCollector):
        def get_create_time(self):
            return create_time          # 与所选进程一致：不触发 PID 复用判定

    task_id, task = _start_task_with_collector(
        manager, _Collector(), create_time=create_time)
    assert poll_until(lambda: manager.db.get_task(task_id) is not None)
    assert manager.db.get_task(task_id).create_time == create_time
    stop_and_join(manager)
    # 停止收尾再次保存任务行，create_time 不丢
    assert manager.db.get_task(task_id).create_time == create_time

    plain = manager.create_task(os.getpid(), "y.exe", ["memory_rss"], 1.0)
    manager.get_task(plain).collector = _SlowCollector()
    manager.start_task(plain)
    assert poll_until(lambda: manager.db.get_task(plain) is not None)
    stop_and_join(manager)
    assert manager.db.get_task(plain).create_time is None


def test_add_marker_failures_return_none(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.add_marker("unknown-task", "x") is None
    assert manager.add_marker(task_id, "   ") is None
    assert manager.add_marker(task_id, "") is None
    assert manager.db.get_markers(task_id) == []
    with pytest.raises(ValueError):
        manager.add_marker(task_id, "x", kind="bogus")


# ---------- 打点语义：默认标签序列、基线唯一、增删改、markers_changed ----------

T0 = datetime(2026, 11, 6, 9, 0, 0)


def _changes(manager):
    seen = []
    manager.markers_changed.connect(seen.append)
    return seen


def _kinds(manager, task_id):
    return [(m.label, m.kind) for m in manager.list_markers(task_id)]


def test_next_marker_label_sequence(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.next_marker_label(task_id) == BASELINE_LABEL == "基线"

    manager.add_marker(task_id, "基线", T0, kind="baseline")
    assert manager.next_marker_label(task_id) == "第 1 轮"
    manager.add_marker(task_id, "第 1 轮", T0 + timedelta(minutes=1))
    # 用户改了名称不影响计数：按 user 打点数 + 1
    manager.add_marker(task_id, "建模 200 次", T0 + timedelta(minutes=2))
    assert manager.next_marker_label(task_id) == "第 3 轮"
    # 系统打点不计入轮次
    manager.add_marker(task_id, "暂停", T0 + timedelta(minutes=3), kind="pause")
    assert manager.next_marker_label(task_id) == "第 3 轮"


def test_renamed_default_baseline_keeps_baseline_kind():
    """默认名称决定类型：默认"基线"时用户改了名称仍是基线；名称恰为"基线"也是基线"""
    assert default_marker_kind("基线", "预热完成") == "baseline"
    assert default_marker_kind("第 2 轮", " 基线 ") == "baseline"
    assert default_marker_kind("第 2 轮", "第 2 轮") == "user"
    assert default_marker_kind("第 2 轮", "开始建模") == "user"


def test_label_exactly_baseline_forces_baseline_kind(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    marker = manager.add_marker(task_id, " 基线 ", T0)
    assert marker.kind == "baseline"


def test_new_baseline_demotes_previous_one(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    first = manager.add_marker(task_id, "基线", T0, kind="baseline")
    second = manager.add_marker(task_id, "重新预热", T0 + timedelta(minutes=5),
                                kind="baseline")

    assert _kinds(manager, task_id) == [("基线", "user"), ("重新预热", "baseline")]
    assert manager.get_baseline(task_id).id == second.id != first.id


def test_set_baseline_keeps_single_baseline_and_emits(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    base = manager.add_marker(task_id, "基线", T0, kind="baseline")
    round1 = manager.add_marker(task_id, "第 1 轮", T0 + timedelta(minutes=1))
    pause = manager.add_marker(task_id, "暂停", T0 + timedelta(minutes=2), kind="pause")
    seen = _changes(manager)

    assert manager.set_baseline(task_id, round1.id) is True
    assert _kinds(manager, task_id) == [
        ("基线", "user"), ("第 1 轮", "baseline"), ("暂停", "pause")]
    assert seen == [task_id]

    # 已是基线：幂等成功；系统打点、别的任务的打点、不存在的打点都拒绝
    assert manager.set_baseline(task_id, round1.id) is True
    assert manager.set_baseline(task_id, pause.id) is False
    assert manager.set_baseline("other", base.id) is False
    assert manager.set_baseline(task_id, 99999) is False
    assert [m.kind for m in manager.list_markers(task_id)].count("baseline") == 1


def test_rename_marker_keeps_kind_and_emits(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    base = manager.add_marker(task_id, "基线", T0, kind="baseline")
    seen = _changes(manager)

    assert manager.rename_marker(base.id, "  预热结束  ") is True
    assert _kinds(manager, task_id) == [("预热结束", "baseline")]
    assert seen == [task_id]

    assert manager.rename_marker(base.id, "   ") is False
    assert manager.rename_marker(99999, "x") is False
    assert seen == [task_id]


def test_rename_marker_of_unseen_marker_uses_given_task_id(manager):
    """打点不是经本管理器添加/读取的（如库里旧打点）：用调用方给的 task_id 发信号"""
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    marker_id = manager.db.markers.add(task_id, T0, "旧打点")
    seen = _changes(manager)

    assert manager.rename_marker(marker_id, "改名", task_id=task_id) is True
    assert seen == [task_id]
    other_id = manager.db.markers.add(task_id, T0, "另一个")
    assert manager.rename_marker(other_id, "改名 2") is True   # 不知道所属任务：照改不发
    assert seen == [task_id]


def test_delete_marker_and_baseline_resets_sequence(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    base = manager.add_marker(task_id, "基线", T0, kind="baseline")
    round1 = manager.add_marker(task_id, "第 1 轮", T0 + timedelta(minutes=1))
    seen = _changes(manager)

    assert manager.delete_marker(round1.id) is True
    assert manager.next_marker_label(task_id) == "第 1 轮"
    assert manager.delete_marker(base.id) is True
    assert manager.get_baseline(task_id) is None
    assert manager.next_marker_label(task_id) == "基线"
    assert seen == [task_id, task_id]
    assert manager.delete_marker(base.id) is False
    assert seen == [task_id, task_id]


def test_add_marker_emits_markers_changed(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    seen = _changes(manager)
    manager.add_marker(task_id, "基线", T0)
    manager.add_marker(task_id, "   ", T0)       # 失败不发
    assert seen == [task_id]


def test_marker_storage_errors_are_reported_as_false(manager, monkeypatch):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    marker = manager.add_marker(task_id, "第 1 轮", T0)

    def boom(*a, **k):
        raise StorageError("database is locked")

    for name in ("update_label", "delete", "set_kind", "list"):
        monkeypatch.setattr(manager.db.markers, name, boom)
    assert manager.rename_marker(marker.id, "x") is False
    assert manager.delete_marker(marker.id) is False
    assert manager.list_markers(task_id) == []
    assert manager.set_baseline(task_id, marker.id) is False


def test_pause_resume_through_manager_emit_markers_changed(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    seen = _changes(manager)
    clicked = datetime(2026, 11, 6, 10, 0, 0)

    assert manager.pause_task(task_id, clicked) is True
    assert manager.pause_task(task_id) is False          # 已暂停：不写打点、不发信号
    assert manager.resume_task(task_id, clicked + timedelta(seconds=30)) is True
    assert manager.resume_task("unknown") is False

    assert seen == [task_id, task_id]
    assert [(m.label, m.kind, m.timestamp) for m in manager.list_markers(task_id)] == [
        ("暂停", "pause", clicked), ("恢复", "resume", clicked + timedelta(seconds=30))]
    manager.stop_all_tasks()


# ---------- 基线样本 ----------

def _write_points(db, task_id, start, values, step=1.0, metric="memory_rss"):
    db.save_data_points([
        DataPoint(task_id=task_id, timestamp=start + timedelta(seconds=i * step),
                  value=float(v), metric_type=metric)
        for i, v in enumerate(values)])


def test_baseline_samples_for_finished_task_takes_first_five_after_marker(manager, db):
    db.save_task(TaskRecord(
        task_id="old", pid=1, process_name="old.exe", metric_types=["memory_rss"],
        interval=1.0, start_time=T0, end_time=T0 + timedelta(minutes=1),
        status=TaskStatus.STOPPED))
    _write_points(db, "old", T0, [1, 2, 3, 100, 200, 300, 400, 500, 9999])
    base = manager.add_marker("old", "基线", T0 + timedelta(seconds=3))

    seed = manager.baseline_samples("old")

    assert seed.marker == base and seed.settled is True
    assert seed.samples == {"memory_rss": [100.0, 200.0, 300.0, 400.0, 500.0]}
    assert median_of(seed.samples["memory_rss"]) == 300.0


def test_baseline_samples_recent_baseline_is_not_settled(manager, db):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    db.save_task(manager.get_task(task_id).task_model)
    base = manager.add_marker(task_id, "基线", T0)
    _write_points(db, task_id, T0 + timedelta(seconds=1), [10, 20])

    seed = manager.baseline_samples(task_id, base)

    assert seed.samples == {"memory_rss": [10.0, 20.0]}
    assert seed.settled is False     # 任务还会继续采集：由调用方用实时数据补足


def test_baseline_samples_without_baseline_is_none(manager):
    task_id = manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)
    assert manager.baseline_samples(task_id) is None
    assert BASELINE_SAMPLE_COUNT == 5


def test_delete_task_data_refused_while_running(manager):
    task_id, task = _start_task_with_collector(manager, _SlowCollector())
    assert poll_until(lambda: manager.db.get_task(task_id) is not None)

    assert manager.delete_task_data(task_id) is False
    assert manager.db.get_task(task_id) is not None

    stop_and_join(manager)
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
    """旧的关键字/位置调用方式仍然可用（note 在末尾）；旧名 MonitorTask 是 MonitorWorker 的别名"""
    assert MonitorTask is MonitorWorker
    t = MonitorTask(1, "a.exe", ["memory_rss"], 1.0, "tid-1", db)
    assert (t.task_id, t.db, t.note) == ("tid-1", db, '')
    assert t.state is WorkerState.WAITING


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
    manager._tasks = {}


def test_join_all_reports_timeout_and_still_joins_rest(manager):
    log = []
    manager._tasks = {"stuck": _JoinTask(log, "stuck", hang=True), "ok": _JoinTask(log, "ok")}
    assert manager.join_all(timeout_ms=50) is False
    assert log == [("stuck", 50), ("ok", 50)]   # 一个超时不影响后面的线程
    manager._tasks = {}


class _SleepyJoinTask(_JoinTask):
    """永不结束：wait 睡满给定超时后返回 False"""

    def wait(self, timeout_ms=None):
        self.log.append((self.name, timeout_ms))
        time.sleep(timeout_ms / 1000.0)
        return False


def test_join_all_deadline_caps_each_wait_and_skips_expired(manager):
    """总截止时间落实到每次等待：等待取单项超时与剩余时间的较小者，到期后其余
    线程不再等待（不会把单项超时累加成远超总期限的等待）"""
    log = []
    manager._tasks = {name: _SleepyJoinTask(log, name) for name in ("a", "b", "c")}
    t0 = time.monotonic()
    assert manager.join_all(timeout_ms=3000, deadline=t0 + 0.2) is False
    assert time.monotonic() - t0 < 1.0
    # 第一个线程等到截止时间为止；Windows 单调时钟粒度约 15 ms 且睡眠可能略早返回，
    # 之后的线程最多只会拿到几十毫秒的尾差，远小于单项超时 3000 ms
    assert log[0][0] == "a" and 0 < log[0][1] <= 200
    assert all(waited <= 50 for _name, waited in log[1:])
    manager._tasks = {}


def test_join_all_expired_deadline_waits_nothing(manager):
    log = []
    manager._tasks = {"a": _JoinTask(log, "a"), "idle": _JoinTask(log, "idle", alive=False)}
    assert manager.join_all(timeout_ms=3000, deadline=time.monotonic() - 1) is False
    assert log == []
    manager._tasks = {"idle": _JoinTask(log, "idle", alive=False)}
    assert manager.join_all(timeout_ms=3000, deadline=time.monotonic() - 1) is True
    manager._tasks = {}


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
