"""任务元数据写失败与后台单任务删除的线程、互斥和回滚契约。"""
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QTimer

from core.maintenance_service import BUSY_DELETION_RUNNING, MaintenanceBusy, MaintenanceService
from core.automation.plan import AutomationPlan
from core.contracts import StopReason
from core.ipc.commands import CommandContext, CommandDispatcher
from core.monitor_manager import MonitorManager
from core.monitor_task import WorkerState
from core.shutdown import ShutdownCoordinator, ThreadRegistry
from core.storage_guard import MB, StorageGuard
from core.target_info import TargetInfo
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from tests.helpers import process_events_until


def seed(db, task_id='stopped', *, status='stopped'):
    when = datetime(2026, 11, 13)
    task = MonitorTask(task_id, 123, 'fixture.exe', ['memory_private'], 1, when,
                       when + timedelta(seconds=2), status, scenario='旧场景',
                       build='旧版本', group_id='old-group')
    db.tasks.save(task)
    db.points.append([DataPoint(task_id, when, 1, 'memory_private')])
    return task


def test_identity_write_failure_keeps_memory_and_retry_state(db, qapp, monkeypatch):
    task = seed(db, status='running')
    manager = MonitorManager(db)
    manager._tasks[task.task_id] = SimpleNamespace(task_model=task)
    original = db.tasks.update_identity
    def fail(*_):
        raise StorageError('数据库被占用')
    monkeypatch.setattr(db.tasks, 'update_identity', fail)
    try:
        with pytest.raises(StorageError):
            manager.update_task_identity(task.task_id, '新场景', '新版')
        assert (task.scenario, task.build) == ('旧场景', '旧版本')
        assert manager._pending_identity == {}
        assert db.tasks.get(task.task_id).scenario == '旧场景'
        monkeypatch.setattr(db.tasks, 'update_identity', original)
        assert manager.update_task_identity(task.task_id, '新场景', '新版')
        assert task.scenario == db.tasks.get(task.task_id).scenario == '新场景'
    finally:
        manager._tasks.clear()


def test_cancel_after_final_delete_rolls_back_entire_transaction(db, monkeypatch):
    import data.repos.task_repo as task_repo

    for task_id in ('one', 'two'):
        seed(db, task_id)
    real_delete = task_repo.delete_task_rows
    cancelled = False

    def delete(cursor, task_id):
        nonlocal cancelled
        real_delete(cursor, task_id)
        if task_id == 'two':
            cancelled = True

    monkeypatch.setattr(task_repo, 'delete_task_rows', delete)
    with pytest.raises(ValueError, match='已取消删除'):
        db.tasks.delete_many_stopped(['one', 'two'], cancelled=lambda: cancelled)
    assert all(db.tasks.get(task_id) is not None for task_id in ('one', 'two'))
    assert all(db.tasks.get(task_id).sample_count == 1 for task_id in ('one', 'two'))


def test_group_failure_is_atomic_for_all_models_and_rows(db, qapp, monkeypatch):
    tasks = [seed(db, key, status='running') for key in ('one', 'two')]
    manager = MonitorManager(db)
    manager._tasks.update({task.task_id: SimpleNamespace(task_model=task) for task in tasks})
    original = db.tasks.set_group
    def fail(*_):
        raise StorageError('数据库被占用')
    monkeypatch.setattr(db.tasks, 'set_group', fail)
    try:
        with pytest.raises(StorageError):
            manager.set_task_group(['one', 'two'], 'new-group')
        assert all(task.group_id == 'old-group' for task in tasks)
        assert manager._pending_groups == {}
        assert all(db.tasks.get(task.task_id).group_id == 'old-group' for task in tasks)
        monkeypatch.setattr(db.tasks, 'set_group', original)
        manager.set_task_group(['one', 'two'], 'new-group')
        assert all(task.group_id == db.tasks.get(task.task_id).group_id == 'new-group'
                   for task in tasks)
    finally:
        manager._tasks.clear()


def test_unpersisted_task_identity_and_group_are_retried(db, qapp):
    task = seed(db)
    db.tasks.delete(task.task_id)
    manager = MonitorManager(db)
    manager._tasks[task.task_id] = SimpleNamespace(task_model=task)
    try:
        assert manager.update_task_identity(task.task_id, '新场景', '新版')
        manager.set_task_group([task.task_id], 'new-group')
        assert manager._pending_identity and manager._pending_groups
        db.tasks.save(task)
        manager._save_pending_task_settings(task.task_id)
        saved = db.tasks.get(task.task_id)
        assert (saved.scenario, saved.build, saved.group_id) == ('新场景', '新版', 'new-group')
        assert not manager._pending_identity and not manager._pending_groups
    finally:
        manager._tasks.clear()


