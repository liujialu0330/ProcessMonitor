"""管理员授权与交接的 GUI 协调，系统启动使用可控替身。"""
import os
from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QWidget

from app.elevation import ElevationController, _LaunchWorker, restart_command
from core.restart_handoff import RestartTicket, write_json
from tests.factories import make_services
from tests.test_elevation_handoff import FakeProcess
from utils.elevation import ElevationCancelled, ProcessIdentity

OWNER = ProcessIdentity(os.getpid(), 100, 'user')
CANDIDATE = ProcessIdentity(987654, 200, 'user')


class Task:
    task_id = 'existing-task'

    def __init__(self):
        self.prepared = False
        self.committed = False
        self.error = None

    def prepare_handoff(self):
        self.prepared = True

    def isFinished(self):
        return False

    def handoff_status(self):
        return self.prepared, self.error

    def handoff_state(self):
        return {'paused': True}

    def commit_handoff(self):
        self.committed = True


class Manager:
    def __init__(self):
        self.task = Task()
        self.shutdown = False
        self.busy = False
        self.flush_ok = True

    def is_maintenance_running(self):
        return self.busy

    def has_running_exports(self):
        return False

    def has_storage_jobs(self):
        return False

    def has_task_deletions(self):
        return False

    def begin_shutdown(self):
        self.shutdown = True

    def get_all_tasks(self):
        return [self.task]

    def get_task(self, task_id):
        assert task_id == self.task.task_id
        return self.task

    def prepare_restart_writes(self, **_kwargs):
        return self.flush_ok

    def cancel_restart_preparation(self):
        self.shutdown = False
        self.task.prepared = False


@pytest.fixture
def flow(qapp, tmp_path, monkeypatch, qtbot):
    monkeypatch.setattr('app.elevation.is_elevated', lambda: False)
    window = QWidget()
    qtbot.addWidget(window)
    window.finished = False
    window.finish_elevated_restart = lambda: setattr(window, 'finished', True)
    presenter = SimpleNamespace(
        suspended=False, restart_ui_state=lambda: {'draft': {}, 'waits': []})
    presenter.suspend_for_restart = lambda state: setattr(presenter, 'suspended', state)
    window.monitor_page = SimpleNamespace(presenter=presenter)
    services = make_services()
    ctx = SimpleNamespace(
        manager=Manager(), paths=SimpleNamespace(data_dir=str(tmp_path)),
        services=services, registry=services.threads, storage_guard=None)
    child = FakeProcess(CANDIDATE, elevated=True)
    launches = []

    def launcher(command, **_kwargs):
        launches.append(command)
        token = command[-1]
        ticket = RestartTicket(str(tmp_path), token)
        write_json(ticket.ready_path, {'token': token, 'child': CANDIDATE.to_dict()})
        return child

    controller = ElevationController(
        ctx, window, SimpleNamespace(dispatcher=None), launcher=launcher,
        process_factory=lambda _pid: FakeProcess(OWNER))
    yield SimpleNamespace(controller=controller, ctx=ctx, window=window,
                          child=child, launches=launches, presenter=presenter)
    controller._timer.stop()
    controller._on_quit()
    services.threads.cancel_all()
    services.threads.shutdown_all()


def test_successful_handoff_stops_old_window_only_after_commit(flow, qtbot):
    flow.controller.request()
    qtbot.waitUntil(lambda: flow.window.finished)
    assert flow.ctx.manager.task.committed
    assert flow.ctx.manager.shutdown
    assert flow.presenter.suspended
    assert len(flow.launches) == 1
    state = flow.controller._ticket.read()
    assert state['phase'] == 'committed'
    assert state['payload']['tasks'] == {'existing-task': {'paused': True}}
    assert flow.child.closed


def test_repeated_click_launches_only_once(flow, qtbot):
    flow.controller.request()
    flow.controller.request()
    qtbot.waitUntil(lambda: flow.window.finished)
    assert len(flow.launches) == 1


