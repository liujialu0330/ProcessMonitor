"""
趋势面板：焦点指标大图 + 概览条，下方是其余指标（小倍数或叠加 Δ%）。

- 焦点大图：一张 ChartView，跟随焦点指标重画（不随焦点切换重建）；水位量/峰值类画
  min/max 带（按时间分桶）+ 中心线（桶均值），累计量画每秒速率；打点竖线、空洞断开与阴影；
- 概览条：焦点指标在整个可选范围内的走势，刷选 → 自定义范围；
- 小倍数：只为判定"持续上涨 / 波动"的指标各建一张小图（compact，不显示轴标题，单位写进
  卡片标题）；平稳、数据不足与不做趋势判定的指标折叠为一行迷你图（指标名 · 当前值 ·
  判定胶囊），点一行即设为焦点、在大图里展开；"全部展开"一次给全部指标建小图。
  各图共享 X 轴（setXLink 到焦点大图），悬停任一张时其余各图的十字线与读数跟随；
  列数按宽度 1–2 列；每张图右键发 context_menu_requested（页面弹打点菜单）；
- 叠加 Δ%：各指标中心线相对基线值（无基线时为显示范围首个桶均值）的百分比，替换小倍数区；
  累计量与参考值为 0 的指标不画，在图下方说明。图例放在绘图区外（不压打点徽标）。
排序与"是否异常"的口径在 ui.viewmodels.history_vm（与摘要表、结论条一致）。
"""
from collections import namedtuple
from typing import Dict, List, Optional, Sequence

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget,
                             QVBoxLayout, QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, StrongBodyLabel,
                            TransparentPushButton, qconfig)

from core.history_query import HistorySnapshot, TaskAnalysis
from ui import theme_tokens as tokens
from ui.chart_theme import series_colors
from ui.components.sparkline import SparklineWidget
from ui.formatting import build_summary_row, percent_change
from ui.typography import DataLabel
from ui.viewmodels.analysis_vm import VIEW_OVERLAY
from ui.viewmodels.history_vm import is_anomalous
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.overview_strip import OverviewStrip
from ui.widgets.analysis.summary_table import pill_style
from ui.widgets.history.chart_view import ChartView, value_axis_title
from ui.widgets.history.common import CARD_PADDING_H
from utils.metrics import VALUE_CLASS_RATE, get_metric_display_name, split_member_key

# 焦点大图、小倍数单图的绘图区最小高度；两列布局所需的最小内容宽度
FOCUS_CHART_MIN_HEIGHT = 280
SMALL_CHART_MIN_HEIGHT = 180
OVERLAY_MIN_HEIGHT = 340
TWO_COLUMNS_MIN_WIDTH = 900
GRID_SPACING = 12

# 折叠行：高度、指标名列宽、当前值列宽、迷你图缓冲点数
FOLDED_ROW_HEIGHT = 40
FOLDED_NAME_WIDTH = 132
FOLDED_VALUE_WIDTH = 120
FOLDED_SPARK_POINTS = 120
FOLDED_ROW_TIP = "点击设为焦点指标，在上方大图中展开"
FOLDED_TITLE = "其余 {count} 项"
FOLDED_HINT = "平稳、数据不足或不做趋势判定的指标折叠为一行；点击一行在大图中展开"
EXPANDED_TITLE = "已全部展开"
EXPAND_ALL_TEXT = "全部展开"
COLLAPSE_TEXT = "收起"
OVERLAY_SKIPPED = "未叠加（累计量或参考值为 0）：{names}"
# 图例色块
LEGEND_SWATCH = "━━"

CenterPoint = namedtuple('CenterPoint', 'timestamp value')


def plain_name(metric: str) -> str:
    """指标显示名；成员序列键（指标@成员键）去掉成员后缀——成员已由工具条与结论条点明，
    折叠行与小图标题不必再重复一遍。"""
    return get_metric_display_name(split_member_key(metric)[0])


def center_line(snapshot: HistorySnapshot) -> list:
    """中心线：每个时间桶的均值，记在桶首末点的中点；没有带时退回分桶点。"""
    if not snapshot.band:
        return list(snapshot.buckets)
    return [CenterPoint(b.start + (b.end - b.start) / 2, b.avg) for b in snapshot.band]


