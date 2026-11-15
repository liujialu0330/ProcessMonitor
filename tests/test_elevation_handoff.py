"""提权交接协议、真实线程保存屏障及 Windows 原生句柄核验。"""
import json
import os
import sys
from dataclasses import replace

import pytest

from core.monitor_task import MonitorWorker, WorkerState
from core.restart_handoff import (
    RESTART_ARG, RestartTicket, await_parent_exit, pending_restart,
    restart_token, strip_restart_args, write_json,
)
from data.repos.point_repo import PointQuery
from utils.elevation import ProcessIdentity, WindowsProcess

METRIC = 'memory_private'
PARENT = ProcessIdentity(987654, 100, '0102')
CHILD = ProcessIdentity(os.getpid(), 200, '0102')


class FakeProcess:
    def __init__(self, identity, *, elevated=False):
        self.value = identity
        self.elevated = elevated
        self.running = True
        self.closed = False

    def identity(self):
        return self.value

    def is_elevated(self):
        return self.elevated

    def alive(self):
        return self.running

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.fixture
def ticket(tmp_path):
    return RestartTicket.create(str(tmp_path), PARENT.to_dict())


def process_table(*, child_identity=CHILD, parent_identity=PARENT, elevated=True):
    child = FakeProcess(child_identity, elevated=elevated)
    parent = FakeProcess(parent_identity)
    table = {os.getpid(): child, PARENT.pid: parent}
    return table.__getitem__, child, parent


@pytest.mark.parametrize('argv', [
    ['app', RESTART_ARG], ['app', RESTART_ARG, '../bad'],
    ['app', RESTART_ARG, '0' * 64, RESTART_ARG, '1' * 64],
])
def test_reject_invalid_restart_arguments(argv):
    with pytest.raises(ValueError):
        restart_token(argv)


def test_restart_arguments_are_removed_before_qt(ticket):
    argv = ['app', RESTART_ARG, ticket.token, '--data-dir', '中文 path']
    assert restart_token(argv) == ticket.token
    assert strip_restart_args(argv) == ['app', '--data-dir', '中文 path']
    assert restart_token(['app']) is None


def test_ticket_is_atomic_and_bound_to_directory(ticket, tmp_path):
    assert ticket.read()['phase'] == 'request'
    assert not list(tmp_path.glob('*.tmp'))
    state = ticket.read()
    state['directory'] += '-other'
    write_json(ticket.path, state)
    with pytest.raises(ValueError):
        ticket.read()


@pytest.mark.parametrize('child_identity,parent_identity,elevated', [
    (replace(CHILD, user_sid='other'), PARENT, True),
    (CHILD, replace(PARENT, created=101), True),
    (CHILD, PARENT, False),
])
def test_candidate_rejects_wrong_identity_or_token(
        ticket, child_identity, parent_identity, elevated):
    factory, _child, _parent = process_table(
        child_identity=child_identity, parent_identity=parent_identity, elevated=elevated)
    with pytest.raises(ValueError):
        await_parent_exit(ticket.directory, ticket.token, process_factory=factory)
    assert not ticket.ready_path.exists()


def test_candidate_waits_for_parent_exit_before_handoff(ticket):
    factory, child, parent = process_table()
    sleeps = []

    def advance(_seconds):
        sleeps.append(True)
        assert ticket.ready(child.identity().to_dict())
        if len(sleeps) == 1:
            ticket.commit(child.identity().to_dict(), {'tasks': {}})
        else:
            parent.running = False

    state = await_parent_exit(ticket.directory, ticket.token,
                              process_factory=factory, sleep=advance)
    assert len(sleeps) == 2
    assert state['phase'] == 'committed'
    assert child.closed and parent.closed


def test_candidate_exits_when_source_cancels(ticket):
    factory, _child, parent = process_table()
    assert await_parent_exit(ticket.directory, ticket.token, process_factory=factory,
                              sleep=lambda _s: ticket.cancel()) is None
    assert parent.running