@pytest.mark.parametrize('begin,end', [
    ('begin_maintenance', 'end_maintenance'), ('begin_export', 'end_export'),
    ('begin_storage_job', 'end_storage_job')])
def test_every_delete_entry_refuses_existing_storage_work(db, qapp, begin, end):
    task = seed(db)
    manager, registry = MonitorManager(db), ThreadRegistry()
    assert getattr(manager, begin)()
    try:
        assert not manager.delete_task_data(task.task_id)
        with pytest.raises(ValueError):
            manager.start_single_task_deletion(task.task_id, registry)
        assert db.tasks.get(task.task_id) is not None
        assert registry.entries() == []
    finally:
        getattr(manager, end)()


def test_single_delete_does_not_block_gui_or_unrelated_monitoring(db, qapp, monkeypatch):
    task = seed(db)
    manager, registry = MonitorManager(db), ThreadRegistry()
    entered, release = threading.Event(), threading.Event()
    thread_ids, signals = [], []
    original = db.tasks.delete_many_stopped
    def blocked(*args, **kwargs):
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(db.tasks, 'delete_many_stopped', blocked)
    unrelated = SimpleNamespace(state=WorkerState.WAITING, pid=456, target_kind='name')
    unrelated.start = lambda: setattr(unrelated, 'state', WorkerState.RUNNING)
    manager._tasks['other'] = unrelated
    try:
        manager.start_single_task_deletion(task.task_id, registry, on_result=signals.append)
        assert entered.wait(1)
        assert manager.has_task_deletions()
        assert not manager.begin_maintenance()
        assert not manager.begin_export()
        assert not manager.begin_storage_job()
        with pytest.raises(MaintenanceBusy) as error:
            MaintenanceService(db, manager).begin()
        assert error.value.reason == BUSY_DELETION_RUNNING
        assert manager.start_task('other')
        with pytest.raises(ValueError):
            manager.update_task_identity(task.task_id, '被删除', '')
        assert manager.add_marker(task.task_id, '删除期间') is None
        delivered = []
        QTimer.singleShot(0, lambda: delivered.append(True))
        assert process_events_until(lambda: bool(delivered), 1)
        assert len(thread_ids) == 1 and thread_ids[0] != threading.get_ident()
        release.set()
        assert process_events_until(lambda: not manager.has_task_deletions() and not registry.entries())
        assert signals == [1]
        assert db.tasks.get(task.task_id) is None
        assert unrelated.state is WorkerState.RUNNING
        assert registry.entries() == []
    finally:
        release.set()
        registry.cancel_all()
        registry.shutdown_all()
        manager._tasks.clear()


@pytest.mark.parametrize('mode', ['cancel', 'failure', 'reference'])
def test_single_delete_failure_or_cancel_releases_lock_and_keeps_data(db, qapp, monkeypatch, mode):
    task = seed(db)
    manager, registry = MonitorManager(db), ThreadRegistry()
    entered, release = threading.Event(), threading.Event()
    original = db.tasks.delete_many_stopped
    if mode == 'reference':
        db.tasks.set_reference(task.task_id)
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        if mode == 'failure':
            raise StorageError('删除失败')
        return original(*args, **kwargs)
    monkeypatch.setattr(db.tasks, 'delete_many_stopped', blocked)
    failures, results = [], []
    try:
        worker = manager.start_single_task_deletion(task.task_id, registry,
                    on_result=results.append, on_failed=failures.append)
        assert entered.wait(1)
        if mode == 'cancel':
            registry.cancel_all()
            assert worker.is_cancelled
        release.set()
        assert process_events_until(lambda: not manager.has_task_deletions() and not registry.entries())
        assert not results
        assert bool(failures) is (mode != 'cancel')
        assert db.tasks.get(task.task_id) is not None
        assert registry.entries() == []
        assert manager.begin_maintenance()
        manager.end_maintenance()
    finally:
        release.set()
        registry.cancel_all()
        registry.shutdown_all()


