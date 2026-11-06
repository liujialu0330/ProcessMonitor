"""
历史趋势图卡片：真实时间轴曲线、打点竖线、悬停十字线与读数、主题配色、导出图片。

绘图 x 数据统一用 epoch float（timestamp.timestamp()），仅用于绘图与悬停吸附，
严禁回流作为 SQL 查询参数（查询参数全链路走 datetime / ISO 字符串）。
"""
import bisect
from datetime import datetime
from typing import Callable, List, Optional, Sequence

import pyqtgraph as pg
from pyqtgraph import exporters
from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import CardWidget, FluentIcon, TransparentToolButton, qconfig

from ui.chart_theme import chart_colors
from ui.components.chart_axes import ContextDateAxisItem, MetricAxisItem
from ui.components.sparkline import compute_y_range
from ui.formatting import marker_context
from ui.typography import DataCaptionLabel, TypeScale, data_font, ui_font
from ui.widgets.history.common import CARD_PADDING_H, DETAIL_TIME_FORMAT
from utils.metrics import (
    format_metric_value, get_metric_display_name, get_metric_display_unit,
    is_integer_metric)

# 保留约 360px 的有效读图高度，同时允许高窗口自动拉伸
CHART_MIN_HEIGHT = 340
# 导出图片的像素宽度
EXPORT_WIDTH = 1600
# 悬停吸附计算的限流频率（Hz）
HOVER_RATE_LIMIT = 60

# 卡片内边距：上 12，左右 16，下 16；工具行与图表间距 8
CARD_PADDING_TOP = 12
CARD_PADDING_BOTTOM = 16
CONTENT_SPACING = 8

DEFAULT_VALUE_AXIS_TITLE = '值'
TIME_AXIS_TITLE = '时间'


def value_axis_title(metric_type: Optional[str], display_unit: str) -> str:
    """纵轴标题：指标名（显示单位）；无指标时为 "值"。"""
    if not metric_type:
        return DEFAULT_VALUE_AXIS_TITLE
    name = get_metric_display_name(metric_type)
    return f"{name}（{display_unit}）" if display_unit else name


def chart_display_unit(metric_type: Optional[str], values: Sequence[float]) -> str:
    """整张图（以及同屏统计、明细、悬停）共用的显示单位：按绝对值最大的点选取。"""
    if not metric_type:
        return ''
    reference_value = max((abs(value) for value in values), default=0.0)
    return get_metric_display_unit(metric_type, reference_value)


def nearest_index(x_array: Sequence[float], x: float) -> int:
    """在升序 x_array 中二分查找与 x 最接近的下标（距离相等取前一个）。"""
    idx = bisect.bisect_left(x_array, x)
    if idx <= 0:
        return 0
    if idx >= len(x_array):
        return len(x_array) - 1
    before = x_array[idx - 1]
    after = x_array[idx]
    return idx - 1 if (x - before) <= (after - x) else idx