def test_candidate_exits_when_parent_dies_without_commit(ticket):
    factory, _child, parent = process_table()
    assert await_parent_exit(
        ticket.directory, ticket.token, process_factory=factory,
        sleep=lambda _s: setattr(parent, 'running', False)) is None


def test_ready_signal_must_match_actual_child(ticket):
    write_json(ticket.ready_path, {'token': ticket.token, 'child': CHILD.to_dict()})
    assert ticket.ready(CHILD.to_dict())
    assert not ticket.ready(replace(CHILD, created=201).to_dict())


def test_committed_state_can_recover_on_next_normal_start(ticket):
    factory, _child, parent = process_table()
    ticket.commit(CHILD.to_dict(), {'tasks': {'t': {'paused': True}}})
    assert pending_restart(ticket.directory, process_factory=factory) is None
    parent.running = False
    saved = pending_restart(ticket.directory, process_factory=factory)
    assert saved['payload']['tasks']['t']['paused']


def test_late_launcher_cancellation_cannot_revoke_committed_handoff(ticket):
    ticket.commit(CHILD.to_dict(), {'tasks': {'t': {'paused': True}}})
    ticket.cancel()
    assert ticket.read()['phase'] == 'committed'


class ConstantCollector:
    def __init__(self):
        self.primed = 0

    def is_process_running(self):
        return True

    def collect_metrics(self, metrics):
        return {metric: 100.0 for metric in metrics}

    def prime_cpu(self):
        self.primed += 1


def make_worker(db, **kwargs):
    return MonitorWorker(
        os.getpid(), 'python.exe', [METRIC], interval=0.03, db=db,
        collector_factory=lambda *_a: ConstantCollector(), **kwargs)


def points(db, task_id):
    return db.points.fetch(PointQuery(task_id, METRIC))


def test_thread_handoff_preserves_task_and_all_buffered_samples(db, qapp, qtbot):
    worker = make_worker(db)
    stopped = []
    worker.task_stopped.connect(lambda *_args: stopped.append(True))
    try:
        worker.start()
        qtbot.waitUntil(lambda: len(points(db, worker.task_id)) >= 2)
        assert worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        assert worker.handoff_status()[1] is None
        before = points(db, worker.task_id)
        assert before and not worker._data_buffer
        worker.commit_handoff()
        assert worker.wait(3000)
        qapp.processEvents()
        record = db.get_task(worker.task_id)
        assert record.status == 'running' and record.end_time is None
        assert points(db, worker.task_id) == before
        assert stopped == []
    finally:
        worker.stop()
        worker.wait(3000)


def test_group_counter_handoff_continues_same_persisted_series(db, qapp, qtbot):
    from core.process_tree import GroupSpec, Observation
    from tests.group_doubles import inst
    from utils.metrics import member_series_key

    metric = 'io_read_count'
    counter = [100]

    class Source:
        def observe(self, _spec, metrics, **_kwargs):
            if metric in metrics:
                counter[0] += 10
            return Observation(taken_monotonic=0, instances=(
                inst('worker.exe', 123, **{metric: counter[0]}),))

    group = GroupSpec.by_name('worker.exe')
    worker = MonitorWorker(0, 'worker.exe', [metric], interval=0.03, db=db,
                           group_spec=group, tree_source=Source())
    restored = None

    def samples():
        return db.points.fetch(PointQuery(worker.task_id, metric))

    try:
        worker.start()
        qtbot.waitUntil(lambda: len(samples()) >= 3)
        worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        state = json.loads(json.dumps(worker.handoff_state()))
        before = samples()
        worker.commit_handoff()
        assert worker.wait(3000)
        state['paused'] = True
        counter[0] += 1000000
        restored = MonitorWorker(
            0, 'worker.exe', [metric], interval=0.03, db=db,
            task_id=worker.task_id, start_time=worker.task_model.start_time,
            recovering=True, group_spec=group, tree_source=Source(), restart_state=state)
        restored.start()
        qtbot.waitUntil(lambda: restored.state is WorkerState.PAUSED)
        qtbot.wait(80)
        assert samples() == before
        restored.resume()
        qtbot.waitUntil(lambda: len(samples()) >= len(before) + 2)
        restored.stop()
        assert restored.wait(3000)
        values = [point.value for point in samples()]
        assert values[len(before):len(before) + 2] == [before[-1].value, before[-1].value + 10]
        member_values = [point.value for point in db.points.fetch(PointQuery(
            worker.task_id, member_series_key(metric, 'worker.exe')))]
        assert member_values == values
        assert db.get_task(worker.task_id).start_time == worker.task_model.start_time
    finally:
        for instance in (worker, restored):
            if instance is not None:
                instance.stop()
                assert instance.wait(3000)


