"""
新建任务表单：发出的 TaskRequest、无效目标的信号、指标摘要、周期范围与默认值、
折叠/展开、"自动化"折叠区（方案拼装、就近校验、回填、折叠时不生效）。表单不调 manager、不弹对话框。
"""
import pytest

from core.automation.plan import AutomationPlan
from tests.doubles import FakePreferences, FakeProcessQuery
from ui.formatting import plan_summary
from ui.widgets.monitor.new_task_form import NewTaskForm, TaskRequest
from ui.widgets.monitor.process_picker import ERROR_EMPTY, ERROR_NOT_FOUND
from utils.metrics import LEAK_CHECK_PRESET, MetricType

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "beta.exe", 1002.5)]


@pytest.fixture
def prefs():
    return FakePreferences(default_interval=3)


@pytest.fixture
def form(qapp, prefs):
    widget = NewTaskForm(FakeProcessQuery(PROCESSES), prefs)
    widget.picker.refresh()
    widget.requests, widget.invalid = [], []
    widget.start_requested.connect(widget.requests.append)
    widget.target_invalid.connect(widget.invalid.append)
    widget.resize(900, 300)
    yield widget
    widget.close()


def test_start_emits_task_request(form):
    form.set_metrics_summary([MetricType.NUM_THREADS])
    form.picker.set_text("beta.exe · PID 1002")
    form.note_input.setText("  建模 200 次  ")

    form.start_button.click()

    assert form.requests == [TaskRequest(
        pid=1002, name="beta.exe", create_time=1002.5,
        metrics=(MetricType.NUM_THREADS,), interval=3.0, note="建模 200 次")]
    assert form.invalid == []


def test_note_return_submits(form):
    form.picker.set_text("1001")
    form.note_input.returnPressed.emit()
    assert [r.pid for r in form.requests] == [1001]


def test_invalid_target_emits_error_not_request(form):
    form.start_button.click()
    form.picker.set_text("999999")
    form.start_button.click()
    assert form.requests == []
    assert [e.kind for e in form.invalid] == [ERROR_EMPTY, ERROR_NOT_FOUND]


def test_metric_summary(form):
    form.set_metrics_summary(LEAK_CHECK_PRESET)
    assert "泄漏排查预设" in form.metric_summary_label.text()
    form.set_metrics_summary([MetricType.CPU_PERCENT, MetricType.MEMORY_RSS])
    assert form.metric_summary_label.text() == "已选 2 项"
    assert form.metrics() == [MetricType.CPU_PERCENT, MetricType.MEMORY_RSS]
    form.set_metrics_summary([])
    assert form.metric_summary_label.text() == "未选择指标"


def test_choose_metrics_button_only_emits(form):
    received = []
    form.choose_metrics_requested.connect(lambda: received.append(True))
    form.metric_button.click()
    assert received == [True]


def test_interval_range_and_default_come_from_preferences(qapp):
    prefs = FakePreferences(default_interval=7)
    prefs.interval_bounds = (2, 120)
    widget = NewTaskForm(FakeProcessQuery(), prefs)
    assert (widget.interval_spinbox.minimum(), widget.interval_spinbox.maximum()) == (2, 120)
    assert widget.interval_spinbox.value() == 7
    widget.set_default_interval(9)
    assert widget.interval() == 9.0
    widget.close()


def test_collapse_to_one_row_and_expand(form):
    form.show()
    assert form.details.isVisible() and not form.expand_button.isVisible()

    form.set_collapsed(True)
    assert form.is_collapsed()
    assert not form.details.isVisible() and not form.title_label.isVisible()
    # 折叠后仍可直接选进程并开始
    assert form.picker.isVisible() and form.start_button.isVisible()
    assert form.expand_button.isVisible()

    form.expand_button.click()
    assert form.details.isVisible() and not form.is_collapsed()
    form.expand_button.click()
    assert form.is_collapsed()

    form.set_collapsed(False)
    assert form.details.isVisible() and not form.expand_button.isVisible()


def test_show_error_and_fill_search(form):
    form.show()
    form.show_error("请输入进程名或 PID")
    assert form.picker.error_text() == "请输入进程名或 PID"
    form.fill_search("demo.exe")
    assert form.picker.text() == "demo.exe" and form.picker.error_text() == ""


# ---------- 键盘顺序 ----------

def test_tab_order_follows_visual_order(form):
    chain = [form.picker.combo.line_edit, form.interval_spinbox, form.metric_button,
             form.note_input, form.wait_checkbox, form.start_button]
    for first, second in zip(chain, chain[1:]):
        widget = first.nextInFocusChain()
        # SpinBox 等复合控件内部可能还有子控件：沿焦点链找到下一个声明的控件
        while widget is not second and widget is not first and widget.parent() is first:
            widget = widget.nextInFocusChain()
        assert widget is second, (first, second)


def test_interval_label_says_interval(form):
    from qfluentwidgets import CaptionLabel
    texts = [label.text() for label in form.details.findChildren(CaptionLabel)]
    assert "采集间隔（秒）" in texts


# ---------- 自动化折叠区 ----------


