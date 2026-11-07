"""
新建任务表单：发出的 TaskRequest、无效目标的信号、指标摘要、周期范围与默认值、
折叠/展开、"自动化"折叠区（方案拼装、就近校验、回填、折叠时不生效）。表单不调 manager、不弹对话框。
"""
import pytest

from core.automation.plan import AutomationPlan
from tests.doubles import FakePreferences, FakeProcessQuery
from ui.formatting import plan_summary
from ui.widgets.monitor.new_task_form import NewTaskForm, TaskRequest
from ui.widgets.monitor.process_picker import (
    ERROR_EMPTY, ERROR_GROUP_NEEDS_NAME, ERROR_NOT_FOUND)
from utils.metrics import LEAK_CHECK_PRESET, PROCESS_COUNT, MetricType

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "beta.exe", 1002.5)]


@pytest.fixture
def prefs():
    return FakePreferences(default_interval=3)


@pytest.fixture
def form(qapp, prefs):
    widget = NewTaskForm(FakeProcessQuery(PROCESSES), prefs)
    widget.picker.refresh()
    widget.requests, widget.invalid, widget.waits = [], [], []
    widget.start_requested.connect(widget.requests.append)
    widget.wait_requested.connect(widget.waits.append)
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


# ---------- 阶段快照 ----------


def test_stage_snapshot_row_builds_plan_and_summary(form):
    form.picker.set_text("1001")
    auto = form.automation
    assert not auto.snapshot_hours_spin.isEnabled() and not auto.snapshot_dir_edit.isEnabled()
    auto.snapshot_check.setChecked(True)
    assert auto.snapshot_hours_spin.isEnabled() and auto.snapshot_browse_button.isEnabled()
    auto.snapshot_hours_spin.setValue(6)
    auto.snapshot_dir_edit.setText("  D:\快照 ")

    form.start_button.click()

    [request] = form.requests
    assert request.plan == AutomationPlan(stage_snapshot_hours=6.0,
                                          stage_snapshot_dir="D:\快照")
    assert auto.summary_label.text() == "每 6 小时写阶段快照"


def test_stage_snapshot_unchecked_does_not_enter_plan(form):
    form.automation.snapshot_dir_edit.setText("D:\快照")
    assert form.automation_plan() is None


def test_stage_snapshot_without_dir_shows_error_near_section(form):
    form.show()
    form.picker.set_text("1001")
    form.automation.snapshot_check.setChecked(True)
    form.start_button.click()
    assert form.requests == []
    assert form.automation.error_text() == "已勾选阶段快照，请选择快照目录"
    assert form.automation.error_label.isVisible()


def test_stage_snapshot_set_plan_round_trip(form):
    plan = AutomationPlan(stage_snapshot_hours=0.5, stage_snapshot_dir="D:\快照")
    form.set_automation_plan(plan)
    assert form.automation.snapshot_check.isChecked()
    assert form.automation_plan() == plan
    form.set_automation_plan(None)
    assert not form.automation.snapshot_check.isChecked()


def test_browse_snapshot_dir_only_emits_and_fill_checks_option(form):
    received = []
    form.browse_snapshot_dir_requested.connect(lambda: received.append("snap"))
    form.automation.snapshot_check.setChecked(True)
    form.automation.snapshot_browse_button.click()
    assert received == ["snap"]
    form.automation.snapshot_check.setChecked(False)
    form.set_snapshot_dir("D:\快照")
    assert form.snapshot_dir() == "D:\快照" and form.automation.snapshot_check.isChecked()


# ---------- 存储预估 ----------


def test_storage_estimate_follows_metrics_and_interval(form):
    assert form.storage_estimate_text() == ""          # 没选指标：不提示
    metrics = [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_THREADS,
               MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS]
    form.interval_spinbox.setValue(1)
    form.set_metrics_summary(metrics)
    assert form.storage_estimate_text() == "每小时约 18,000 行 · 12 小时约 4 MB"
    assert "24 小时约 8 MB" in form.storage_label.toolTip()

    form.interval_spinbox.setValue(2)                  # 改间隔：实时刷新
    assert form.storage_estimate_text() == "每小时约 9,000 行 · 12 小时约 2 MB"
    form.set_metrics_summary(metrics[:1])              # 改指标数：实时刷新
    assert form.storage_estimate_text() == "每小时约 1,800 行 · 12 小时约 464 KB"
    form.set_metrics_summary([])
    assert form.storage_estimate_text() == "" and form.storage_label.toolTip() == ""


def test_storage_estimate_sits_next_to_metric_summary(form):
    form.show()
    form.set_metrics_summary(LEAK_CHECK_PRESET)
    assert form.storage_label.isVisible() and form.storage_label.text()
    summary = form.metric_summary_label.geometry()
    estimate_geometry = form.storage_label.geometry()
    assert estimate_geometry.left() >= summary.right()
    assert abs(estimate_geometry.center().y() - summary.center().y()) <= 2


# ---------- 启动被测程序 ----------

