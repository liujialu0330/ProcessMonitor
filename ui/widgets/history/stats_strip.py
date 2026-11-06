"""
历史统计条：六格（开始值或基线值 / 当前值 / 变化量 / 增长 / 最小 / 最大）。

格位固定、标题随指标类别变化（累计量为平均速率 / 峰值速率 / 数据跨度，峰值类为
峰值抬升），文案由 ui.formatting.build_stat_cells 生成，本控件只负责摆放：
每格是"标题行（标题 + 判定胶囊）/ 主值 / 小字说明"，门槛未过的格置灰。

列数按控件自身的内容宽度计算（宽屏一行六格，中等宽度两行三列，窄窗口三行两列），
只在列数变化时重排，避免拖动窗口时反复拆装布局。
"""
from typing import Dict, List, Optional, Sequence

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout,
                             QWidget)
from qfluentwidgets import CaptionLabel, CardWidget, setCustomStyleSheet

from ui.formatting import (
    STAT_EMPTY, STAT_SLOTS, StatCell, basis_text, build_stat_cells, empty_stat_cells)
from ui.typography import StatValueLabel
from ui.widgets.history.common import (
    CARD_PADDING_H, VIEWPORT_TO_CONTENT_OFFSET, content_width_of)

EMPTY_TEXT = STAT_EMPTY

# 原规则按页面视口：≥1100 六列、≥560 三列；换算成卡片内容宽度
SIX_COLUMNS_MIN_WIDTH = 1100 - VIEWPORT_TO_CONTENT_OFFSET
THREE_COLUMNS_MIN_WIDTH = 560 - VIEWPORT_TO_CONTENT_OFFSET

# 卡片内边距：上下 10，左右 16；格间距横 8 纵 10；格内左右 12、标题与数值间 3
CARD_PADDING_V = 10
CELL_SPACING_H = 8
CELL_SPACING_V = 10
CELL_PADDING_H = 12
CELL_INNER_SPACING = 3

# 置灰：文字降到约 45% 不透明度（浅色/深色主题各一）
DIMMED_LIGHT = QColor(0, 0, 0, 115)
DIMMED_DARK = QColor(255, 255, 255, 115)
DEFAULT_LIGHT = QColor(0, 0, 0)
DEFAULT_DARK = QColor(255, 255, 255)

# 判定胶囊：描边圆角、不着色（设计规范：增长方向只用箭头不着色）
PILL_QSS_LIGHT = ("CaptionLabel{border:1px solid rgba(0,0,0,0.25);border-radius:8px;"
                  "padding:0px 6px;}")
PILL_QSS_DARK = ("CaptionLabel{border:1px solid rgba(255,255,255,0.3);border-radius:8px;"
                 "padding:0px 6px;}")


class _StatCellWidget(QWidget):
    """统计条的一格：标题行（标题 + 胶囊）、主值、小字说明。"""

    def __init__(self, slot: str, parent=None):
        super().__init__(parent)
        self.slot = slot
        self.dimmed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CELL_PADDING_H, 0, CELL_PADDING_H, 0)
        layout.setSpacing(CELL_INNER_SPACING)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        self.caption = CaptionLabel("", self)
        title_row.addWidget(self.caption)
        self.pill = CaptionLabel("", self)
        setCustomStyleSheet(self.pill, PILL_QSS_LIGHT, PILL_QSS_DARK)
        self.pill.setVisible(False)
        title_row.addWidget(self.pill)
        title_row.addStretch()
        layout.addLayout(title_row)

        self.value_label = StatValueLabel(EMPTY_TEXT, self)
        self.value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 长文本（如变化量 + 百分比）宁可被裁剪也不撑宽整行，完整内容见 tooltip
        self.value_label.setMinimumWidth(1)
        self.value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.value_label)

        self.note_label = CaptionLabel("", self)
        self.note_label.setMinimumWidth(1)
        self.note_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # 说明行为空时也占位，保证六格主值基线对齐
        policy = self.note_label.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.note_label.setSizePolicy(policy)
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

    def apply(self, cell: StatCell) -> None:
        self.caption.setText(cell.title)
        self.caption.setToolTip(cell.tooltip)
        self.value_label.setText(cell.value)
        # 完整文本放进 tooltip（窄列会裁剪）；值格 tooltip 另带依据说明
        value_tip = "" if cell.value == EMPTY_TEXT else cell.value
        if value_tip and cell.tooltip:
            value_tip = f"{value_tip}\n{cell.tooltip}"
        self.value_label.setToolTip(value_tip)
        self.note_label.setText(cell.note)
        self.note_label.setToolTip(cell.note)
        self.note_label.setVisible(bool(cell.note))
        self.pill.setText(cell.pill)
        self.pill.setToolTip(cell.pill_tooltip)
        self.pill.setVisible(bool(cell.pill))
        self._set_dimmed(cell.dimmed)

    def _set_dimmed(self, dimmed: bool) -> None:
        if dimmed == self.dimmed:
            return
        self.dimmed = dimmed
        light, dark = (DIMMED_LIGHT, DIMMED_DARK) if dimmed else (DEFAULT_LIGHT, DEFAULT_DARK)
        for label in (self.caption, self.value_label, self.note_label):
            label.setTextColor(light, dark)