@pytest.mark.parametrize('error,cancelled', [
    (ElevationCancelled('用户取消'), True), (OSError('启动失败'), False),
])
def test_launch_failure_leaves_original_tasks_running(flow, qtbot, error, cancelled):
    def fail(*_args, **_kwargs):
        raise error
    flow.controller.launcher = fail
    flow.controller.request()
    qtbot.waitUntil(lambda: bool(flow.ctx.services.notifier.notices))
    assert flow.window.isEnabled() and not flow.window.finished
    assert not flow.ctx.manager.task.prepared
    assert not flow.ctx.manager.shutdown
    assert flow.ctx.services.notifier.last().level == ('info' if cancelled else 'error')


@pytest.mark.parametrize('failure', ['different_user', 'not_elevated', 'early_exit'])
def test_invalid_candidate_keeps_original_window(flow, qtbot, failure):
    if failure == 'different_user':
        flow.child.value = ProcessIdentity(CANDIDATE.pid, 200, 'another-user')
    elif failure == 'not_elevated':
        flow.child.elevated = False
    else:
        flow.child.running = False
    flow.controller.request()
    qtbot.waitUntil(lambda: bool(flow.ctx.services.notifier.notices))
    assert flow.window.isEnabled() and not flow.window.finished
    assert not flow.ctx.manager.shutdown
    assert flow.child.closed


def test_save_failure_releases_all_prepared_tasks(flow, qtbot):
    flow.ctx.manager.task.error = '磁盘写入失败'
    flow.controller.request()
    qtbot.waitUntil(lambda: bool(flow.ctx.services.notifier.notices))
    assert not flow.ctx.manager.task.committed
    assert not flow.ctx.manager.task.prepared
    assert not flow.ctx.manager.shutdown and not flow.presenter.suspended
    assert flow.window.isEnabled() and not flow.window.finished


def test_prepare_timeout_releases_barrier(flow, qtbot):
    flow.ctx.manager.flush_ok = False
    flow.controller.request()
    qtbot.waitUntil(lambda: flow.controller._frozen)
    flow.controller._deadline = 0
    flow.controller._poll()
    assert flow.window.isEnabled() and not flow.presenter.suspended
    assert not flow.ctx.manager.shutdown and not flow.ctx.manager.task.committed


def test_ready_timeout_does_not_freeze_source(flow, qtbot):
    def launch(command, **_kwargs):
        return flow.child
    flow.controller.launcher = launch
    flow.controller.request()
    qtbot.waitUntil(lambda: flow.controller._process is not None)
    flow.controller._deadline = 0
    flow.controller._poll()
    assert flow.window.isEnabled() and not flow.ctx.manager.task.prepared


def test_child_exit_during_preparation_releases_source(flow, qtbot):
    flow.ctx.manager.flush_ok = False
    flow.controller.request()
    qtbot.waitUntil(lambda: flow.controller._frozen)
    flow.child.running = False
    flow.controller._poll()
    assert not flow.ctx.manager.shutdown and flow.window.isEnabled()
    assert not flow.ctx.manager.task.committed


def test_maintenance_blocks_launch_with_specific_reason(flow):
    flow.ctx.manager.busy = True
    flow.controller.request()
    assert not flow.launches
    assert '维护或导出' in flow.ctx.services.notifier.last().content


def test_command_keeps_data_directory_and_uses_absolute_entry(tmp_path):
    command = restart_command(str(tmp_path), 'a' * 64)
    assert os.path.isabs(command[0])
    assert command[-4:] == ['--data-dir', str(tmp_path), '--restart-token', 'a' * 64]


def test_launcher_worker_cancel_releases_returned_handle(tmp_path):
    child = FakeProcess(CANDIDATE)
    ticket = RestartTicket.create(str(tmp_path), OWNER.to_dict())
    worker = _LaunchWorker(ticket, 0, lambda *_a, **_kw: child)
    worker.cancel()
    worker.run()
    assert child.closed


def test_exception_after_commit_keeps_recovery_record(flow, qtbot, monkeypatch):
    exits = []
    monkeypatch.setattr('app.elevation.QApplication.quit', lambda: exits.append(True))

    def fail_commit():
        raise RuntimeError('交接后的异常')

    flow.ctx.manager.task.commit_handoff = fail_commit
    flow.controller.request()
    qtbot.waitUntil(lambda: bool(exits))
    assert flow.controller._committed
    assert flow.controller._ticket.read()['phase'] == 'committed'
    assert flow.ctx.manager.task.prepared
    assert flow.ctx.manager.shutdown