def test_launch_section_is_collapsed_and_off_by_default(form):
    assert not form.launch_options.is_expanded()
    assert not form.launch_enabled()
    assert form.launch_options.summary_text() == "未启用"
    assert form.start_button.text() == "开始监控"


def test_launch_submit_sends_spec_instead_of_picked_process(form):
    from core.process_launcher import LaunchSpec
    form.picker.set_text("1001")
    form.set_launch_exe(r"D:\app\demo.exe")
    form.launch_options.args_edit.setText('--mode soak "C:\\Program Files\\x"')
    form.set_launch_cwd(r"D:\work")
    form.launch_options.close_check.setChecked(True)

    assert form.start_button.text() == "启动并监控"
    # 先启动程序时用不着等待进程
    assert not form.wait_checkbox.isEnabled()
    form.submit()

    [request] = form.requests
    assert request.launch == LaunchSpec(
        exe=r"D:\app\demo.exe", args=("--mode", "soak", "C:\\Program Files\\x"),
        cwd=r"D:\work")
    assert request.close_on_stop is True
    assert request.pid == 0 and request.create_time is None
    assert request.name == "demo.exe"
    assert request.interval == 3


def test_launch_without_exe_shows_error_in_section(form):
    form.launch_options.set_enabled(True)

    form.submit()

    assert form.requests == [] and form.invalid == []
    assert form.launch_error_text() == "请填写要启动的程序"
    assert form.launch_options.is_expanded()


def test_launch_error_clears_when_input_changes(form):
    form.set_launch_exe(r"D:\app\demo.exe")
    form.show_launch_error("找不到程序")
    assert form.launch_error_text() == "找不到程序"

    form.launch_options.args_edit.setText("-v")

    assert form.launch_error_text() == ""


def test_unchecking_launch_restores_normal_start(form):
    form.set_launch_exe(r"D:\app\demo.exe")
    form.launch_options.set_enabled(False)
    form.picker.set_text("1001")

    form.submit()

    assert form.start_button.text() == "开始监控"
    assert form.wait_checkbox.isEnabled()
    [request] = form.requests
    assert request.launch is None and request.pid == 1001


def test_collapsed_form_ignores_launch_settings(form):
    """折叠后藏起来的启动设置不生效（与"等待进程启动"一致）。"""
    form.set_launch_exe(r"D:\app\demo.exe")
    form.set_collapsed(True)
    form.picker.set_text("1001")

    form.submit()

    [request] = form.requests
    assert request.launch is None


def test_set_launch_spec_fills_section_and_expands(form):
    from core.process_launcher import LaunchSpec
    form.set_collapsed(True)
    spec = LaunchSpec(exe=r"D:\app\demo.exe", args=("a b", "c"), cwd=None)

    form.set_launch_spec(spec, close_on_stop=True)

    assert not form.is_collapsed() and form.launch_options.is_expanded()
    assert form.launch_options.spec() == (spec, None)
    assert form.launch_options.close_on_stop()
    assert form.launch_options.summary_text() == "开始时启动 demo.exe · 停止时关闭"


def test_launch_browse_buttons_only_emit_signals(form):
    received = []
    form.browse_exe_requested.connect(lambda: received.append("exe"))
    form.browse_cwd_requested.connect(lambda: received.append("cwd"))
    form.launch_options.set_enabled(True)

    form.launch_options.exe_browse_button.click()
    form.launch_options.cwd_browse_button.click()

    assert received == ["exe", "cwd"]


# ========== 目标类型：单个进程 / 进程树 / 同名进程 ==========

PEAK = MetricType.MEMORY_PEAK_WSET
PRIORITY = MetricType.CPU_PRIORITY


def _pick_kind(form, kind):
    """像用户点分段控件那样切换目标类型。"""
    form.kind_segmented.setCurrentItem(kind)
    assert form.target_kind() == kind


def test_kind_segmented_offers_three_types_default_pid(form):
    assert [form.kind_segmented.items[k].text() for k in ('pid', 'tree', 'name')] == [
        "单个进程", "进程树", "同名进程"]
    assert form.target_kind() == 'pid'


def test_pid_request_keeps_pid_kind_and_unfiltered_metrics(form):
    form.set_metrics_summary([MetricType.CPU_PERCENT, PEAK, PRIORITY])
    form.picker.set_text("beta.exe · PID 1002")
    form.start_button.click()
    request = form.requests[0]
    assert request.target_kind == 'pid'
    assert request.metrics == (MetricType.CPU_PERCENT, PEAK, PRIORITY)     # pid 不过滤、不加进程总数
    assert PROCESS_COUNT not in request.metrics


def test_tree_request_filters_metrics_and_adds_process_count(form):
    form.set_metrics_summary([MetricType.CPU_PERCENT, PEAK, PRIORITY, MetricType.MEMORY_RSS])
    _pick_kind(form, 'tree')
    form.picker.set_text("beta.exe · PID 1002")
    form.start_button.click()
    assert form.requests == [TaskRequest(
        pid=1002, name="beta.exe", create_time=1002.5,
        metrics=(MetricType.CPU_PERCENT, MetricType.MEMORY_RSS, PROCESS_COUNT),
        interval=3.0, note="", target_kind='tree')]