def _check_and_set(form, **values):
    auto = form.automation
    if 'timed' in values:
        auto.timed_check.setChecked(True)
        auto.timed_spin.setValue(values['timed'])
    if 'log' in values:
        auto.log_check.setChecked(True)
        auto.log_path_edit.setText(values['log'][0])
        auto.log_pattern_edit.setText(values['log'][1])
    if 'minutes' in values:
        auto.stop_time_check.setChecked(True)
        auto.stop_minutes_spin.setValue(values['minutes'])
    if 'rounds' in values:
        auto.stop_rounds_check.setChecked(True)
        auto.stop_rounds_spin.setValue(values['rounds'])
    if 'export' in values:
        auto.export_check.setChecked(True)
        auto.export_dir_edit.setText(values['export'])


def test_automation_section_is_collapsed_and_off_by_default(form):
    form.show()
    auto = form.automation
    assert not auto.is_expanded() and not auto.body.isVisible()
    assert auto.summary_label.text() == "未启用"
    assert form.automation_plan() is None
    # 未勾选的项置灰
    assert not auto.timed_spin.isEnabled() and not auto.log_path_edit.isEnabled()
    auto.toggle_button.click()
    assert auto.is_expanded() and auto.body.isVisible()


def test_request_carries_plan_built_from_inputs(form):
    form.picker.set_text("1001")
    _check_and_set(form, timed=600, log=("  C:\\logs\\app.log ", r"Case (?P<label>\d+)"),
                   minutes=90, rounds=12, export="D:\\out")
    form.automation.log_encoding_combo.setCurrentIndex(1)

    form.start_button.click()

    [request] = form.requests
    assert request.plan == AutomationPlan(
        timed_marker_seconds=600, log_path="C:\\logs\\app.log",
        log_pattern=r"Case (?P<label>\d+)", log_encoding="gbk", stop_after_seconds=90 * 60,
        stop_after_rounds=12, export_on_stop=True, export_dir="D:\\out")
    assert form.automation.summary_label.text() == plan_summary(request.plan)


def test_unchecked_items_do_not_enter_plan(form):
    auto = form.automation
    auto.log_path_edit.setText("C:\\a.log")        # 填了但没勾选
    auto.timed_spin.setValue(30)
    assert form.automation_plan() is None


def test_invalid_plan_shows_error_near_section_and_blocks_start(form):
    form.show()
    form.picker.set_text("1001")
    _check_and_set(form, timed=1, log=("relative.log", "x"))   # 周期 3 秒

    form.start_button.click()

    assert form.requests == [] and form.invalid == []
    error = form.automation.error_text()
    assert "不能小于采集间隔（3 秒）" in error and "绝对路径" in error
    assert form.automation.is_expanded() and form.automation.error_label.isVisible()
    # 改输入后旧错误清掉；改对了即可开始
    form.automation.timed_spin.setValue(5)
    assert form.automation.error_text() == ""
    form.automation.log_path_edit.setText("C:\\logs\\a.log")
    form.start_button.click()
    assert len(form.requests) == 1 and form.automation.error_text() == ""


def test_target_error_takes_precedence_over_plan_error(form):
    _check_and_set(form, timed=1)
    form.start_button.click()
    assert [e.kind for e in form.invalid] == [ERROR_EMPTY]
    assert form.automation.error_text() == ""


def test_collapsed_form_ignores_hidden_plan(form):
    form.show()
    form.picker.set_text("1001")
    _check_and_set(form, timed=600)
    form.set_collapsed(True)
    form.start_button.click()
    assert form.requests[-1].plan is None
    form.expand_button.click()
    form.start_button.click()
    assert form.requests[-1].plan == AutomationPlan(timed_marker_seconds=600)


def test_set_automation_plan_round_trip_and_expands(form):
    form.show()
    form.set_collapsed(True)
    plan = AutomationPlan(timed_marker_seconds=45, log_path="C:\\a.log", log_pattern="ok",
                          log_encoding="cp936", stop_after_seconds=7200, stop_after_rounds=3,
                          export_on_stop=True, export_dir="D:\\out")
    form.set_automation_plan(plan)
    assert not form.is_collapsed() and form.automation.is_expanded()
    assert form.automation_plan() == plan

    form.set_automation_plan(None)
    assert form.automation_plan() is None


def test_wait_request_carries_plan(form):
    waits = []
    form.wait_requested.connect(waits.append)
    form.set_wait_enabled(True)
    form.picker.set_text("late.exe")
    _check_and_set(form, rounds=5)
    form.start_button.click()
    assert waits[0].plan == AutomationPlan(stop_after_rounds=5)


def test_browse_buttons_only_emit(form):
    received = []
    form.browse_log_requested.connect(lambda: received.append("log"))
    form.browse_export_dir_requested.connect(lambda: received.append("dir"))
    form.automation.log_check.setChecked(True)
    form.automation.export_check.setChecked(True)
    form.automation.log_browse_button.click()
    form.automation.export_browse_button.click()
    assert received == ["log", "dir"]
    form.set_log_path("C:\\x\\a.log")
    form.set_export_dir("D:\\out")
    assert (form.log_path(), form.export_dir()) == ("C:\\x\\a.log", "D:\\out")


def test_set_log_path_checks_the_option(form):
    form.automation.set_log_path("C:\\x\\a.log")
    assert form.automation.log_check.isChecked() and form.automation.log_path_edit.isEnabled()


def test_automation_comes_after_start_in_tab_order(form):
    chain = [form.expand_button, *form.automation.focus_chain()]
    for first, second in zip(chain, chain[1:]):
        widget = first.nextInFocusChain()
        while widget is not second and widget is not first and widget.parent() is first:
            widget = widget.nextInFocusChain()
        assert widget is second, (first, second)
