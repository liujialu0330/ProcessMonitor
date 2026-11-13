"""场景参考与任务库交互：真实控件、替身确认框、隔离数据库。"""
import pytest
import threading

from core.monitor_manager import MonitorManager
from core.reference import ReferenceComparison
from tests.factories import make_services
from tests.helpers import dispose_widget, process_events_until
from tests.test_reference_library import task
from ui.models.task_options import TaskOptionsModel
from ui.models.task_options import TaskOption
from ui.widgets.task_selector import TaskSelector
from ui.widgets.analysis.reference_controls import ReferenceControls
from ui.widgets.analysis.task_library import TaskLibraryPanel


@pytest.fixture
def env(db, qapp):
    manager = MonitorManager(db, target_probe=lambda *_a, **_kw: None)
    services = make_services()
    widgets = []
    yield manager, services, widgets
    services.threads.cancel_all()
    services.threads.shutdown_all()
    for widget in widgets:
        dispose_widget(widget)


def test_reference_controls_save_and_star_and_reference(db, env):
    manager, services, widgets = env
    record = task(db, 'a')
    controls = ReferenceControls(manager, services)
    widgets.append(controls)
    controls.set_task(record)
    controls.scenario_edit.setText('新场景')
    controls.build_edit.setText('2.1')
    controls.save_button.click()
    assert db.tasks.get('a').scenario == '新场景'
    assert db.tasks.get('a').build == '2.1'
    controls.set_task(db.tasks.get('a'))
    controls.reference_button.click()
    assert db.tasks.reference_for('新场景').task_id == 'a'
    controls.star_button.click()
    assert db.tasks.get('a').starred


def test_reference_controls_ignore_previous_task_result(db, env):
    manager, services, widgets = env
    controls = ReferenceControls(manager, services)
    widgets.append(controls)
    controls.set_task(task(db, 'a'))
    controls.set_result(ReferenceComparison('old', 'ref', '打开窗口'))
    assert not controls.compare_button.isEnabled()
    controls.set_result(ReferenceComparison('a', 'ref', '打开窗口'))
    selected = []
    controls.compare_requested.connect(selected.append)
    controls.compare_button.click()
    assert selected == ['ref']


def test_library_search_scene_star_and_filtered_selection(db, env):
    manager, services, widgets = env
    task(db, 'a', scenario='窗口', build='2.1')
    task(db, 'b', scenario='页面', build='1.0')
    db.tasks.set_starred('b', True)
    panel = TaskLibraryPanel(manager, services)
    widgets.append(panel)
    panel.set_tasks(db.tasks.list_all())
    panel.search_edit.setText('2.1')
    assert len(panel._visible) == 1 and panel._visible[0].task_id == 'a'
    panel.select_button.click()
    assert [t.task_id for t in panel.selected_tasks()] == ['a']
    panel.search_edit.setText('1.0')
    assert panel.selected_tasks() == []
    panel.search_edit.clear()
    panel.starred_only.setChecked(True)
    assert [t.task_id for t in panel._visible] == ['b']


def test_reference_delete_requires_second_confirmation(db, env):
    manager, services, widgets = env
    task(db, 'a')
    db.tasks.set_reference('a')
    panel = TaskLibraryPanel(manager, services)
    widgets.append(panel)
    panel.set_tasks(db.tasks.list_all())
    panel.table.selectAll()
    services.dialogs.push('confirm', True, False)
    panel.delete_button.click()
    assert db.tasks.get('a') is not None
    assert len(services.dialogs.calls_of('confirm')) == 2
    assert not manager.is_maintenance_running()


def test_batch_delete_runs_in_background_and_releases_maintenance(db, env):
    manager, services, widgets = env
    task(db, 'a')
    task(db, 'b')
    panel = TaskLibraryPanel(manager, services)
    widgets.append(panel)
    panel.set_tasks(db.tasks.list_all())
    panel.table.selectAll()
    services.dialogs.push('confirm', True)
    panel.delete_button.click()
    assert process_events_until(lambda: panel._worker is None, timeout=3)
    assert db.tasks.list_all() == []
    assert not manager.is_maintenance_running()


def test_registered_deletion_cancel_rolls_back_and_releases_lock(db, env, monkeypatch):
    manager, services, _widgets = env
    task(db, 'a')
    entered, release = threading.Event(), threading.Event()
    delete = db.tasks.delete_many_stopped

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(3)
        return delete(*args, **kwargs)

    monkeypatch.setattr(db.tasks, 'delete_many_stopped', blocked)
    manager.start_task_deletion(['a'], services.threads)
    try:
        assert entered.wait(2)
        assert services.threads.entries()[0].cancel is not None
        services.threads.cancel_all()
    finally:
        release.set()
    assert process_events_until(lambda: not manager.is_maintenance_running(), timeout=3)
    assert db.tasks.get('a') is not None


def test_selector_search_covers_full_note_scene_and_version(db, env):
    _manager, services, widgets = env
    record = task(db, 'a', scenario='窗口反复开关', build='2.3.4')
    selector = TaskSelector()
    widgets.append(selector)
    options = TaskOptionsModel().rebuild([record], set(), services.clock.now())
    selector.set_options(options)
    selector.select_by_id('a', silent=True)
    selector.search_edit.setText('2.3.4')
    assert selector.count() == 1
    selector.search_edit.setText('不匹配')
    assert selector.count() == 0
    selector.search_edit.clear()
    assert selector.count() == 1
    assert selector.current_id() == 'a'


def test_grouping_keeps_recent_group_order_star_priority_and_id_refresh(env):
    _manager, _services, widgets = env
    selector = TaskSelector()
    widgets.append(selector)
    options = [
        TaskOption('b-new', 'B 最新', '', False, process_name='b.exe'),
        TaskOption('a-new', 'A 最新', '', False, process_name='a.exe'),
        TaskOption('b-old', 'B 较早', '', False, process_name='b.exe'),
        TaskOption('star', '星标任务', '', False, process_name='c.exe', starred=True),
        TaskOption('a-old', 'A 较早', '', False, process_name='a.exe'),
    ]
    selector.set_grouped(True)
    selector.set_options(options, keep_id='b-new')
    assert [item.task_id for item in selector.options()] == [
        'star', 'b-new', 'b-old', 'a-new', 'a-old']
    assert selector.current_id() == 'b-new'
    changed = TaskOption('a-new', 'A 刷新后', '', True, process_name='a.exe')
    selector.update_option(1, changed)
    assert selector.item_text(3) == 'A 刷新后'
    assert selector.item_text(1) == 'B 最新' and selector.current_id() == 'b-new'