def test_single_delete_worker_construction_failure_releases_target(db, qapp, monkeypatch):
    from core import task_library
    task = seed(db)
    manager, registry = MonitorManager(db), ThreadRegistry()
    def fail(*args, **kwargs):
        raise RuntimeError('线程资源不可用')
    monkeypatch.setattr(task_library, 'TaskDeletionWorker', fail)
    with pytest.raises(RuntimeError):
        manager.start_single_task_deletion(task.task_id, registry)
    assert not manager.has_task_deletions()
    assert registry.entries() == []
    assert db.tasks.get(task.task_id) is not None


def test_single_delete_confirmed_reference_is_deleted(db, qapp):
    task = seed(db)
    db.tasks.set_reference(task.task_id)
    manager, registry = MonitorManager(db), ThreadRegistry()
    results = []
    manager.start_single_task_deletion(task.task_id, registry,
                                      allow_reference=True, on_result=results.append)
    assert process_events_until(lambda: not manager.has_task_deletions() and not registry.entries())
    assert results == [1]
    assert db.tasks.reference_for('旧场景') is None


def test_single_delete_cannot_start_its_target_but_new_tasks_are_allowed(db, qapp, monkeypatch):
    task = seed(db)
    manager, registry = MonitorManager(db), ThreadRegistry()
    entered, release = threading.Event(), threading.Event()
    original = db.tasks.delete_many_stopped
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(db.tasks, 'delete_many_stopped', blocked)
    waiting = SimpleNamespace(state=WorkerState.WAITING)
    waiting.start = lambda: pytest.fail('删除目标不能启动')
    manager._tasks[task.task_id] = waiting
    try:
        manager.start_single_task_deletion(task.task_id, registry)
        assert entered.wait(1)
        assert not manager.start_task(task.task_id)
        assert not manager.recover_task(task.task_id)
        other = manager.create_task(456, 'other.exe', ['memory_private'], 1)
        assert other
        manager.remove_task(other)
        release.set()
        assert process_events_until(lambda: not registry.entries())
    finally:
        release.set()
        registry.cancel_all()
        registry.shutdown_all()
        manager._tasks.clear()


@contextmanager
def deletion_holding_write_lock(manager, db, monkeypatch, *, outcome='success'):
    """暂停在 DELETE 已执行、事务尚未提交的位置，实际持有 SQLite 写锁。"""
    import data.repos.task_repo as task_repo

    entered, release = threading.Event(), threading.Event()
    original = task_repo.delete_task_rows
    registry = ThreadRegistry()

    def blocked(cursor, task_id):
        original(cursor, task_id)
        entered.set()
        assert release.wait(5)
        if outcome == 'failure':
            raise StorageError('删除事务故障')

    with monkeypatch.context() as patch:
        patch.setattr(task_repo, 'delete_task_rows', blocked)
        worker = manager.start_single_task_deletion('stopped', registry)
        try:
            assert entered.wait(2)
            yield worker, release, registry
            if outcome == 'cancel':
                worker.cancel()
            release.set()
            assert process_events_until(lambda: not registry.entries(), 3)
        finally:
            release.set()
            registry.cancel_all()
            assert registry.shutdown_all()
            process_events_until(lambda: not registry.entries(), 2)


def make_registered_task(manager, db):
    task_id = manager.create_task(456, 'other.exe', ['memory_private'], 1)
    worker = manager._tasks[task_id]
    worker.task_model.start_time = datetime(2026, 11, 13)
    worker.task_model.status = 'running'
    db.tasks.save(worker.task_model)
    return task_id, worker


