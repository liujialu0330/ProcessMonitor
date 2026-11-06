"""
ChartView 为分析页增加的扩展点：min/max 带（空洞处断开、读数带范围）、十字线联动
（hover_moved / set_crosshair）、打点命中、相对时间轴、标题与工具按钮、右键菜单开关；
以及 OverlayChart / OverviewStrip 的基本行为。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QApplication

from data.models import DataPoint, Marker
from ui.components.chart_axes import relative_tick_spacing
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.overview_strip import OverviewStrip
from ui.widgets.history.chart_view import RELATIVE_TIME_AXIS_TITLE, ChartView
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE


def _points(offsets):
    return [DataPoint("t", BASE + timedelta(seconds=s), 1024.0 * (100 + s), MEM) for s in offsets]


def _bucket(start, end, low, high):
    return SimpleNamespace(start=BASE + timedelta(seconds=start), end=BASE + timedelta(seconds=end),
                           min=low, max=high)


@pytest.fixture
def chart(qapp):
    widget = ChartView()
    widget.resize(800, 300)
    yield widget
    widget.close()


def test_band_segments_split_at_gaps(chart):
    chart.set_series(_points(range(0, 100)), MEM, [],
                     gaps=[(BASE + timedelta(seconds=40), BASE + timedelta(seconds=80))])
    chart.set_band([_bucket(0, 19, 1000, 2000), _bucket(20, 40, 1500, 2500),
                    _bucket(80, 99, 3000, 4000)])
    assert len(chart.band) == 6
    assert len(chart.band_items()) == 2                 # 空洞两侧各一段
    assert chart.band_range_at((BASE + timedelta(seconds=85)).timestamp()) == (3000, 4000)
    assert "（" in chart.hover_text(10) and " – " in chart.hover_text(10)
    chart.set_band(None)
    assert chart.band == [] and chart.band_items() == []
    chart.set_band([_bucket(0, 19, 1, 2)])
    chart.clear()
    assert chart.band == []


def test_crosshair_link_signals(chart):
    chart.set_series(_points(range(10)), MEM, [])
    moved = []
    chart.hover_moved.connect(moved.append)
    x = (BASE + timedelta(seconds=3.2)).timestamp()
    assert chart.set_crosshair(x) == 3
    assert moved == []                                  # 联动用：不回发
    assert chart.crosshair_line.isVisible()
    chart.show_hover_at(x)
    assert moved == [(BASE + timedelta(seconds=3)).timestamp()]
    chart.hide_hover()
    assert moved[-1] is None and not chart.crosshair_line.isVisible()


def test_marker_near_skips_system_markers(chart):
    markers = [Marker("t", BASE + timedelta(seconds=50), "第 1 轮", 1, 'user'),
               Marker("t", BASE + timedelta(seconds=20), "暂停", 2, 'pause')]
    chart.set_series(_points(range(100)), MEM, markers)
    chart.show()
    QApplication.processEvents()
    near = (BASE + timedelta(seconds=50.5)).timestamp()
    assert chart.marker_near(near).label == "第 1 轮"
    assert chart.marker_near((BASE + timedelta(seconds=20)).timestamp()) is None
    assert chart.marker_near((BASE + timedelta(seconds=80)).timestamp()) is None


def test_relative_origin_title_and_tools(chart):
    chart.set_series(_points(range(100)), MEM, [])
    origin = (BASE + timedelta(seconds=10)).timestamp()
    chart.set_relative_origin(origin)
    assert chart.relative_origin == origin
    assert chart.chart_widget.getPlotItem().getAxis('bottom').labelText == RELATIVE_TIME_AXIS_TITLE
    assert chart.hover_text(70).startswith("T+01:00")
    assert chart.time_axis.tickStrings([origin + 90], 1, 30) == ["T+01:30"]
    chart.set_relative_origin(None)
    assert not chart.hover_text(70).startswith("T")
    chart.set_title("私有字节")
    assert chart.title_label.text() == "私有字节" and not chart.title_label.isHidden()
    chart.set_tools_visible(False)
    assert chart.save_png_button.isHidden() and chart.copy_chart_button.isHidden()
    chart.enable_context_menu()
    chart.enable_context_menu()                         # 重复调用无副作用
    assert not chart.chart_widget.getPlotItem().menuEnabled()
    assert relative_tick_spacing(3600, 800) >= 300


def test_overlay_chart_series_and_hover(qapp):
    chart = OverlayChart()
    xs = [(BASE + timedelta(seconds=s)).timestamp() for s in range(5)]
    chart.set_overlay([OverlaySeries("甲", xs, [0, 1, 2, 3, 4]),
                       OverlaySeries("乙", xs, [0, -1, -2, -3, -4])], [])
    assert chart.legend_names() == ["甲", "乙"] and len(chart.curve_items()) == 2
    assert chart.values_at(xs[2]) == [2, -2]
    assert "甲" in chart.hover_text(2) and "乙" in chart.hover_text(2)
    chart.set_overlay([], [])
    assert chart.legend_names() == [] and not chart.has_data()
    chart.close()


def test_overview_strip_region(qapp):
    strip = OverviewStrip()
    picked = []
    strip.region_selected.connect(lambda a, b: picked.append((a, b)))
    strip.set_series(_points(range(100)))
    assert strip.has_data()
    assert strip.region_values() == pytest.approx(strip.x_bounds())
    strip.set_region((BASE + timedelta(seconds=10), BASE + timedelta(seconds=20)))
    assert picked == []                                 # 程序设置不发信号
    strip.select((BASE + timedelta(seconds=30)).timestamp(),
                 (BASE + timedelta(seconds=500)).timestamp())
    assert picked == [(BASE + timedelta(seconds=30), BASE + timedelta(seconds=99))]
    strip.clear()
    assert not strip.has_data()
    strip.close()
