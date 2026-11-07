"""
判定摘要表（卡片）：每个指标一行，回答"哪些指标在涨、涨多快、依据是什么"。

列：指标 / 基线 / 当前 / 较基线(Δ, %) / 每轮均增 / 增速±CI · R² / 末段趋势 / 判定 / 上限占比
（没有带上限的指标时整列隐藏）；
文本与 tooltip 由 ui.formatting 生成（分析页用 build_table_row：平稳行增速写"—"、上限占比
只写百分比）；行序由调用方决定（分析页按判定严重程度排好再传入）。表格上方的标题与依据行如
"基线 16:37:33 起 · 2小时15分 · 8,100 点"（分析页把依据并入结论条，不显示这一行）；
样本不足的行整行置灰，判定胶囊后加 ⓘ（tooltip 写原因）。
点击某行把该指标设为焦点（明细表、区间表与概览条跟随）。

列宽：任何列都不截断。各列按内容定宽，"指标"列另有内容下限；表宽有富余时多出的宽度
给"指标"列，不够时"指标"列取下限、表格出现横向滚动条（并为滚动条让出高度）。
"""
from typing import List, Optional, Sequence

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QTableWidgetItem,
                             QVBoxLayout)
from qfluentwidgets import (CaptionLabel, CardWidget, StrongBodyLabel, TableWidget,
                            qconfig, setCustomStyleSheet)

from ui.formatting import INFO_MARK, SUMMARY_COLUMNS, SUMMARY_HEADERS, SummaryRow
from ui import theme_tokens as tokens
from ui.typography import TypeScale, data_font
from ui.widgets.history.common import CARD_PADDING_H

ROW_HEIGHT = 36
# 单元格左右内边距（库默认 16 px）
CELL_PADDING_H = 6
HEADER_HEIGHT = 36
VERDICT_COLUMN = SUMMARY_COLUMNS.index("verdict")
USAGE_COLUMN = SUMMARY_COLUMNS.index("usage")
# "指标"列内容宽度之外的留白（左右内边距），算下限宽度时加上
METRIC_COLUMN_PADDING = 16
# qfluentwidgets 表格不用原生横向滚动条，而是在表格底边上浮一条 12px 高、距底 1px 的
# 滚动条；它出现时表格多留这么高，免得盖住最后一行
H_SCROLLBAR_SPACE = 14
# 置灰行文字色与判定胶囊配色统一在 ui.theme_tokens（DIMMED_TEXT、PILL_*、VERDICT_TONES），
# 与任务卡片状态胶囊同源；胶囊文字色显式写进样式而不用 setEnabled 置灰——禁用态 QLabel
# 走调色板的 disabled 文字色，在深色主题的半透明胶囊底上几乎看不清


def pill_style(text: str, dimmed: bool = False) -> str:
    """判定胶囊样式：底色按判定的语气取，文字色按主题与是否置灰取（结论条、折叠行共用）。"""
    background = tokens.pill_background(tokens.verdict_tone(text))
    color = tokens.pick(tokens.PILL_TEXT_DIMMED if dimmed else tokens.PILL_TEXT)
    return ("QLabel{border-radius: 10px; padding: 2px 10px; font-size: 12px;"
            f"background: {background}; color: {color};}}")


