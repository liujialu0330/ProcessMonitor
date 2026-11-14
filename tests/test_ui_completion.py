"""延迟页面、键盘访问、紧凑列表、方案执行与新增偏好的行为契约。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtTest import QTest
from PyQt5 import sip

from app_config import cfg, update_values
from core.monitor_manager import MonitorManager
from core.plans import PlanStore, PlanTarget, TestPlan
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget
from ui.components.task_card import TaskCard
from ui.live_stats import LiveStats
from ui.metric_presets import default_metrics, metric_presets, save_metric_preset
from ui.presenters.plan_flow import PlanFlow
from ui.widgets.monitor.new_task_form import NewTaskForm
from ui.widgets.monitor.task_list_row import TaskListRow


def test_monitor_start_does_not_construct_analysis_or_settings(qapp, db):
    window = make_main_window(db, MonitorManager(db))
    try:
        assert window._analysis_content is None and window._setting_content is None
        window.current_window_memory()
        assert window._analysis_content is None and window._setting_content is None
        window.switchTo(window._analysis_slot)
        assert window.stackedWidget.currentWidget() is window.analysis_page
        assert window._setting_content is None
        window.switchTo(window._setting_slot)
        assert window.setting_page.about_group is window.about_group
        assert window.stackedWidget.count() == 3
    finally:
        dispose_widget(window)


def test_window_page_shortcuts_construct_only_requested_page(qapp, db):
    window = make_main_window(db, MonitorManager(db))
    window.show()
    window.activateWindow()
    qapp.processEvents()
    try:
        QTest.keyClick(window, Qt.Key_3, Qt.ControlModifier)
        qapp.processEvents()
        assert window._page_key() == 'setting'
        assert window._analysis_content is None
        QTest.keyClick(window, Qt.Key_1, Qt.ControlModifier)
        assert window._page_key() == 'monitor'
    finally:
        dispose_widget(window)


def test_live_trend_uses_same_sufficiency_and_rising_rules():
    stats = LiveStats()
    start = datetime(2026, 1, 1)
    for index in range(601):
        stats.add('num_handles', 100 + index, start + timedelta(seconds=index))
        stats.add('num_threads', 10, start + timedelta(seconds=index))
    assert stats.verdict('num_handles', 1) == 'rising'
    assert stats.verdict('num_threads', 1) == 'stable'
    stats.clear()
    assert stats.verdict('num_handles', 1) == 'insufficient'


def test_metric_presets_persist_and_default_is_independent_of_last_choice():
    assert set(metric_presets()['GDI / USER 专项']) == {'num_gdi_objects', 'num_user_objects', 'num_handles'}
    save_metric_preset('线程', ['num_threads'])
    update_values([(cfg.default_metric_preset, '自定义：线程')])
    assert default_metrics(['memory_private']) == ['num_threads']
    update_values([(cfg.default_metric_preset, '')])
    assert default_metrics(['memory_private']) == ['memory_private']


def test_list_row_is_compact_and_actions_target_same_card(qapp):
    card = TaskCard('t', 'demo.exe', 1, ['num_handles'])
    row = TaskListRow(card)
    events = []
    card.marker_requested.connect(events.append)
    try:
        row.marker.click()
        assert events == ['t'] and row.height() == 48
        expanded = []
        row.expanded_changed.connect(expanded.append)
        row.expand.click()
        assert expanded == [True]
    finally:
        row.deleteLater()
        card.deleteLater()


def test_list_row_menu_stays_alive_until_closed_and_triggers_pause(qapp):
    """RoundMenu.exec()非阻塞：行控件必须持有菜单，直到用户选中或关闭它。"""
    card = TaskCard('t', 'demo.exe', 1, ['num_handles'])
    row = TaskListRow(card)
    pauses = []
    card.pause_requested.connect(pauses.append)
    try:
        menu = row._menu()
        qapp.processEvents()
        assert row._active_menu is menu and not sip.isdeleted(menu)
        pause = menu.menuActions()[0]
        QTest.mouseClick(menu.view.viewport(), Qt.LeftButton,
                         pos=menu.view.visualItemRect(pause.property('item')).center())
        qapp.processEvents()
        assert pauses == ['t'] and row._active_menu is None
    finally:
        if row._active_menu is not None:
            row._active_menu.close()
        row.deleteLater()
        card.deleteLater()


def test_plan_menu_stays_alive_until_closed_and_runs_action(qapp, db, tmp_path, monkeypatch):
    """方案菜单不能在show_menu返回时被deleteLater；点击后仍应调用对应操作。"""
    services = make_services()
    form = NewTaskForm(services.process_query, services.preferences)
    signals = _WaitSignals()
    presenter = SimpleNamespace(wait_service=signals)
    flow = PlanFlow(db, presenter, form, services, store=PlanStore(tmp_path))
    called = []
    monkeypatch.setattr(flow, 'save_current', lambda: called.append('saved'))
    try:
        menu = flow.show_menu()
        qapp.processEvents()
        assert flow._active_menu is menu and not sip.isdeleted(menu)
        save = next(action for action in menu.menuActions()
                    if action.text() == '另存当前配置')
        QTest.mouseClick(menu.view.viewport(), Qt.LeftButton,
                         pos=menu.view.visualItemRect(save.property('item')).center())
        qapp.processEvents()
        assert called == ['saved'] and flow._active_menu is None
    finally:
        if flow._active_menu is not None:
            flow._active_menu.close()
        flow.deleteLater()
        dispose_widget(form)


class _WaitSignals(QObject):
    started = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


def test_plan_partial_success_and_immediate_wait_keep_group(qapp, db, tmp_path):
    services = make_services()
    services.process_query.processes = [(1, 'a.exe', 1.0), (2, 'b.exe', 2.0)]
    form = NewTaskForm(services.process_query, services.preferences)
    signals = _WaitSignals()
    assigned, requests = [], []
    manager = SimpleNamespace(set_task_group=lambda ids, group: assigned.append((ids, group)))

    def start(request):
        requests.append(request)
        return None if request.pid == 2 else 'task-1'

    def wait(request):
        signals.started.emit('wait-1', 'task-wait')
        return 'wait-1'

    presenter = SimpleNamespace(wait_service=signals, manager=manager, start_monitoring=start,
                                on_wait_requested=wait, wait_ids=lambda: [])
    flow = PlanFlow(db, presenter, form, services, store=PlanStore(tmp_path))
    plan = TestPlan('回归', (PlanTarget(pid=1, metrics=('num_handles',)),
                            PlanTarget(pid=2, metrics=('num_handles',)),
                            PlanTarget(name='wait.exe', wait=True, metrics=('num_handles',))))
    flow.store.save(plan)
    flow.refresh(plan.name)
    results = flow.run_selected()
    assert len(results) == 3 and results[1].error
    assert [ids for ids, _ in assigned] == [['task-1'], ['task-wait']]
    assert len({group for _, group in assigned}) == 1
    assert flow._pending_groups == {}
    form.deleteLater()
    flow.deleteLater()


def test_waiting_restart_keeps_pause_intent_visible(qapp):
    card = TaskCard('t', 'demo.exe', 1, ['num_handles'])
    try:
        card.set_waiting_restart(True, 1, 3)
        card.set_paused(True)
        assert card.waiting_restart and card.status == 'waiting_restart'
        assert '续接后保持暂停' in card.status_label.text()
        assert card.pause_button.text() == '恢复' and card.pause_button.isEnabled()
        card.set_paused(False)
        assert card.status == 'waiting_restart' and '保持暂停' not in card.status_label.text()
    finally:
        card.deleteLater()


def test_preferences_do_not_write_before_config_is_loaded(tmp_path, monkeypatch):
    from qfluentwidgets import qconfig
    target = tmp_path / 'unloaded.json'
    monkeypatch.setattr(qconfig, '_cfg', qconfig)
    monkeypatch.setattr(qconfig, 'file', target)
    save_metric_preset('临时', ['num_handles'])
    update_values([(cfg.monitor_view, 'list'), (cfg.guide_started, True)])
    assert not target.exists()


def test_five_task_list_rows_fit_1000_by_700(qapp, db):
    from core.contracts import TaskInfo, TaskStatus
    window = make_main_window(db, MonitorManager(db))
    page = window.monitor_page
    try:
        for index in range(5):
            page.board.create_card(TaskInfo(
                task_id=str(index), pid=index + 1, process_name=f'app-{index}.exe',
                metric_types=('num_handles',), interval=1, note='测试备注',
                status=TaskStatus.RUNNING, start_time=datetime.now(), end_time=None,
                create_time=None, is_paused=False, is_stopping=False))
        page.form.set_collapsed(True)
        page.view_switch.setCurrentItem('list')
        window.resize(1000, 700)
        window.show()
        qapp.processEvents()
        assert page.verticalScrollBar().maximum() == 0
        assert all(row.isVisible() and row.height() == 48 for row in page.board._rows.values())
        assert all('测试备注' in row.name.text() for row in page.board._rows.values())
    finally:
        dispose_widget(window)


def test_settings_find_older_backups_and_delete_selected(qapp, db, monkeypatch):
    from pathlib import Path
    from tests.factories import make_setting_page
    from tests.helpers import process_events_until
    first = Path(db.db_path + '.bak_v1')
    second = Path(db.db_path + '.bak_v2')
    first.write_bytes(b'backup-one')
    second.write_bytes(b'backup-two')
    db.backup_path = None
    manager = MonitorManager(db)
    services = make_services()
    services.dialogs.push('confirm', True)
    page = make_setting_page(db, manager, services)
    try:
        assert process_events_until(lambda: page._backup_list_worker is None)
        assert set(page._backup_paths) == {str(first), str(second)}
        page.backup_combo.setCurrentIndex(page._backup_paths.index(str(first)))
        page.delete_migration_backup()
        assert process_events_until(lambda: page._backup_worker is None)
        assert not first.exists() and second.exists()
        assert page._backup_paths == [str(second)]
        assert not manager.is_maintenance_running()
    finally:
        dispose_widget(page)


def test_shortcut_commands_dispatch_only_to_analysis(qapp, db, monkeypatch):
    window = make_main_window(db, MonitorManager(db))
    window.show(); window.activateWindow(); qapp.processEvents()
    calls = []
    try:
        monkeypatch.setattr(window.monitor_page.presenter, 'quick_marker', lambda *args: calls.append('marker'))
        QTest.keyClick(window, Qt.Key_M, Qt.ControlModifier)
        assert calls == ['marker']
        window.switchTo(window.analysis_page)
        from tests.helpers import process_events_until
        assert process_events_until(lambda: window._page_key() == 'analysis')
        window.activateWindow()
        window.setFocus()
        qapp.processEvents()
        page = window.analysis_page
        for name in ('export_report', 'copy_report_image', 'refresh', 'focus_search'):
            monkeypatch.setattr(page, name, lambda n=name: calls.append(n))
        monkeypatch.setattr(page, 'select_range_index', lambda n: calls.append(n))
        QTest.keyClick(window, Qt.Key_E, Qt.ControlModifier)
        QTest.keyClick(window, Qt.Key_C, Qt.ControlModifier | Qt.ShiftModifier)
        QTest.keyClick(window, Qt.Key_F5)
        QTest.keyClick(window, Qt.Key_F, Qt.ControlModifier)
        for key in range(Qt.Key_1, Qt.Key_7 + 1):
            QTest.keyClick(window, key, Qt.AltModifier)
        assert calls[1:] == ['export_report', 'copy_report_image', 'refresh', 'focus_search', *range(7)]
        from ui import shortcuts
        help_calls = []
        monkeypatch.setattr(shortcuts.Flyout, 'create', lambda **kwargs: help_calls.append(kwargs))
        QTest.keyClick(window, Qt.Key_F1)
        assert help_calls[0]['title'] == '键盘快捷键'
        assert 'Ctrl+M' in help_calls[0]['content'] and 'Alt+1' in help_calls[0]['content']
    finally:
        dispose_widget(window)


def test_clear_finished_can_restore_same_cards_without_deleting_data(qapp, db):
    from core.contracts import StopReason
    from tests.factories import make_monitor_page
    manager = MonitorManager(db)
    page = make_monitor_page(db, manager)
    try:
        tid = manager.create_task(123, 'demo.exe', ['num_handles'], 1)
        manager.task_stopped.emit(tid, StopReason.USER)
        card = page.board.card(tid)
        assert card.is_finished
        page.presenter.on_clear_finished()
        assert page.board.card(tid) is None
        page.presenter.undo_clear_finished()
        assert page.board.card(tid) is card
        assert page.presenter._tasks[tid].finished
    finally:
        manager.stop_all_tasks(); manager.join_all(3000)
        dispose_widget(page)


def test_form_scenario_and_build_survive_start_request(qapp):
    services = make_services()
    services.process_query.processes = [(123, 'demo.exe', 12.)]
    form = NewTaskForm(services.process_query, services.preferences)
    requests = []
    try:
        form.picker.set_text('123')
        form.set_metrics_summary(['num_handles'])
        form.scenario_edit.setText('打开模型')
        form.build_edit.setText('2026.11')
        form.start_requested.connect(requests.append)
        form.submit()
        assert requests[0].scenario == '打开模型' and requests[0].build == '2026.11'
    finally:
        dispose_widget(form)


def test_wait_failure_is_not_reported_as_success_without_shared_markers(qapp, db, tmp_path):
    services = make_services()
    form = NewTaskForm(services.process_query, services.preferences)
    signals = _WaitSignals()
    presenter = SimpleNamespace(wait_service=signals, manager=SimpleNamespace(),
                                on_wait_requested=lambda request: 'failed-wait', wait_ids=lambda: [])
    flow = PlanFlow(db, presenter, form, services, store=PlanStore(tmp_path))
    plan = TestPlan('等待失败', (PlanTarget(name='app.exe', wait=True),), shared_markers=False)
    try:
        flow.store.save(plan); flow.refresh(plan.name)
        results = flow.run_selected()
        assert results[0].task_id is None and results[0].error
    finally:
        dispose_widget(form)
        flow.deleteLater()


def test_name_group_wait_plan_starts_group_even_without_instances(qapp, db, tmp_path):
    services = make_services()
    form = NewTaskForm(services.process_query, services.preferences)
    signals = _WaitSignals()
    requests = []
    presenter = SimpleNamespace(wait_service=signals, manager=SimpleNamespace(),
                                start_monitoring=lambda request: requests.append(request) or 'group-task',
                                on_wait_requested=lambda request: pytest.fail('同名组不走单实例等待'),
                                wait_ids=lambda: [])
    flow = PlanFlow(db, presenter, form, services, store=PlanStore(tmp_path))
    plan = TestPlan('同名组', (PlanTarget(name='worker.exe', target_kind='name', wait=True),), False)
    try:
        flow.store.save(plan); flow.refresh(plan.name)
        [result] = flow.run_selected()
        assert result.task_id == 'group-task' and not result.error
        assert requests[0].pid == 0 and requests[0].target_kind == 'name'
    finally:
        dispose_widget(form)
        flow.deleteLater()


def test_old_clear_undo_never_restores_a_different_batch(qapp, db):
    from core.contracts import StopReason
    from tests.factories import make_monitor_page
    manager = MonitorManager(db)
    page = make_monitor_page(db, manager)
    try:
        first = manager.create_task(1, 'first.exe', ['num_handles'], 1)
        manager.task_stopped.emit(first, StopReason.USER)
        page.presenter.on_clear_finished()
        old_generation = page.presenter._clear_generation
        second = manager.create_task(2, 'second.exe', ['num_handles'], 1)
        manager.task_stopped.emit(second, StopReason.USER)
        page.presenter.on_clear_finished()
        assert page.presenter.undo_clear_finished(old_generation) is False
        assert page.board.card(second) is None
        assert page.presenter.undo_clear_finished() is True
        assert page.board.card(second) is not None and page.board.card(first) is None
    finally:
        manager.stop_all_tasks(); manager.join_all(3000)
        dispose_widget(page)
