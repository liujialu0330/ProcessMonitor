"""
趋势面板：小倍数（每个指标一张 ChartView）或叠加 Δ%（一张多曲线图），下方概览条刷选。

小倍数：
- 各图共享 X 轴（setXLink 到第一张），悬停任一张时其余各图的十字线与读数跟随；
- 水位量/峰值类画 min/max 带（按时间分桶）+ 中心线（桶均值），累计量画每秒速率；
- 打点竖线在每张图上都画（同一批打点），空洞断开并画阴影；
- 列数按宽度 1–2 列；每张图右键发 context_menu_requested（页面弹打点菜单）。
叠加 Δ%：各指标中心线相对基线值（无基线时为显示范围首个桶均值）的百分比；累计量与
参考值为 0 的指标不画，在图下方说明。
"""
from collections import namedtuple
from typing import Dict, List, Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel

from core.history_query import HistorySnapshot, TaskAnalysis
from ui.formatting import percent_change
from ui.viewmodels.analysis_vm import VIEW_OVERLAY
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.overview_strip import OverviewStrip
from ui.widgets.history.chart_view import ChartView
from utils.metrics import VALUE_CLASS_RATE, get_metric_display_name

# 小倍数单图最小高度；两列布局所需的最小内容宽度
SMALL_CHART_MIN_HEIGHT = 200
OVERLAY_MIN_HEIGHT = 340
TWO_COLUMNS_MIN_WIDTH = 900
GRID_SPACING = 12

CenterPoint = namedtuple('CenterPoint', 'timestamp value')


def center_line(snapshot: HistorySnapshot) -> list:
    """中心线：每个时间桶的均值，记在桶首末点的中点；没有带时退回分桶点。"""
    if not snapshot.band:
        return list(snapshot.buckets)
    return [CenterPoint(b.start + (b.end - b.start) / 2, b.avg) for b in snapshot.band]


def draw_snapshot(chart: ChartView, metric: str, snapshot: HistorySnapshot,
                  origin: Optional[float] = None, keep_view: bool = True) -> None:
    """按分析快照画一张小倍数图：累计量画每秒速率，其余画中心线 + min/max 带。"""
    rate = snapshot.value_class == VALUE_CLASS_RATE
    chart.set_series(
        snapshot.rate_series if rate else center_line(snapshot), metric,
        snapshot.markers, keep_view=keep_view, gaps=snapshot.gaps, rate=rate)
    chart.set_band(None if rate else snapshot.band)
    chart.set_relative_origin(origin)


def overlay_reference(snapshot: HistorySnapshot) -> Optional[float]:
    """叠加 Δ% 的参考值：基线值（统计只用基线之后时），否则显示范围首个桶均值。"""
    baseline = snapshot.baseline
    if baseline is not None and baseline.value is not None and snapshot.request.baseline_only:
        return baseline.value
    line = center_line(snapshot)
    return line[0].value if line else None


def overlay_series(analysis: TaskAnalysis) -> tuple:
    """(可画的 OverlaySeries 列表, 未画的指标名列表)。累计量（IO 字节/次数、CPU 时间等）
    只增不减，百分比会把其他指标压成一条平线，叠加图里略去。"""
    series, skipped = [], []
    for metric, snapshot in analysis.snapshots.items():
        if not snapshot.points or not metric:
            continue
        if snapshot.value_class == VALUE_CLASS_RATE:
            skipped.append(get_metric_display_name(metric))
            continue
        line = center_line(snapshot)
        reference = overlay_reference(snapshot)
        name = get_metric_display_name(metric)
        if reference is None or reference == 0 or not line:
            skipped.append(name)
            continue
        series.append(OverlaySeries(
            name, [p.timestamp.timestamp() for p in line],
            [percent_change(p.value, reference) for p in line], snapshot.gaps))
    return series, skipped


