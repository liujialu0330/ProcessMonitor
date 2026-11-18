"""
ChartView 为分析页增加的扩展点：min/max 带（空洞处断开、读数带范围）、十字线联动
（hover_moved / set_crosshair）、打点命中、相对时间轴、标题与工具按钮、右键菜单开关、
挨得太近的打点徽标错开高度；以及 OverlayChart / OverviewStrip 的基本行为。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pyqtgraph as pg
from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QHelpEvent, QMouseEvent, QPalette, QTextDocument
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QToolTip
from qfluentwidgets import Theme, qconfig

from core.monitor_manager import MonitorManager
from data.models import DataPoint, Marker, MonitorTask
from tests.factories import make_analysis_page
from tests.helpers import SyncHistoryRunner, process_events_until
from ui.chart_theme import chart_palette
from ui.color_contrast import TEXT_MIN_CONTRAST, contrast_ratio
from ui.components.chart_axes import relative_tick_spacing
from ui.widgets.analysis.compare_panel import ComparisonChart
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.overview_strip import OverviewStrip
from ui.widgets.analysis.trend_panel import TrendPanel
from ui.widgets.history.chart_view import (
    BADGE_POSITION, BADGE_STEP, RELATIVE_TIME_AXIS_TITLE, ChartView, badge_positions)
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
    chart.show_hover_at(x + 0.1)
    assert moved == [(BASE + timedelta(seconds=3)).timestamp()]
    chart.hide_hover()
    assert moved[-1] is None and not chart.crosshair_line.isVisible()


def test_delayed_move_and_axis_hover_do_not_reopen_readout(chart):
    chart.set_series(_points(range(10)), MEM, [])
    chart.show()
    QApplication.processEvents()
    plot = chart.chart_widget.getPlotItem()
    pos = plot.vb.sceneBoundingRect().center()
    viewport = chart.chart_widget.viewport()
    viewport.setAttribute(Qt.WA_UnderMouse)
    chart._on_mouse_moved((pos,))
    assert chart.crosshair_line.isVisible()
    axis_pos = QPointF(plot.vb.sceneBoundingRect().left() - 8, pos.y())
    assert plot.sceneBoundingRect().contains(axis_pos)
    assert not plot.vb.sceneBoundingRect().contains(axis_pos)
    chart._on_mouse_moved((axis_pos,))
    assert not chart.crosshair_line.isVisible()
    # 模拟离开后才投递到图表的旧坐标，不依赖限流器的某个版本或计时周期。
    viewport.setAttribute(Qt.WA_UnderMouse, False)
    chart._on_mouse_moved((pos,))
    assert not chart.is_interacting() and chart.hover_label.text() == ''


def test_compact_hover_shows_value_then_sample_time(qapp):
    widget = ChartView(compact=True)
    try:
        points = [DataPoint('t', BASE + timedelta(seconds=i), 170 + i, MetricType.NUM_HANDLES)
                  for i in range(5)]
        widget.set_series(points, MetricType.NUM_HANDLES, [Marker('t', BASE, '基线', 1, 'baseline')])
        widget.set_band([_bucket(0, 4, 165, 180)])
        widget.show_hover_at(points[3].timestamp.timestamp())
        assert widget.hover_label.text() == '173 个 · 07-16 16:00:03'
        widget.set_relative_origin(BASE.timestamp())
        assert widget.hover_label.text() == '173 个 · T+00:03'
    finally:
        widget.close()


def test_hidden_chart_events_do_not_override_current_crosshair(qapp):
    panel = TrendPanel()
    try:
        chart = panel.focus_chart
        chart.set_series(_points(range(5)), MEM)
        chart.show_hover_at(BASE.timestamp())
        label = chart.hover_label.text()
        panel.overlay_chart.hover_moved.emit((BASE + timedelta(seconds=4)).timestamp())
        panel.overlay_chart.hover_moved.emit(None)
        assert chart.crosshair_line.isVisible() and chart.hover_label.text() == label
        QApplication.sendEvent(chart.chart_widget, QEvent(QEvent.Hide))
        assert not chart.crosshair_line.isVisible() and not chart.is_interacting()
        assert not chart.hover_label.text()
    finally:
        panel.close()


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


def test_badge_positions_pure():
    top, step = 0.94, 0.07
    assert badge_positions([], 22, top, step, 3) == []
    assert badge_positions([100.0], 22, top, step, 3) == [top]
    assert badge_positions([100, 105, 110], 22, top, step, 3) == pytest.approx(
        [top, top - step, top - 2 * step])
    # 第四个挤在一起的回到顶层
    assert badge_positions([100, 105, 110, 115], 22, top, step, 3) == pytest.approx(
        [top, top - step, top - 2 * step, top])
    # 簇间互不影响：隔开足够远的从顶层重新开始
    assert badge_positions([100, 110, 300, 310], 22, top, step, 3) == pytest.approx(
        [top, top - step, top, top - step])


def _badge_chart(chart, offsets):
    """跨度 1 小时、每分钟一点的曲线，在 offsets（秒）处打用户点；窗口显示后再排版。"""
    markers = [Marker("t", BASE + timedelta(seconds=s), f"第 {i + 1} 轮", i + 1, 'user')
               for i, s in enumerate(offsets)]
    chart.set_series(_points(range(0, 3601, 60)), MEM, markers)
    chart.resize(900, 400)
    chart.show()
    QApplication.processEvents()


def test_close_badges_are_staggered_and_relayout_on_zoom(chart):
    _badge_chart(chart, (1800, 1802))
    first, second = chart.badge_layout()
    assert first == pytest.approx(BADGE_POSITION)
    assert second == pytest.approx(BADGE_POSITION - BADGE_STEP)
    # 放大到两点相隔足够远后，重新回到同一高度
    t1 = (BASE + timedelta(seconds=1800)).timestamp()
    t2 = (BASE + timedelta(seconds=1802)).timestamp()
    chart.chart_widget.setXRange(t1 - 5, t2 + 5, padding=0)
    QApplication.processEvents()
    assert chart.badge_layout() == pytest.approx([BADGE_POSITION, BADGE_POSITION])
    # marker_items 结构不变
    assert [badge for _kind, badge, _line in chart.marker_items()] == ["①", "②"]


def test_far_badges_share_top_row(chart):
    _badge_chart(chart, (600, 2400))
    assert chart.badge_layout() == pytest.approx([BADGE_POSITION, BADGE_POSITION])


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
    chart.set_band([_bucket(0, 4, 2, 5)])
    chart.set_overlay([], [])
    assert chart.legend_names() == [] and not chart.has_data()
    assert not chart.band and not chart._band_times
    chart.close()


@pytest.mark.parametrize('chart_class', [OverlayChart, ComparisonChart], ids=['overlay', 'comparison'])
def test_plot_tooltip_shows_every_elided_series_and_does_not_change_layout(
        qapp, monkeypatch, chart_class):
    chart = chart_class()
    tooltips = []
    monkeypatch.setattr(QToolTip, 'showText', lambda pos, text, *_args: tooltips.append(text))
    try:
        count = 4 if chart_class is ComparisonChart else 5
        xs = [0., 1., 2.] if chart_class is ComparisonChart else [
            (BASE + timedelta(seconds=i)).timestamp() for i in range(3)]
        names = [f'验证任务 <{i + 1}> · 试验 & 扫描结果' for i in range(count)]
        curves = [OverlaySeries(name, xs, [0., i + 1., 0.]) for i, name in enumerate(names)]
        chart.set_overlay(curves)
        chart.legend.hide()
        chart.resize(420, 280)
        chart.show()
        qapp.processEvents()
        plot = chart.chart_widget
        viewport = plot.viewport()
        pos = plot.mapFromScene(plot.getPlotItem().vb.sceneBoundingRect().center())

        def request_tip():
            event = QHelpEvent(QEvent.ToolTip, pos, viewport.mapToGlobal(pos))
            QApplication.sendEvent(viewport, event)

        before = (chart.size(), plot.size(), chart.hover_label.size())
        chart.show_hover_at(xs[1])
        label = chart.hover_label
        elided = label.displayed_text()
        assert elided.endswith('…') and label.text().startswith(elided[:-1])
        assert label.fontMetrics().horizontalAdvance(elided) <= label.contentsRect().width()
        request_tip()
        assert len(tooltips) == 1
        doc = QTextDocument()
        doc.setHtml(tooltips.pop())
        head = '第 1 轮' if chart_class is ComparisonChart else '07-16 16:00:01'
        assert doc.toPlainText().splitlines() == [
            head, *[f'{name} +{i + 1}.0%' for i, name in enumerate(names)]]
        assert (chart.size(), plot.size(), chart.hover_label.size()) == before

        chart.set_overlay([OverlaySeries('甲', xs, [0., 1., 2.])])
        chart.show_hover_at(xs[1])
        assert chart.hover_label.displayed_text() == chart.hover_label.text()
        request_tip()
        chart.set_overlay([])
        request_tip()
        assert not tooltips
    finally:
        chart.close()


@pytest.mark.parametrize('theme', [Theme.LIGHT, Theme.DARK])
def test_plot_tooltip_text_contrasts_with_its_actual_background(qapp, monkeypatch, theme):
    monkeypatch.setattr(qconfig, '_theme', theme)
    chart = ChartView()
    try:
        chart.resize(320, 220)
        name = '扫描 <A> & 核对' * 12
        chart.set_series(_points(range(10)), MEM, [Marker('t', BASE, name, 1)])
        chart.show()
        qapp.processEvents()
        chart.show_hover_at((BASE + timedelta(seconds=4)).timestamp())
        assert chart.hover_label.displayed_text() != chart.hover_label.text()
        plot = chart.chart_widget
        pos = plot.mapFromScene(plot.getPlotItem().vb.sceneBoundingRect().center())
        viewport = plot.viewport()
        QApplication.sendEvent(viewport, QHelpEvent(QEvent.ToolTip, pos, viewport.mapToGlobal(pos)))
        assert process_events_until(QToolTip.isVisible)
        popup = next(w for w in qapp.topLevelWidgets() if w.isVisible() and w.windowType() == Qt.ToolTip)
        colors = popup.palette()
        text = colors.color(QPalette.Inactive, QPalette.ToolTipText)
        background = colors.color(QPalette.Inactive, QPalette.ToolTipBase)
        expected = chart_palette(theme == Theme.DARK)
        assert text.name() == expected['text'] and background.name() == expected['surface']
        assert contrast_ratio(text, background) >= TEXT_MIN_CONTRAST
        doc = QTextDocument()
        doc.setHtml(QToolTip.text())
        assert doc.toPlainText().splitlines()[:3] == [
            '07-16 16:00:04', '104 MB', f'打点「① {name}」后 4秒']

        chart.hide_hover()
        assert process_events_until(lambda: not QToolTip.isVisible())
        assert chart.hover_label.text() == ''
    finally:
        QToolTip.hideText()
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


@pytest.fixture
def deliver_all_mouse_moves():
    # 指针由测试逐步移动，无需按显示帧率丢弃相邻的两个事件。
    previous = pg.getConfigOption('mouseRateLimit')
    pg.setConfigOption('mouseRateLimit', 0)
    try:
        yield
    finally:
        pg.setConfigOption('mouseRateLimit', previous)


def _drag_overview(strip, move_edge):
    plot = strip.plot
    viewport = plot.viewport()
    vb = plot.getPlotItem().vb
    low, high = strip.region_values()
    x = low if move_edge else (low + high) / 2
    start = plot.mapFromScene(vb.mapViewToScene(QPointF(x, sum(vb.viewRange()[1]) / 2)))
    QTest.mouseMove(viewport, start)
    QTest.mousePress(viewport, Qt.LeftButton, pos=start)
    for dx in (20, 65):
        pos = start + QPoint(dx, 0)
        move = QMouseEvent(QEvent.MouseMove, QPointF(pos), QPointF(viewport.mapToGlobal(pos)),
                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(viewport, move)
    return pos


@pytest.mark.parametrize('move_edge', [False, True], ids=['region', 'left_edge'])
def test_overview_drag_commits_once_on_release(qapp, move_edge, deliver_all_mouse_moves):
    strip = OverviewStrip()
    try:
        strip.resize(680, 115)
        strip.set_series(_points(range(100)))
        strip.set_region((BASE + timedelta(seconds=20), BASE + timedelta(seconds=60)))
        strip.show()
        qapp.processEvents()
        low, high = strip.region_values()
        picked = []
        strip.region_selected.connect(lambda a, b: picked.append((a, b)))
        assert not strip.is_dragging() and not picked
        assert strip.region.brush.color() == strip.region.hoverBrush.color()
        pos = _drag_overview(strip, move_edge)
        assert strip.is_dragging() and not picked
        QTest.mouseRelease(strip.plot.viewport(), Qt.LeftButton, pos=pos)
        assert not strip.is_dragging() and len(picked) == 1
        assert picked[0][0] > datetime.fromtimestamp(low)
        if move_edge:
            assert picked[0][1] == datetime.fromtimestamp(high)
        else:
            assert picked[0][1] > datetime.fromtimestamp(high)
    finally:
        strip.close()


@pytest.mark.parametrize('move_edge,interrupt', [(False, QEvent.Hide),
                                                 (True, QEvent.WindowDeactivate),
                                                 (False, None)],
                         ids=['region-hidden', 'handle-deactivated', 'release-elsewhere'])
def test_interrupted_overview_drag_restores_selection_and_allows_next_refresh(
        qapp, db, deliver_all_mouse_moves, move_edge, interrupt):
    points = _points(range(100))
    assert db.save_task(MonitorTask(task_id='t', pid=4321, process_name='demo.exe',
                                    metric_types=[MEM], interval=1., start_time=BASE,
                                    end_time=points[-1].timestamp, status='stopped'))
    assert db.save_data_points(points)
    page = make_analysis_page(db, MonitorManager(db=db), worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    try:
        page.resize(900, 800)
        page.show()
        page.select_task('t')
        strip = page.trend_panel.overview
        strip.set_region((points[20].timestamp, points[60].timestamp))
        page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
        qapp.processEvents()
        committed = strip.region_values()
        picked = []
        strip.region_selected.connect(lambda start, end: picked.append((start, end)))
        _drag_overview(strip, move_edge)
        assert strip.region_values() != committed and not picked
        assert page.trend_panel.is_interacting() and page._should_skip_refresh()

        if interrupt == QEvent.Hide:
            strip.hide()
        elif interrupt is not None:
            QApplication.sendEvent(strip.plot.viewport(), QEvent(interrupt))
        QTest.mouseRelease(page.toolbar, Qt.LeftButton, pos=QPoint(1, 1))
        if interrupt is None:
            assert not strip.is_dragging()  # 在任何显隐事件前由轮询发现已松手。
        strip.show()
        qapp.processEvents()
        assert strip.region_values() == committed and not picked
        assert not strip.is_dragging() and not page._should_skip_refresh()

        pos = _drag_overview(strip, move_edge)
        assert strip.is_dragging() and not picked
        QTest.mouseRelease(strip.plot.viewport(), Qt.LeftButton, pos=pos)
        assert len(picked) == 1 and picked[0][0] > points[20].timestamp
        assert not strip.is_dragging()
    finally:
        page.auto_refresh.stop()
        page.close()
