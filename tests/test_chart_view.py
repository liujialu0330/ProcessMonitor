"""
历史页趋势图控件 ChartView：曲线点数、打点竖线与编号徽标（基线"基线"、暂停/恢复不编号的
短竖线）、空洞断开与阴影、累计量的速率曲线、纵轴标题与显示单位（与历史页安全网/
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

from core.history_query import build_request, run_history_query
from data.models import DataPoint
from tests.test_history_query import _seed_series
from tests.history_dataset import (
    BASE, MARKER_LINES, MAX_BUCKETS, MEM, QUERY_CASES, TABLE_LIMIT, THREADS,
    _seed_baseline_dataset, _seed_query_dataset)
from ui.components.sparkline import compute_y_range
from ui.widgets.history.chart_view import (
    SYSTEM_MARKER_SPAN, ChartView, gap_label_text, marker_badges, nearest_index,
    value_axis_title)
from utils.metrics import MetricType

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
        (item.value(), item.label.toPlainText() if getattr(item, 'label', None) else None)
        for item in chart.chart_widget.getPlotItem().items
        if isinstance(item, pg.InfiniteLine) and item is not chart.crosshair_line)


def _snapshot(db, task_id, metric, range_key='all'):
    return run_history_query(db, build_request(
        task_id, metric, range_key, table_limit=TABLE_LIMIT, max_buckets=MAX_BUCKETS))


def _curves(chart):
    return [item for item in chart.chart_widget.getPlotItem().items
            if isinstance(item, pg.PlotDataItem)]


def _flow_points(samples=10, metric=MEM):
    """历史页流程测试的数据形状：每秒一点，100 MB 起每秒涨 1 MB。"""
    return [DataPoint("t", FLOW_BASE + timedelta(seconds=i), 1024.0 * (100 + i), metric)
            for i in range(samples)]


def _marker(seconds, label, kind='user'):
    return SimpleNamespace(timestamp=FLOW_BASE + timedelta(seconds=seconds), label=label,
                           kind=kind)


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, _current",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_render_matches_history_safety_net(
        chart, db, metric, range_key, since_minutes, rows, count, buckets,
        markers, _current):
    task = _seed_query_dataset(db)
    snapshot = _snapshot(db, task.task_id, metric, range_key)

    chart.set_snapshot(snapshot)

    assert [len(c.getData()[0]) for c in _curves(chart)] == [buckets]
    assert len(chart.x_values) == buckets
    assert chart.x_values == [p.timestamp.timestamp() for p in snapshot.buckets]
    lines = _marker_lines(chart)
    assert len(lines) == markers
    assert lines == [(pos, badge) for pos, badge, _tip in MARKER_LINES]
    assert [item.toolTip() for _kind, _badge, item in chart.marker_items()] == [
        tip for _pos, _badge, tip in MARKER_LINES]
    assert chart.gaps == [] and chart.gap_items() == [] and chart.is_rate is False
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
    # 标签横排编号，文字放在 tooltip
    assert [label for _value, label in _marker_lines(chart)] == ["①", "②"]
    assert [item.toolTip() for _k, _b, item in chart.marker_items()] == [
        "① 第1轮 · 07-16 20:00:02", "② 第2轮 · 07-16 20:00:06"]

    # set_markers 只换打点；set_series 不传 markers 时保留现有打点
    chart.set_markers([])
    assert _marker_lines(chart) == []
    chart.set_markers([_marker(3, "基线", 'baseline')])
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
    assert chart.hover_label.text() == "07-16 20:00:04 · 104 MB · 打点「① 第1轮」后 2秒"

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
    chart.set_snapshot(_snapshot(db, task.task_id, MEM))
    chart.show_hover_at(chart.x_values[-1])
    # 图表点集按时间等分（2.0）：最后一个 7 分钟桶含 119、120 分钟两点，最小值点是 120 分钟
    # 的 160 MB（1.x 按行号等分时最后一点是 119 分钟的尖峰 209.5 MB）
    assert chart.hover_label.text() == "07-16 10:00:00 · 160 MB · 打点「② 第2轮」后 5分钟"


# ---------- 打点种类、空洞与速率曲线 ----------

def test_marker_badges_number_user_markers_only():
    markers = [_marker(0, "预热", 'user'), _marker(1, "基线", 'baseline'),
               _marker(2, "暂停", 'pause'), _marker(3, "恢复", 'resume'),
               _marker(4, "第1轮"), SimpleNamespace(timestamp=FLOW_BASE, label="旧")]
    assert marker_badges(markers) == ["①", "基线", None, None, "②", "③"]


def test_baseline_and_gaps_dataset(chart, db):
    """基线实线 + "基线"；暂停/恢复为底部浅色短竖线、不编号；空洞处断开并画阴影。"""
    task = _seed_baseline_dataset(db)
    chart.set_snapshot(_snapshot(db, task.task_id, MEM))

    kinds = [(kind, badge) for kind, badge, _line in chart.marker_items()]
    assert kinds == [('baseline', "基线"), ('user', "①"), ('pause', None),
                     ('resume', None), ('user', "②")]
    lines = {kind: line for kind, _badge, line in chart.marker_items()}
    assert lines['baseline'].pen.style() == pg.QtCore.Qt.SolidLine
    assert lines['baseline'].pen.width() == 2
    assert lines['user'].pen.style() == pg.QtCore.Qt.DashLine
    assert lines['pause'].span == (0, SYSTEM_MARKER_SPAN)
    assert lines['pause'].toolTip() == "暂停 · 07-16 09:09:30"

    gap_start, gap_end = BASE + timedelta(minutes=69), BASE + timedelta(minutes=85)
    assert chart.gaps == [(gap_start.timestamp(), gap_end.timestamp())]
    assert [text for _region, text in chart.gap_items()] == ["无数据 16分钟"]
    region, _text = chart.gap_items()[0]
    assert region.getRegion() == (gap_start.timestamp(), gap_end.timestamp())
    connect = chart.connect_array()
    assert connect.count(False) == 1
    broken = connect.index(False)
    assert chart.x_values[broken] <= gap_start.timestamp()
    assert chart.x_values[broken + 1] >= gap_end.timestamp()
    # 曲线把 connect 数组交给 pyqtgraph
    curve, = _curves(chart)
    assert list(curve.opts['connect']) == connect

    # 悬停区间说明只看用户打点与基线，不看暂停/恢复
    chart.show_hover_at((BASE + timedelta(minutes=90)).timestamp())
    assert chart.hover_label.text().endswith("打点「① 第1轮」后 40分钟")

    # 主题切换重绘：阴影与打点仍在
    chart.apply_theme()
    assert len(chart.gap_items()) == 1 and len(chart.marker_items()) == 5


def test_gap_label_text():
    assert gap_label_text(BASE, BASE + timedelta(minutes=12)) == "无数据 12分钟"


def test_cumulative_metric_draws_rate_series(chart, db):
    values = [1024.0 * 6 * i for i in range(40)]   # 每分钟 6 MB → 每秒 0.1 MB
    task = _seed_series(db, values, metric=MetricType.IO_READ_BYTES, skip=range(10, 15))
    snapshot = _snapshot(db, task.task_id, MetricType.IO_READ_BYTES)
    chart.set_snapshot(snapshot)

    assert chart.is_rate is True
    assert chart.y_values == pytest.approx([102.4] * len(snapshot.rate_series))
    assert chart.axis_title() == "每秒I/O 读取字节（KB）"
    assert chart.value_axis.integer_only is False
    assert chart.connect_array().count(False) == 1
    chart.show_hover_at(chart.x_values[0])
    assert chart.hover_label.text().split(" · ")[1] == "102 KB/秒"


def test_rate_axis_title_and_count_metric_not_integer(chart):
    assert value_axis_title(MetricType.IO_READ_COUNT, "次", rate=True) == "每秒I/O 读取次数（次）"
    points = [DataPoint("t", FLOW_BASE + timedelta(seconds=i), 0.5, MetricType.IO_READ_COUNT)
              for i in range(3)]
    chart.set_series(points, MetricType.IO_READ_COUNT, rate=True)
    assert chart.value_axis.integer_only is False


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


def test_constant_rate_series_gets_relative_y_span(chart, monkeypatch):
    """累计量的恒定速率（每秒 1000 KB）：纵轴跨度扩到参考值的 5%（975–1025），
    不再被拉伸成 999.6–1000.4 这类无意义刻度；速率为 0 时至少跨 1 个单位/秒。"""
    ranges = []
    monkeypatch.setattr(chart.chart_widget, 'setYRange',
                        lambda low, high, padding=None: ranges.append((low, high, padding)))
    flat = [DataPoint("t", FLOW_BASE + timedelta(seconds=i), 1000.0, MetricType.IO_READ_BYTES)
            for i in range(5)]
    chart.set_series(flat, MetricType.IO_READ_BYTES, rate=True)
    assert ranges == [(pytest.approx(975.0), pytest.approx(1025.0), 0.05)]

    ranges.clear()
    zero = [DataPoint("t", FLOW_BASE + timedelta(seconds=i), 0.0, MetricType.IO_READ_COUNT)
            for i in range(5)]
    chart.set_series(zero, MetricType.IO_READ_COUNT, rate=True)
    assert ranges == [(pytest.approx(-0.5), pytest.approx(0.5), 0.05)]
    assert compute_y_range([0.0, 0.0], MetricType.IO_READ_COUNT, rate=True) == (-0.5, 0.5)


def _axis_text_color(chart) -> str:
    return chart.chart_widget.getAxis('left').textPen().color().name()


def test_theme_switch_recolors_and_keeps_data(chart):
    # 背景透明、由卡片提供底色（不再写死 #ffffff / #2b2b2b）；主题切换只换前景色
    chart.set_series(_flow_points(), MEM, markers=[_marker(2, "第1轮")])
    assert chart.is_background_transparent()
    light_text = _axis_text_color(chart)

    qconfig._theme = Theme.DARK
    qconfig.themeChanged.emit(Theme.DARK)

    assert chart.is_background_transparent()
    assert _axis_text_color(chart) != light_text
    curve, = _curves(chart)
    assert curve.opts['pen'].color().name() == '#44d7d1'
    assert len(curve.getData()[0]) == 10
    assert len(_marker_lines(chart)) == 1

    qconfig._theme = Theme.LIGHT
    qconfig.themeChanged.emit(Theme.LIGHT)
    assert chart.is_background_transparent()
    assert _axis_text_color(chart) == light_text


def test_export_and_copy_use_opaque_card_background(chart, tmp_path):
    """屏幕上透明，导出 PNG 与复制到剪贴板的图片垫当前主题的卡片底色（离开卡片后仍可读）"""
    from PyQt5.QtGui import QColor, QImage
    from ui.chart_theme import chart_palette

    chart.set_series(_flow_points(), MEM)
    chart.resize(600, 400)
    original = qconfig._theme
    try:
        for theme, dark in ((Theme.LIGHT, False), (Theme.DARK, True)):
            qconfig._theme = theme
            qconfig.themeChanged.emit(theme)
            expected = QColor(chart_palette(dark)['background'])
            corner = chart.grab_pixmap().toImage().pixelColor(1, 1)
            assert corner.alpha() == 255 and corner.name() == expected.name()
            target = tmp_path / f"chart_{theme.value}.png"
            chart.export_png(str(target), width=400)
            exported = QImage(str(target)).pixelColor(1, 1)
            assert exported.alpha() == 255 and exported.name() == expected.name()
    finally:
        qconfig._theme = original
        qconfig.themeChanged.emit(original)


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
