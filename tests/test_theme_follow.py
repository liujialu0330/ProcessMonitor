"""
主题跟随：图表控件切换主题后背景仍透明、前景色换成另一套 palette；析构后的控件不再响应；
"跟随系统"时 SystemThemeWatcher 轮询系统深浅色（测试注入 is_dark），变化后重新解析主题并
发 themeChanged。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PyQt5.QtGui import QColor, QImage, QRegion
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import Theme, qconfig

from data.models import DataPoint
from tests.helpers import process_events_until
from ui import theme_tokens as tokens
from ui.chart_theme import chart_palette, series_colors
from ui.components.sparkline import SparklineWidget
from ui.theme_follow import POLL_INTERVAL_MS, SystemThemeWatcher
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.overview_strip import OverviewStrip
from ui.widgets.history.chart_view import ChartView
from utils.metrics import MetricType

MEM = MetricType.MEMORY_PRIVATE
BASE = datetime(2026, 7, 16, 20, 0, 0)


def _points(count=20):
    return [DataPoint("t", BASE + timedelta(seconds=10 * i), 1000.0 + i * 5, MEM)
            for i in range(count)]


def _markers():
    return [SimpleNamespace(timestamp=BASE + timedelta(seconds=30), label="基线",
                            kind='baseline'),
            SimpleNamespace(timestamp=BASE + timedelta(seconds=90), label="第 1 轮",
                            kind='user')]


def _switch(theme: Theme) -> None:
    qconfig.theme = theme
    qconfig.themeChanged.emit(theme)


@pytest.fixture
def restore_theme():
    original_theme = qconfig.theme
    original_mode = qconfig.themeMode.value
    yield
    qconfig.themeMode.value = original_mode
    _switch(original_theme)


def _dispose(widget) -> None:
    widget.hide()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def charts(qapp, restore_theme):
    _switch(Theme.LIGHT)
    chart = ChartView()
    chart.set_series(_points(), MEM, markers=_markers(),
                     gaps=[(BASE + timedelta(seconds=100), BASE + timedelta(seconds=150))])
    overlay = OverlayChart()
    xs = [p.timestamp.timestamp() for p in _points()]
    overlay.set_overlay([OverlaySeries("私有字节", xs, [i * 1.0 for i in range(len(xs))]),
                         OverlaySeries("句柄数", xs, [i * 0.5 for i in range(len(xs))])],
                        _markers())
    overview = OverviewStrip()
    overview.set_series(_points())
    widgets = SimpleNamespace(chart=chart, overlay=overlay, overview=overview)
    yield widgets
    for widget in (chart, overlay, overview):
        _dispose(widget)


def _chart_foreground(chart: ChartView) -> dict:
    curve = next(item for item in chart.chart_widget.getPlotItem().listDataItems()
                 if item.opts['pen'] is not None)
    result = {
        'text': chart.chart_widget.getAxis('left').textPen().color().name(),
        'axis': chart.chart_widget.getAxis('left').pen().color().name(),
        'crosshair': chart.crosshair_line.pen.color().name(),
        'curve': QColor(curve.opts['pen'].color()).name(),
    }
    if chart.marker_items():
        result['marker'] = chart.marker_items()[0][2].pen.color().name()
    return result


def _expected(dark: bool, keys) -> dict:
    palette = chart_palette(dark)
    return {key: QColor(palette[key]).name() for key in keys}


@pytest.mark.parametrize("first, second", [(Theme.LIGHT, Theme.DARK), (Theme.DARK, Theme.LIGHT)])
def test_theme_switch_keeps_transparent_and_swaps_palette(charts, first, second):
    for theme in (first, second, first):
        _switch(theme)
        dark = theme == Theme.DARK
        for widget in (charts.chart, charts.overlay, charts.overview):
            assert widget.is_background_transparent(), (type(widget).__name__, theme)
        fg = _chart_foreground(charts.chart)
        assert fg == _expected(dark, fg.keys())
        # 空洞阴影基色与说明文字
        region, _text = charts.chart.gap_items()[0]
        shade = region.brush.color()
        assert shade.name() == QColor(chart_palette(dark)['gap_shade']).name()
        assert 0 < shade.alpha() < 255
        # 叠加图：系列色与图例文字
        pens = [c.opts['pen'].color().name() for c in charts.overlay.curve_items()]
        assert pens == [QColor(c).name() for c in series_colors(dark)[:2]]
        assert charts.overlay.legend.opts['labelTextColor'].name() == \
            QColor(chart_palette(dark)['text']).name()
        # 概览条：走势线与选区（选中色条）
        palette = chart_palette(dark)
        assert charts.overview.curve.opts['pen'].color().name() == \
            QColor(palette['overview_curve']).name()
        for line in charts.overview.region.lines:
            assert line.pen.color().name() == QColor(palette['selection']).name()


def _view_paints_nothing_at(view, x, y) -> bool:
    """单独渲染绘图区（含视口的自动填充，不含任何祖先的底色）后 (x, y) 处是否仍透明。"""
    image = QImage(view.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    # pyqtgraph 的 GraphicsView 把 render 覆盖成了 QGraphicsView 的版本，这里要 QWidget 的
    QWidget.render(view, image, QPoint(), QRegion(),
                   QWidget.DrawWindowBackground | QWidget.DrawChildren)
    return image.pixelColor(x, y).alpha() == 0


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_plot_viewport_does_not_paint_background(charts, theme):
    """不依赖祖先样式表：绘图区视口自身不画底色（否则在报告离屏渲染等场合露出 Qt 默认浅灰）。"""
    _switch(theme)
    for view in (charts.chart.chart_widget, charts.overlay.chart_widget, charts.overview.plot):
        view.resize(400, 200)
        assert _view_paints_nothing_at(view, 2, 2), type(view).__name__


def test_two_palettes_really_differ_on_screen(charts):
    _switch(Theme.LIGHT)
    light = _chart_foreground(charts.chart)
    _switch(Theme.DARK)
    dark = _chart_foreground(charts.chart)
    assert all(light[key] != dark[key] for key in light)


def test_sparkline_transparent_and_follows_theme(qapp, restore_theme):
    widget = SparklineWidget(metric_type=MEM)
    widget.resize(160, 36)
    for i in range(30):
        widget.append(1000.0 + (i % 5) * 400)
    try:
        for theme in (Theme.LIGHT, Theme.DARK):
            _switch(theme)
            # 只渲染控件自己的绘制（不含窗口底）：没画到的地方应保持透明
            assert not widget.autoFillBackground()
            image = QImage(widget.size(), QImage.Format_ARGB32_Premultiplied)
            image.fill(Qt.transparent)
            widget.render(image, QPoint(), QRegion(), QWidget.DrawChildren)
            assert image.pixelColor(0, 0).alpha() == 0, "迷你图背景应透明（底色来自卡片）"
            strongest = max((image.pixelColor(x, y) for x in range(0, image.width(), 2)
                             for y in range(image.height())), key=lambda c: c.alpha())
            expected = tokens.qcolor(tokens.DATA, theme == Theme.DARK)
            assert strongest.alpha() > 200
            assert abs(strongest.red() - expected.red()) <= 6
            assert abs(strongest.green() - expected.green()) <= 6
            assert abs(strongest.blue() - expected.blue()) <= 6
    finally:
        _dispose(widget)


def test_destroyed_widgets_stop_following(qapp, restore_theme):
    """C++ 端已删除的控件不再响应主题切换（绑定方法的连接随对象销毁自动断开）。"""
    _switch(Theme.LIGHT)
    calls = []
    chart, overview = ChartView(), OverviewStrip()
    chart.apply_theme = lambda: calls.append('chart')
    overview.apply_theme = lambda: calls.append('overview')
    _switch(Theme.DARK)
    assert calls == ['chart', 'overview']
    calls.clear()
    _dispose(chart)
    _dispose(overview)
    _switch(Theme.LIGHT)
    assert calls == []


@pytest.mark.parametrize("factory", [ChartView, OverviewStrip])
def test_runtime_error_unsubscribes(qapp, restore_theme, factory):
    """重新取色碰到已删除的 C++ 对象（RuntimeError）时退订，之后的切换不再进来。"""
    widget = factory()
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("wrapped C/C++ object has been deleted")

    widget.apply_theme = broken
    try:
        _switch(Theme.DARK)
        _switch(Theme.LIGHT)
        assert calls == [1]
    finally:
        _dispose(widget)


# ---------- 跟随系统 ----------

class _System:
    """可控的系统深浅色。"""

    def __init__(self, dark=False):
        self.dark = dark
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.dark


@pytest.fixture
def auto_mode(qapp, restore_theme):
    qconfig.themeMode.value = Theme.AUTO
    _switch(Theme.LIGHT)
    emitted = []
    qconfig.themeChanged.connect(emitted.append)
    yield emitted
    qconfig.themeChanged.disconnect(emitted.append)


def test_watcher_follows_system_in_auto_mode(auto_mode):
    system = _System(dark=False)
    watcher = SystemThemeWatcher(is_dark=system)
    changed = []
    watcher.system_theme_changed.connect(changed.append)

    assert watcher.poll() is False and auto_mode == []       # 未变化不发信号
    system.dark = True
    assert watcher.poll() is True
    assert qconfig.theme == Theme.DARK
    assert auto_mode == [Theme.AUTO] and changed == [Theme.DARK]
    assert watcher.poll() is False                          # 同一状态只切一次
    system.dark = False
    assert watcher.poll() is True and qconfig.theme == Theme.LIGHT
    assert changed == [Theme.DARK, Theme.LIGHT]
    # 配置仍是"跟随系统"，没有被改写成具体主题
    assert qconfig.themeMode.value == Theme.AUTO


@pytest.mark.parametrize("mode", [Theme.LIGHT, Theme.DARK])
def test_watcher_ignores_system_when_theme_fixed(auto_mode, mode):
    qconfig.themeMode.value = mode
    _switch(mode)
    auto_mode.clear()
    system = _System(dark=mode == Theme.LIGHT)
    watcher = SystemThemeWatcher(is_dark=system)
    assert watcher.poll() is False
    assert qconfig.theme == mode and auto_mode == []
    assert system.reads == 0


def test_watcher_redraws_open_charts(auto_mode):
    chart = ChartView()
    chart.set_series(_points(), MEM)
    try:
        system = _System(dark=True)
        SystemThemeWatcher(is_dark=system).poll()
        assert chart.is_background_transparent()
        assert _chart_foreground(chart)['text'] == QColor(chart_palette(True)['text']).name()
    finally:
        _dispose(chart)


def test_watcher_timer_polls_and_stops(auto_mode):
    system = _System(dark=False)
    watcher = SystemThemeWatcher(is_dark=system, interval_ms=10)
    assert SystemThemeWatcher(is_dark=system).interval() == POLL_INTERVAL_MS == 5000
    assert watcher.start() is watcher and watcher.is_active()
    system.dark = True
    assert process_events_until(lambda: qconfig.theme == Theme.DARK, timeout=2)
    watcher.stop()
    assert not watcher.is_active()
    reads = system.reads
    process_events_until(lambda: False, timeout=0.1)
    assert system.reads == reads


def test_main_window_holds_running_watcher(qapp, db, restore_theme):
    """主窗口持有并启动了 SystemThemeWatcher（默认轮询间隔），只在"跟随系统"时生效；
    真退出收尾时停止。"""
    from unittest.mock import MagicMock
    from PyQt5.QtWidgets import QApplication
    from core.monitor_manager import MonitorManager
    from tests.factories import make_main_window
    from tests.helpers import dispose_widget

    manager = MonitorManager(db=db)
    window = make_main_window(db, manager)
    try:
        window.about_group.check_update = lambda *a, **k: None
        watcher = window.theme_watcher
        assert isinstance(watcher, SystemThemeWatcher) and watcher.parent() is window
        assert watcher.is_active() and watcher.interval() == POLL_INTERVAL_MS
        watcher._is_dark = lambda: qconfig.theme != Theme.DARK    # 系统总与当前相反
        qconfig.themeMode.value = Theme.LIGHT
        _switch(Theme.LIGHT)
        assert watcher.poll() is False and qconfig.theme == Theme.LIGHT
        qconfig.themeMode.value = Theme.AUTO
        assert watcher.poll() is True and qconfig.theme == Theme.DARK
        original_quit = QApplication.quit
        QApplication.quit = MagicMock()
        try:
            window.closeEvent(MagicMock())
        finally:
            QApplication.quit = original_quit
        assert not watcher.is_active()
    finally:
        dispose_widget(window)