class TrendPanel(QWidget):
    """趋势面板。"""

    region_selected = pyqtSignal(object, object)
    # (x epoch 秒, 附近打点或 None, 屏幕坐标)
    context_menu_requested = pyqtSignal(float, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._charts: Dict[str, ChartView] = {}
        self._columns = 1
        self._view_mode = 'small'
        self._origin: Optional[float] = None

        self.grid_host = QWidget(self)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(GRID_SPACING)
        self.overlay_chart = OverlayChart(self)
        self.overlay_chart.set_chart_min_height(OVERLAY_MIN_HEIGHT)
        self.overlay_chart.enable_context_menu()
        self.overlay_chart.context_menu_requested.connect(self.context_menu_requested)
        self.overlay_note = CaptionLabel("", self)
        self.overlay_note.setVisible(False)
        overlay_host = QWidget(self)
        overlay_layout = QVBoxLayout(overlay_host)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.addWidget(self.overlay_chart, 1)
        overlay_layout.addWidget(self.overlay_note)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.grid_host)
        self.stack.addWidget(overlay_host)
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.overview = OverviewStrip(self)
        self.overview.region_selected.connect(self.region_selected)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.overview)

    # ---------- 只读 ----------

    def charts(self) -> Dict[str, ChartView]:
        """小倍数各图（按指标顺序）。"""
        return dict(self._charts)

    def columns(self) -> int:
        return self._columns

    def view_mode(self) -> str:
        return self._view_mode

    def is_interacting(self) -> bool:
        return (any(c.is_interacting() for c in self._charts.values())
                or self.overlay_chart.is_interacting())

    # ---------- 数据 ----------

    def set_analysis(self, analysis: TaskAnalysis, keep_view: bool = False) -> None:
        snapshots = [(m, s) for m, s in analysis.snapshots.items() if s.points and m]
        if [m for m, _ in snapshots] != list(self._charts):
            self._rebuild([m for m, _ in snapshots])
            keep_view = False
        for metric, snapshot in snapshots:
            draw_snapshot(self._charts[metric], metric, snapshot, self._origin)
        if self._charts and not keep_view:
            for chart in self._charts.values():
                chart.reset_view()
        series, skipped = overlay_series(analysis)
        self.overlay_chart.set_overlay(series, analysis.markers, keep_view=keep_view)
        self.overlay_chart.set_relative_origin(self._origin)
        self.overlay_note.setText(
            f"未叠加（累计量或参考值为 0）：{'、'.join(skipped)}" if skipped else "")
        self.overlay_note.setVisible(bool(skipped))

    def clear(self) -> None:
        self._rebuild([])
        self.overlay_chart.clear()
        self.overview.clear()

    def _rebuild(self, metrics: List[str]) -> None:
        for chart in self._charts.values():
            self.grid.removeWidget(chart)
            chart.setParent(None)
            chart.deleteLater()
        self._charts = {}
        first: Optional[ChartView] = None
        for metric in metrics:
            chart = ChartView(self.grid_host)
            chart.set_title(get_metric_display_name(metric))
            chart.set_tools_visible(False)
            chart.set_chart_min_height(SMALL_CHART_MIN_HEIGHT)
            chart.enable_context_menu()
            chart.context_menu_requested.connect(self.context_menu_requested)
            chart.hover_moved.connect(lambda x, src=chart: self._sync_crosshair(src, x))
            if first is None:
                first = chart
            else:
                chart.chart_widget.setXLink(first.chart_widget)
            self._charts[metric] = chart
        self._layout_grid()

    def _sync_crosshair(self, source: ChartView, x) -> None:
        for chart in self._charts.values():
            if chart is not source:
                chart.set_crosshair(x)

    # ---------- 视图 ----------

    def set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self.stack.setCurrentIndex(1 if mode == VIEW_OVERLAY else 0)

    def set_relative_origin(self, origin: Optional[float]) -> None:
        """相对时间原点（epoch 秒）；None 为绝对时间。"""
        self._origin = origin
        for chart in (*self._charts.values(), self.overlay_chart):
            chart.set_relative_origin(origin)

    def zoom_to(self, start: float, end: float) -> None:
        """把图表横向缩放到 [start, end]（epoch 秒，两侧留 2% 余量）。"""
        pad = max((end - start) * 0.02, 1.0)
        targets = list(self._charts.values())[:1] + [self.overlay_chart]
        for chart in targets:
            chart.chart_widget.setXRange(start - pad, end + pad, padding=0)

    def visible_x_range(self) -> Optional[tuple]:
        chart = next(iter(self._charts.values()), None)
        if chart is None:
            return None
        return tuple(chart.chart_widget.getPlotItem().vb.viewRange()[0])

    def update_columns(self, content_width: int) -> bool:
        columns = 2 if content_width >= TWO_COLUMNS_MIN_WIDTH and len(self._charts) > 1 else 1
        if columns == self._columns:
            return False
        self._columns = columns
        self._layout_grid()
        return True

    def _layout_grid(self) -> None:
        for chart in self._charts.values():
            self.grid.removeWidget(chart)
        for i, chart in enumerate(self._charts.values()):
            self.grid.addWidget(chart, i // self._columns, i % self._columns)