def test_cancelled_thread_preparation_resumes_same_worker(db, qapp, qtbot):
    worker = make_worker(db)
    try:
        worker.start()
        qtbot.waitUntil(lambda: len(points(db, worker.task_id)) >= 2)
        worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        count = len(points(db, worker.task_id))
        worker.cancel_handoff()
        qtbot.waitUntil(lambda: len(points(db, worker.task_id)) > count)
        assert worker.state is WorkerState.RUNNING
    finally:
        worker.stop()
        assert worker.wait(3000)


def test_failed_flush_blocks_handoff_and_keeps_samples(db, qapp, qtbot, monkeypatch):
    worker = make_worker(db)
    original = db.save_data_points
    try:
        worker.start()
        qtbot.waitUntil(lambda: len(points(db, worker.task_id)) >= 2)
        monkeypatch.setattr(db, 'save_data_points', lambda *_a: False)
        qtbot.waitUntil(lambda: bool(worker._data_buffer))
        worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        assert worker.handoff_status()[1] and worker._data_buffer
        with pytest.raises(RuntimeError):
            worker.commit_handoff()
        count = len(points(db, worker.task_id))
        monkeypatch.setattr(db, 'save_data_points', original)
        worker.cancel_handoff()
        qtbot.waitUntil(lambda: len(points(db, worker.task_id)) > count)
    finally:
        monkeypatch.setattr(db, 'save_data_points', original)
        worker.stop()
        assert worker.wait(3000)


@pytest.mark.parametrize('raises', [False, True])
def test_quality_write_failure_blocks_handoff(db, qapp, qtbot, monkeypatch, raises):
    worker = make_worker(db)
    original = db.tasks.save_quality
    try:
        worker.start()
        qtbot.waitUntil(lambda: len(points(db, worker.task_id)) >= 2)

        def fail(*_args, **_kwargs):
            if raises:
                raise OSError('质量写入失败')
            return False

        monkeypatch.setattr(db.tasks, 'save_quality', fail)
        worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        assert worker.handoff_status()[1]
        with pytest.raises(RuntimeError):
            worker.commit_handoff()
        monkeypatch.setattr(db.tasks, 'save_quality', original)
        worker.cancel_handoff()
        assert worker.state is WorkerState.RUNNING
    finally:
        monkeypatch.setattr(db.tasks, 'save_quality', original)
        worker.stop()
        assert worker.wait(3000)


def test_waiting_for_target_restart_survives_handoff(db, qapp, qtbot):
    from core.restart_finder import RestartTarget
    from tests.soak_doubles import FakeProbe
    target = RestartTarget('python.exe', None, 987654, 100.0,
                           exit_time=150.0, ancestor_pids=frozenset({987654}))
    state = {
        'paused': True, 'waiting_target': {
            'name': target.name, 'exe_path': None, 'last_pid': target.last_pid,
            'last_create_time': target.last_create_time, 'exit_time': 150.0,
            'last_seen_time': None, 'ancestor_pids': [987654]},
        'exit_code': 1, 'exit_reason': 'process_exited'}
    worker = make_worker(
        db, restart_state=state, resume_on_restart=True, restart_limit=3,
        restart_finder=lambda _target: None,
        exit_probe_factory=lambda _pid: FakeProbe(None))
    try:
        worker.start()
        qtbot.waitUntil(lambda: worker.state is WorkerState.WAITING_RESTART)
        assert worker.is_paused()
        worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        assert worker.handoff_state() == state
        assert points(db, worker.task_id) == []
        worker.commit_handoff()
        assert worker.wait(3000)
        assert db.get_task(worker.task_id).status == 'running'
    finally:
        worker.stop()
        worker.wait(3000)