@pytest.mark.parametrize('outcome', ['success', 'cancel', 'failure'])
def test_real_delete_lock_defers_settings_and_keeps_them_after_task_removed(
        db, qapp, monkeypatch, outcome):
    seed(db)
    seed(db, 'historical')
    manager = MonitorManager(db)
    task_id, worker = make_registered_task(manager, db)
    baseline = manager.add_marker(task_id, '基线')
    plan = AutomationPlan(resume_on_restart=True, restart_limit=3)
    manager.seed_restart_policy(task_id, True, 3)
    delivered, durations, errors, check_errors = [], [], [], []
    manager.error_occurred.connect(lambda *_: errors.append(True))
    try:
        with deletion_holding_write_lock(manager, db, monkeypatch, outcome=outcome):
            def exercise():
                begin = time.monotonic()
                assert manager.update_task_identity(task_id, '新场景', '新版本')
                manager.set_task_group([task_id], 'new-group')
                info = TargetInfo(pid=456, file_version='默认版本', captured_at=datetime.now())
                manager._probe_results[task_id] = info
                manager._on_target_probed(task_id)
                assert not manager.save_task_plan(task_id, plan.to_json())
                for action in (
                    lambda: manager.update_task_identity('historical', '场景', '版本'),
                    lambda: manager.set_task_group(['historical'], 'group'),
                    lambda: manager.set_task_reference('historical'),
                    lambda: manager.set_task_starred('historical', True),
                ):
                    with pytest.raises(ValueError, match='删除'):
                        action()
                assert manager.add_marker(task_id, '删除期间') is None
                assert not manager.rename_marker(baseline.id, '改名')
                assert not manager.delete_marker(baseline.id)
                assert not manager.set_baseline(task_id, baseline.id)
                assert not manager.undo_marker(baseline.id).ok
                manager._on_task_data_updated(task_id, {'memory_private': 4})
                manager._on_task_stopped(task_id, StopReason.USER)
                assert manager.remove_task(task_id)
                durations.append(time.monotonic() - begin)
                delivered.append('operations')

            def dispatch_exercise():
                try:
                    exercise()
                except BaseException as error:
                    check_errors.append(error)
                    delivered.append('operations')

            QTimer.singleShot(0, dispatch_exercise)
            QTimer.singleShot(0, lambda: delivered.append('heartbeat'))
            assert process_events_until(lambda: len(delivered) == 2, 1)
            assert not check_errors
            assert max(durations) < 0.5
            assert manager._pending_identity[task_id] == ('新场景', '新版本')
            assert manager._pending_groups[task_id] == 'new-group'
            assert task_id in manager._pending_meta and task_id in manager._pending_plans
            assert worker.restart_policy() == (True, 3)
            assert db.tasks.get(task_id).scenario == ''
            assert db.tasks.load_meta(task_id) is None
            assert db.tasks.load_plan(task_id) is None
            assert errors
        assert process_events_until(lambda: not manager._pending_write_ids(), 3)
        saved = db.tasks.get(task_id)
        assert (saved.scenario, saved.build, saved.group_id) == ('新场景', '新版本', 'new-group')
        assert db.tasks.load_meta(task_id) is not None
        assert db.tasks.load_plan(task_id) == plan.to_json()
        assert (db.tasks.get('stopped') is None) == (outcome == 'success')
    finally:
        manager._pending_write_timer.stop()
        for tid in tuple(manager._tasks):
            manager.remove_task(tid)


def test_low_space_pause_and_guard_repause_do_not_wait_for_delete_lock(db, qapp, monkeypatch):
    seed(db)
    manager = MonitorManager(db)
    task_id, worker = make_registered_task(manager, db)
    worker._state = WorkerState.RUNNING
    disk = SimpleNamespace(free=100 * MB)
    guard = StorageGuard(manager, MaintenanceService(db, manager), ThreadRegistry(), db.db_path,
                         disk_usage=lambda _: disk, quick_check_delay_s=None)
    try:
        with deletion_holding_write_lock(manager, db, monkeypatch):
            begin = time.monotonic()
            guard.check_disk()
            assert worker.state is WorkerState.PAUSED
            # 强行恢复会被仍处于临界空间的守卫立即再次暂停，两次都不能等写锁。
            assert manager.resume_task(task_id)
            assert worker.state is WorkerState.PAUSED
            disk.free = 400 * MB
            guard.check_disk()
            assert manager.resume_task(task_id)
            assert worker.state is WorkerState.RUNNING
            assert time.monotonic() - begin < 0.5
            pending = tuple(manager._pending_system_markers)
            assert [kind for _, kind, _ in pending] == ['pause', 'resume', 'pause', 'resume']
            assert db.markers.list(task_id) == []
        assert process_events_until(lambda: not manager._pending_system_markers, 3)
        assert [(marker.kind, marker.timestamp) for marker in db.markers.list(task_id)] == [
            (kind, when) for _, kind, when in pending]
    finally:
        guard.stop()
        manager.remove_task(task_id)


