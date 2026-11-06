"""
新建任务表单：发出的 TaskRequest、无效目标的信号、指标摘要、周期范围与默认值、
折叠/展开。表单不调 manager、不弹对话框。
"""
import pytest

from tests.doubles import FakePreferences, FakeProcessQuery
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
