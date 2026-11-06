"""
历史页趋势图控件 ChartView：曲线点数、打点竖线、纵轴标题与显示单位（与历史页安全网/
流程测试逐字一致）、清空、十字线最近点吸附与读数、交互状态、最小纵向跨度、
自适应范围、主题切换、导出图片与截图。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyqtgraph as pg
import pytest
from PyQt5.QtCore import QEvent
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import Theme, qconfig

from data.models import DataPoint
from tests.helpers import fetch_history_render_inputs
from tests.test_safety_net_history import (
    BASE, MAX_BUCKETS, MEM, QUERY_CASES, TABLE_LIMIT, THREADS, _seed_query_dataset)
from ui.components.sparkline import compute_y_range
from ui.widgets.history.chart_view import ChartView, nearest_index

# 每个用例的纵轴标题与显示单位（安全网快照里的 left_axis / display_unit）
AXIS_TITLES = {MEM: ("私有字节（MB）", "MB"), THREADS: ("线程数（个）", "个")}

FLOW_BASE = datetime(2026, 7, 16, 20, 0, 0)


@pytest.fixture
def chart(qapp):
    widget = ChartView()
    yield widget
    widget.close()


def _marker_lines(chart):
    return sorted(
        (item.value(), item.label.toPlainText() if item.label else None)
        for item in chart.chart_widget.getPlotItem().items
        if isinstance(item, pg.InfiniteLine) and item is not chart.crosshair_line)


def _curves(chart):
    return [item for item in chart.chart_widget.getPlotItem().items
            if isinstance(item, pg.PlotDataItem)]


def _flow_points(samples=10, metric=MEM):
    """历史页流程测试的数据形状：每秒一点，100 MB 起每秒涨 1 MB。"""
    return [DataPoint("t", FLOW_BASE + timedelta(seconds=i), 1024.0 * (100 + i), metric)
            for i in range(samples)]


def _marker(seconds, label):
    return SimpleNamespace(timestamp=FLOW_BASE + timedelta(seconds=seconds), label=label)


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, stat_texts",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_render_matches_history_safety_net(
        chart, db, metric, range_key, since_minutes, rows, count, buckets,
        markers, stat_texts):
    task = _seed_query_dataset(db)
    since = None if since_minutes is None else BASE + timedelta(minutes=since_minutes)
    _table, _stats, chart_points, marker_rows = fetch_history_render_inputs(
        db, task.task_id, metric, since, TABLE_LIMIT, MAX_BUCKETS)

    chart.set_series(chart_points, metric, markers=marker_rows)

    assert [len(c.getData()[0]) for c in _curves(chart)] == [buckets]
    assert len(chart.x_values) == buckets
    assert chart.x_values == [p.timestamp.timestamp() for p in chart_points]
    lines = _marker_lines(chart)
    assert len(lines) == markers
    assert lines == sorted((m.timestamp.timestamp(), m.label) for m in marker_rows)
    title, unit = AXIS_TITLES[metric]
    assert chart.axis_title() == title
    assert chart.display_unit == unit
    assert chart.crosshair_line.isVisible() is False
    assert chart.hover_label.text() == ""


def test_markers_drawn_as_vertical_lines(chart):
    chart.set_series(_flow_points(), MEM, markers=[_marker(2, "第1轮"), _marker(6, "第2轮")])
    assert [value for value, _label in _marker_lines(chart)] == [
        (FLOW_BASE + timedelta(seconds=2)).timestamp(),
        (FLOW_BASE + timedelta(seconds=6)).timestamp()]
    assert [label for _value, label in _marker_lines(chart)] == ["第1轮", "第2轮"]

    # set_markers 只换打点；set_series 不传 markers 时保留现有打点
    chart.set_markers([])
    assert _marker_lines(chart) == []
    chart.set_markers([_marker(3, "基线")])
    chart.set_series(_flow_points(5), MEM)
    assert [label for _value, label in _marker_lines(chart)] == ["基线"]


def test_axis_title_without_metric_and_non_byte_units(chart):
    assert chart.axis_title() == "值"
    chart.set_series(_flow_points(3, THREADS), THREADS)
    assert chart.axis_title() == "线程数（个）"
    assert chart.value_axis.integer_only is True
    chart.set_series(_flow_points(3, MEM), MEM)
    assert chart.value_axis.integer_only is False
    assert chart.value_axis.display_unit == "MB"


def test_clear_empties_series_but_keeps_axis_title(chart):
    chart.set_series(_flow_points(), MEM, markers=[_marker(2, "第1轮")])
    chart.show_hover_at(chart.x_values[3])
    chart.clear()

    assert chart.has_data() is False
    assert chart.x_values == [] and chart.y_values == []
    assert chart.display_unit == '' and chart.metric_type is None
    assert chart.value_axis.display_unit == ''
    assert _curves(chart) == [] and _marker_lines(chart) == []
    assert chart.crosshair_line.isVisible() is False
    assert chart.hover_label.text() == ""
    # 与原页面一致：清空不改纵轴标题
    assert chart.axis_title() == "私有字节（MB）"


@pytest.mark.parametrize("x, expected", [
    (-5.0, 0), (0.0, 0), (0.4, 0), (0.5, 0), (0.6, 1), (2.0, 2), (2.9, 3),
    (3.0, 3), (99.0, 3)])
def test_nearest_index(x, expected):
    # 距离相等时取前一个点
    assert nearest_index([0.0, 1.0, 2.0, 3.0], x) == expected


def test_hover_snaps_to_nearest_point_with_marker_context(chart):
    chart.set_series(_flow_points(), MEM, markers=[_marker(2, "第1轮")])
    x4 = (FLOW_BASE + timedelta(seconds=4)).timestamp()

    index = chart.show_hover_at(x4 + 0.3)

    assert index == 4
    assert chart.crosshair_line.isVisible() is True
    assert chart.crosshair_line.value() == x4
    assert chart.hover_label.text() == "07-16 20:00:04 · 104 MB · 打点「第1轮」后 2秒"

    # 第一个打点之前没有区间说明
    chart.show_hover_at((FLOW_BASE + timedelta(seconds=1)).timestamp())
    assert chart.hover_label.text() == "07-16 20:00:01 · 101 MB"

    chart.hide_hover()
    assert chart.crosshair_line.isVisible() is False
    assert chart.hover_label.text() == ""


def test_hover_without_data_does_nothing(chart):
    assert chart.show_hover_at(123.0) is None
    assert chart.crosshair_line.isVisible() is False


def test_hover_text_matches_safety_net_dataset(chart, db):
    task = _seed_query_dataset(db)
    _table, _stats, chart_points, markers = fetch_history_render_inputs(
        db, task.task_id, MEM, None, TABLE_LIMIT, MAX_BUCKETS)
    chart.set_series(chart_points, MEM, markers=markers)
    chart.show_hover_at(chart.x_values[-1])
    assert chart.hover_label.text() == "07-16 09:59:00 · 209.5 MB · 打点「第2轮」后 4分钟"


def test_leave_event_hides_crosshair(chart):
    chart.set_series(_flow_points(), MEM)
    chart.show_hover_at(chart.x_values[2])
    QApplication.sendEvent(chart.chart_widget, QEvent(QEvent.Leave))
    assert chart.crosshair_line.isVisible() is False
    assert chart.hover_label.text() == ""


def test_interaction_state_and_callback(chart):
    states = []
    chart.set_interaction_callback(states.append)
    chart.set_series(_flow_points(), MEM)
    assert chart.is_interacting() is False

    chart.show_hover_at(chart.x_values[1])
    chart.show_hover_at(chart.x_values[2])   # 状态未变化不重复回调
    assert chart.is_interacting() is True
    QApplication.sendEvent(chart.chart_widget, QEvent(QEvent.Leave))
    assert chart.is_interacting() is False
    # 重绘会隐藏十字线，交互随之结束
    chart.show_hover_at(chart.x_values[1])
    chart.set_series(_flow_points(), MEM, keep_view=True)
    assert states == [True, False, True, False]

    chart.set_interaction_callback(None)
    chart.show_hover_at(chart.x_values[1])
    assert states == [True, False, True, False]


def test_reset_view_unless_keep_view(chart, monkeypatch):
    calls = []
    monkeypatch.setattr(chart.chart_widget, 'enableAutoRange',
                        lambda *a, **k: calls.append(1))
    chart.set_series(_flow_points(), MEM)
    assert calls == [1]
    chart.set_series(_flow_points(), MEM, keep_view=True)
    assert calls == [1]


def test_flat_series_gets_minimum_y_span(chart, monkeypatch):
    """几乎是平线时纵轴跨度扩到下限（与迷你趋势图同一规则）。"""
    flat = [DataPoint("t", FLOW_BASE + timedelta(seconds=i), 1024.0 * 100, MEM)
            for i in range(5)]
    ranges = []
    monkeypatch.setattr(chart.chart_widget, 'setYRange',
                        lambda low, high, padding=None: ranges.append((low, high, padding)))
    chart.set_series(flat, MEM)
    low, high = compute_y_range([p.value for p in flat], MEM)
    assert ranges == [(low, high, 0.05)]

    # 跨度足够时保持自动范围
    ranges.clear()
    chart.set_series(_flow_points(), MEM)
    assert ranges == []


def test_theme_switch_recolors_and_keeps_data(chart):
    chart.set_series(_flow_points(), MEM, markers=[_marker(2, "第1轮")])
    assert chart.chart_widget.backgroundBrush().color().name() == '#ffffff'

    qconfig._theme = Theme.DARK
    qconfig.themeChanged.emit(Theme.DARK)

    assert chart.chart_widget.backgroundBrush().color().name() == '#2b2b2b'
    curve, = _curves(chart)
    assert curve.opts['pen'].color().name() == '#44d7d1'
    assert len(curve.getData()[0]) == 10
    assert len(_marker_lines(chart)) == 1

    qconfig._theme = Theme.LIGHT
    qconfig.themeChanged.emit(Theme.LIGHT)
    assert chart.chart_widget.backgroundBrush().color().name() == '#ffffff'


def test_export_png_writes_file(chart, tmp_path):
    chart.set_series(_flow_points(), MEM)
    target = tmp_path / "chart.png"
    chart.export_png(str(target))
    assert target.exists() and target.stat().st_size > 0


def test_grab_pixmap_not_null(chart):
    chart.set_series(_flow_points(), MEM)
    assert not chart.grab_pixmap().isNull()


def test_toolbar_buttons_emit_requests(chart):
    saved, copied = [], []
    chart.save_requested.connect(lambda: saved.append(1))
    chart.copy_requested.connect(lambda: copied.append(1))
    chart.save_png_button.click()
    chart.copy_chart_button.click()
    assert saved == [1] and copied == [1]
    assert chart.save_png_button.toolTip() == "保存图表为图片"
    assert chart.copy_chart_button.toolTip() == "复制图表到剪贴板"


def test_chart_stays_inside_card(qapp, chart):
    chart.set_series(_flow_points(), MEM)
    chart.resize(882, 420)
    chart.show()
    qapp.processEvents()
    assert chart.chart_widget.parentWidget() is chart
    assert chart.chart_widget.geometry().bottom() <= chart.rect().bottom()
