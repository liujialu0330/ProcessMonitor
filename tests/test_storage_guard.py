"""
存储守卫（core/storage_guard.py）：磁盘空间两档阈值与回滞、-wal 兜底 checkpoint、后台 quick_check。

全部离线：注入假单调时钟、假 disk_usage、假 -wal 大小与假维护服务，守卫的定时器不启动，
由测试直接 tick()。真实线程只在需要证明"登记到线程登记表、退出时能取消"时才起。
"""
import ast
import logging
import os
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from core import storage_guard as sg
from core.maintenance_service import (
    BUSY_STORAGE_JOB_RUNNING, MaintenanceBusy, MaintenanceService,
)
from core.shutdown import ThreadRegistry
from core.storage_guard import GB, MB, StorageGuard
from data.db.maintenance import CheckpointOutcome, QuickCheckResult
from tests.helpers import poll_until, process_events_until

Usage = namedtuple('Usage', 'total used free')


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Disk:
    """假 disk_usage：free 由测试设定，记录调用的路径与次数"""

    def __init__(self, free=50 * GB):
        self.free = free
        self.calls = []
        self.error = None

    def __call__(self, path):
        self.calls.append(path)
        if self.error is not None:
            raise self.error
        return Usage(500 * GB, 500 * GB - self.free, self.free)


class FakeManager(QObject):
    """守卫用到的 manager 公开接口：任务快照、状态查询、暂停与三个信号"""
    task_resumed = pyqtSignal(str)
    task_stopped = pyqtSignal(str, object)
    task_removed = pyqtSignal(str)

    class Info:
        def __init__(self, task_id):
            self.task_id = task_id

    def __init__(self, running=(), paused=(), stopping=()):
        super().__init__()
        self.running = list(running)
        self.paused = set(paused)
        self.stopping = set(stopping)
        self.pause_calls = []
        self.resume_calls = []
        self.pause_fail = set()

    def list_task_infos(self):
        return [self.Info(t) for t in [*self.running, *self.paused, *self.stopping]]

    def is_task_active(self, task_id):
        return task_id in self.running or task_id in self.paused

    def is_task_paused(self, task_id):
        return task_id in self.paused

    def pause_task(self, task_id, timestamp=None):
        self.pause_calls.append(task_id)
        if task_id in self.pause_fail or task_id not in self.running:
            return False
        self.running.remove(task_id)
        self.paused.add(task_id)
        return True

    def resume_task(self, task_id, timestamp=None):
        self.resume_calls.append(task_id)
        return self.user_resumes(task_id) is None

    def user_resumes(self, task_id):
        self.paused.discard(task_id)
        self.running.append(task_id)
        self.task_resumed.emit(task_id)


class FakeQuickCheck:
    def __init__(self, result=None, block=False):
        self.result = result or QuickCheckResult(True, ('ok',), 0.01)
        self.block = block
        self.started = threading.Event()
        self._stop = threading.Event()
        self.interrupted_at = None

    def run(self):
        self.started.set()
        if self.block:
            self._stop.wait(10)
            if self._stop.is_set():
                return QuickCheckResult(False, (), 0.0, interrupted=True)
        return self.result

    def interrupt(self):
        self.interrupted_at = time.perf_counter()
        self._stop.set()


class FakeMaintenance:
    """守卫用到的维护服务接口"""

    def __init__(self):
        self.busy = False
        self.jobs = 0
        self.begin_calls = 0
        self.wal = 0
        self.checkpoints = []
        self.checkpoint_gate = None
        self.quick_checks = []
        self.next_quick_check = None

    def is_busy(self):
        return self.busy

    def begin_storage_job(self):
        self.begin_calls += 1
        if self.busy:
            return False
        self.jobs += 1
        return True

    def end_storage_job(self):
        self.jobs -= 1

    def wal_size_bytes(self):
        return self.wal

    def checkpoint_wal(self):
        self.checkpoints.append(self.wal)
        if self.checkpoint_gate is not None:
            self.checkpoint_gate.wait(5)
        return CheckpointOutcome('TRUNCATE', True, False, self.wal, 0, 0.01)

    def new_quick_check(self):
        qc = self.next_quick_check or FakeQuickCheck()
        self.quick_checks.append(qc)
        return qc


