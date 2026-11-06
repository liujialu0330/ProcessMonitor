"""
概览条：趋势图下方的一条窄图，显示焦点指标在整个可选范围内的走势，上面一个可拖动的
选区（pyqtgraph LinearRegionItem）。拖动/拉伸选区结束即发 region_selected(start, end)，
页面据此把统计范围设为"自定义"。

选区由程序设置时（载入新数据、切回预设范围）不发信号。处于自定义范围时，分析结果只覆盖
选区内的数据，概览条仍保留之前整段的曲线，便于重新刷选。
"""
from datetime import datetime
from typing import Optional, Sequence, Tuple

import pyqtgraph as pg
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, qconfig

from ui.chart_theme import chart_colors
from ui.components.chart_axes import ContextDateAxisItem
from ui.typography import TypeScale, data_font

OVERVIEW_HEIGHT = 84
REGION_ALPHA = 40
HINT_TEXT = "拖动或拉伸选区即按选区重新统计；点「全部」等范围恢复"


class OverviewStrip(QWidget):
    """概览条（含刷选）。"""

    region_selected = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._xs: list = []
        self._ys: list = []
        self._programmatic = False

        self.time_axis = ContextDateAxisItem(orientation='bottom')
        self.time_axis.setTickFont(data_font(TypeScale.CAPTION))
        self.plot = pg.PlotWidget(axisItems={'bottom': self.time_axis})
        self.plot.hideAxis('left')
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setFixedHeight(OVERVIEW_HEIGHT)
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.curve = self.plot.plot([], [])
        self.region = pg.LinearRegionItem(orientation='vertical')
        self.region.setZValue(10)
        self.region.sigRegionChangeFinished.connect(self._on_region_finished)
        self.plot.addItem(self.region, ignoreBounds=True)
        self.region.setVisible(False)

        self.hint_label = CaptionLabel(HINT_TEXT, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.plot)
        layout.addWidget(self.hint_label)
        self.apply_theme()
        # 连到绑定方法（不用 lambda）：控件销毁后 Qt 自动断开，主题切换不会碰已删除对象
        qconfig.themeChanged.connect(self._on_theme_changed)

    # ---------- 数据 ----------

    def has_data(self) -> bool:
        return bool(self._xs)

    def x_bounds(self) -> Optional[Tuple[float, float]]:
        return (self._xs[0], self._xs[-1]) if self._xs else None

    def set_series(self, points: Sequence) -> None:
        """整段走势（需有 timestamp / value，按时间升序）；选区重置为整段。"""
        self._xs = [p.timestamp.timestamp() for p in points]
        self._ys = [p.value for p in points]
        self.curve.setData(self._xs, self._ys)
        self.region.setVisible(bool(self._xs))
        if self._xs:
            self.plot.setXRange(self._xs[0], self._xs[-1], padding=0.01)
            self.set_region(None)

    def clear(self) -> None:
        self.set_series([])

    def set_region(self, region: Optional[Tuple[datetime, datetime]]) -> None:
        """程序设置选区（不发 region_selected）；None 表示整段。"""
        if not self._xs:
            return
        if region is None:
            values = (self._xs[0], self._xs[-1])
        else:
            values = (region[0].timestamp(), region[1].timestamp())
        self._programmatic = True
        try:
            self.region.setRegion(values)
        finally:
            self._programmatic = False

    def region_values(self) -> Tuple[float, float]:
        low, high = self.region.getRegion()
        return float(low), float(high)

    def select(self, start: float, end: float) -> None:
        """等同用户把选区拖到 [start, end]（epoch 秒）：设置后发一次 region_selected
        （setRegion 自身会发 sigRegionChangeFinished，这里先屏蔽，避免重复）。"""
        self._programmatic = True
        try:
            self.region.setRegion((start, end))
        finally:
            self._programmatic = False
        self._on_region_finished()

    def _on_region_finished(self, *_args) -> None:
        if self._programmatic or not self._xs:
            return
        low, high = self.region_values()
        low, high = max(low, self._xs[0]), min(high, self._xs[-1])
        if high <= low:
            return
        self.region_selected.emit(datetime.fromtimestamp(low), datetime.fromtimestamp(high))

    # ---------- 主题 ----------

    def _on_theme_changed(self, _theme) -> None:
        self.apply_theme()

    def apply_theme(self) -> None:
        colors = chart_colors()
        self.plot.setBackground(colors['background'])
        self.time_axis.setPen(pg.mkPen(color=colors['axis']))
        self.time_axis.setTextPen(pg.mkPen(color=colors['text']))
        self.curve.setPen(pg.mkPen(color=colors['crosshair'], width=1))
        fill = QColor(colors['curve'])
        fill.setAlpha(REGION_ALPHA)
        self.region.setBrush(pg.mkBrush(fill))
        edge = QColor(colors['curve'])
        for line in self.region.lines:
            line.setPen(pg.mkPen(color=edge, width=2))
