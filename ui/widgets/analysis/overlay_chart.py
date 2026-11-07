"""
多曲线叠加图：在 ChartView（空洞阴影、打点竖线、十字线、主题、相对时间轴）之上
一张图画多条曲线 + 图例。

两处用途：
- 叠加 Δ%：各指标相对基线（无基线时为起点）的百分比变化，量纲不同的指标可放在一起看；
- 任务对比：两个任务同一指标相对各自起点的变化量，横轴对齐到相对时间。
悬停读数列出十字线处各曲线的值（按各自最近的点）。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence

import numpy as np
import pyqtgraph as pg

from core.analysis.series import connect_mask
from ui.chart_theme import chart_colors, series_colors
from ui.formatting import format_relative
from ui.widgets.history.chart_view import ChartView, nearest_index
from ui.widgets.history.common import DETAIL_TIME_FORMAT

PERCENT_AXIS_TITLE = '较基线变化（%）'


def format_percent(value: float) -> str:
    return f"{value:+.1f}%"


@dataclass(frozen=True)
class OverlaySeries:
    """一条叠加曲线：名称与 (x epoch 秒, y) 序列（x 升序）；gaps 为该曲线自己的空洞。"""
    name: str
    xs: Sequence[float]
    ys: Sequence[float]
    gaps: Sequence = ()


class OverlayChart(ChartView):
    """多曲线叠加图（不显示保存/复制图标，由页面统一导出）。"""

    def __init__(self, parent=None):
        self._series: List[OverlaySeries] = []
        self._formatter: Callable[[float], str] = format_percent
        self._curve_items: list = []
        super().__init__(parent)
        self.set_tools_visible(False)
        self.legend = self.chart_widget.getPlotItem().addLegend(offset=(10, 6))
        self.legend.setLabelTextColor(chart_colors()['text'])

    @property
    def series(self) -> List[OverlaySeries]:
        return list(self._series)

    def curve_items(self) -> list:
        return list(self._curve_items)

    def legend_names(self) -> List[str]:
        return [label.text for _sample, label in self.legend.items]

    def set_overlay(self, series: Sequence[OverlaySeries], markers: Sequence = (),
                    *, axis_title: str = PERCENT_AXIS_TITLE,
                    formatter: Callable[[float], str] = format_percent,
                    display_unit: str = '', keep_view: bool = False) -> None:
        """设置全部曲线并重绘；十字线吸附以点数最多的曲线为准。"""
        self._series = [s for s in series if len(s.xs)]
        self._formatter = formatter
        self._markers = list(markers)
        self._metric_type = None
        self._rate = False
        self._band = []
        reference = max(self._series, key=lambda s: len(s.xs), default=None)
        self._x = list(reference.xs) if reference else []
        self._y = list(reference.ys) if reference else []
        self._gaps = []
        self._display_unit = display_unit
        self.value_axis.set_display_unit(display_unit)
        self._redraw()
        self.chart_widget.setLabel('left', axis_title)
        if not keep_view:
            self.chart_widget.enableAutoRange()

    def clear(self) -> None:
        self._series = []
        super().clear()

    def _redraw(self) -> None:
        self.chart_widget.clear()
        self._marker_items = []
        self._gap_items = []
        self._band_items = []
        self._curve_items = []
        self.chart_widget.addItem(self.crosshair_line, ignoreBounds=True)
        self.hide_hover()
        if not self._series:
            return
        colors = chart_colors()
        palette = series_colors()
        # 图例文字默认是 pyqtgraph 的固定灰（浅色卡片上不足 3:1），按主题取色
        legend = getattr(self, 'legend', None)
        if legend is not None:
            legend.setLabelTextColor(colors['text'])
        for i, item in enumerate(self._series):
            gaps = [(a.timestamp(), b.timestamp()) if isinstance(a, datetime) else (a, b)
                    for a, b in item.gaps]
            extra = ({'connect': np.array(connect_mask(list(item.xs), gaps), dtype=bool)}
                     if gaps else {})
            curve = self.chart_widget.plot(
                list(item.xs), list(item.ys), name=item.name,
                pen=pg.mkPen(color=palette[i % len(palette)], width=2), **extra)
            self._curve_items.append(curve)
        self._draw_markers(colors)

    def hover_text(self, index: int) -> str:
        x_val = self._x[index]
        if self._relative_origin is not None:
            head = format_relative(x_val - self._relative_origin)
        else:
            head = datetime.fromtimestamp(x_val).strftime(DETAIL_TIME_FORMAT)
        parts = [head]
        for item in self._series:
            i = nearest_index(list(item.xs), x_val)
            parts.append(f"{item.name} {self._formatter(item.ys[i])}")
        return " · ".join(parts)

    def values_at(self, x: float) -> List[Optional[float]]:
        """各曲线在 x 处（最近点）的值。"""
        return [item.ys[nearest_index(list(item.xs), x)] if len(item.xs) else None
                for item in self._series]