class Recorder:
    """记录守卫发出的全部信号"""

    def __init__(self, guard):
        self.low, self.critical, self.recovered = [], [], []
        self.paused_changes, self.integrity, self.qc_done = [], [], []
        guard.low_space.connect(self.low.append)
        guard.space_critical.connect(lambda free, ids: self.critical.append((free, ids)))
        guard.space_recovered.connect(self.recovered.append)
        guard.space_paused_changed.connect(self.paused_changes.append)
        guard.integrity_problem.connect(self.integrity.append)
        guard.quick_check_finished.connect(self.qc_done.append)


@pytest.fixture
def env(qapp, tmp_path):
    clock, disk = Clock(), Disk()
    manager = FakeManager(running=['a', 'b', 'c'], paused=['p'], stopping=['s'])
    maintenance = FakeMaintenance()
    registry = ThreadRegistry()
    made = []

    def make(**kwargs):
        options = dict(clock=clock, disk_usage=disk, wal_size=lambda: maintenance.wal,
                       quick_check_delay_s=None)
        options.update(kwargs)
        guard = StorageGuard(manager, maintenance, registry,
                             str(tmp_path / "data" / "monitor.db"), **options)
        made.append(guard)
        return guard, Recorder(guard)

    class Env:
        pass

    e = Env()
    e.clock, e.disk, e.manager, e.maintenance, e.registry, e.make = (
        clock, disk, manager, maintenance, registry, make)
    e.db_path = str(tmp_path / "data" / "monitor.db")
    yield e
    for guard in made:
        guard.stop()
    registry.cancel_all()
    registry.shutdown_all()


def _check(env, guard, free):
    env.disk.free = free
    guard.check_disk()


# ========== 磁盘空间：1 GB 提示与回滞 ==========

def test_plenty_of_space_is_silent(env):
    guard, rec = env.make()
    _check(env, guard, 5 * GB)
    assert (rec.low, rec.critical, rec.recovered, rec.paused_changes) == ([], [], [], [])
    assert env.manager.pause_calls == []


def test_disk_usage_reads_the_database_directory(env):
    guard, _ = env.make()
    guard.check_disk()
    assert env.disk.calls == [os.path.dirname(env.db_path)]


def test_below_1gb_warns_once(env):
    guard, rec = env.make()
    _check(env, guard, 900 * MB)
    _check(env, guard, 800 * MB)
    _check(env, guard, 700 * MB)
    assert rec.low == [900 * MB]
    assert rec.critical == [] and env.manager.pause_calls == []


def test_warning_rearms_only_after_rising_above_1_2gb(env):
    guard, rec = env.make()
    _check(env, guard, 900 * MB)
    assert len(rec.low) == 1
    _check(env, guard, int(1.1 * GB))     # 回到 1 GB 以上但没到 1.2 GB：仍未武装
    _check(env, guard, 900 * MB)
    assert len(rec.low) == 1
    _check(env, guard, int(1.2 * GB))     # 回升到 1.2 GB 以上
    _check(env, guard, 950 * MB)          # 再次跌破：提示第二次
    assert len(rec.low) == 2
    assert rec.low[1] == 950 * MB


def test_exactly_1gb_is_not_below(env):
    guard, rec = env.make()
    _check(env, guard, 1 * GB)
    assert rec.low == []


# ========== 磁盘空间：200 MB 暂停 ==========

