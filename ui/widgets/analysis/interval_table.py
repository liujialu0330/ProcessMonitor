"""
打点区间表（卡片）与下方子视图切换

打点区间表：焦点指标按打点（基线 + 用户打点）切出的各段：区间 / 时长 / 起值 / 终值 /
Δ / 速率 / 峰值 / 与上轮差（文本见 ui.formatting.interval_row_texts）；点击一行发
interval_activated(start, end)，页面据此把趋势图缩放到该区间。

LowerViews：[打点区间（默认）| 采集明细] 分段 + 堆叠，采集明细复用 ui/widgets/history 的 SampleTable。
"""
from typing import List, Optional, Sequence

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QStackedWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, CardWidget, SegmentedWidget, StrongBodyLabel,
                            TableWidget)

from core.report import interval_spans
from ui.formatting import INTERVAL_HEADERS, interval_row_texts
from ui.typography import TypeScale, data_font
from ui.widgets.history.common import CARD_PADDING_H
from ui.widgets.history.sample_table import SampleTable
from utils.metrics import get_metric_display_name

ROW_HEIGHT = 36
TABLE_MIN_HEIGHT = 240
EMPTY_HINT = "没有打点时整段为一个区间；在趋势图上右键可补加打点"

VIEW_INTERVALS = 'intervals'
VIEW_SAMPLES = 'samples'
LOWER_VIEW_OPTIONS = ((VIEW_INTERVALS, '打点区间'), (VIEW_SAMPLES, '采集明细'))


class IntervalTable(CardWidget):
    """打点区间表。"""

    interval_activated = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._intervals: list = []
        self._font = data_font(TypeScale.BODY)
        self.title_label = StrongBodyLabel("打点区间", self)
        self.hint_label = CaptionLabel(EMPTY_HINT, self)
        self.table = TableWidget(self)
        self.table.setColumnCount(len(INTERVAL_HEADERS))
        self.table.setHorizontalHeaderLabels(list(INTERVAL_HEADERS))
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(TableWidget.SelectRows)
        self.table.setSelectionMode(TableWidget.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setMinimumHeight(TABLE_MIN_HEIGHT)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.setToolTip("点击一行把趋势图缩放到该区间")

        title_row = QHBoxLayout()
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        title_row.addWidget(self.hint_label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, 12, CARD_PADDING_H, 16)
        layout.setSpacing(8)
        layout.addLayout(title_row)
        layout.addWidget(self.table)

    def set_intervals(self, metric: Optional[str], intervals: Sequence,
                      markers: Sequence = (), display_unit: Optional[str] = None) -> None:
        """填充焦点指标的区间（intervals 为 IntervalStats 序列）。"""
        self._intervals = list(intervals)
        self.title_label.setText(
            f"打点区间 · {get_metric_display_name(metric)}" if metric else "打点区间")
        user_count = sum(1 for m in markers if getattr(m, 'kind', 'user') in ('user', 'baseline'))
        self.hint_label.setText(
            f"{len(self._intervals)} 段 · 点击行缩放趋势图" if user_count else EMPTY_HINT)
        spans = interval_spans(self._intervals, markers)
        table = self.table
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(self._intervals))
            for r, (item, span) in enumerate(zip(self._intervals, spans)):
                texts = interval_row_texts(metric, item, span, display_unit)
                for c, text in enumerate(texts):
                    cell = QTableWidgetItem(text)
                    cell.setFont(self._font)
                    cell.setTextAlignment(Qt.AlignVCenter | (
                        Qt.AlignLeft if c == 0 else Qt.AlignRight))
                    table.setItem(r, c, cell)
        finally:
            table.setUpdatesEnabled(True)

    def clear(self) -> None:
        self.set_intervals(None, [])

    def row_texts(self) -> List[tuple]:
        table = self.table
        return [tuple(table.item(r, c).text() for c in range(table.columnCount()))
                for r in range(table.rowCount())]

    def interval_at(self, row: int):
        return self._intervals[row] if 0 <= row < len(self._intervals) else None

    def activate_row(self, row: int) -> None:
        """等同用户点击第 row 行。"""
        self._on_cell_clicked(row, 0)

    def _on_cell_clicked(self, row: int, _column: int) -> None:
        item = self.interval_at(row)
        if item is not None:
            self.interval_activated.emit(item.start, item.end)


class LowerViews(QWidget):
    """趋势图下方的子视图：[打点区间 | 采集明细]。"""

    view_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.segmented = SegmentedWidget(self)
        for key, text in LOWER_VIEW_OPTIONS:
            self.segmented.addItem(routeKey=key, text=text)
        self.segmented.setCurrentItem(VIEW_INTERVALS)
        self.segmented.currentItemChanged.connect(self._on_changed)
        self.interval_table = IntervalTable(self)
        self.sample_table = SampleTable(self)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.interval_table)
        self.stack.addWidget(self.sample_table)

        bar = QHBoxLayout()
        bar.addWidget(self.segmented)
        bar.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(bar)
        layout.addWidget(self.stack, 1)

    def current_view(self) -> str:
        return self.segmented.currentRouteKey()

    def set_view(self, key: str) -> None:
        self.segmented.setCurrentItem(key)
        self._on_changed(key)

    def _on_changed(self, key: str) -> None:
        self.stack.setCurrentWidget(
            self.interval_table if key == VIEW_INTERVALS else self.sample_table)
        self.view_changed.emit(key)

    def clear(self) -> None:
        self.interval_table.clear()
        self.sample_table.clear()

