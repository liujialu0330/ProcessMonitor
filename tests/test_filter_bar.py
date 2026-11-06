"""
历史页筛选条控件 FilterBar：任务选择器槽位、指标下拉、时间范围、趋势/明细、刷新、
更多菜单（原始值、删除）的信号与静默设置；按自身宽度的单行/换行档位与防抖。
"""
import pytest
from qfluentwidgets import ComboBox

from ui.widgets.history.common import VIEWPORT_TO_CONTENT_OFFSET
from ui.widgets.history.filter_bar import (
    DEFAULT_RANGE_KEY, RANGE_OPTIONS, FilterBar)


@pytest.fixture
def bar(qapp):
    widget = FilterBar()
    yield widget
    widget.close()


def _record(signal):
    received = []
    signal.connect(lambda *args: received.append(args[0] if args else None))
    return received


def _position(bar, widget):
    layout = bar.select_layout
    return layout.getItemPosition(layout.indexOf(widget))


# ---------- 指标 ----------

def test_set_metrics_selects_current_without_signal(bar):
    selected = _record(bar.metric_selected)
    metrics = [("memory_private", "私有字节"), ("num_threads", "线程数")]

    assert bar.set_metrics(metrics, "num_threads") == "num_threads"
    assert bar.current_metric() == "num_threads"
    assert bar.metric_combo.isEnabled() is True
    assert [bar.metric_combo.itemText(i) for i in range(2)] == ["私有字节", "线程数"]

    # 不在列表里的首选回退首项
    assert bar.set_metrics(metrics, "nope") == "memory_private"
    assert selected == []


def test_user_metric_change_emits_key(bar):
    selected = _record(bar.metric_selected)
    bar.set_metrics([("memory_private", "私有字节"), ("num_threads", "线程数")])
    bar.metric_combo.setCurrentIndex(1)
    assert selected == ["num_threads"]


def test_empty_metrics_disable_combo(bar):
    bar.set_metrics([("memory_private", "私有字节")])
    assert bar.set_metrics([]) is None
    assert bar.metric_combo.count() == 0
    assert bar.metric_combo.isEnabled() is False
    assert bar.current_metric() is None
    bar.set_metrics([("memory_private", "私有字节")])
    bar.clear_metrics()
    assert bar.metric_combo.count() == 0 and not bar.metric_combo.isEnabled()


# ---------- 时间范围 / 视图 ----------

def test_range_defaults_to_all_and_set_range_is_silent(bar):
    changed = _record(bar.range_changed)
    assert DEFAULT_RANGE_KEY == 'all'
    assert bar.current_range() == 'all'
    assert [item.text() for item in bar.range_segmented.items.values()] == [
        text for _key, text in RANGE_OPTIONS] == [
            '自基线', '10分钟', '1小时', '6小时', '24小时', '全部']

    bar.set_range('1h')
    assert bar.current_range() == '1h'
    assert changed == []

    bar.range_segmented.setCurrentItem('10m')   # 等同用户点击
    assert changed == ['10m']


def test_baseline_range_disabled_until_available(bar):
    """"自基线"在任务没有基线打点时禁用（tooltip 说明原因），有基线后可选"""
    item = bar.range_segmented.items['baseline']
    assert bar.baseline_available() is False and not item.isEnabled()
    assert item.toolTip() == "该任务还没有基线打点"

    bar.set_baseline_available(True)
    assert bar.baseline_available() is True and item.isEnabled()
    assert item.toolTip() == "只看基线打点之后的数据"

    changed = _record(bar.range_changed)
    bar.range_segmented.setCurrentItem('baseline')
    assert changed == ['baseline'] and bar.current_range() == 'baseline'

    bar.set_baseline_available(False)
    assert not item.isEnabled()


def test_view_switch(bar):
    changed = _record(bar.view_changed)
    assert bar.current_view() == 'trend'
    bar.view_segmented.setCurrentItem('detail')
    assert changed == ['detail']
    bar.set_view('trend')
    assert bar.current_view() == 'trend'
    assert changed == ['detail']


# ---------- 刷新 / 更多菜单 ----------

def test_refresh_button_emits(bar):
    clicked = _record(bar.refresh_clicked)
    bar.refresh_button.click()
    assert clicked == [None]


