"""
指标选择对话框的进程组模式：峰值类与基本优先级置灰并说明原因，进程总数始终选中，
get_selected 在末尾带进程总数；单个进程模式不受影响。
"""
import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget

from ui.components.metric_selector import (
    GROUP_HINT, GROUP_UNSUPPORTED_TIP, PROCESS_COUNT_TIP, MetricSelectorDialog)
from utils.metrics import (
    AVAILABLE_METRICS, LEAK_CHECK_PRESET, PROCESS_COUNT, MetricType, all_metric_types,
    group_selectable_metrics)

UNSUPPORTED = sorted(set(all_metric_types()) - set(group_selectable_metrics()))


@pytest.fixture
def host(qapp):
    widget = QWidget()
    widget.resize(800, 600)
    yield widget
    widget.close()


def make_dialog(host, selected, group):
    dialog = MetricSelectorDialog(selected, host, group_mode=group)
    return dialog


def test_unsupported_metrics_are_disabled_with_reason(host):
    dialog = make_dialog(host, [MetricType.CPU_PERCENT, MetricType.MEMORY_PEAK_WSET], True)
    assert UNSUPPORTED                                       # 峰值类与基本优先级
    for metric in UNSUPPORTED:
        box = dialog.metric_checkboxes[metric]
        assert not box.isEnabled() and not box.isChecked()
        assert GROUP_UNSUPPORTED_TIP in box.toolTip()
    assert dialog.metric_checkboxes[MetricType.CPU_PERCENT].isEnabled()
    assert dialog.group_hint_label is not None and dialog.group_hint_label.text() == GROUP_HINT


def test_process_count_is_checked_disabled_and_always_returned(host):
    dialog = make_dialog(host, [MetricType.CPU_PERCENT], True)
    box = dialog.metric_checkboxes[PROCESS_COUNT]
    assert box.isChecked() and not box.isEnabled() and PROCESS_COUNT_TIP in box.toolTip()
    assert dialog.get_selected() == [MetricType.CPU_PERCENT, PROCESS_COUNT]
    assert "进程组" in dialog.category_checkboxes


def test_process_count_alone_does_not_enable_confirm(host):
    dialog = make_dialog(host, [], True)
    assert not dialog.yesButton.isEnabled()                  # 进程总数不算"用户选了指标"
    dialog.metric_checkboxes[MetricType.CPU_PERCENT].setChecked(True)
    assert dialog.yesButton.isEnabled()


def test_preset_and_category_checks_skip_disabled_metrics(host):
    dialog = make_dialog(host, [], True)
    dialog.preset_button.click()
    assert dialog.get_selected() == [*[m for m in all_metric_types() if m in LEAK_CHECK_PRESET],
                                     PROCESS_COUNT]
    memory = dialog.category_checkboxes["内存"]
    memory.setCheckState(Qt.Checked)
    memory.clicked.emit(True)
    selected = set(dialog.get_selected())
    assert set(AVAILABLE_METRICS["内存"]) & selected == (
        set(AVAILABLE_METRICS["内存"]) & set(group_selectable_metrics()))
    assert not selected & set(UNSUPPORTED)


def test_preselected_unsupported_metric_is_dropped_in_group_mode(host):
    dialog = make_dialog(host, [MetricType.MEMORY_PEAK_WSET, MetricType.NUM_HANDLES], True)
    assert dialog.get_selected() == [MetricType.NUM_HANDLES, PROCESS_COUNT]


def test_pid_mode_is_unchanged(host):
    dialog = make_dialog(host, [MetricType.MEMORY_PEAK_WSET, MetricType.CPU_PERCENT], False)
    assert dialog.group_hint_label is None
    assert PROCESS_COUNT not in dialog.metric_checkboxes
    assert "进程组" not in dialog.category_checkboxes
    assert all(dialog.metric_checkboxes[m].isEnabled() for m in UNSUPPORTED)
    assert set(dialog.get_selected()) == {MetricType.CPU_PERCENT, MetricType.MEMORY_PEAK_WSET}