def test_restored_restart_wait_adopts_new_target_and_keeps_pause(db, qapp, qtbot):
    import psutil
    from core.restart_finder import RestartHit
    from tests.soak_doubles import FakeProbe
    current = psutil.Process()
    state = {
        'paused': True,
        'waiting_target': {
            'name': current.name(), 'exe_path': None, 'last_pid': 987654,
            'last_create_time': 100.0, 'exit_time': current.create_time() - 1,
            'last_seen_time': None, 'ancestor_pids': [987654]},
        'exit_code': 1, 'exit_reason': 'process_exited'}
    worker = MonitorWorker(
        987654, current.name(), [METRIC], interval=0.03, db=db,
        collector_factory=lambda *_a: ConstantCollector(), restart_state=state,
        resume_on_restart=True, restart_limit=3,
        restart_finder=lambda _t: RestartHit(current.pid, current.create_time(), current.name()),
        exit_probe_factory=lambda _p: FakeProbe(None))
    try:
        worker.start()
        qtbot.waitUntil(lambda: worker.restart_count == 1)
        qtbot.wait(90)
        assert worker.pid == current.pid
        assert worker.state is WorkerState.PAUSED
        assert points(db, worker.task_id) == []
        assert any(marker.kind == 'restart' for marker in db.markers.list(worker.task_id))
        worker.resume()
        qtbot.waitUntil(lambda: bool(points(db, worker.task_id)))
    finally:
        worker.stop()
        assert worker.wait(3000)


def test_restored_pause_prevents_any_initial_sample(db, qapp, qtbot):
    worker = make_worker(db, restart_state={'paused': True})
    try:
        worker.start()
        qtbot.waitUntil(lambda: db.get_task(worker.task_id) is not None)
        qtbot.wait(120)
        assert worker.state is WorkerState.PAUSED
        assert points(db, worker.task_id) == []
        worker.prepare_handoff()
        qtbot.waitUntil(lambda: worker.handoff_status()[0])
        assert worker.handoff_state()['paused']
        worker.cancel_handoff()
        qtbot.wait(70)
        assert worker.state is WorkerState.PAUSED
        assert points(db, worker.task_id) == []
        worker.resume()
        qtbot.waitUntil(lambda: bool(points(db, worker.task_id)))
    finally:
        worker.stop()
        assert worker.wait(3000)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows 进程句柄')
def test_native_process_handle_identity_and_elevation():
    from core.target_info import process_is_elevated
    import psutil
    with WindowsProcess.open(os.getpid()) as process:
        identity = process.identity()
        assert process.alive()
        assert identity.pid == os.getpid()
        assert identity.user_sid
        assert abs(identity.created / 10_000_000 - 11644473600
                   - psutil.Process().create_time()) < 0.01
        assert process.is_elevated() == process_is_elevated(os.getpid())
    assert process.handle is None


def test_metadata_probe_must_finish_before_commit(db, qapp, monkeypatch):
    from core.monitor_manager import MonitorManager
    from types import SimpleNamespace
    import time
    manager = MonitorManager(db)
    thread = SimpleNamespace(running=True, is_alive=lambda: thread.running,
                             join=lambda *_a: None)
    manager._probe_threads['existing'] = thread
    writes = []
    monkeypatch.setattr(manager, 'flush_pending_task_writes',
                        lambda **_kw: writes.append(True) or True)
    try:
        assert not manager.prepare_restart_writes(deadline=time.monotonic() + 1)
        assert writes == []
        thread.running = False
        assert manager.prepare_restart_writes(deadline=time.monotonic() + 1)
        assert writes == [True]
    finally:
        manager._probe_threads.clear()