class ChartView(CardWidget):
    """趋势图卡片：工具行（悬停读数 + 保存/复制图标）+ pyqtgraph 绘图区。

    保存/复制只发信号：选择路径、提示结果由页面负责（需要对话框与提示服务），
    控件提供 export_png / grab_pixmap 完成实际导出。
    """

    save_requested = pyqtSignal()
    copy_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # 绘图缓存：悬停吸附与主题重绘复用，不重新查库
        self._x: List[float] = []
        self._y: List[float] = []
        self._metric_type: Optional[str] = None
        self._display_unit = ''
        # 当前范围内的打点（按时间升序）：图上画竖线，悬停读数据此提示所处区间
        self._markers: list = []

        self._pressed = False
        self._interacting = False
        self._interaction_callback: Optional[Callable[[bool], None]] = None

        # 十字线：chart_widget.clear() 会清空 PlotItem 下全部 item，故每次重绘重新加入
        self.crosshair_line = pg.InfiniteLine(angle=90, movable=False)
        self.crosshair_line.setVisible(False)

        self.value_axis = MetricAxisItem(orientation='left')
        self.value_axis.enableAutoSIPrefix(False)
        self.time_axis = ContextDateAxisItem(orientation='bottom')
        self.chart_widget = pg.PlotWidget(axisItems={
            'left': self.value_axis,
            'bottom': self.time_axis,
        })
        self.chart_widget.showGrid(x=False, y=True, alpha=0.18)
        self.chart_widget.setLabel('left', DEFAULT_VALUE_AXIS_TITLE)
        self.chart_widget.setLabel('bottom', TIME_AXIS_TITLE)
        for axis in (self.value_axis, self.time_axis):
            axis.setTickFont(data_font(TypeScale.CHART_TICK))
            axis.setStyle(maxTickLevel=0, tickTextOffset=6)
            axis.setTickDensity(0.55)
            axis.label.setFont(ui_font(TypeScale.CAPTION))
        self.chart_widget.setMinimumHeight(CHART_MIN_HEIGHT)
        self.chart_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 分桶数据已保留尖峰，这里叠加 pyqtgraph 的峰值抽稀与视口裁剪应对缩放卡顿。
        # pyqtgraph 0.14 中 PlotWidget.setDownsampling 转发到 PlotItem，形参名是 mode
        self.chart_widget.setDownsampling(auto=True, mode='peak')
        self.chart_widget.setClipToView(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            CARD_PADDING_H, CARD_PADDING_TOP, CARD_PADDING_H, CARD_PADDING_BOTTOM)
        layout.setSpacing(CONTENT_SPACING)

        # 工具行只保留当前读数和图片快捷操作；透明图标是低强调入口
        toolbar = QHBoxLayout()
        self.hover_label = DataCaptionLabel("", self)
        toolbar.addWidget(self.hover_label)
        toolbar.addStretch()
        self.save_png_button = TransparentToolButton(FluentIcon.SAVE, self)
        self.save_png_button.setToolTip("保存图表为图片")
        self.save_png_button.clicked.connect(self.save_requested)
        self.copy_chart_button = TransparentToolButton(FluentIcon.COPY, self)
        self.copy_chart_button.setToolTip("复制图表到剪贴板")
        self.copy_chart_button.clicked.connect(self.copy_requested)
        toolbar.addWidget(self.save_png_button)
        toolbar.addWidget(self.copy_chart_button)
        layout.addLayout(toolbar)
        layout.addWidget(self.chart_widget)

        # 鼠标离开图表时清空十字线与读数；视口上的按下/松开用于判断"正在拖动"
        self.chart_widget.installEventFilter(self)
        self.chart_widget.viewport().installEventFilter(self)
        # SignalProxy 限流，避免每个原始鼠标移动事件都触发吸附与重绘；需持有引用防 GC
        self._mouse_proxy = pg.SignalProxy(
            self.chart_widget.scene().sigMouseMoved,
            rateLimit=HOVER_RATE_LIMIT, slot=self._on_mouse_moved)

        # 构造时应用一次当前主题，并随全局主题切换重新取色重绘
        self.apply_theme()
        qconfig.themeChanged.connect(self._on_theme_changed)

    # ---------- 只读状态 ----------

    @property
    def x_values(self) -> List[float]:
        """当前绘制的 x（epoch 秒）。"""
        return self._x

    @property
    def y_values(self) -> List[float]:
        return self._y

    @property
    def metric_type(self) -> Optional[str]:
        return self._metric_type

    @property
    def display_unit(self) -> str:
        """当前图固定的显示单位；统计条与明细表应使用同一单位。"""
        return self._display_unit

    @property
    def markers(self) -> list:
        return self._markers

    def has_data(self) -> bool:
        return bool(self._x)

    def axis_title(self) -> str:
        """纵轴当前标题文本。"""
        return self.chart_widget.getPlotItem().getAxis('left').labelText

    # ---------- 数据 ----------

    def set_series(self, points: Sequence, metric_type: Optional[str],
                   markers: Optional[Sequence] = None, keep_view: bool = False) -> None:
        """
        设置曲线数据并重绘。

        Args:
            points: 分桶降采样后的数据点（需有 timestamp / value，按时间升序）
            metric_type: 指标类型
            markers: 打点列表（需有 timestamp / label，按时间升序）；None 表示保持现有打点
            keep_view: True 保留用户当前缩放/平移（自动刷新用）；False 重新自适应范围
        """
        if markers is not None:
            self._markers = list(markers)
        self._metric_type = metric_type
        if points:
            self._x = [dp.timestamp.timestamp() for dp in points]
            self._y = [dp.value for dp in points]
        else:
            self._x = []
            self._y = []

        self._display_unit = chart_display_unit(metric_type, self._y)
        # 计数类指标强制整数刻度；字节类的刻度间距按显示单位选取
        self.value_axis.set_display_unit(
            self._display_unit,
            integer_only=bool(metric_type) and is_integer_metric(metric_type))

        self._redraw()
        self.chart_widget.setLabel('left', value_axis_title(metric_type, self._display_unit))
        self.chart_widget.setLabel('bottom', TIME_AXIS_TITLE)

        if not keep_view:
            self.reset_view()

    def set_markers(self, markers: Sequence) -> None:
        """替换打点并重绘（曲线数据不变）。"""
        self._markers = list(markers)
        self._redraw()

    def clear(self) -> None:
        """清空曲线、打点与单位；纵轴标题保持上一次的文本。"""
        self._x = []
        self._y = []
        self._metric_type = None
        self._display_unit = ''
        self._markers = []
        self.value_axis.set_display_unit('')
        self._redraw()

    def reset_view(self) -> None:
        """重新自适应范围；数据几乎是平线时把纵向跨度扩到下限。"""
        self.chart_widget.enableAutoRange()
        self._apply_min_y_span()

    def _apply_min_y_span(self) -> None:
        """数据几乎是平线时，纵轴跨度扩到下限（与迷你趋势图同一规则），
        避免把几字节的抖动放大成满幅、刻度出现多位小数。跨度足够时保持自动范围。"""
        if not self._y:
            return
        low, high = compute_y_range(self._y, self._metric_type)
        if high - low > max(self._y) - min(self._y):
            self.chart_widget.setYRange(low, high, padding=0.05)

    def _redraw(self) -> None:
        """按缓存的 x/y 重绘曲线与打点竖线（主题切换也走这里，不重新查库）。"""
        self.chart_widget.clear()
        self.chart_widget.addItem(self.crosshair_line, ignoreBounds=True)
        self.hide_hover()

        if not self._x:
            return

        colors = chart_colors()
        pen = pg.mkPen(color=colors['curve'], width=2)
        self.chart_widget.plot(self._x, self._y, pen=pen)

        # 打点竖线：颜色随主题；ignoreBounds 保证它不参与自动范围计算
        marker_pen = pg.mkPen(color=colors['marker'], width=1, style=Qt.DashLine)
        for marker in self._markers:
            line = pg.InfiniteLine(
                pos=marker.timestamp.timestamp(), angle=90, movable=False,
                pen=marker_pen, label=marker.label,
                labelOpts={
                    'position': 0.94, 'color': colors['marker'],
                    'movable': False, 'rotateAxis': (1, 0),
                })
            line.setToolTip(marker.label)
            self.chart_widget.addItem(line, ignoreBounds=True)

    # ---------- 悬停十字线 ----------

    def hover_text(self, index: int) -> str:
        """第 index 个绘制点的悬停读数：时间 · 值 · 所处打点区间（若有）。"""
        x_val = self._x[index]
        time_str = datetime.fromtimestamp(x_val).strftime(DETAIL_TIME_FORMAT)
        formatted_value = format_metric_value(
            self._metric_type, self._y[index], display_unit=self._display_unit)
        text = f"{time_str} · {formatted_value}"
        context = marker_context(
            [m.timestamp.timestamp() for m in self._markers],
            [m.label for m in self._markers], x_val)
        if context:
            text += f" · {context}"
        return text

    def show_hover_at(self, x: float) -> Optional[int]:
        """把十字线吸附到与 x（epoch 秒）最近的绘制点并显示读数；无数据返回 None。"""
        if not self._x:
            return None
        idx = nearest_index(self._x, x)
        self.crosshair_line.setPos(self._x[idx])
        self.crosshair_line.setVisible(True)
        self.hover_label.setText(self.hover_text(idx))
        self._update_interaction()
        return idx

    def hide_hover(self) -> None:
        """隐藏十字线并清空读数。"""
        self.crosshair_line.setVisible(False)
        self.hover_label.setText("")
        self._update_interaction()

    def _on_mouse_moved(self, evt) -> None:
        """SignalProxy 回调（evt 是单元素元组）：鼠标在绘图区内时吸附，离开时隐藏。"""
        if not self._x:
            return
        pos = evt[0]
        plot_item = self.chart_widget.getPlotItem()
        if not plot_item.sceneBoundingRect().contains(pos):
            self.hide_hover()
            return
        self.show_hover_at(plot_item.vb.mapSceneToView(pos).x())

    # ---------- 交互状态（供自动刷新判断是否跳过） ----------

    def is_interacting(self) -> bool:
        """用户是否正在看图：悬停读数中，或按住鼠标拖动/框选中。"""
        return self._pressed or self.crosshair_line.isVisible()

    def set_interaction_callback(self, callback: Optional[Callable[[bool], None]]) -> None:
        """交互状态变化时回调 callback(is_interacting)；传 None 取消。"""
        self._interaction_callback = callback

    def _update_interaction(self) -> None:
        state = self.is_interacting()
        if state == self._interacting:
            return
        self._interacting = state
        if self._interaction_callback is not None:
            self._interaction_callback(state)

    def eventFilter(self, obj, event):
        """鼠标离开图表时清空十字线与读数；记录视口上的按下/松开。"""
        event_type = event.type()
        if obj is self.chart_widget and event_type == QEvent.Leave:
            self._pressed = False
            self.hide_hover()
        elif obj is self.chart_widget.viewport():
            if event_type == QEvent.MouseButtonPress:
                self._pressed = True
                self._update_interaction()
            elif event_type == QEvent.MouseButtonRelease:
                self._pressed = False
                self._update_interaction()
        return super().eventFilter(obj, event)

    # ---------- 主题 ----------

    def apply_theme(self) -> None:
        """按 chart_colors() 设置背景、坐标轴、十字线配色，并用缓存数据重绘曲线。"""
        colors = chart_colors()
        self.chart_widget.setBackground(colors['background'])
        axis_pen = pg.mkPen(color=colors['axis'])
        text_pen = pg.mkPen(color=colors['text'])
        for axis_name in ('bottom', 'left'):
            axis = self.chart_widget.getAxis(axis_name)
            axis.setPen(axis_pen)
            axis.setTextPen(text_pen)
        self.crosshair_line.setPen(pg.mkPen(color=colors['crosshair'], style=Qt.DashLine))
        self._redraw()

    def _on_theme_changed(self, _theme) -> None:
        self.apply_theme()

    # ---------- 导出 ----------

    def export_png(self, path: str, width: int = EXPORT_WIDTH) -> None:
        """把绘图区导出为 PNG；失败时异常原样抛出，由调用方提示。"""
        exporter = exporters.ImageExporter(self.chart_widget.getPlotItem())
        exporter.parameters()['width'] = width
        exporter.export(path)

    def grab_pixmap(self) -> QPixmap:
        """绘图区截图（复制到剪贴板用）。"""
        return self.chart_widget.grab()
