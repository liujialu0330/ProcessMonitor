"""用真实数据库、采集线程和页面核验交接记录的恢复入口。"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest

from app.bootstrap import build_context, release_session_lock, _attach_ipc_to_window
from app.elevation import restore_handoff
from core.ipc.commands import CommandDispatcher
from core.process_launcher import LaunchSpec
from core.process_tree import GroupSpec
from core.restart_handoff import RestartTicket, pending_restart
from data.database import Database
from data.models import DataPoint, MonitorTask
from data.repos.point_repo import PointQuery
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget
from ui.components.task_card import TaskCard
from ui.services.clock import SystemClock
from ui.services.task_status import ManagerTaskStatus
from utils.elevation import WindowsProcess


@pytest.mark.skipif(sys.platform != 'win32', reason='核验真实 Windows 用户身份')
def test_committed_handoff_recovers_mixed_paused_tasks_and_launch_links(
        qapp, qtbot, app_paths):
    app_paths.ensure_dirs()
    process = psutil.Process()
    started = datetime.now().replace(microsecond=0) - timedelta(minutes=2)
    seed = Database(app_paths.db_path)
    definitions = {
        'legacy-cpu': (['cpu_percent'], 'pid'),
        'private': (['memory_private'], 'pid'),
        'tree': (['memory_private', 'process_count'], 'tree')}
    original_points = {}
    for task_id, (metrics, kind) in definitions.items():
        seed.save_task(MonitorTask(
            task_id, process.pid, process.name(), metrics, 1,
            start_time=started, end_time=None, status='running', create_time=process.create_time(),
            target_kind=kind,
            target_spec=GroupSpec.tree(process.pid, process.create_time()).to_json()
            if kind == 'tree' else None))
        points = [DataPoint(task_id, started + timedelta(seconds=index), 160 + index, metric)
                  for index in range(3) for metric in metrics]
        assert seed.save_data_points(points)
        original_points[task_id] = points
        seed.markers.add(task_id, started, '原基线', 'baseline')
    seed.close()

    with WindowsProcess.open(os.getpid()) as current:
        identity = current.identity().to_dict()
    assert not psutil.pid_exists(987654)
    parent = dict(identity, pid=987654)
    ticket = RestartTicket.create(app_paths.data_dir, parent)
    ticket.commit(identity, {'tasks': {task_id: {'paused': True} for task_id in definitions}})
    state = pending_restart(app_paths.data_dir)
    assert state is not None
    ctx = build_context(app_paths, retention_days=0, restart_state=state)
    services = make_services(
        threads=ctx.registry, task_status=ManagerTaskStatus(ctx.manager), clock=SystemClock())
    ctx.services = services
    window = make_main_window(ctx.db, ctx.manager, services)
    window.attach_session_lock(ctx.session_lock)
    guard = SimpleNamespace(dispatcher=CommandDispatcher(app_version='2.0.2'))
    _attach_ipc_to_window(guard, ctx, window)
    snapshots = {}
    for task_id, (metrics, kind) in definitions.items():
        card = TaskCard(task_id, process.name(), process.pid, metrics, interval=1,
                        target_kind=kind)
        for index in range(3):
            card.update_values({metric: 160 + index for metric in metrics},
                               started + timedelta(seconds=index))
            card.increment_count()
        snapshots[task_id] = card.restart_view()
        dispose_widget(card)
    payload = {
        'tasks': {task_id: {'paused': True} for task_id in definitions},
        'ui': {
            'draft': window.monitor_page.form.restart_draft(), 'waits': [],
            'cards': snapshots,
            'launches': {'legacy-cpu': {
                'spec': LaunchSpec(exe=sys.executable).to_json(), 'close': True,
                'target': [process.pid, process.create_time()]}}},
        'ipc_launches': {'private': [process.pid, process.create_time()]}}
    ctx.restart_state['payload'] = payload
    ticket.commit(identity, payload)
    try:
        restore_handoff(ctx, window, guard)
        qtbot.waitUntil(lambda: all(ctx.manager.is_task_paused(t) for t in definitions))
        qtbot.wait(150)
        for task_id, (metrics, _kind) in definitions.items():
            record = ctx.db.get_task(task_id)
            assert record.status == 'running'
            assert record.start_time == started
            assert record.metric_types == metrics
            card = window.monitor_page.board.card(task_id)
            assert card.count == 3
            for metric in metrics:
                expected = [point for point in original_points[task_id]
                            if point.metric_type == metric]
                assert ctx.db.points.fetch(PointQuery(task_id, metric)) == expected
                assert card.last_values[metric] == 162
            assert ctx.manager.get_baseline(task_id).label == '原基线'
        assert services.launcher.terminated == []
        assert not ticket.path.exists()
        assert guard.dispatcher.restart_launches() == {
            'private': (process.pid, process.create_time())}
        assert window.monitor_page.presenter.is_launched('legacy-cpu')
        ctx.manager.stop_task('legacy-cpu')
        ctx.manager.stop_task('private')
        qtbot.waitUntil(lambda: len(services.launcher.terminated) == 2)
    finally:
        window.monitor_page.presenter.shutdown_live_stats()
        ctx.manager.stop_all_tasks()
        assert ctx.manager.join_all(3000)
        services.threads.cancel_all()
        assert services.threads.shutdown_all()
        release_session_lock(ctx)
        ctx.db.close()
        dispose_widget(window)