def test_delete_action_state_and_signal(bar):
    clicked = _record(bar.delete_clicked)
    assert bar.delete_action.isEnabled() is False
    assert bar.delete_action.text() == "删除此任务数据"

    bar.set_delete_enabled(False, "任务正在运行中，请先停止任务再删除")
    assert bar.delete_action.toolTip() == "任务正在运行中，请先停止任务再删除"
    bar.delete_action.trigger()
    assert clicked == []

    bar.set_delete_enabled(True)
    assert bar.delete_action.isEnabled() is True
    # QAction 未设置提示时 toolTip() 回落为动作文本
    assert bar.delete_action.toolTip() == "删除此任务数据"
    bar.delete_action.trigger()
    assert clicked == [None]
    assert bar.delete_action in bar.more_menu.actions()


def test_raw_values_toggle_text_and_signal(bar):
    toggled = _record(bar.raw_values_toggled)
    assert bar.raw_value_action.text() == "显示原始值"
    bar.raw_value_action.setChecked(True)
    assert bar.raw_value_action.text() == "隐藏原始值"
    bar.raw_value_action.setChecked(False)
    assert bar.raw_value_action.text() == "显示原始值"
    assert toggled == [True, False]

    bar.set_raw_values_checked(True)
    assert bar.raw_value_action.isChecked() is True
    assert bar.raw_value_action.text() == "隐藏原始值"
    assert toggled == [True, False]


# ---------- 任务选择器槽位 ----------

def test_task_selector_slot_and_replace(bar):
    assert bar.task_selector() is None
    first = ComboBox()
    bar.set_task_selector(first)
    assert bar.task_selector() is first
    assert first.parentWidget() is bar.task_slot

    second = ComboBox()
    bar.set_task_selector(second)
    assert bar.task_selector() is second
    assert second.parentWidget() is bar.task_slot
    assert first.parentWidget() is None
    first.deleteLater()


# ---------- 响应式 ----------

def test_compact_threshold_matches_legacy_viewport_rule():
    # 原规则：视口 < 720 换行；控件按内容宽度（扣除页边距 2×24 与卡片内边距 2×16）
    assert FilterBar.is_compact_width(720 - VIEWPORT_TO_CONTENT_OFFSET) is False
    assert FilterBar.is_compact_width(720 - VIEWPORT_TO_CONTENT_OFFSET - 1) is True
    assert FilterBar.is_compact_width(600 - VIEWPORT_TO_CONTENT_OFFSET) is True
    assert FilterBar.is_compact_width(900 - VIEWPORT_TO_CONTENT_OFFSET) is False


def test_layout_positions_per_tier(bar):
    bar.update_layout(900 - VIEWPORT_TO_CONTENT_OFFSET)
    assert bar.is_compact() is False
    # 常规：时间范围与指标同处第 1 行；命令组在任务行右侧
    assert _position(bar, bar.range_segmented) == (1, 3, 1, 1)
    assert _position(bar, bar.range_label) == (1, 2, 1, 1)
    assert _position(bar, bar.task_row) == (0, 1, 1, 3)
    assert bar.command_bar.parentWidget() is bar.task_row
    assert bar.select_layout.indexOf(bar.command_bar) == -1
    assert _position(bar, bar.metric_combo) == (1, 1, 1, 1)

    bar.update_layout(600 - VIEWPORT_TO_CONTENT_OFFSET)
    assert bar.is_compact() is True
    # 窄：时间范围换到第 2 行独占；命令组移到指标右侧
    assert _position(bar, bar.range_segmented) == (2, 1, 1, 3)
    assert _position(bar, bar.range_label) == (2, 0, 1, 1)
    assert _position(bar, bar.task_row) == (0, 1, 1, 3)
    assert _position(bar, bar.command_bar) == (1, 3, 1, 1)
    assert _position(bar, bar.metric_combo) == (1, 1, 1, 1)


def test_relayout_only_when_tier_changes(bar):
    bar.update_layout(900)
    assert bar.update_layout(800) is False
    assert bar.update_layout(500) is True
    assert bar.update_layout(400) is False
    assert bar.update_layout(1000) is True


@pytest.mark.parametrize("width, compact", [(560, True), (671, True), (672, False), (900, False)])
def test_resize_drives_tier_by_own_width(qapp, bar, width, compact):
    """控件按自身宽度（减去左右内边距 2×16）决定档位：672 即原视口 720。"""
    bar.show()
    bar.resize(width, 120)
    qapp.processEvents()
    assert bar.is_compact() is compact
    expected = (2, 1, 1, 3) if compact else (1, 3, 1, 1)
    assert _position(bar, bar.range_segmented) == expected