def test_below_200mb_pauses_all_running_tasks(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    # 只暂停运行中的 a b c；已暂停的 p、正在停止的 s 不动
    assert env.manager.pause_calls == ['a', 'b', 'c']
    assert env.manager.paused == {'p', 'a', 'b', 'c'}
    assert rec.critical == [(150 * MB, ('a', 'b', 'c'))]
    assert guard.paused_task_ids() == frozenset({'a', 'b', 'c'})
    assert rec.paused_changes == [frozenset({'a', 'b', 'c'})]
    assert guard.is_critical


def test_exactly_200mb_does_not_pause(env):
    guard, rec = env.make()
    _check(env, guard, 200 * MB)
    assert rec.critical == [] and env.manager.pause_calls == []


def test_dropping_straight_below_200mb_skips_the_1gb_warning(env):
    guard, rec = env.make()
    _check(env, guard, 5 * GB)
    _check(env, guard, 100 * MB)
    assert rec.low == [] and len(rec.critical) == 1


def test_critical_is_announced_once(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    _check(env, guard, 120 * MB)
    _check(env, guard, 90 * MB)
    assert len(rec.critical) == 1
    assert env.manager.pause_calls == ['a', 'b', 'c']    # 没有重复暂停


def test_pause_failure_for_one_task_does_not_stop_the_others(env):
    guard, rec = env.make()
    env.manager.pause_fail = {'a'}
    _check(env, guard, 100 * MB)
    assert rec.critical[0][1] == ('b', 'c')
    assert guard.paused_task_ids() == frozenset({'b', 'c'})


def test_pause_exception_is_contained(env, caplog):
    guard, rec = env.make()

    def boom(task_id, timestamp=None):
        raise RuntimeError("x")
    env.manager.pause_task = boom
    caplog.set_level(logging.ERROR)
    _check(env, guard, 100 * MB)
    assert len(rec.critical) == 1 and rec.critical[0][1] == ()


# ========== 回升：提示改变，但不自动恢复 ==========

def test_recovery_needs_300mb_and_does_not_resume_tasks(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    _check(env, guard, 250 * MB)                 # 200–300 MB：回滞区，仍是严重不足
    assert rec.recovered == [] and guard.is_critical
    _check(env, guard, 300 * MB)
    assert rec.recovered == [300 * MB] and not guard.is_critical
    # 任务仍暂停，守卫没有碰它们
    assert env.manager.paused == {'p', 'a', 'b', 'c'}
    assert guard.paused_task_ids() == frozenset({'a', 'b', 'c'})
    assert env.manager.running == [] and env.manager.resume_calls == []


def test_recovery_is_announced_once_and_1gb_warning_not_repeated(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    _check(env, guard, 400 * MB)       # 回升到 300 MB 以上、仍低于 1 GB
    _check(env, guard, 500 * MB)
    assert len(rec.recovered) == 1
    assert rec.low == []               # 已经在严重不足里提示过，不再补发较轻的 1 GB 提示


def test_second_episode_announces_again(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    _check(env, guard, 2 * GB)
    env.manager.user_resumes('a')
    _check(env, guard, 100 * MB)
    assert len(rec.critical) == 2 and len(rec.recovered) == 1


def test_user_resumed_task_is_repaused_while_still_below_200mb(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    env.manager.user_resumes('a')
    assert 'a' not in guard.paused_task_ids()    # 用户恢复后不再算"守卫暂停的"
    _check(env, guard, 140 * MB)
    assert 'a' in env.manager.paused and 'a' in guard.paused_task_ids()
    assert len(rec.critical) == 1                # 不重复提示


def test_user_resumed_task_is_left_alone_between_200_and_300mb(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    env.manager.user_resumes('a')
    calls = len(env.manager.pause_calls)
    _check(env, guard, 250 * MB)
    assert len(env.manager.pause_calls) == calls and 'a' in env.manager.running


def test_paused_ids_follow_resume_stop_and_remove(env):
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    rec.paused_changes.clear()
    env.manager.user_resumes('a')
    env.manager.task_stopped.emit('b', object())
    env.manager.task_removed.emit('c')
    assert guard.paused_task_ids() == frozenset()
    assert rec.paused_changes[-1] == frozenset()
    env.manager.task_removed.emit('zzz')        # 与守卫无关的任务：不发信号
    assert len(rec.paused_changes) == 3


# ========== 数据层报告磁盘已满（SQLITE_FULL 同路径）==========

def test_report_storage_full_follows_the_critical_path(env):
    guard, rec = env.make()
    env.disk.free = 50 * GB                     # 磁盘读数很充裕也照做：数据层说满了就是满了
    guard.report_storage_full()
    assert env.manager.pause_calls == ['a', 'b', 'c']
    assert len(rec.critical) == 1 and rec.critical[0][0] == 50 * GB
    guard.report_storage_full()                 # 重复报告不重复提示
    assert len(rec.critical) == 1


def test_report_storage_full_repauses_resumed_tasks(env):
    guard, rec = env.make()
    guard.report_storage_full()
    env.manager.user_resumes('b')
    guard.report_storage_full()
    assert 'b' in env.manager.paused and len(rec.critical) == 1


def test_report_storage_full_after_stop_is_ignored(env):
    guard, rec = env.make()
    guard.stop()
    guard.report_storage_full()
    assert rec.critical == [] and env.manager.pause_calls == []


# ========== 读不到磁盘空间 ==========

def test_unreadable_disk_does_not_crash_and_logs_once(env, caplog):
    guard, rec = env.make()
    env.disk.error = OSError("device not ready")
    caplog.set_level(logging.WARNING, logger='core.storage_guard')
    assert guard.check_disk() is None
    assert guard.check_disk() is None
    assert len([r for r in caplog.records if '无法读取磁盘剩余空间' in r.getMessage()]) == 1
    assert rec.low == [] and rec.critical == []
    env.disk.error = None
    assert guard.check_disk() == 50 * GB


# ========== 定时（注入时钟）==========

def test_disk_is_checked_on_the_injected_clock(env):
    guard, rec = env.make()
    guard.start()
    guard.tick()                                   # start 后的第一次 tick 就检查
    assert len(env.disk.calls) == 1
    env.clock.advance(29)
    guard.tick()
    assert len(env.disk.calls) == 1                # 30 秒周期未到
    env.clock.advance(1)
    guard.tick()
    assert len(env.disk.calls) == 2
    guard.stop()


def test_tick_drives_the_state_machine(env):
    guard, rec = env.make()
    guard.start()
    env.disk.free = 100 * MB
    guard.tick()
    assert len(rec.critical) == 1
    guard.stop()


def test_start_is_idempotent_and_stop_is_final(env):
    guard, rec = env.make()
    guard.start()
    guard.start()
    assert guard.is_running and guard._timer.isActive()
    guard.stop()
    guard.stop()
    assert not guard.is_running and not guard._timer.isActive()
    env.disk.free = 10 * MB
    guard.tick()
    assert rec.critical == [] and env.disk.calls == []
    guard.start()                                   # 停止后不能再启动
    assert not guard._timer.isActive()


def test_one_failing_check_does_not_block_the_others(env, caplog):
    guard, rec = env.make(wal_interval_s=1, quick_check_delay_s=0)
    guard.start()

    def boom():
        raise RuntimeError("disk probe failed")
    guard._disk_usage = lambda path: boom()
    env.maintenance.wal = 100 * MB
    env.clock.advance(5)
    caplog.set_level(logging.ERROR, logger='core.storage_guard')
    guard.tick()
    assert any("磁盘空间检查失败" in r.getMessage() for r in caplog.records)
    assert poll_until(lambda: env.maintenance.checkpoints, 3)     # -wal 检查照常
    guard.stop()


# ========== 无窗口模式：只记日志、不碰界面 ==========

def test_guard_logs_every_transition_without_any_ui_connection(env, caplog):
    """没有任何界面连接时（无窗口模式），各档状态都在日志里留下记录"""
    guard, _ = env.make()
    caplog.set_level(logging.INFO, logger='core.storage_guard')
    _check(env, guard, 900 * MB)
    _check(env, guard, 150 * MB)
    _check(env, guard, 400 * MB)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "磁盘剩余空间不足 1 GB" in text
    assert "磁盘空间严重不足" in text and "已暂停 3 个任务" in text
    assert "已回升到" in text and "不会自动恢复" in text
    levels = {r.levelno for r in caplog.records}
    assert logging.WARNING in levels and logging.ERROR in levels


def test_storage_guard_module_does_not_depend_on_ui():
    source = Path(sg.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split('.')[0] != 'ui'
        if isinstance(node, ast.Import):
            assert all(alias.name.split('.')[0] != 'ui' for alias in node.names)


def test_headless_context_gets_a_guard_without_a_window(qapp, app_paths):
    """build_context 组装守卫，start_storage_guard 在没有窗口时只启动它（不连界面）"""
    from app import bootstrap
    app_paths.ensure_dirs()
    ctx = bootstrap.build_context(app_paths, retention_days=0)
    try:
        guard = ctx.storage_guard
        assert isinstance(guard, StorageGuard) and not guard.is_running
        bootstrap.start_storage_guard(ctx)               # 没有 window
        assert guard.is_running
    finally:
        ctx.storage_guard.stop()
        ctx.db.close()


def test_start_storage_guard_attaches_window_before_starting(qapp, app_paths):
    from app import bootstrap
    app_paths.ensure_dirs()
    ctx = bootstrap.build_context(app_paths, retention_days=0)
    order = []

    class Window:
        def attach_storage_guard(self, guard):
            order.append(('attach', guard.is_running))

    try:
        bootstrap.start_storage_guard(ctx, Window())
        assert order == [('attach', False)]             # 先接信号、后启动
        assert ctx.storage_guard.is_running
    finally:
        ctx.storage_guard.stop()
        ctx.db.close()


def test_headless_runtime_stops_the_guard_before_shutdown(qapp):
    """无窗口模式：退出收尾之前先调用守卫的 stop"""
    from core.headless import HeadlessRuntime
    events = []

    class Coordinator:
        def __init__(self, manager, registry, db=None):
            pass

        def run(self, on_progress=None):
            events.append('coordinator')
            return True

    class M(QObject):
        task_stopped = pyqtSignal(str, object)
        markers_changed = pyqtSignal(str)
        data_updated = pyqtSignal(str, dict)
        task_removed = pyqtSignal(str)
        task_started = pyqtSignal(str)

        def get_task_count(self):
            return 0

        def list_task_infos(self):
            return []

        def get_task_info(self, task_id):
            return None

    runtime = HeadlessRuntime(M(), 'registry', 'db', quit_app=lambda code: events.append('exit'),
                              coordinator_factory=Coordinator,
                              quit_hooks=[lambda: events.append('guard.stop')])
    try:
        runtime.quit()
        assert events == ['guard.stop', 'coordinator', 'exit']
    finally:
        runtime.deleteLater()


# ========== -wal 兜底 checkpoint ==========

def _wait_wal_job(env, guard):
    return process_events_until(lambda: not guard.wal_job_running, 5)


def test_wal_over_64mb_triggers_checkpoint(env):
    guard, _ = env.make()
    env.maintenance.wal = 64 * MB
    assert guard.check_wal() is False and env.maintenance.checkpoints == []   # 恰好 64 MB 不算超
    env.maintenance.wal = 64 * MB + 1
    assert guard.check_wal() is True
    assert _wait_wal_job(env, guard)
    assert env.maintenance.checkpoints == [64 * MB + 1]
    assert env.maintenance.jobs == 0 and env.maintenance.begin_calls == 1   # 登记与注销成对


def test_wal_job_is_registered_in_the_thread_registry(env):
    guard, _ = env.make()
    env.maintenance.wal = 100 * MB
    env.maintenance.checkpoint_gate = threading.Event()
    guard.check_wal()
    try:
        entries = env.registry.entries()
        assert [e.name for e in entries] == [sg.WAL_THREAD_NAME]
        assert entries[0].cancel is not None and entries[0].timeout_ms == sg.WAL_THREAD_TIMEOUT_MS
        assert env.maintenance.jobs == 1            # 作业期间登记着，清理与重建汇总开始不了
    finally:
        env.maintenance.checkpoint_gate.set()
        assert _wait_wal_job(env, guard)


def test_wal_check_is_skipped_during_maintenance(env):
    guard, _ = env.make()
    env.maintenance.wal = 500 * MB
    env.maintenance.busy = True
    assert guard.check_wal() is False
    assert env.maintenance.checkpoints == [] and not guard.wal_job_running
    env.maintenance.busy = False
    assert guard.check_wal() is True
    assert _wait_wal_job(env, guard)


def test_wal_check_skipped_when_begin_is_refused(env):
    """is_busy 与登记之间维护恰好开始：begin_storage_job 返回 False 也跳过"""
    guard, _ = env.make()
    env.maintenance.wal = 500 * MB
    env.maintenance.is_busy = lambda: False
    env.maintenance.busy = True      # begin_storage_job 看到 busy 拒绝
    assert guard.check_wal() is False and not guard.wal_job_running


def test_wal_check_does_not_overlap_itself(env):
    guard, _ = env.make()
    env.maintenance.wal = 100 * MB
    env.maintenance.checkpoint_gate = threading.Event()
    assert guard.check_wal() is True
    assert guard.check_wal() is False and env.maintenance.begin_calls == 1
    env.maintenance.checkpoint_gate.set()
    assert _wait_wal_job(env, guard)


def test_wal_check_skipped_for_read_only_database(env):
    guard, _ = env.make(read_only=True)
    env.maintenance.wal = 500 * MB
    assert guard.check_wal() is False and env.maintenance.checkpoints == []


def test_wal_check_runs_on_its_own_interval(env):
    guard, _ = env.make(wal_interval_s=300)
    env.maintenance.wal = 100 * MB
    guard.start()
    env.clock.advance(299)
    guard.tick()
    assert env.maintenance.begin_calls == 0
    env.clock.advance(1)
    guard.tick()
    assert env.maintenance.begin_calls == 1
    assert _wait_wal_job(env, guard)
    guard.stop()


def test_wal_start_failure_releases_the_job(env, monkeypatch):
    guard, _ = env.make()
    env.maintenance.wal = 100 * MB

    def boom(*args, **kwargs):
        raise RuntimeError("cannot start")
    monkeypatch.setattr(env.registry, 'register', boom)
    with pytest.raises(RuntimeError):
        guard.check_wal()
    assert env.maintenance.jobs == 0 and not guard.wal_job_running


# ========== quick_check ==========

def _run_quick_check(env, guard, rec, expect_done=1):
    return process_events_until(lambda: len(rec.qc_done) >= expect_done
                                and not guard.quick_check_running, 5)


def test_quick_check_starts_after_the_delay_and_runs_once(env):
    guard, rec = env.make(quick_check_delay_s=120)
    guard.start()
    env.clock.advance(119)
    guard.tick()
    assert env.maintenance.quick_checks == []
    env.clock.advance(1)
    guard.tick()
    assert _run_quick_check(env, guard, rec)
    env.clock.advance(1000)
    guard.tick()
    guard.tick()
    assert len(env.maintenance.quick_checks) == 1          # 只跑一次
    assert rec.integrity == []
    guard.stop()


def test_quick_check_failure_prompts_exactly_once(env):
    bad = QuickCheckResult(False, ("*** in database main *** Page 5: btreeInitPage() "
                                   "returns error code 11", "second"), 0.2)
    env.maintenance.next_quick_check = FakeQuickCheck(bad)
    guard, rec = env.make(quick_check_delay_s=0)
    guard.start()
    guard.tick()
    assert _run_quick_check(env, guard, rec)
    assert len(rec.integrity) == 1 and "Page 5" in rec.integrity[0]
    assert guard.integrity_summary == rec.integrity[0]
    # 再来一次（例如重新排期）也不再提示
    guard._quick_check_at = env.clock.t
    env.maintenance.next_quick_check = FakeQuickCheck(bad)
    guard.tick()
    assert _run_quick_check(env, guard, rec, expect_done=2)
    assert len(rec.integrity) == 1
    guard.stop()


def test_quick_check_that_could_not_run_is_not_an_integrity_alarm(env, caplog):
    env.maintenance.next_quick_check = FakeQuickCheck(
        QuickCheckResult(False, (), 0.1, error="database is locked"))
    guard, rec = env.make(quick_check_delay_s=0)
    caplog.set_level(logging.INFO, logger='core.storage_guard')
    guard.start()
    guard.tick()
    assert _run_quick_check(env, guard, rec)
    assert rec.integrity == [] and guard.integrity_summary is None
    assert any("未能完成" in r.getMessage() for r in caplog.records)
    guard.stop()


def test_quick_check_result_is_logged_with_duration(env, caplog):
    env.maintenance.next_quick_check = FakeQuickCheck(QuickCheckResult(True, ('ok',), 1.234))
    guard, rec = env.make(quick_check_delay_s=0)
    caplog.set_level(logging.INFO, logger='core.storage_guard')
    guard.start()
    guard.tick()
    assert _run_quick_check(env, guard, rec)
    assert any("完整性检查通过" in r.getMessage() and "1.23" in r.getMessage()
               for r in caplog.records)
    guard.stop()


def test_quick_check_postponed_while_maintenance_runs(env):
    guard, rec = env.make(quick_check_delay_s=0)
    env.maintenance.busy = True
    guard.start()
    guard.tick()
    assert env.maintenance.quick_checks == []
    env.clock.advance(sg.QUICK_CHECK_RETRY_S - 1)
    guard.tick()
    assert env.maintenance.quick_checks == []
    env.maintenance.busy = False
    env.clock.advance(1)
    guard.tick()
    assert _run_quick_check(env, guard, rec)
    guard.stop()


def test_quick_check_is_registered_and_waits_for_wal_job(env):
    env.maintenance.next_quick_check = FakeQuickCheck(block=True)
    guard, rec = env.make(quick_check_delay_s=0)
    guard.start()
    guard.tick()
    qc = env.maintenance.quick_checks[0]
    try:
        assert qc.started.wait(3)
        entries = env.registry.entries()
        assert [e.name for e in entries] == [sg.QUICK_CHECK_THREAD_NAME]
        assert guard.quick_check_running
        # quick_check 在跑时，兜底 checkpoint 不并行
        env.maintenance.wal = 500 * MB
        assert guard.check_wal() is False
    finally:
        guard.stop()
        assert process_events_until(lambda: not guard.quick_check_running, 5)


# ========== 退出路径：quick_check 跑到一半不能卡住退出 ==========

def test_exit_path_interrupts_a_running_quick_check_promptly(env):
    """退出收尾（登记表 cancel_all + shutdown_all）能及时中止跑到一半的 quick_check，
    且中止后不再发任何提示"""
    env.maintenance.next_quick_check = FakeQuickCheck(block=True)
    guard, rec = env.make(quick_check_delay_s=0)
    guard.start()
    guard.tick()
    qc = env.maintenance.quick_checks[0]
    assert qc.started.wait(3)
    guard.stop()                                 # 退出路径第一步：守卫停定时器并取消作业
    begin = time.perf_counter()
    assert env.registry.shutdown_all(deadline=time.monotonic() + 5) is True
    elapsed = time.perf_counter() - begin
    assert elapsed < 1.5, f"退出被 quick_check 拖住了 {elapsed:.2f}s"
    assert qc.interrupted_at is not None
    process_events_until(lambda: False, 0.1)
    assert rec.integrity == [] and rec.qc_done == []      # 取消后不发结果信号


def test_registry_cancel_alone_interrupts_quick_check(env):
    """即使守卫没来得及 stop，登记表的取消回调也能中止检查（ShutdownCoordinator 的第 1 步）"""
    env.maintenance.next_quick_check = FakeQuickCheck(block=True)
    guard, rec = env.make(quick_check_delay_s=0)
    guard.start()
    guard.tick()
    qc = env.maintenance.quick_checks[0]
    assert qc.started.wait(3)
    env.registry.cancel_all()
    assert env.registry.shutdown_all(deadline=time.monotonic() + 5) is True
    assert qc.interrupted_at is not None


def test_stop_cancels_a_pending_wal_job_token(env):
    guard, _ = env.make()
    env.maintenance.wal = 100 * MB
    env.maintenance.checkpoint_gate = threading.Event()
    guard.check_wal()
    worker = guard._wal_worker
    guard.stop()
    assert worker.is_cancelled
    env.maintenance.checkpoint_gate.set()
    process_events_until(lambda: not guard.wal_job_running, 5)


# ========== 与真实维护服务、真实库的联动 ==========

class RealManagerStub:
    """MaintenanceService 需要的维护锁接口（真实 MonitorManager 的子集）"""

    def __init__(self):
        self.maintenance = False
        self.running = False
        self.exports = False

    def is_maintenance_running(self):
        return self.maintenance

    def begin_maintenance(self):
        if self.running or self.exports:
            return False
        self.maintenance = True
        return True

    def end_maintenance(self):
        self.maintenance = False

    def has_running_tasks(self):
        return self.running

    def has_running_exports(self):
        return self.exports


def test_storage_job_blocks_maintenance_and_maintenance_blocks_job(db):
    manager = RealManagerStub()
    service = MaintenanceService(db, manager)
    assert service.begin_storage_job() is True
    with pytest.raises(MaintenanceBusy) as info:
        service.begin()
    assert info.value.reason == BUSY_STORAGE_JOB_RUNNING
    assert manager.maintenance is False          # 被拒时不留下半截维护锁
    service.end_storage_job()
    service.begin()                              # 作业结束后维护可以开始
    assert service.begin_storage_job() is False  # 维护期间登记被拒（守卫据此跳过）
    assert service.is_busy()
    service.end()
    assert service.begin_storage_job() is True
    service.end_storage_job()
    service.end_storage_job()                    # 多余的注销不会让计数变负
    assert not service.has_storage_job()


def test_real_guard_end_to_end_checkpoints_a_big_wal(qapp, db):
    """真实库、真实维护服务：-wal 超过（缩小的）阈值时守卫真的把它截回 0"""
    from data.models import DataPoint, MonitorTask
    from datetime import datetime, timedelta
    db.tasks.save(MonitorTask(task_id="t", pid=1, process_name="p", metric_types=["memory_private"],
                              interval=1.0, note="", start_time=datetime(2026, 11, 1),
                              end_time=None, status="running"))
    db.points.append([DataPoint("t", datetime(2026, 11, 1, 0, 0, 1) + timedelta(seconds=i),
                                float(i), "memory_private") for i in range(2000)])
    manager = RealManagerStub()
    manager.list_task_infos = lambda: []
    service = MaintenanceService(db, manager)
    registry = ThreadRegistry()
    guard = StorageGuard(manager, service, registry, db.db_path, wal_limit_bytes=1024,
                         quick_check_delay_s=None, disk_usage=Disk())
    try:
        assert service.wal_size_bytes() > 1024
        assert guard.check_wal() is True
        assert process_events_until(lambda: not guard.wal_job_running, 5)
        assert service.wal_size_bytes() == 0
        assert not service.has_storage_job()
    finally:
        guard.stop()
        registry.shutdown_all()


def test_real_guard_end_to_end_flags_a_corrupt_database(qapp, tmp_path):
    from data.database import Database
    from data.models import DataPoint, MonitorTask
    from datetime import datetime, timedelta
    path = str(tmp_path / "bad.db")
    db = Database(path)
    db.tasks.save(MonitorTask(task_id="t", pid=1, process_name="p", metric_types=["memory_private"],
                              interval=1.0, note="", start_time=datetime(2026, 11, 1),
                              end_time=None, status="running"))
    for k in range(40):
        db.points.append([DataPoint("t", datetime(2026, 11, 1) + timedelta(seconds=k * 500 + i),
                                    float(i), "memory_private") for i in range(500)])
    db.checkpoint_wal()
    db.close()
    size = os.path.getsize(path)
    with open(path, 'r+b') as f:
        f.seek(size // 2 // 4096 * 4096)
        f.write(b'\xde\xad\xbe\xef' * 1024)
    db = Database(path)
    manager = RealManagerStub()
    manager.list_task_infos = lambda: []
    service = MaintenanceService(db, manager)
    registry = ThreadRegistry()
    guard = StorageGuard(manager, service, registry, path, quick_check_delay_s=0,
                         disk_usage=Disk(), clock=Clock())
    rec = Recorder(guard)
    try:
        guard.start()
        guard.tick()
        assert process_events_until(lambda: rec.qc_done, 10)
        assert len(rec.integrity) == 1 and rec.integrity[0] not in ("", "ok")
    finally:
        guard.stop()
        registry.shutdown_all()
        db.close()


def test_guard_pauses_real_manager_tasks_through_public_api(qapp, db):
    """真实 MonitorManager：守卫经 list_task_infos / pause_task 暂停运行中的任务，
    任务的 is_paused 变真；回升后任务仍处于暂停（不自动恢复）；用户 resume 后守卫不再认领"""
    from core.monitor_manager import MonitorManager
    from tests.helpers import stop_and_join
    manager = MonitorManager(db=db)
    task_id = manager.create_task(os.getpid(), "pytest.exe", ["memory_rss"], 0.1)
    assert task_id is not None
    manager.start_task(task_id)
    assert poll_until(lambda: manager.is_task_active(task_id), 3)
    service = MaintenanceService(db, manager)
    disk = Disk(100 * MB)
    guard = StorageGuard(manager, service, ThreadRegistry(), db.db_path, disk_usage=disk,
                         quick_check_delay_s=None, clock=Clock())
    rec = Recorder(guard)
    try:
        guard.check_disk()
        assert manager.is_task_paused(task_id)
        assert guard.paused_task_ids() == frozenset({task_id})
        assert rec.critical[0][1] == (task_id,)
        disk.free = 2 * GB
        guard.check_disk()
        assert len(rec.recovered) == 1
        assert manager.is_task_paused(task_id)             # 不自动恢复
        assert manager.resume_task(task_id)
        assert guard.paused_task_ids() == frozenset()
    finally:
        guard.stop()
        stop_and_join(manager)


def test_signals_carry_free_space_above_2gb_without_overflow(env):
    """剩余空间的字节数经 object 传递：pyqtSignal(int) 会把超过 2 GB 的值溢出成负数"""
    guard, rec = env.make()
    _check(env, guard, 150 * MB)
    _check(env, guard, 50 * GB)
    assert rec.recovered == [50 * GB]