def test_pending_writes_keep_values_when_next_writer_still_holds_lock(db, qapp, monkeypatch):
    seed(db)
    manager = MonitorManager(db)
    task_id, _ = make_registered_task(manager, db)
    with deletion_holding_write_lock(manager, db, monkeypatch) as (_, release, registry):
        manager.update_task_identity(task_id, '已接受场景', '已接受版本')
        manager.set_task_group([task_id], 'accepted-group')
        manager.save_task_plan(task_id, AutomationPlan(resume_on_restart=True).to_json())
        manager._pending_meta[task_id] = TargetInfo(pid=456).to_json()
        manager.remove_task(task_id)
        # QTimer 尚未得到补存机会，先让删除线程完成。
        release.set()
        assert registry.entries()[0].thread.wait(3000)
        with db._transaction():
            begin = time.monotonic()
            assert not manager.flush_pending_task_writes()
            assert time.monotonic() - begin < 0.5
            assert task_id in manager._pending_identity
            assert task_id in manager._pending_groups
            assert task_id in manager._pending_meta
            assert task_id in manager._pending_plans
        assert process_events_until(lambda: not manager._pending_write_ids(), 3)
        assert db.tasks.get(task_id).scenario == '已接受场景'


def test_deferred_group_change_keeps_all_existing_members_atomic(db, qapp, monkeypatch):
    seed(db)
    manager = MonitorManager(db)
    ids = [make_registered_task(manager, db)[0] for _ in range(2)]
    observed = []
    manager.task_library_changed.connect(
        lambda: observed.append(tuple(db.tasks.get(tid).group_id for tid in ids)))
    # 第二个成员写失败时，真实事务必须回滚第一个成员的更新。
    with db._transaction() as cursor:
        cursor.execute(f"""CREATE TRIGGER fail_group BEFORE UPDATE OF group_id ON tasks
            WHEN NEW.task_id = '{ids[1]}' AND NEW.group_id = 'atomic-group'
            BEGIN SELECT RAISE(ABORT, 'group write failed'); END""")
    try:
        with deletion_holding_write_lock(manager, db, monkeypatch) as (_, release, registry):
            manager.set_task_group(ids, 'atomic-group')
            for tid in ids:
                manager.remove_task(tid)
            release.set()
            assert registry.entries()[0].thread.wait(3000)
            assert not manager.flush_pending_task_writes()
            assert [db.tasks.get(tid).group_id for tid in ids] == [None, None]
            assert set(manager._pending_groups) == set(ids)
            with db._transaction() as cursor:
                cursor.execute('DROP TRIGGER fail_group')
            assert process_events_until(lambda: not manager._pending_groups, 3)
            assert [db.tasks.get(tid).group_id for tid in ids] == ['atomic-group', 'atomic-group']
            assert observed[-1] == ('atomic-group', 'atomic-group')
    finally:
        manager._pending_write_timer.stop()
        for tid in tuple(manager._tasks):
            manager.remove_task(tid)


def test_ipc_new_plan_can_start_while_delete_holds_write_lock(db, qapp, monkeypatch):
    from tests.soak_doubles import FakeProbe, SegmentCollector, StepClock

    seed(db)
    manager = MonitorManager(db, target_probe=lambda *a, **kw: None)
    manager.set_worker_options(
        clock=StepClock(), collector_factory=lambda *_: SegmentCollector(['memory_private'], [5]),
        exit_probe_factory=lambda _: FakeProbe(0))
    dispatcher = CommandDispatcher(app_version='test')
    dispatcher.set_context(CommandContext(manager, db, None))
    plan = AutomationPlan(resume_on_restart=True, restart_limit=1)
    try:
        with deletion_holding_write_lock(manager, db, monkeypatch):
            begin = time.monotonic()
            result = dispatcher._create(456, 'other.exe', None, ['memory_private'], 1, '', plan,
                                        identity={'scenario': '场景', 'build': '版本', 'group_id': '组'})
            assert time.monotonic() - begin < 0.5
            task_id = result['task_id']
            assert task_id in manager._pending_plans
            assert manager.get_task(task_id).restart_policy() == (True, 1)
            assert db.tasks.load_plan(task_id) is None
        assert process_events_until(lambda: db.tasks.load_plan(task_id) is not None, 3)
        assert task_id not in dispatcher._unsaved_plans
        assert db.tasks.get(task_id).scenario == '场景'
    finally:
        manager.stop_all_tasks()
        assert manager.join_all(3000)
        qapp.processEvents()
        dispatcher.deleteLater()