def draw_snapshot(chart: ChartView, metric: str, snapshot: HistorySnapshot,
                  origin: Optional[float] = None, keep_view: bool = True) -> None:
    """按分析快照画一张图：累计量画每秒速率，其余画中心线 + min/max 带。"""
    rate = snapshot.value_class == VALUE_CLASS_RATE
    chart.set_series(
        snapshot.rate_series if rate else center_line(snapshot), metric,
        snapshot.markers, keep_view=keep_view, gaps=snapshot.gaps, rate=rate)
    chart.set_band(None if rate else snapshot.band)
    chart.set_relative_origin(origin)


def overlay_reference(snapshot: HistorySnapshot) -> Optional[float]:
    """叠加 Δ% 的参考值：基线值（统计只用基线之后、且基线与统计在同一启动段时），否则显示
    范围首个桶均值。"""
    if snapshot.baseline_in_use:
        return snapshot.baseline.value
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
            skipped.append(plain_name(metric))
            continue
        line = center_line(snapshot)
        reference = overlay_reference(snapshot)
        name = plain_name(metric)
        if reference is None or reference == 0 or not line:
            skipped.append(name)
            continue
        series.append(OverlaySeries(
            name, [p.timestamp.timestamp() for p in line],
            [percent_change(p.value, reference) for p in line], snapshot.gaps))
    return series, skipped


def chart_series(snapshot: HistorySnapshot) -> list:
    """图上实际画的序列：累计量为每秒速率，其余为中心线。"""
    return snapshot.rate_series if snapshot.value_class == VALUE_CLASS_RATE \
        else center_line(snapshot)