class StatsStrip(CardWidget):
    """六格统计条（卡片）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stats_layout = QGridLayout(self)
        self.stats_layout.setContentsMargins(
            CARD_PADDING_H, CARD_PADDING_V, CARD_PADDING_H, CARD_PADDING_V)
        self.stats_layout.setHorizontalSpacing(CELL_SPACING_H)
        self.stats_layout.setVerticalSpacing(CELL_SPACING_V)

        self._cells: Dict[str, _StatCellWidget] = {
            slot: _StatCellWidget(slot, self) for slot in STAT_SLOTS}
        self._current: List[StatCell] = []
        self._basis = ""

        self._columns: Optional[int] = None
        self.update_columns()
        self.set_cells(empty_stat_cells())

    # ---------- 内容 ----------

    def set_snapshot(self, snapshot, display_unit: str = '') -> None:
        """按历史查询快照写入六格。

        Args:
            snapshot: core.history_query.HistorySnapshot；None 时全部 "--"
            display_unit: 图表当前固定的显示单位（开始值/当前值/最小/最大与图表同单位）
        """
        self._basis = basis_text(snapshot) if snapshot is not None else ""
        self.setToolTip(self._basis)
        self.set_cells(build_stat_cells(snapshot, display_unit))

    def set_cells(self, cells: Sequence[StatCell]) -> None:
        """按格位写入（未给出的格位保持不变）。"""
        for cell in cells:
            self._cells[cell.slot].apply(cell)
        by_slot = {cell.slot: cell for cell in self._current}
        by_slot.update((cell.slot, cell) for cell in cells)
        self._current = [by_slot[slot] for slot in STAT_SLOTS if slot in by_slot]

    def clear(self) -> None:
        """全部恢复为默认标题与 "--"。"""
        self._basis = ""
        self.setToolTip("")
        self.set_cells(empty_stat_cells())

    def basis(self) -> str:
        """当前统计依据行（"基线 16:37:33 起 · 2小时15分 · 8,100 点"），无数据为空串。"""
        return self._basis

    def cells(self) -> List[StatCell]:
        """当前六格的完整内容（按格位顺序）。"""
        return list(self._current)

    def texts(self) -> Dict[str, str]:
        """当前各格 标题 → 主值文本（按格位顺序）。"""
        return {w.caption.text(): w.value_label.text() for w in self._cells.values()}

    def notes(self) -> Dict[str, str]:
        """当前各格 标题 → 小字说明（无说明为空串）。"""
        return {w.caption.text(): w.note_label.text() for w in self._cells.values()}

    def pill_text(self) -> str:
        """判定胶囊文本（未显示时为空串）。"""
        return next((w.pill.text() for w in self._cells.values() if w.pill.isVisibleTo(self)),
                    "")

    def dimmed_titles(self) -> List[str]:
        """置灰的格（标题）。"""
        return [w.caption.text() for w in self._cells.values() if w.dimmed]

    def cell_widget(self, slot: str) -> QWidget:
        return self._cells[slot]

    def value_label(self, slot: str):
        return self._cells[slot].value_label

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
        cells = list(self._cells.values())
        for cell in cells:
            self.stats_layout.removeWidget(cell)
        for index, cell in enumerate(cells):
            self.stats_layout.addWidget(cell, index // columns, index % columns)
        for column in range(len(STAT_SLOTS)):
            self.stats_layout.setColumnStretch(column, 1 if column < columns else 0)
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_columns()
