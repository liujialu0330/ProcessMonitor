"""
会话锁与工具重启后的续采

覆盖：
- session.lock：无锁 / 本进程的锁 = CLEAN；残留且进程已死 / PID 被复用 / 锁损坏 = ABNORMAL；
  进程还活着（另一个实例）= OTHER_INSTANCE，不覆盖、不删它的锁；release 只删自己的；
- 找出可续采的任务：被测进程活着且创建时间一致才算；按名任务总是候选；
- 续采决定：选中的沿用原 task_id 继续采集（recover 打点、旧数据保留、开始时间不变），放弃的
  按孤儿任务收尾；对话框默认全选；无窗口模式不弹窗，缺省收尾、--resume-tasks 全部续采；
- 经 build_context 的端到端：异常退出后的启动不会把仍在运行的目标的任务收掉；
- 被强杀后最多丢失一个周期的数据（SAVE_BATCH_SIZE=1，每周期落库）。
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

import psutil
import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget

from app import bootstrap
from app.bootstrap import RESUME_ARG, build_context, release_session_lock, run_recovery
from core.exit_forensics import NullExitCodeProbe
from core.session_lock import (
    LOCK_FILE_NAME, LockInfo, PreviousSession, ProbeIndeterminate, SessionLock, probe_process,
)
from core.session_recovery import (
    RecoveryCandidate, apply_decision, close_all, find_recoverable,
)
from data.database import Database
from data.models import DataPoint, MonitorTask
from data.repos.point_repo import PointQuery
from tests.soak_doubles import METRICS, SegmentCollector, StepClock

METRIC = 'memory_private'
NOW = datetime(2026, 11, 6, 9, 0, 0)


class FakeProcs:
    """假进程表：pid -> (创建时间, 名称)；可作 SessionLock / find_recoverable 的 probe。"""

    def __init__(self, table=None):
        self.table = dict(table or {})

    def __call__(self, pid):
        return self.table.get(pid)


def _write_lock(directory, pid, create_time=111.0, exe='procmon.exe', version='2.0.0'):
    info = LockInfo(pid=pid, started_at='2026-11-06 08:00:00', version=version,
                    create_time=create_time, exe_name=exe)
    path = os.path.join(directory, LOCK_FILE_NAME)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(info.to_json())
    return path


# ========== 会话锁 ==========

def test_no_lock_is_clean(tmp_path):
    lock = SessionLock(str(tmp_path), pid=10, probe=FakeProcs())
    assert lock.check_previous() is PreviousSession.CLEAN


def test_acquire_writes_pid_start_time_and_version(tmp_path):
    procs = FakeProcs({10: (500.0, 'procmon.exe')})
    lock = SessionLock(str(tmp_path), pid=10, probe=procs, now=lambda: NOW, version='2.0.0')
    assert lock.acquire() is True
    data = json.loads((tmp_path / LOCK_FILE_NAME).read_text(encoding='utf-8'))
    assert data['pid'] == 10
    assert data['started_at'] == '2026-11-06 09:00:00'
    assert data['version'] == '2.0.0'
    assert data['create_time'] == 500.0
    assert lock.owned


def test_release_deletes_own_lock_and_is_repeatable(tmp_path):
    lock = SessionLock(str(tmp_path), pid=10, probe=FakeProcs())
    lock.acquire()
    assert lock.release() is True
    assert not (tmp_path / LOCK_FILE_NAME).exists()
    assert lock.release() is False


def test_stale_lock_with_dead_pid_is_abnormal_exit(tmp_path):
    _write_lock(str(tmp_path), pid=777)
    lock = SessionLock(str(tmp_path), pid=10, probe=FakeProcs())      # 777 不在进程表里
    assert lock.check_previous() is PreviousSession.ABNORMAL


def test_live_pid_is_other_instance_not_abnormal(tmp_path):
    """记录的进程仍是本程序（创建时间一致）：是另一个实例，不能判为异常退出"""
    _write_lock(str(tmp_path), pid=777, create_time=111.0)
    procs = FakeProcs({777: (111.5, 'procmon.exe')})
    lock = SessionLock(str(tmp_path), pid=10, probe=procs)
    assert lock.check_previous() is PreviousSession.OTHER_INSTANCE


def test_other_instance_lock_is_neither_overwritten_nor_deleted(tmp_path):
    path = _write_lock(str(tmp_path), pid=777, create_time=111.0)
    before = open(path, encoding='utf-8').read()
    procs = FakeProcs({777: (111.0, 'procmon.exe')})
    lock = SessionLock(str(tmp_path), pid=10, probe=procs)
    assert lock.acquire() is False
    assert lock.release() is False
    assert open(path, encoding='utf-8').read() == before
    assert not lock.owned


def test_reused_pid_with_different_create_time_is_abnormal(tmp_path):
    """PID 被别的程序占用（创建时间对不上）：记录的实例早已不在"""
    _write_lock(str(tmp_path), pid=777, create_time=111.0)
    procs = FakeProcs({777: (99999.0, 'chrome.exe')})
    lock = SessionLock(str(tmp_path), pid=10, probe=procs)
    assert lock.check_previous() is PreviousSession.ABNORMAL


def test_without_create_time_falls_back_to_exe_name(tmp_path):
    _write_lock(str(tmp_path), pid=777, create_time=None, exe='procmon.exe')
    same = SessionLock(str(tmp_path), pid=10, probe=FakeProcs({777: (None, 'ProcMon.exe')}))
    assert same.check_previous() is PreviousSession.OTHER_INSTANCE
    other = SessionLock(str(tmp_path), pid=10, probe=FakeProcs({777: (None, 'chrome.exe')}))
    assert other.check_previous() is PreviousSession.ABNORMAL


def test_corrupt_lock_is_abnormal_and_replaced_on_acquire(tmp_path):
    (tmp_path / LOCK_FILE_NAME).write_text("{不是 json", encoding='utf-8')
    lock = SessionLock(str(tmp_path), pid=10, probe=FakeProcs())
    assert lock.check_previous() is PreviousSession.ABNORMAL
    assert lock.acquire() is True
    assert json.loads((tmp_path / LOCK_FILE_NAME).read_text(encoding='utf-8'))['pid'] == 10


def test_lock_written_by_this_process_is_not_previous_session(tmp_path):
    _write_lock(str(tmp_path), pid=10)
    assert SessionLock(str(tmp_path), pid=10, probe=FakeProcs()).check_previous() \
        is PreviousSession.CLEAN


def test_acquire_failure_is_logged_not_raised(tmp_path):
    missing = tmp_path / "不存在的目录"
    lock = SessionLock(str(missing), pid=10, probe=FakeProcs())
    assert lock.acquire() is False
    assert not lock.owned


def test_release_removes_own_corrupt_lock(tmp_path):
    lock = SessionLock(str(tmp_path), pid=10, probe=FakeProcs())
    lock.acquire()
    (tmp_path / LOCK_FILE_NAME).write_text("坏了", encoding='utf-8')
    assert lock.release() is True
    assert not (tmp_path / LOCK_FILE_NAME).exists()


class _IndeterminateProbe:
    """探测不了记录的进程（如权限不足）"""

    def __call__(self, pid):
        raise ProbeIndeterminate("拒绝访问")


def test_unconfirmable_process_is_unknown_not_abnormal(tmp_path):
    """探测记录的进程时权限不足：无法确认它在不在，既不是异常退出，也不是另一个实例"""
    _write_lock(str(tmp_path), pid=777)
    lock = SessionLock(str(tmp_path), pid=10, probe=_IndeterminateProbe())
    assert lock.check_previous() is PreviousSession.UNKNOWN


def test_unknown_previous_session_lock_is_not_overwritten_or_deleted(tmp_path):
    path = _write_lock(str(tmp_path), pid=777)
    before = open(path, encoding='utf-8').read()
    lock = SessionLock(str(tmp_path), pid=10, probe=_IndeterminateProbe())
    assert lock.acquire() is False
    assert lock.release() is False
    assert open(path, encoding='utf-8').read() == before
    assert not lock.owned


def test_probe_process_maps_access_denied_to_indeterminate(monkeypatch):
    def denied(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(psutil, 'Process', denied)
    with pytest.raises(ProbeIndeterminate):
        probe_process(4321)


def test_probe_process_still_maps_no_such_process_to_none(monkeypatch):
    def gone(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, 'Process', gone)
    assert probe_process(4321) is None


def test_real_probe_sees_this_process_and_not_a_dead_one():
    found = probe_process(os.getpid())
    assert found is not None
    assert abs(found[0] - psutil.Process().create_time()) < 0.01
    assert probe_process(dead_pid()) is None


# ========== 辅助：真实子进程 ==========

def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait(timeout=30)
    return child.pid


@pytest.fixture
def live_child():
    """一个存活的真实进程（用例结束一定回收）：作为"仍在运行的被测进程"。"""
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        yield child
    finally:
        child.kill()
        child.wait(timeout=10)


def _running_task(db, *, pid, create_time, task_id='t-running', samples=5, kind='pid',
                  spec=None, name='app.exe'):
    start = NOW - timedelta(minutes=10)
    task = MonitorTask(task_id=task_id, pid=pid, process_name=name, metric_types=[METRIC],
                       interval=1.0, start_time=start, end_time=None, status='running',
                       create_time=create_time, target_kind=kind, target_spec=spec)
    db.save_task(task)
    db.save_data_points([DataPoint(task_id, start + timedelta(seconds=i), 1000.0 + i, METRIC)
                         for i in range(samples)])
    return task


# ========== 找可续采的任务 ==========

def test_find_recoverable_requires_alive_target_with_same_create_time(db):
    _running_task(db, pid=11, create_time=100.0, task_id='alive')
    _running_task(db, pid=12, create_time=100.0, task_id='reused')
    _running_task(db, pid=13, create_time=100.0, task_id='dead')
    _running_task(db, pid=14, create_time=None, task_id='unknown-ct')
    procs = FakeProcs({11: (100.004, 'app.exe'), 12: (555.0, 'other.exe'),
                       14: (200.0, 'app.exe')})
    found = find_recoverable(db, probe=procs)
    assert sorted(c.task_id for c in found) == ['alive', 'unknown-ct']


def test_find_recoverable_compares_process_name_when_create_time_is_unknown(db):
    """手动输入 PID 的任务没有创建时间：PID 被别的程序占用时进程名对不上，不能续采；
    名字一致（不区分大小写）才算候选"""
    _running_task(db, pid=11, create_time=None, task_id='same-name', name='App.exe')
    _running_task(db, pid=12, create_time=None, task_id='other-program', name='app.exe')
    procs = FakeProcs({11: (200.0, 'app.EXE'), 12: (200.0, 'chrome.exe')})
    assert [c.task_id for c in find_recoverable(db, probe=procs)] == ['same-name']


def test_find_recoverable_rejects_different_name_even_with_matching_create_time(db):
    _running_task(db, pid=11, create_time=100.0, task_id='renamed')
    assert find_recoverable(db, probe=FakeProcs({11: (100.0, 'other.exe')})) == []


def test_find_recoverable_skips_target_that_cannot_be_confirmed(db):
    _running_task(db, pid=11, create_time=100.0, task_id='denied')
    assert find_recoverable(db, probe=_IndeterminateProbe()) == []


def test_find_recoverable_name_task_is_always_candidate(db):
    _running_task(db, pid=0, create_time=None, task_id='by-name', kind='name',
                  spec=json.dumps({'name': 'app.exe'}))
    found = find_recoverable(db, probe=FakeProcs())
    assert [c.task_id for c in found] == ['by-name']
    assert found[0].target_kind == 'name'


def test_find_recoverable_ignores_stopped_tasks(db):
    task = _running_task(db, pid=11, create_time=100.0, task_id='done')
    db.update_task_status(task.task_id, 'stopped', NOW)
    assert find_recoverable(db, probe=FakeProcs({11: (100.0, 'app.exe')})) == []


# ========== 经 build_context 的端到端 ==========

@pytest.fixture
def paths(app_paths):
    app_paths.ensure_dirs()
    return app_paths


def _seed_running(paths, pid, create_time, task_id='t-live', **options):
    # 任务记的进程名要与真实的被测进程一致：续采会核对名字
    try:
        options.setdefault('name', psutil.Process(pid).name())
    except psutil.Error:
        pass
    seed = Database(paths.db_path)
    _running_task(seed, pid=pid, create_time=create_time, task_id=task_id, **options)
    seed.close()


def _install_fake_collector(ctx, after=3):
    """让续采出来的线程用假时钟与假采集器：采满 after 次后停止任务。"""
    clock = StepClock(start=NOW)

    def factory(pid, _spec):
        def on_collect(n):
            if n == after:
                for worker in ctx.manager.get_all_tasks():
                    worker.stop()
        return SegmentCollector(METRICS[:1], [7.0] * (after + 5), on_collect=on_collect)

    ctx.manager.set_worker_options(clock=clock, collector_factory=factory)


def _finish(ctx):
    assert ctx.manager.join_all(timeout_ms=20000)


def test_abnormal_exit_keeps_live_target_task_for_recovery(qapp, paths, live_child):
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=dead_pid())

    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.previous_session is PreviousSession.ABNORMAL
        assert [c.task_id for c in ctx.recovery_candidates] == ['t-live']
        # 受保护：启动校正没有把它收掉
        assert ctx.db.get_task('t-live').status == 'running'
        assert ctx.reconcile.orphans == 0
        # 新实例已写入自己的锁
        info = SessionLock(paths.data_dir).read()
        assert info.pid == os.getpid()
    finally:
        release_session_lock(ctx)
        ctx.db.close()
    assert not os.path.exists(os.path.join(paths.data_dir, LOCK_FILE_NAME))


def test_abnormal_exit_with_dead_target_still_reconciles(qapp, paths):
    _seed_running(paths, dead_pid(), 100.0)
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.recovery_candidates == ()
        assert ctx.db.get_task('t-live').status == 'stopped'
        assert ctx.reconcile.orphans == 1
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_clean_previous_session_never_offers_recovery(qapp, paths, live_child):
    """没有残留的锁（上次正常退出）：即便库里有 running 任务也只是照常校正，不弹续采"""
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.previous_session is PreviousSession.CLEAN
        assert ctx.recovery_candidates == ()
        assert ctx.db.get_task('t-live').status == 'stopped'
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_other_instance_lock_is_not_abnormal_and_survives(qapp, paths, live_child):
    ct = psutil.Process(live_child.pid).create_time()
    path = _write_lock(paths.data_dir, pid=live_child.pid, create_time=ct,
                       exe=psutil.Process(live_child.pid).name())
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.previous_session is PreviousSession.OTHER_INSTANCE
        assert ctx.recovery_candidates == ()
        assert json.loads(open(path, encoding='utf-8').read())['pid'] == live_child.pid
    finally:
        release_session_lock(ctx)       # 不是自己的锁：不删
        ctx.db.close()
    assert os.path.exists(path)


def test_other_instance_running_tasks_are_not_reconciled_away(qapp, paths, live_child):
    """另一个实例仍在运行时，库里"运行中"的任务是它正在跑的：启动校正会把它们收尾成
    "已停止"并清理数据，必须整个跳过"""
    ct = psutil.Process(live_child.pid).create_time()
    name = psutil.Process(live_child.pid).name()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=live_child.pid, create_time=ct, exe=name)
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.previous_session is PreviousSession.OTHER_INSTANCE
        assert ctx.db.get_task('t-live').status == 'running'
        assert (ctx.reconcile.orphans, ctx.reconcile.recounted, ctx.reconcile.cleaned) == (0, 0, 0)
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_unknown_previous_session_skips_startup_reconcile_too(qapp, paths, live_child,
                                                              monkeypatch, caplog):
    """探测记录的进程时权限不足（无法排除它还在运行，如上一个实例以管理员身份运行）：同
    OTHER_INSTANCE 一样整个跳过启动校正，不把库里"运行中"的任务收尾成已停止；记 WARNING"""
    import logging
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    path = _write_lock(paths.data_dir, pid=dead_pid())
    monkeypatch.setattr(SessionLock, 'check_previous', lambda self: PreviousSession.UNKNOWN)
    with caplog.at_level(logging.WARNING):
        ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.previous_session is PreviousSession.UNKNOWN
        assert ctx.recovery_candidates == ()
        assert ctx.db.get_task('t-live').status == 'running'
        assert (ctx.reconcile.orphans, ctx.reconcile.recounted, ctx.reconcile.cleaned) == (0, 0, 0)
        assert any("无法确认是否仍在运行" in r.getMessage() and "跳过启动校正" in r.getMessage()
                   for r in caplog.records if r.levelno == logging.WARNING)
        assert os.path.exists(path)         # 它的锁没被覆盖
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_abnormal_previous_session_still_reconciles_for_contrast(qapp, paths, live_child):
    """对照：异常退出（锁里的进程已不在）照常校正，不留"运行中"的孤儿"""
    _seed_running(paths, live_child.pid, None, name='nothing-else.exe')
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.previous_session is PreviousSession.ABNORMAL
        assert ctx.recovery_candidates == ()
        assert ctx.db.get_task('t-live').status == 'stopped'
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_dialog_continue_path_resumes_same_task(qapp, paths, live_child):
    """用户选"继续监控"：沿用 task_id 与开始时间，旧数据保留，写 recover 打点，继续采集"""
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    asked = []
    try:
        _install_fake_collector(ctx, after=3)
        start_before = ctx.db.get_task('t-live').start_time

        def prompt(candidates):
            asked.append([c.task_id for c in candidates])
            return [c.task_id for c in candidates]

        outcome = run_recovery(ctx, prompt=prompt)
        assert asked == [['t-live']]
        assert outcome.resumed == ('t-live',) and outcome.abandoned == ()
        assert ctx.recovery_candidates == ()
        _finish(ctx)

        task = ctx.db.get_task('t-live')
        assert task.task_id == 't-live'
        assert task.start_time == start_before
        points = ctx.db.points.fetch(PointQuery('t-live', METRIC))
        assert len(points) == 5 + 3                       # 旧的 5 个 + 续采后的 3 个
        recover = [m for m in ctx.db.markers.list('t-live') if m.kind == 'recover']
        assert len(recover) == 1 and recover[0].label == "工具重启后继续监控"
        # 再次处理不会重复执行
        assert run_recovery(ctx, prompt=prompt) is None
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_dialog_abandon_path_closes_the_task(qapp, paths, live_child):
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    try:
        outcome = run_recovery(ctx, prompt=lambda candidates: [])
        assert outcome.abandoned == ('t-live',) and outcome.resumed == ()
        task = ctx.db.get_task('t-live')
        assert task.status == 'stopped'
        # 结束时间取最后一次采样，不是"现在"
        assert task.end_time == task.last_sample_at
        assert [m for m in ctx.db.markers.list('t-live') if m.kind == 'recover'] == []
        assert not ctx.manager.get_all_tasks()
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_prompt_error_is_treated_as_abandon(qapp, paths, live_child, caplog):
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    try:
        def boom(_candidates):
            raise RuntimeError("对话框炸了")

        outcome = run_recovery(ctx, prompt=boom)
        assert outcome.abandoned == ('t-live',)
        assert ctx.db.get_task('t-live').status == 'stopped'
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_headless_without_prompt_closes_tasks_and_logs(qapp, paths, live_child, caplog):
    """无窗口模式：不弹窗（没有 prompt），任务照常收尾并记一行日志"""
    caplog.set_level(logging.INFO)
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    try:
        outcome = run_recovery(ctx, prompt=None)
        assert outcome.abandoned == ('t-live',)
        assert ctx.db.get_task('t-live').status == 'stopped'
        assert not ctx.manager.get_all_tasks()
        assert any("当前无法询问" in r.getMessage() and RESUME_ARG in r.getMessage()
                   for r in caplog.records)
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_resume_all_flag_resumes_without_prompt(qapp, paths, live_child):
    ct = psutil.Process(live_child.pid).create_time()
    _seed_running(paths, live_child.pid, ct)
    _write_lock(paths.data_dir, pid=dead_pid())
    ctx = build_context(paths, retention_days=0)
    try:
        _install_fake_collector(ctx, after=2)
        outcome = run_recovery(ctx, prompt=None, resume_all=True)
        assert outcome.resumed == ('t-live',)
        _finish(ctx)
        assert ctx.db.get_task('t-live').status == 'stopped'
        assert [m.kind for m in ctx.db.markers.list('t-live')] == ['recover']
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_resume_arg_is_stripped_from_qt_args():
    argv = ['prog', RESUME_ARG, '--background', '-style', 'fusion']
    assert bootstrap.strip_background_args(argv) == ['prog', '-style', 'fusion']


def test_recovery_with_no_candidates_does_nothing(qapp, paths):
    ctx = build_context(paths, retention_days=0)
    try:
        assert run_recovery(ctx, prompt=lambda c: pytest.fail("不该询问")) is None
    finally:
        release_session_lock(ctx)
        ctx.db.close()


def test_release_session_lock_swallows_errors(qapp, paths):
    ctx = build_context(paths, retention_days=0)
    try:
        class Broken:
            def release(self):
                raise OSError("磁盘错误")

        ctx.session_lock = Broken()
        release_session_lock(ctx)      # 不抛
    finally:
        ctx.db.close()


# ========== apply_decision 单测（假管理器）==========

class FakeManager:
    def __init__(self, results):
        self.results = dict(results)
        self.calls = []

    def recover_task(self, task_id):
        self.calls.append(task_id)
        outcome = self.results[task_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _cand(task_id):
    return RecoveryCandidate(task_id=task_id, process_name='a.exe', pid=1, target_kind='pid',
                             start_time=NOW, last_sample_at=NOW)


def test_apply_decision_splits_resumed_abandoned_and_failed(db):
    for tid in ('a', 'b', 'c', 'd'):
        _running_task(db, pid=1, create_time=1.0, task_id=tid)
    manager = FakeManager({'a': True, 'b': False, 'c': RuntimeError("炸了")})
    candidates = [_cand('a'), _cand('b'), _cand('c'), _cand('d')]
    outcome = apply_decision(manager, db, candidates, ['a', 'b', 'c', 'not-a-candidate'])
    assert (outcome.resumed, outcome.failed, outcome.abandoned) == (('a',), ('b', 'c'), ('d',))
    assert manager.calls == ['a', 'b', 'c']
    assert db.get_task('a').status == 'running'       # 续采成功的不动
    for tid in ('b', 'c', 'd'):
        assert db.get_task(tid).status == 'stopped'    # 失败与放弃的按孤儿收尾


def test_close_all_closes_every_candidate(db):
    for tid in ('a', 'b'):
        _running_task(db, pid=1, create_time=1.0, task_id=tid)
    outcome = close_all(db, [_cand('a'), _cand('b')])
    assert outcome.abandoned == ('a', 'b')
    assert {db.get_task(t).status for t in ('a', 'b')} == {'stopped'}


def _plan_marker(events):
    class Marker:
        def attach(self, task_id, plan, started_at=None):
            events.append(('attach', task_id, plan.timed_marker_seconds, started_at))
    return Marker()


def test_apply_decision_attaches_plan_only_after_recovery_succeeds(db):
    """方案在续采成功之后才挂（先挂的话，续采失败的任务会留着一份没人解除的跟踪），
    且停止时长从任务原来的开始时间算——工具停摆的那段时间也算在内"""
    from core.automation.plan import AutomationPlan
    for tid in ('a', 'b'):
        _running_task(db, pid=1, create_time=1.0, task_id=tid)
        db.tasks.save_plan(tid, AutomationPlan(timed_marker_seconds=60).to_json())
    events = []

    class Manager(FakeManager):
        def recover_task(self, task_id):
            events.append(('recover', task_id))
            return super().recover_task(task_id)

    apply_decision(Manager({'a': True, 'b': False}), db, [_cand('a'), _cand('b')], ['a', 'b'],
                   auto_marker=_plan_marker(events))
    assert events == [('recover', 'a'), ('attach', 'a', 60, NOW), ('recover', 'b')]


# ========== 对话框 ==========

def test_recover_dialog_selects_all_by_default_and_follows_checks(qapp):
    from ui.dialogs.recover_dialog import RecoverDialog, candidate_text
    host = QWidget()
    host.resize(800, 600)
    dialog = RecoverDialog([_cand('a'), _cand('b'), _cand('c')], host)
    try:
        assert dialog.selected_task_ids() == ['a', 'b', 'c']
        dialog.list_widget.item(1).setCheckState(Qt.Unchecked)
        assert dialog.selected_task_ids() == ['a', 'c']
        assert dialog.yesButton.text() == "继续监控"
        assert dialog.cancelButton.text() == "全部放弃"
        for row in range(3):
            dialog.list_widget.item(row).setCheckState(Qt.Unchecked)
        assert dialog.selected_task_ids() == []
        assert not dialog.yesButton.isEnabled()
        assert 'a.exe' in candidate_text(_cand('a')) and 'PID 1' in candidate_text(_cand('a'))
    finally:
        dialog.deleteLater()
        host.deleteLater()


def test_ask_recover_returns_selection_or_empty(qapp, monkeypatch):
    from ui.dialogs import recover_dialog
    from ui.dialogs.recover_dialog import RecoverDialog, ask_recover

    host = QWidget()
    host.resize(800, 600)
    monkeypatch.setattr(RecoverDialog, 'exec', lambda self: True)
    assert ask_recover([_cand('a'), _cand('b')], host) == ['a', 'b']
    monkeypatch.setattr(RecoverDialog, 'exec', lambda self: False)
    assert ask_recover([_cand('a')], host) == []
    assert callable(recover_dialog.default_prompt(host))
    host.deleteLater()


# ========== 被强杀后最多丢一个周期 ==========

def test_at_most_one_cycle_is_lost_when_killed(db):
    """SAVE_BATCH_SIZE=1：每个周期采到就落库。第 n 次采集发生时，前 n-1 个周期必须已经在库里，
    也就是工具在任意时刻被强杀，最多丢失正在进行的那一个周期。"""
    import config
    from core.monitor_task import MonitorWorker

    assert config.SAVE_BATCH_SIZE == 1
    clock = StepClock(start=NOW)
    seen = []

    def on_collect(n):
        # on_collect 在本周期的数据点入缓冲之前调用：此刻库里应已有前 n-1 个周期
        seen.append((n, len(db.points.fetch(PointQuery(worker.task_id, METRIC)))))
        if n == 12:
            worker.stop()

    collector = SegmentCollector([METRIC], [1.0] * 20, on_collect=on_collect)
    worker = MonitorWorker(pid=os.getpid(), process_name='x.exe', metric_types=[METRIC],
                           interval=1.0, db=db, clock=clock,
                           collector_factory=lambda pid, spec: collector,
                           exit_probe_factory=lambda pid: NullExitCodeProbe())
    worker.start()
    assert worker.wait(20000)
    assert seen == [(n, n - 1) for n in range(1, 13)]