def test_presenter_plan_save_waits_for_delete_and_receives_persisted_signal(db, qapp, monkeypatch):
    from ui.presenters.monitor_presenter import MonitorPresenter

    seed(db)
    manager = MonitorManager(db)
    task_id, worker = make_registered_task(manager, db)
    plan = AutomationPlan(resume_on_restart=True, restart_limit=2)
    entry = SimpleNamespace(plan_saved=False)
    presenter = SimpleNamespace(manager=manager, _db=db, _tasks={task_id: entry})
    manager.task_plan_saved.connect(lambda tid: MonitorPresenter._on_plan_saved(presenter, tid))
    manager.seed_restart_policy(task_id, True, 2)
    try:
        with deletion_holding_write_lock(manager, db, monkeypatch):
            begin = time.monotonic()
            assert not MonitorPresenter._save_plan(presenter, task_id, plan)
            assert time.monotonic() - begin < 0.5
            assert not entry.plan_saved and worker.restart_policy() == (True, 2)
        assert process_events_until(lambda: entry.plan_saved, 3)
        assert db.tasks.load_plan(task_id) == plan.to_json()
        db.tasks.save_plan(task_id, None)
        worker._refresh_restart_policy(force=True)
        assert worker.restart_policy() == (False, 0)
    finally:
        manager.remove_task(task_id)


def test_auto_marker_tick_reports_busy_without_waiting_for_delete(db, qapp, monkeypatch):
    from core.automation.auto_marker import AutoMarkerService

    seed(db)
    manager = MonitorManager(db)
    task_id, worker = make_registered_task(manager, db)
    worker._state = WorkerState.RUNNING
    now = datetime(2026, 11, 13)
    clock = SimpleNamespace(now=lambda: now)
    auto = AutoMarkerService(manager, clock=clock)
    auto.attach(task_id, AutomationPlan(timed_marker_seconds=1))
    errors = []
    manager.error_occurred.connect(lambda tid, message: errors.append(message))
    try:
        with deletion_holding_write_lock(manager, db, monkeypatch):
            now += timedelta(seconds=1)
            begin = time.monotonic()
            auto.tick()
            assert time.monotonic() - begin < 0.5
            assert errors and '删除' in errors[-1]
            assert db.markers.list(task_id) == []
        now += timedelta(seconds=1)
        auto.tick()
        assert len(db.markers.list(task_id)) == 1
    finally:
        auto.detach(task_id)
        manager.remove_task(task_id)


def test_shutdown_flushes_deferred_values_after_delete_cancel_without_qt_events(
        db, qapp, monkeypatch):
    seed(db)
    manager = MonitorManager(db)
    task_id, _ = make_registered_task(manager, db)
    with deletion_holding_write_lock(manager, db, monkeypatch) as (worker, release, registry):
        manager.update_task_identity(task_id, '退出前保存', '版本')
        manager.save_task_plan(task_id, AutomationPlan(resume_on_restart=True).to_json())
        manager.remove_task(task_id)
        original_cancel = worker.cancel
        def cancel_and_release():
            original_cancel()
            release.set()
        registry.entries()[0].cancel = cancel_and_release
        closed = []
        def verify_close():
            assert not worker.isRunning()
            assert not manager._pending_write_ids()
            assert db.tasks.get(task_id).scenario == '退出前保存'
            assert db.tasks.load_plan(task_id) is not None
            closed.append(True)
        coordinator = ShutdownCoordinator(manager, registry, total_budget_ms=2000,
                                          db=SimpleNamespace(close=verify_close))
        assert coordinator.run()
        assert closed and db.tasks.get('stopped') is not None


def test_shutdown_write_failure_retains_pending_without_timer_retry_or_close(db, qapp, monkeypatch):
    manager = MonitorManager(db)
    task_id, _ = make_registered_task(manager, db)
    manager._pending_identity[task_id] = ('仍待保存', '版本')
    calls, closed = [], []

    def fail(*_):
        calls.append(True)
        raise StorageError('暂时不能写入')

    monkeypatch.setattr(db.tasks, 'update_identity', fail)
    try:
        coordinator = ShutdownCoordinator(manager, ThreadRegistry(), total_budget_ms=2000,
                                          db=SimpleNamespace(close=lambda: closed.append(True)))
        assert not coordinator.run()
        assert not closed and not manager._pending_write_timer.isActive()
        assert manager._pending_identity[task_id] == ('仍待保存', '版本')
        attempted = len(calls)
        # 退出中排队的事件和后到的完成回调不能重新启动写入或定时器。
        manager._on_task_data_updated(task_id, {})
        manager._on_task_stopped(task_id, StopReason.USER)
        manager.end_maintenance()
        manager._retry_pending_task_writes()
        qapp.processEvents()
        assert len(calls) == attempted
        assert not manager._pending_write_timer.isActive()
        assert not manager.create_task(1, 'new.exe', ['memory_private'], 1)
    finally:
        manager.remove_task(task_id)