class SummaryTable(CardWidget):
    """判定摘要表。"""

    metric_selected = pyqtSignal(str)

    def __init__(self, parent=None, *, show_header: bool = True):
        """show_header=False 时不显示"判定摘要"标题与依据行（分析页由结论条承担），
        省出一行高度；依据文本仍记在 basis() 里。"""
        super().__init__(parent)
        self._rows: List[SummaryRow] = []
        self._font = data_font(TypeScale.BODY)

        self.basis_label = CaptionLabel("", self)
        self.table = TableWidget(self)
        self.table.setColumnCount(len(SUMMARY_COLUMNS))
        self.table.setHorizontalHeaderLabels([SUMMARY_HEADERS[c] for c in SUMMARY_COLUMNS])
        header = self.table.horizontalHeader()
        # 不拉伸最后一列（上限占比）：其它列撑满后它会被压窄而截断。
        # 第 0 列的模式由 _apply_column_policy 按可用宽度在 Stretch / Interactive 间切换
        header.setStretchLastSection(False)
        for column in range(len(SUMMARY_COLUMNS)):
            header.setSectionResizeMode(
                column, QHeaderView.Stretch if column == 0 else QHeaderView.ResizeToContents)
        header.setFixedHeight(HEADER_HEIGHT)
        # 当前全部列能否在视口内放下（放不下时出现横向滚动条）
        self._columns_fit = True
        # 卡片 resizeEvent 时表格视口未必已按新尺寸布局（首次显示时尤其如此），
        # 视口自身尺寸变化时再判断一次才可靠
        self.table.viewport().installEventFilter(self)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(TableWidget.SelectRows)
        self.table.setSelectionMode(TableWidget.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 库的 qss 给单元格左右各 16 px 内边距，9 列下来多占近 300 px；
        # 用 setCustomStyleSheet 追加（随主题切换保留），收紧到 CELL_PADDING_H
        cell_qss = (f"QTableView::item{{padding-left: {CELL_PADDING_H}px;"
                    f"padding-right: {CELL_PADDING_H}px;}}")
        setCustomStyleSheet(self.table, cell_qss, cell_qss)
        self.table.cellClicked.connect(self._on_cell_clicked)

        self.title_label = StrongBodyLabel("判定摘要", self)
        title_row = QHBoxLayout()
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        title_row.addWidget(self.basis_label)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        if show_header:
            layout.setContentsMargins(CARD_PADDING_H, 12, CARD_PADDING_H, 16)
            layout.addLayout(title_row)
        else:
            layout.setContentsMargins(CARD_PADDING_H, 12, CARD_PADDING_H, 12)
            self.title_label.setVisible(False)
            self.basis_label.setVisible(False)
        layout.addWidget(self.table)
        self._fit_height()
        qconfig.themeChanged.connect(self._on_theme_changed)

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
            for r, row in enumerate(self._rows):
                for c, column in enumerate(SUMMARY_COLUMNS):
                    # 判定列由胶囊控件显示，单元格本身不再画字（否则两层文字叠在一起）
                    item = QTableWidgetItem("" if c == VERDICT_COLUMN else row.text(column))
                    item.setFont(self._font)
                    item.setToolTip(row.tooltips.get(column, ""))
                    item.setTextAlignment(Qt.AlignVCenter | (
                        Qt.AlignLeft if c == 0 else Qt.AlignRight))
                    table.setItem(r, c, item)
                # 样本不足：胶囊里判定文字后跟 ⓘ，悬停说明还差什么
                verdict = row.text("verdict")
                pill = QLabel(f"{verdict} {INFO_MARK}" if row.note else verdict)
                pill.setAlignment(Qt.AlignCenter)
                pill.setToolTip("\n".join(
                    t for t in (row.note, row.tooltips.get("verdict", "")) if t))
                table.setCellWidget(r, VERDICT_COLUMN, pill)
            self._apply_theme_colors()
            # 没有任何指标有上限（未选 GDI/USER 对象）时整列隐藏，把宽度留给其它列
            table.setColumnHidden(
                USAGE_COLUMN, not any(row.texts.get("usage") for row in self._rows))
        finally:
            table.setUpdatesEnabled(True)
        self._fit_height()
        self.select_metric(focus)
        self._apply_column_policy()

    def _apply_theme_colors(self) -> None:
        """按当前主题给置灰行文字与判定胶囊上色（建表与主题切换共用）。
        只改已有单元格与胶囊的颜色，不重建行，选中行与键盘焦点保持不变。"""
        dimmed_color = tokens.qcolor(tokens.DIMMED_TEXT)
        table = self.table
        for r, row in enumerate(self._rows):
            if row.dimmed:
                for c in range(len(SUMMARY_COLUMNS)):
                    item = table.item(r, c)
                    if item is not None:
                        item.setForeground(dimmed_color)
            pill = table.cellWidget(r, VERDICT_COLUMN)
            if pill is not None:
                pill.setStyleSheet(pill_style(row.text("verdict"), row.dimmed))

    def _on_theme_changed(self, _theme) -> None:
        # 无父对象的表格可能 C++ 端已删除而 Python 对象还在：遇到就退订，不再跟随主题
        try:
            self._apply_theme_colors()
        except RuntimeError:
            try:
                qconfig.themeChanged.disconnect(self._on_theme_changed)
            except TypeError:
                pass

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
        # 表格不纵向滚动：高度随行数，页面整体滚动；有横向滚动条时为它让出高度
        rows = max(1, self.table.rowCount())
        extra = 0 if self._columns_fit else H_SCROLLBAR_SPACE
        self.table.setFixedHeight(HEADER_HEIGHT + rows * ROW_HEIGHT + 4 + extra)

    # ---------- 列宽 ----------

    def metric_column_floor(self) -> int:
        """第 0 列"指标"的下限宽度：最宽单元格内容加留白，且不小于表头文字。"""
        header = self.table.horizontalHeader()
        return max(self.table.sizeHintForColumn(0) + METRIC_COLUMN_PADDING,
                   header.sectionSizeHint(0), header.minimumSectionSize())

    def contents_width(self) -> int:
        """全部可见列按内容排开（第 0 列按下限）所需的总宽度。"""
        header = self.table.horizontalHeader()
        others = sum(max(self.table.sizeHintForColumn(c), header.sectionSizeHint(c),
                         header.minimumSectionSize())
                     for c in range(1, len(SUMMARY_COLUMNS))
                     if not self.table.isColumnHidden(c))
        return self.metric_column_floor() + others

    def _apply_column_policy(self) -> None:
        """放得下时"指标"列拉伸吃掉富余宽度；放不下时取下限宽度、让表格横向滚动，
        而不是把任何一列压扁截断。视口尚未布局（宽度为 0）时按放得下处理。"""
        header = self.table.horizontalHeader()
        available = self.table.viewport().width()
        fits = available <= 0 or available >= self.contents_width()
        if fits:
            if header.sectionResizeMode(0) != QHeaderView.Stretch:
                header.setSectionResizeMode(0, QHeaderView.Stretch)
        else:
            if header.sectionResizeMode(0) != QHeaderView.Interactive:
                header.setSectionResizeMode(0, QHeaderView.Interactive)
            header.resizeSection(0, self.metric_column_floor())
        if fits != self._columns_fit:
            self._columns_fit = fits
            self._fit_height()

    def columns_fit(self) -> bool:
        """当前全部列能否在视口内放下（False 时表格横向滚动）。"""
        return self._columns_fit

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_column_policy()

    def eventFilter(self, obj, event):
        # 基类构造期间就可能有事件经过这里，那时表格还没建好
        table = getattr(self, "table", None)
        if table is not None and obj is table.viewport() and event.type() == QEvent.Resize:
            self._apply_column_policy()
        return super().eventFilter(obj, event)

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

    def metrics(self) -> List[str]:
        """各行的指标（显示顺序）。"""
        return [row.metric for row in self._rows]

    def pill_text(self, row: int) -> str:
        pill = self.table.cellWidget(row, VERDICT_COLUMN)
        return pill.text() if pill is not None else ""

    def selected_metric(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        index = rows[0].row()
        return self._rows[index].metric if 0 <= index < len(self._rows) else None