class SeriesLegend(CaptionLabel):
    """绘图区外的一行图例（"━━ 当前任务  ━━ 对比任务"），色块按 series_colors 的顺序取色。
    pyqtgraph 自带图例浮在绘图区左上角，会压住打点徽标，叠加图与对比图改用它。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._names: List[str] = []
        self.setTextFormat(Qt.RichText)
        self.setVisible(False)
        qconfig.themeChanged.connect(self._on_theme_changed)

    def set_names(self, names: Sequence[str]) -> None:
        self._names = list(names)
        self._render()

    def names(self) -> List[str]:
        return list(self._names)

    def _render(self) -> None:
        palette = series_colors()
        self.setText("&nbsp;&nbsp;&nbsp;".join(
            f'<span style="color:{palette[i % len(palette)]}">{LEGEND_SWATCH}</span> {name}'
            for i, name in enumerate(self._names)))
        self.setVisible(bool(self._names))

    def _on_theme_changed(self, _theme) -> None:
        try:
            self._render()
        except RuntimeError:
            try:
                qconfig.themeChanged.disconnect(self._on_theme_changed)
            except TypeError:
                pass


def move_legend_out(chart: OverlayChart, legend: SeriesLegend) -> None:
    """隐藏绘图区内的 pyqtgraph 图例（legend_names 仍可读），改由 legend 在图外显示。"""
    chart.legend.setVisible(False)
    legend.set_names(chart.legend_names())


class FoldedMetricRow(QWidget):
    """折叠行：指标名 · 迷你图 · 当前值 · 判定胶囊；点击发 clicked(metric)。"""

    clicked = pyqtSignal(str)

    def __init__(self, metric: str, parent=None):
        super().__init__(parent)
        self.metric = metric
        self._hovered = False
        self._verdict = ""
        self._dimmed = False
        self.setFixedHeight(FOLDED_ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(FOLDED_ROW_TIP)
        self.name_label = BodyLabel(plain_name(metric), self)
        self.name_label.setFixedWidth(FOLDED_NAME_WIDTH)
        self.sparkline = SparklineWidget(FOLDED_SPARK_POINTS, metric_type=metric, parent=self)
        self.value_label = DataLabel("", self)
        self.value_label.setFixedWidth(FOLDED_VALUE_WIDTH)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pill = QLabel("", self)
        self.pill.setAlignment(Qt.AlignCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(12)
        layout.addWidget(self.name_label)
        layout.addWidget(self.sparkline, 1)
        layout.addWidget(self.value_label)
        layout.addWidget(self.pill)
        qconfig.themeChanged.connect(self._on_theme_changed)

    def set_snapshot(self, snapshot: HistorySnapshot) -> None:
        row = build_summary_row(self.metric, snapshot)
        self.value_label.setText(row.text("current"))
        self._verdict, self._dimmed = row.text("verdict"), row.dimmed
        self.pill.setText(self._verdict)
        self.pill.setToolTip(row.tooltips.get("verdict", ""))
        self.sparkline.clear()
        for point in chart_series(snapshot):
            self.sparkline.append(point.value, point.timestamp)
        self.sparkline.set_markers([(m.timestamp, m.kind) for m in snapshot.markers])
        self._apply_theme()

    def verdict(self) -> str:
        return self._verdict

    def click(self) -> None:
        """等同用户点击这一行。"""
        self.clicked.emit(self.metric)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.click()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._hovered:
            painter = QPainter(self)
            try:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                painter.setBrush(tokens.qcolor(tokens.CELL_HIGHLIGHT))
                painter.drawRoundedRect(QRectF(self.rect()), 6, 6)
            finally:
                painter.end()
        super().paintEvent(event)

    def _apply_theme(self) -> None:
        self.pill.setStyleSheet(pill_style(self._verdict, self._dimmed))

    def _on_theme_changed(self, _theme) -> None:
        try:
            self._apply_theme()
        except RuntimeError:
            try:
                qconfig.themeChanged.disconnect(self._on_theme_changed)
            except TypeError:
                pass


class FoldedPanel(CardWidget):
    """折叠区（卡片）：标题"其余 N 项"+"全部展开"，下面每个折叠指标一行。"""

    expand_toggled = pyqtSignal(bool)
    metric_activated = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: Dict[str, FoldedMetricRow] = {}
        self._expanded = False
        self.title_label = StrongBodyLabel("", self)
        self.hint_label = CaptionLabel(FOLDED_HINT, self)
        self.expand_button = TransparentPushButton(EXPAND_ALL_TEXT, self)
        self.expand_button.clicked.connect(lambda: self.expand_toggled.emit(not self._expanded))
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(self.title_label)
        head.addWidget(self.hint_label)
        head.addStretch()
        head.addWidget(self.expand_button)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, 10, CARD_PADDING_H, 10)
        layout.setSpacing(4)
        layout.addLayout(head)
        layout.addLayout(self.rows_layout)

    def rows(self) -> Dict[str, FoldedMetricRow]:
        return dict(self._rows)

    def set_rows(self, items: Sequence, expanded: bool, expandable: int) -> None:
        """items 为 [(指标, 快照)]（显示顺序）；expandable 为可展开的指标数（展开后仍要
        留着"收起"按钮）。行按指标复用，自动刷新不闪烁。"""
        self._expanded = expanded
        wanted = [metric for metric, _snapshot in items]
        for metric in [m for m in self._rows if m not in wanted]:
            row = self._rows.pop(metric)
            self.rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        for index, (metric, snapshot) in enumerate(items):
            row = self._rows.get(metric)
            if row is None:
                row = FoldedMetricRow(metric, self)
                row.clicked.connect(self.metric_activated)
                self._rows[metric] = row
            else:
                self.rows_layout.removeWidget(row)
            self.rows_layout.insertWidget(index, row)
            row.set_snapshot(snapshot)
        self._rows = {metric: self._rows[metric] for metric in wanted}
        self.title_label.setText(EXPANDED_TITLE if expanded
                                 else FOLDED_TITLE.format(count=len(items)))
        self.hint_label.setVisible(not expanded)
        self.expand_button.setText(COLLAPSE_TEXT if expanded else EXPAND_ALL_TEXT)
        self.setVisible(bool(items) or (expanded and expandable > 0))


class TrendPanel(QWidget):
    """趋势面板。"""

    region_selected = pyqtSignal(object, object)
    # (x epoch 秒, 附近打点或 None, 屏幕坐标)
    context_menu_requested = pyqtSignal(float, object, object)
    # 用户点了折叠行：把该指标设为焦点
    metric_activated = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._small: Dict[str, ChartView] = {}
        self._snapshots: Dict[str, HistorySnapshot] = {}
        self._order: List[str] = []
        self._anomalous: set = set()
        self._focus: Optional[str] = None
        self._expanded = False
        self._columns = 1
        # 最近一次布局的内容宽度：小图数量随展开/切焦点变化时据此重算列数
        self._content_width = 0
        self._view_mode = 'small'
        self._origin: Optional[float] = None

        self.focus_chart = ChartView(self)
        self.focus_chart.set_tools_visible(False)
        self.focus_chart.set_chart_min_height(FOCUS_CHART_MIN_HEIGHT)
        self.focus_chart.setVisible(False)
        self._wire_chart(self.focus_chart)
        self.overview = OverviewStrip(self)
        self.overview.region_selected.connect(self.region_selected)

        self.grid_host = QWidget(self)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(GRID_SPACING)
        self.grid_host.setVisible(False)
        self.folded_panel = FoldedPanel(self)
        self.folded_panel.setVisible(False)
        self.folded_panel.metric_activated.connect(self.metric_activated)
        self.folded_panel.expand_toggled.connect(self.set_expanded)
        small_host = QWidget(self)
        small_layout = QVBoxLayout(small_host)
        small_layout.setContentsMargins(0, 0, 0, 0)
        small_layout.setSpacing(GRID_SPACING)
        small_layout.addWidget(self.grid_host)
        small_layout.addWidget(self.folded_panel)
        small_layout.addStretch()

        self.overlay_chart = OverlayChart(self)
        self.overlay_chart.set_chart_min_height(OVERLAY_MIN_HEIGHT)
        self.overlay_chart.enable_context_menu()
        self.overlay_chart.context_menu_requested.connect(self.context_menu_requested)
        self.overlay_legend = SeriesLegend(self)
        self.overlay_note = CaptionLabel("", self)
        self.overlay_note.setVisible(False)
        overlay_host = QWidget(self)
        overlay_layout = QVBoxLayout(overlay_host)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        overlay_layout.setSpacing(4)
        overlay_layout.addWidget(self.overlay_legend)
        overlay_layout.addWidget(self.overlay_chart, 1)
        overlay_layout.addWidget(self.overlay_note)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(small_host)
        self.stack.addWidget(overlay_host)
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GRID_SPACING)
        layout.addWidget(self.focus_chart)
        layout.addWidget(self.overview)
        layout.addWidget(self.stack, 1)

    def _wire_chart(self, chart: ChartView) -> None:
        chart.enable_context_menu()
        chart.context_menu_requested.connect(self.context_menu_requested)
        chart.hover_moved.connect(lambda x, src=chart: self._sync_crosshair(src, x))

    # ---------- 只读 ----------

    def charts(self) -> Dict[str, ChartView]:
        """当前画出的各图：焦点大图在前，其后是小倍数（按严重程度）。"""
        result: Dict[str, ChartView] = {}
        if self._focus is not None and self.focus_chart.has_data():
            result[self._focus] = self.focus_chart
        result.update(self._small)
        return result

    def small_charts(self) -> Dict[str, ChartView]:
        return dict(self._small)

    def focus_metric(self) -> Optional[str]:
        return self._focus

    def folded_metrics(self) -> List[str]:
        return list(self.folded_panel.rows())

    def folded_rows(self) -> Dict[str, FoldedMetricRow]:
        return self.folded_panel.rows()

    def expanded(self) -> bool:
        return self._expanded

    def columns(self) -> int:
        return self._columns

    def view_mode(self) -> str:
        return self._view_mode

    def is_interacting(self) -> bool:
        return (any(c.is_interacting() for c in self.charts().values())
                or self.overlay_chart.is_interacting())

    # ---------- 数据 ----------

    def set_analysis(self, analysis: TaskAnalysis, keep_view: bool = False, *,
                     order: Optional[Sequence[str]] = None,
                     focus: Optional[str] = None) -> None:
        """order 为显示顺序（缺省按任务的指标顺序），focus 为焦点指标。指标集合变化时
        （换任务、换范围后某指标没数据）收起"全部展开"并重新自适应范围。"""
        snapshots = {m: s for m, s in analysis.snapshots.items() if s.points and m}
        order = [m for m in (order if order is not None else analysis.metrics) if m in snapshots]
        if set(order) != set(self._snapshots):
            self._expanded = False
            keep_view = False
        self._snapshots = snapshots
        self._order = order
        self._anomalous = {m for m in order if is_anomalous(snapshots[m])}
        if focus is not None:
            self._focus = focus
        elif self._focus not in snapshots:
            self._focus = order[0] if order else None
        self._sync(keep_view)
        series, skipped = overlay_series(analysis)
        self.overlay_chart.set_overlay(series, analysis.markers, keep_view=keep_view)
        self.overlay_chart.set_relative_origin(self._origin)
        move_legend_out(self.overlay_chart, self.overlay_legend)
        self.overlay_note.setText(
            OVERLAY_SKIPPED.format(names='、'.join(skipped)) if skipped else "")
        self.overlay_note.setVisible(bool(skipped))

    def set_focus(self, metric: Optional[str]) -> None:
        """切换焦点指标：大图改画它，它原来的小图或折叠行让出位置。"""
        if metric == self._focus:
            return
        self._focus = metric
        self._sync(keep_view=False)

    def set_expanded(self, expanded: bool) -> None:
        """全部展开（每个指标一张小图）/ 收起（只留持续上涨与波动的小图）。"""
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._sync(keep_view=False)

    def expand_all(self) -> None:
        self.set_expanded(True)

    def clear(self) -> None:
        self._snapshots, self._order, self._anomalous = {}, [], set()
        self._focus = None
        self._expanded = False
        self._sync(keep_view=False)
        self.overlay_chart.clear()
        self.overlay_legend.set_names([])
        self.overview.clear()

    def _sync(self, keep_view: bool) -> None:
        """按焦点、异常集合与展开状态摆放大图、小图与折叠行。小图按指标复用。"""
        focus = self._focus if self._focus in self._snapshots else None
        if focus is not None:
            draw_snapshot(self.focus_chart, focus, self._snapshots[focus], self._origin)
            self.focus_chart.set_title(get_metric_display_name(focus))
        else:
            self.focus_chart.clear()
        self.focus_chart.setVisible(focus is not None)

        others = [m for m in self._order if m != focus]
        small = [m for m in others if self._expanded or m in self._anomalous]
        folded = [m for m in others if m not in small]
        for metric in [m for m in self._small if m not in small]:
            chart = self._small.pop(metric)
            self.grid.removeWidget(chart)
            chart.setParent(None)
            chart.deleteLater()
        charts: Dict[str, ChartView] = {}
        for metric in small:
            chart = self._small.get(metric) or self._make_small()
            draw_snapshot(chart, metric, self._snapshots[metric], self._origin)
            chart.set_title(value_axis_title(
                split_member_key(metric)[0], chart.display_unit, chart.is_rate))
            charts[metric] = chart
        self._small = charts
        if not self._fit_columns():
            self._layout_grid()
        self.grid_host.setVisible(bool(charts))
        self.folded_panel.set_rows([(m, self._snapshots[m]) for m in folded], self._expanded,
                                   len([m for m in others if m not in self._anomalous]))
        if not keep_view:
            for chart in self.charts().values():
                chart.reset_view()

    def _make_small(self) -> ChartView:
        chart = ChartView(self.grid_host, compact=True)
        chart.set_tools_visible(False)
        chart.set_chart_min_height(SMALL_CHART_MIN_HEIGHT)
        self._wire_chart(chart)
        chart.chart_widget.setXLink(self.focus_chart.chart_widget)
        return chart

    def _sync_crosshair(self, source: ChartView, x) -> None:
        for chart in self.charts().values():
            if chart is not source:
                chart.set_crosshair(x)

    # ---------- 视图 ----------

    def set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self.stack.setCurrentIndex(1 if mode == VIEW_OVERLAY else 0)

    def set_relative_origin(self, origin: Optional[float]) -> None:
        """相对时间原点（epoch 秒）；None 为绝对时间。"""
        self._origin = origin
        for chart in (self.focus_chart, *self._small.values(), self.overlay_chart):
            chart.set_relative_origin(origin)

    def zoom_to(self, start: float, end: float) -> None:
        """把图表横向缩放到 [start, end]（epoch 秒，两侧留 2% 余量）；小图跟随焦点大图。"""
        pad = max((end - start) * 0.02, 1.0)
        for chart in (self.focus_chart, self.overlay_chart):
            chart.chart_widget.setXRange(start - pad, end + pad, padding=0)

    def visible_x_range(self) -> Optional[tuple]:
        if not self.focus_chart.has_data():
            return None
        return tuple(self.focus_chart.chart_widget.getPlotItem().vb.viewRange()[0])

    def update_columns(self, content_width: int) -> bool:
        """按内容宽度定小倍数列数（1–2 列）；列数变了才重排，返回是否重排。"""
        self._content_width = content_width
        return self._fit_columns()

    def _fit_columns(self) -> bool:
        wide = self._content_width >= TWO_COLUMNS_MIN_WIDTH
        columns = 2 if wide and len(self._small) > 1 else 1
        if columns == self._columns:
            return False
        self._columns = columns
        self._layout_grid()
        return True

    def _layout_grid(self) -> None:
        for chart in self._small.values():
            self.grid.removeWidget(chart)
        for i, chart in enumerate(self._small.values()):
            self.grid.addWidget(chart, i // self._columns, i % self._columns)
