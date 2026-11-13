"""方案经真实表单选择、另存和覆盖后保持备注与等待超时。"""
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from core.plans import MAX_NOTE_CHARS, MAX_WAIT_TIMEOUT_S, PlanStore, PlanTarget, TestPlan
from tests.factories import make_services
from tests.helpers import dispose_widget
from ui.presenters.plan_flow import PlanFlow
from ui.widgets.monitor.new_task_form import NewTaskForm


class WaitSignals(QObject):
    started = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)


@pytest.fixture
def plan_form(qapp, db, tmp_path):
    services = make_services()
    form = NewTaskForm(services.process_query, services.preferences)
    form.set_metrics_summary(['memory_private'])
    presenter = SimpleNamespace(wait_service=WaitSignals())
    flow = PlanFlow(db, presenter, form, services, store=PlanStore(tmp_path))
    yield form, flow, services
    flow.deleteLater()
    dispose_widget(form)


@pytest.mark.parametrize('overwrite', [False, True])
@pytest.mark.parametrize('timeout', [None, 1.23456789012345, 45.125, MAX_WAIT_TIMEOUT_S])
@pytest.mark.parametrize('character', ['中', '🧪'])
def test_select_save_reload_keeps_full_note_and_timeout(plan_form, overwrite, timeout, character):
    form, flow, services = plan_form
    note = character * MAX_NOTE_CHARS
    original = TestPlan('原方案', (PlanTarget(name='later.exe', metrics=('memory_private',),
                                          note=note, wait=True, wait_timeout_s=timeout),))
    flow.store.save(original)
    flow.refresh(original.name)
    flow.select(original.name)
    assert form.note() == note and form.wait_timeout_s() == timeout
    if overwrite:
        services.dialogs.push('confirm', True)
    else:
        flow.name.setText('另存方案')
    saved = flow.save_current()
    reloaded = next(plan for plan in flow.store.list() if plan.name == saved.name)
    assert reloaded.targets == original.targets


@pytest.mark.parametrize('character', ['中', '🧪'])
def test_note_character_limit_is_same_for_wait_submission_and_plan_save(plan_form, character):
    form, flow, _ = plan_form
    form.set_wait_enabled(True)
    form.fill_search('later.exe')
    requests = []
    form.wait_requested.connect(requests.append)
    # ASCII 前缀使 Qt 的 4000 单元保护上限内也能放入 2001 个合法 Unicode 字符。
    note = 'abc' + character * (MAX_NOTE_CHARS - 2)
    form.note_input.setText(note)
    form.submit()
    assert not requests and '测试备注不能超过' in form.picker.error_text()
    with pytest.raises(ValueError):
        flow.current_target()
    form.note_input.setText(character * MAX_NOTE_CHARS)
    form.submit()
    assert len(requests) == 1 and requests[0].note == character * MAX_NOTE_CHARS


def test_select_unlimited_plan_clears_previous_timeout(plan_form):
    form, flow, _ = plan_form
    plans = [TestPlan('有限', (PlanTarget(name='x.exe', wait=True, wait_timeout_s=42.5),)),
             TestPlan('不限', (PlanTarget(name='x.exe', wait=True),))]
    for plan in plans:
        flow.store.save(plan)
    flow.refresh()
    flow.select('有限')
    assert form.wait_timeout_s() == 42.5
    flow.select('不限')
    assert form.wait_timeout_input.text() == '' and flow.current_target().wait_timeout_s is None


@pytest.mark.parametrize('text, expected', [('', None), ('1', 1), (' 45.125 ', 45.125),
                                            (str(MAX_WAIT_TIMEOUT_S), MAX_WAIT_TIMEOUT_S)])
def test_wait_submission_contains_visible_timeout(plan_form, text, expected):
    form, _, _ = plan_form
    requests = []
    form.wait_requested.connect(requests.append)
    form.set_wait_enabled(True)
    form.fill_search('later.exe')
    form.wait_timeout_input.setText(text)
    form.submit()
    assert len(requests) == 1 and requests[0].timeout_s == expected
    assert not form.wait_timeout_box.isHidden() and form.wait_timeout_input.isEnabled()


@pytest.mark.parametrize('text', ['nan', 'inf', '-inf', '0', '0.5', '-1', '2592001', 'abc'])
def test_invalid_wait_timeout_does_not_submit(plan_form, text):
    form, _, _ = plan_form
    requests = []
    form.wait_requested.connect(requests.append)
    form.set_wait_enabled(True)
    form.fill_search('later.exe')
    form.wait_timeout_input.setText(text)
    form.submit()
    assert not requests
    assert '等待超时' in form.picker.error_text()


def test_wait_timeout_hidden_and_disabled_outside_wait_mode(plan_form):
    form, _, services = plan_form
    assert form.wait_timeout_box.isHidden() and not form.wait_timeout_input.isEnabled()
    form.set_wait_enabled(True)
    assert not form.wait_timeout_box.isHidden() and form.wait_timeout_input.isEnabled()
    form.set_wait_enabled(False)
    assert form.wait_timeout_box.isHidden() and not form.wait_timeout_input.isEnabled()
    # 停用等待后的普通监控不受保留的输入约束。
    services.process_query.processes = [(100, 'now.exe', 100.0)]
    form.picker.refresh()
    form.fill_search('100')
    form.wait_timeout_input.setText('nan')
    requests = []
    form.start_requested.connect(requests.append)
    form.submit()
    assert len(requests) == 1 and requests[0].pid == 100
