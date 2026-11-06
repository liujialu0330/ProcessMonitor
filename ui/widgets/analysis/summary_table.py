"""
判定摘要表（卡片）：每个指标一行，回答"哪些指标在涨、涨多快、依据是什么"。

列：指标 / 基线 / 当前 / 较基线(Δ, %) / 每轮均增 / 增速±CI · R² / 末段趋势 / 判定 / 上限占比；
文本与 tooltip 由 ui.formatting.build_summary_row 生成。表格上方的依据行如
"基线 16:37:33 起 · 2小时15分 · 8,100 点"；样本不足的行整行置灰。
点击某行把该指标设为焦点（明细表、区间表与概览条跟随）。
"""
from typing import List, Optional, Sequence

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QTableWidgetItem,
                             QVBoxLayout)
from qfluentwidgets import (CaptionLabel, CardWidget, StrongBodyLabel, TableWidget,
                            isDarkTheme)

from ui.formatting import SUMMARY_COLUMNS, SUMMARY_HEADERS, SummaryRow
from ui.typography import TypeScale, data_font
from ui.widgets.history.common import CARD_PADDING_H

ROW_HEIGHT = 36
HEADER_HEIGHT = 36
VERDICT_COLUMN = SUMMARY_COLUMNS.index("verdict")
# 置灰行文字色（浅色 / 深色主题）
DIMMED_LIGHT = QColor(0, 0, 0, 100)
DIMMED_DARK = QColor(255, 255, 255, 100)
# 判定胶囊配色：(浅色背景, 深色背景)；其余判定用中性色
PILL_COLORS = {
    "持续上涨": ("rgba(196, 43, 28, 0.14)", "rgba(255, 153, 164, 0.22)"),
    "平稳": ("rgba(15, 123, 15, 0.14)", "rgba(108, 203, 95, 0.22)"),
    "波动": ("rgba(157, 93, 0, 0.14)", "rgba(252, 225, 0, 0.20)"),
}
NEUTRAL_PILL = ("rgba(0, 0, 0, 0.06)", "rgba(255, 255, 255, 0.10)")


def _pill_style(text: str) -> str:
    light, dark = PILL_COLORS.get(text, NEUTRAL_PILL)
    return ("QLabel{border-radius: 10px; padding: 2px 10px; font-size: 12px;"
            f"background: {dark if isDarkTheme() else light};}}")


class SummaryTable(CardWidget):
    """判定摘要表。"""

    metric_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[SummaryRow] = []
        self._font = data_font(TypeScale.BODY)

        self.basis_label = CaptionLabel("", self)
        self.table = TableWidget(self)
        self.table.setColumnCount(len(SUMMARY_COLUMNS))
        self.table.setHorizontalHeaderLabels([SUMMARY_HEADERS[c] for c in SUMMARY_COLUMNS])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        header.setFixedHeight(HEADER_HEIGHT)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(TableWidget.SelectRows)
        self.table.setSelectionMode(TableWidget.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.cellClicked.connect(self._on_cell_clicked)

        title_row = QHBoxLayout()
        title_row.addWidget(StrongBodyLabel("判定摘要", self))
        title_row.addStretch()
        title_row.addWidget(self.basis_label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, 12, CARD_PADDING_H, 16)
        layout.setSpacing(8)
        layout.addLayout(title_row)
        layout.addWidget(self.table)
        self._fit_height()

    # ---------- 数据 ----------

    def set_rows(self, rows: Sequence[SummaryRow], basis: str = "",
                 focus: Optional[str] = None) -> None:
        """整体替换各行与依据行；focus 为焦点指标（选中该行，不发信号）。"""
        self._rows = list(rows)
        self.basis_label.setText(f"依据：{basis}" if basis else "")
        table = self.table
        table.setUpdatesEnabled(False)
        try:
            table.clearContents()
            table.setRowCount(len(self._rows))
            dimmed_color = DIMMED_DARK if isDarkTheme() else DIMMED_LIGHT
            for r, row in enumerate(self._rows):
                for c, column in enumerate(SUMMARY_COLUMNS):
                    # 判定列由胶囊控件显示，单元格本身不再画字（否则两层文字叠在一起）
                    item = QTableWidgetItem("" if c == VERDICT_COLUMN else row.text(column))
                    item.setFont(self._font)
                    item.setToolTip(row.tooltips.get(column, ""))
                    item.setTextAlignment(Qt.AlignVCenter | (
                        Qt.AlignLeft if c == 0 else Qt.AlignRight))
                    if row.dimmed:
                        item.setForeground(dimmed_color)
                    table.setItem(r, c, item)
                pill = QLabel(row.text("verdict"))
                pill.setAlignment(Qt.AlignCenter)
                pill.setToolTip(row.tooltips.get("verdict", ""))
                pill.setStyleSheet(_pill_style(row.text("verdict")))
                pill.setEnabled(not row.dimmed)
                table.setCellWidget(r, VERDICT_COLUMN, pill)
        finally:
            table.setUpdatesEnabled(True)
        self._fit_height()
        self.select_metric(focus)

    def clear(self) -> None:
        self.set_rows([], "")

    def select_metric(self, metric: Optional[str]) -> None:
        """选中焦点指标所在行（不发 metric_selected）；不在表中时清除选中。"""
        row = self.row_of(metric)
        self.table.blockSignals(True)
        try:
            if row < 0:
                self.table.clearSelection()
            else:
                self.table.selectRow(row)
        finally:
            self.table.blockSignals(False)

    def _fit_height(self) -> None:
        # 表格不内部滚动：高度随行数，页面整体滚动
        rows = max(1, self.table.rowCount())
        self.table.setFixedHeight(HEADER_HEIGHT + rows * ROW_HEIGHT + 4)

    def _on_cell_clicked(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._rows):
            self.metric_selected.emit(self._rows[row].metric)

    # ---------- 查询（测试与报告用） ----------

    def row_of(self, metric: Optional[str]) -> int:
        return next((i for i, row in enumerate(self._rows) if row.metric == metric), -1)

    def rows(self) -> List[SummaryRow]:
        return list(self._rows)

    def basis(self) -> str:
        return self.basis_label.text()

    def row_texts(self) -> List[tuple]:
        return [tuple(row.text(c) for c in SUMMARY_COLUMNS) for row in self._rows]

    def dimmed_metrics(self) -> List[str]:
        return [row.metric for row in self._rows if row.dimmed]

    def selected_metric(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        index = rows[0].row()
        return self._rows[index].metric if 0 <= index < len(self._rows) else None
