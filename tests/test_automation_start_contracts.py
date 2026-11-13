"""自动化启动策略、存储守卫恢复与精确采样信号的回归。"""
import threading
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt

from core.automation.plan import AutomationPlan
from core.ipc.commands import CommandContext, CommandDispatcher
from core.ipc.protocol import Request
from core.maintenance_service import MaintenanceService
from core.monitor_manager import MonitorManager
from core.monitor_task import WorkerState
from core.restart_finder import RestartHit
from core.shutdown import ThreadRegistry
from core.storage_guard import MB, StorageGuard
from data.errors import StorageError
from data.repos.point_repo import PointQuery
from tests.soak_doubles import FakeProbe, ScriptedFinder, SegmentCollector, StepClock, build_rig
from tests.test_ipc_server import FakeConn


@pytest.fixture
def environment(qapp, db, monkeypatch):
    manager = MonitorManager(db, target_probe=lambda *a, **kw: None)
    monkeypatch.setattr(manager, '_start_target_probe', lambda *a, **kw: None)
    dispatcher = CommandDispatcher(app_version='test')
    dispatcher.set_context(CommandContext(manager, db, None))
    yield manager, dispatcher
    manager.stop_all_tasks()
    assert manager.join_all(3000)
    qapp.processEvents()
    dispatcher.deleteLater()


class InitiallyUnavailableCollector(SegmentCollector):
    def collect_metrics(self, metric_types):
        if self.calls == 0:
            self.calls += 1
            return {}
        return None


@pytest.mark.parametrize('unavailable_first', [False, True])
def test_ipc_restart_policy_applies_before_first_valid_sample(
        environment, qapp, db, unavailable_first):
    manager, dispatcher = environment
    clock = StepClock()
    finder = ScriptedFinder({1: RestartHit(200, 200.0, 'fake.exe')})
    first_type = InitiallyUnavailableCollector if unavailable_first else SegmentCollector
    collectors = {100: first_type(['memory_private'], []),
                  200: SegmentCollector(['memory_private'], [123])}
    manager.set_worker_options(
        clock=clock, collector_factory=lambda pid, spec: collectors[pid],
        exit_probe_factory=lambda pid: FakeProbe(0), restart_finder=finder)
    plan = AutomationPlan(resume_on_restart=True, restart_limit=1)
    result = dispatcher._create(100, 'fake.exe', None, ['memory_private'], 1, '', plan)
    worker = manager._tasks[result['task_id']]
    assert worker.wait(3000)

    # 不投递 GUI 事件：没有首个有效样本时，方案仍未落库，续接必须已经发生。
    assert db.tasks.load_restart_policy(worker.task_id) == (False, 0)
    assert finder.calls == 1 and worker.restart_count == 1 and worker.pid == 200
    assert [p.value for p in db.points.fetch(PointQuery(worker.task_id, 'memory_private'))] == [123]
    assert worker.restart_policy() == (True, 1)

    qapp.processEvents()
    assert db.tasks.load_restart_policy(worker.task_id) == (True, 1)
    assert worker.task_id not in dispatcher._unsaved_plans
    # 持久化成功后必须回到动态读库，后续关闭续接应立即生效。
    db.tasks.save_plan(worker.task_id, None)
    worker._refresh_restart_policy(force=True)
    assert worker.restart_policy() == (False, 0)


@pytest.mark.parametrize('write_error', [False, True])
def test_plan_seed_is_retained_until_persistence_succeeds(environment, db, monkeypatch, write_error):
    manager, dispatcher = environment
    tid = manager.create_task(100, 'fake.exe', ['memory_private'], 1)
    worker = manager._tasks[tid]
    plan = AutomationPlan(resume_on_restart=True, restart_limit=2)
    manager.seed_restart_policy(tid, True, 2)
    dispatcher._unsaved_plans[tid] = plan
    def fail_write(*args):
        raise StorageError('模拟磁盘写入失败')

    with monkeypatch.context() as patch:
        if write_error:
            patch.setattr(db.tasks, 'save_plan', fail_write)
        dispatcher._save_plan(tid)
    worker._refresh_restart_policy(force=True)
    assert worker.restart_policy() == (True, 2)
    assert tid in dispatcher._unsaved_plans

    worker.task_model.start_time = StepClock().now()
    assert db.save_task(worker.task_model)
    dispatcher._save_plan(tid)
    assert tid not in dispatcher._unsaved_plans
    db.tasks.save_plan(tid, AutomationPlan(resume_on_restart=True, restart_limit=4).to_json())
    worker._refresh_restart_policy()
    assert worker.restart_policy() == (True, 4)