def test_name_request_takes_selected_process_name(form):
    form.set_metrics_summary([MetricType.CPU_PERCENT, PEAK])
    _pick_kind(form, 'name')
    form.picker.set_text("beta.exe · PID 1002")
    form.start_button.click()
    assert form.requests == [TaskRequest(
        pid=0, name="beta.exe", create_time=None,
        metrics=(MetricType.CPU_PERCENT, PROCESS_COUNT), interval=3.0, note="",
        target_kind='name')]


def test_name_request_accepts_typed_name_of_process_not_running(form):
    _pick_kind(form, 'name')
    form.picker.set_text("chrome.exe")                       # 此刻没有这个进程
    form.start_button.click()
    assert [(r.pid, r.name, r.create_time, r.target_kind) for r in form.requests] == [
        (0, "chrome.exe", None, 'name')]
    assert form.invalid == []


def test_name_without_extension_completes_to_listed_process(form):
    _pick_kind(form, 'name')
    form.picker.set_text("beta")
    form.start_button.click()
    assert form.requests[0].name == "beta.exe"


def test_name_kind_needs_a_name_not_a_pid(form):
    _pick_kind(form, 'name')
    form.start_button.click()
    form.picker.set_text("1002")                             # 纯数字不是进程名
    form.start_button.click()
    assert form.requests == []
    assert [e.kind for e in form.invalid] == [ERROR_GROUP_NEEDS_NAME, ERROR_GROUP_NEEDS_NAME]


def test_tree_kind_still_requires_existing_process(form):
    _pick_kind(form, 'tree')
    form.picker.set_text("999999")
    form.start_button.click()
    assert form.requests == [] and [e.kind for e in form.invalid] == [ERROR_NOT_FOUND]


def test_group_metrics_are_kept_apart_from_pid_metrics(form):
    form.set_metrics_summary([MetricType.CPU_PERCENT, PEAK])
    _pick_kind(form, 'tree')
    assert form.metrics() == [MetricType.CPU_PERCENT, PROCESS_COUNT]    # 沿用时剔除不适用的
    form.set_metrics_summary([MetricType.MEMORY_RSS])
    assert form.metrics() == [MetricType.MEMORY_RSS, PROCESS_COUNT]
    _pick_kind(form, 'pid')
    assert form.metrics() == [MetricType.CPU_PERCENT, PEAK]             # 单个进程的选择没被改动
    assert form.metric_summary_label.text() == "已选 2 项"


def test_group_metric_summary_counts_process_count(form):
    _pick_kind(form, 'tree')
    form.set_metrics_summary([MetricType.MEMORY_RSS])
    assert form.metric_summary_label.text() == "已选 2 项"
    assert "进程总数" in form.metric_summary_label.toolTip()


def test_storage_estimate_is_per_member_for_groups(form):
    form.set_metrics_summary([MetricType.CPU_PERCENT, MetricType.MEMORY_RSS])
    pid_text = form.storage_estimate_text()
    assert pid_text and "成员" not in pid_text
    _pick_kind(form, 'tree')
    group_text = form.storage_estimate_text()
    assert group_text.startswith("按 3 种成员估算（每种成员另加一份）：")
    assert "按成员数估算，每种成员另加一份" in form.storage_label.toolTip()
    _pick_kind(form, 'pid')
    assert form.storage_estimate_text() == pid_text


def test_name_kind_disables_wait_and_launch(form):
    form.show()
    form.set_wait_enabled(True)
    _pick_kind(form, 'name')
    assert not form.wait_enabled() and not form.wait_checkbox.isEnabled()
    assert not form.launch_enabled() and not form.launch_options.isEnabled()
    form.picker.set_text("alpha.exe")
    form.start_button.click()
    assert [r.target_kind for r in form.requests] == ['name'] and form.waits == []
    _pick_kind(form, 'tree')
    assert form.wait_checkbox.isEnabled() and form.launch_options.isEnabled()


def test_tree_kind_waits_by_name_with_tree_kind(form):
    form.show()
    _pick_kind(form, 'tree')
    form.set_wait_enabled(True)
    form.picker.set_text("later.exe")
    form.start_button.click()
    assert [(w.name, w.target_kind) for w in form.waits] == [("later.exe", 'tree')]
    assert PROCESS_COUNT in form.waits[0].metrics


def test_target_kind_changed_signal_and_unknown_kind(form):
    seen = []
    form.target_kind_changed.connect(seen.append)
    _pick_kind(form, 'tree')
    form.set_target_kind('name')
    form.set_target_kind('name')                             # 没变不再发
    assert seen == ['tree', 'name']
    with pytest.raises(ValueError):
        form.set_target_kind('bogus')


def test_name_kind_placeholder_asks_for_a_name(form):
    plain = form.picker.combo.line_edit.placeholderText()
    _pick_kind(form, 'name')
    assert "进程名" in form.picker.combo.line_edit.placeholderText()
    _pick_kind(form, 'pid')
    assert form.picker.combo.line_edit.placeholderText() == plain
