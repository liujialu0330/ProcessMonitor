"""
历史统计条：起点 / 当前 / 变化 / 增速 / 最小 / 最大 六格。

列数按控件自身的内容宽度计算（宽屏一行六格，中等宽度两行三列，窄窗口三行两列），
只在列数变化时重排，避免拖动窗口时反复拆装布局。
"""
from typing import Dict, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QGridLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget

from ui.formatting import build_stat_texts
from ui.typography import StatValueLabel
from ui.widgets.history.common import (
    CARD_PADDING_H, VIEWPORT_TO_CONTENT_OFFSET, content_width_of)

# 统计条各格的名称（顺序即显示顺序）
STAT_KEYS = ("起点", "当前", "变化", "增速", "最小", "最大")
STAT_TOOLTIPS = {
    "起点": "所选范围内最早一次采集的值",
    "当前": "所选范围内最新一次采集的值",
    "变化": "当前值相对起点的变化量（绝对值与百分比）",
    "增速": "对所选范围内全部数据做线性回归得到的每小时变化量，持续为正说明在持续上涨",
    "最小": "所选范围内的最小值",
    "最大": "所选范围内的最大值",
}
EMPTY_TEXT = "--"

# 原规则按页面视口：≥1100 六列、≥560 三列；换算成卡片内容宽度
SIX_COLUMNS_MIN_WIDTH = 1100 - VIEWPORT_TO_CONTENT_OFFSET
THREE_COLUMNS_MIN_WIDTH = 560 - VIEWPORT_TO_CONTENT_OFFSET

# 卡片内边距：上下 10，左右 16；格间距横 8 纵 10；格内左右 12、标题与数值间 3
CARD_PADDING_V = 10
CELL_SPACING_H = 8
CELL_SPACING_V = 10
CELL_PADDING_H = 12
CELL_INNER_SPACING = 3


class StatsStrip(CardWidget):
    """六格统计条（卡片）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stats_layout = QGridLayout(self)
        self.stats_layout.setContentsMargins(
            CARD_PADDING_H, CARD_PADDING_V, CARD_PADDING_H, CARD_PADDING_V)
        self.stats_layout.setHorizontalSpacing(CELL_SPACING_H)
        self.stats_layout.setVerticalSpacing(CELL_SPACING_V)

        self.stat_value_labels: Dict[str, StatValueLabel] = {}
        self._cells = []
        for key in STAT_KEYS:
            cell = QWidget(self)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(CELL_PADDING_H, 0, CELL_PADDING_H, 0)
            cell_layout.setSpacing(CELL_INNER_SPACING)
            caption = CaptionLabel(key, cell)
            caption.setToolTip(STAT_TOOLTIPS[key])
            cell_layout.addWidget(caption)
            value_label = StatValueLabel(EMPTY_TEXT, cell)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            # 长文本（如变化量 + 百分比）宁可被裁剪也不撑宽整行，完整内容见 tooltip
            value_label.setMinimumWidth(1)
            value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            cell_layout.addWidget(value_label)
            self.stat_value_labels[key] = value_label
            self._cells.append(cell)

        self._columns: Optional[int] = None
        self.update_columns()

    # ---------- 内容 ----------

    def set_values(self, stats: Optional[dict], metric_type: Optional[str],
                   display_unit: str = '') -> None:
        """按统计结果写入六格文本。

        Args:
            stats: analytics.legacy_stats 的结果（覆盖所选范围内全部数据）；None 时全部 "--"
            metric_type: 指标类型
            display_unit: 图表当前固定的显示单位（起点/当前/最小/最大与图表同单位）
        """
        self.set_texts(build_stat_texts(metric_type, stats, display_unit))

    def set_texts(self, texts: dict) -> None:
        """把各格文本写入统计条（完整文本同时放进 tooltip，防止被窄列裁剪）。"""
        for key in STAT_KEYS:
            text = texts.get(key, EMPTY_TEXT)
            label = self.stat_value_labels[key]
            label.setText(text)
            label.setToolTip(text if text != EMPTY_TEXT else "")

    def clear(self) -> None:
        """全部恢复为 "--"。"""
        self.set_values(None, None)

    def texts(self) -> Dict[str, str]:
        """当前各格显示文本（按 STAT_KEYS 顺序）。"""
        return {key: label.text() for key, label in self.stat_value_labels.items()}

    # ---------- 响应式列数 ----------

    @staticmethod
    def column_count(content_width: int) -> int:
        """统计条列数：宽屏一行六格，中等宽度两行三列，窄窗口三行两列。

        Args:
            content_width: 卡片内容区宽度（控件宽度减左右内边距）
        """
        if content_width >= SIX_COLUMNS_MIN_WIDTH:
            return 6
        if content_width >= THREE_COLUMNS_MIN_WIDTH:
            return 3
        return 2

    def columns(self) -> int:
        """当前列数。"""
        return self._columns

    def update_columns(self, content_width: Optional[int] = None) -> bool:
        """按内容宽度重排；列数未变化时不动布局。返回是否发生了重排。"""
        if content_width is None:
            content_width = content_width_of(self)
        columns = self.column_count(content_width)
        if columns == self._columns:
            return False
        self._columns = columns
        for cell in self._cells:
            self.stats_layout.removeWidget(cell)
        for index, cell in enumerate(self._cells):
            self.stats_layout.addWidget(cell, index // columns, index % columns)
        for column in range(len(STAT_KEYS)):
            self.stats_layout.setColumnStretch(column, 1 if column < columns else 0)
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_columns()