@pytest.mark.parametrize('fixed_policy', [(False, 0), (True, 3)])
def test_temporary_policy_never_overrides_explicit_policy(qapp, db, fixed_policy):
    enabled, limit = fixed_policy
    worker = build_rig(db, segments={100: []}, codes={100: 0},
                       resume=enabled, limit=limit).worker
    worker.seed_restart_policy(not enabled, 8)
    worker.release_restart_policy_seed()
    worker._refresh_restart_policy(force=True)
    assert worker.restart_policy() == fixed_policy


@pytest.mark.parametrize('new_policy', ['seed', 'override'])
def test_in_flight_policy_read_cannot_overwrite_new_policy(qapp, db, monkeypatch, new_policy):
    worker = build_rig(db, segments={100: []}, codes={100: 0}, resume=None).worker
    entered, release = threading.Event(), threading.Event()

    def blocked_load(task_id):
        entered.set()
        assert release.wait(3)
        return False, 0

    monkeypatch.setattr(db.tasks, 'load_restart_policy', blocked_load)
    reader = threading.Thread(target=worker._refresh_restart_policy)
    reader.start()
    try:
        assert entered.wait(3)
        if new_policy == 'seed':
            worker.seed_restart_policy(True, 4)
        else:
            worker.set_restart_policy(True, 4)
    finally:
        release.set()
        reader.join(3)
    assert not reader.is_alive()
    assert worker.restart_policy() == (True, 4)


def test_resume_reports_guard_repause_and_succeeds_after_space_recovers(environment, db):
    manager, dispatcher = environment
    tid = manager.create_task(100, 'fake.exe', ['memory_private'], 1)
    manager._tasks[tid]._state = WorkerState.PAUSED
    disk = SimpleNamespace(free=100 * MB)
    guard = StorageGuard(manager, MaintenanceService(db, manager), ThreadRegistry(), db.db_path,
                         disk_usage=lambda path: disk, quick_check_delay_s=None)
    request = Request(id=1, cmd='resume', args={'task_id': tid})
    try:
        guard.check_disk()
        conn = FakeConn()
        dispatcher.handle(conn, request)
        assert conn.sent[-1]['ok'] is False and conn.sent[-1]['code'] == 'invalid_state'
        assert conn.sent[-1]['task_id'] == tid and conn.sent[-1]['paused'] is True
        assert manager.is_task_paused(tid)

        disk.free = 400 * MB
        dispatcher.handle(conn, request)
        assert conn.sent[-1] == {'id': 1, 'ok': True, 'result': {'task_id': tid, 'paused': False}}
        assert not manager.is_task_paused(tid)
    finally:
        guard.stop()


def test_sample_signal_has_exact_buffer_timestamp_and_keeps_old_signal(qapp, db):
    rig = build_rig(db, segments={100: [10, 20]}, codes={100: 0}, metrics=['memory_private'])
    worker = rig.worker
    events, buffered = [], []
    worker.data_updated.connect(lambda tid, values: events.append(('legacy', tid, values)),
                                Qt.DirectConnection)

    def on_sample(tid, values, timestamp):
        events.append(('sample', tid, values, timestamp))
        buffered.append(worker._data_buffer[-1].timestamp)

    worker.sample_updated.connect(on_sample, Qt.DirectConnection)
    worker.start()
    try:
        assert worker.wait(3000)
    finally:
        worker.stop()
        assert worker.wait(3000)
    assert [event[0] for event in events] == ['legacy', 'sample', 'legacy', 'sample']
    assert events[0][1:3] == events[1][1:3]
    assert events[2][1:3] == events[3][1:3]
    samples = [event[3] for event in events if event[0] == 'sample']
    assert samples == buffered
    assert samples == [p.timestamp for p in db.points.fetch(PointQuery(worker.task_id, 'memory_private'))]
