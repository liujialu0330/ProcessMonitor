"""
采集明细表：单指标的采集账本（时间 / 数值 / 较上次 / 较基线 / 原始值），最新一条在最上面。

宽屏下表格与标题共享一条居中的阅读轨道（最宽 960px），避免时间和值被拉到卡片两端；
窄窗口时轨道填满卡片内容区。原始值列默认隐藏，按需显示。
"""
from typing import Optional, Sequence

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QSizePolicy,
                             QTableWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import (CaptionLabel, CardWidget, StrongBodyLabel,
                            TableWidget, setCustomStyleSheet)

from ui.typography import TypeScale, data_font, ui_font
from ui.widgets.history.common import CARD_PADDING_H, DETAIL_TIME_FORMAT
from utils.metrics import format_metric_delta, format_metric_value

DETAIL_TABLE_MAX_WIDTH = 960
# 五列（含隐藏的原始值列）在 800 宽的最小窗口下也不出现横向滚动
DETAIL_TABLE_MIN_COLUMN_WIDTH = 120
DETAIL_TABLE_HEADER_HEIGHT = 40
DETAIL_TABLE_ROW_HEIGHT = 38
DETAIL_TABLE_MIN_HEIGHT = 340
HEADER_LABELS = ('时间', '数值', '较上次', '较基线', '原始值')
RAW_VALUE_COLUMN = 4
EMPTY_CELL = "--"

# 卡片内边距：上 12，左右 16，下 16；标题行与表格间距 8
CARD_PADDING_TOP = 12
CARD_PADDING_BOTTOM = 16
CONTENT_SPACING = 8

HINT_DEFAULT = ""


def table_hint_text(shown: int, total: Optional[int]) -> str:
    """表格提示："共 12 条（倒序）"；范围内总条数多于已显示条数时追加
    "仅显示最近 2,000 条"（total 为 None 时按已显示条数）。"""
    total = shown if total is None else max(total, shown)
    text = f"共 {total:,} 条（倒序）"
    if total > shown:
        text += f" · 仅显示最近 {shown:,} 条"
    return text


class SampleTable(CardWidget):
    """采集明细卡片：标题 + 提示 + 表格（原始值列默认隐藏）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data_font = data_font(TypeScale.BODY)

        self.data_table = TableWidget(self)
        self.data_table.setFont(self._data_font)
        self.data_table.setColumnCount(len(HEADER_LABELS))
        self.data_table.setHorizontalHeaderLabels(list(HEADER_LABELS))
        header_view = self.data_table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.Stretch)
        header_view.setDefaultAlignment(Qt.AlignCenter)
        header_view.setMinimumSectionSize(DETAIL_TABLE_MIN_COLUMN_WIDTH)
        header_view.setFixedHeight(DETAIL_TABLE_HEADER_HEIGHT)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setEditTriggers(TableWidget.NoEditTriggers)
        self.data_table.setSelectionBehavior(TableWidget.SelectRows)
        self.data_table.setMinimumHeight(DETAIL_TABLE_MIN_HEIGHT)
        self.data_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.data_table.setBorderVisible(True)
        self.data_table.setBorderRadius(8)
        self.data_table.setWordWrap(False)
        self.data_table.setColumnHidden(RAW_VALUE_COLUMN, True)
        self.data_table.verticalHeader().hide()
        self.data_table.verticalHeader().setDefaultSectionSize(DETAIL_TABLE_ROW_HEIGHT)
        # 表头用界面字体的 Caption 半粗：QFluentWidgets 的表头样式表会覆盖 setFont，
        # 所以同时写进自定义 QSS
        header_font = ui_font(TypeScale.CAPTION, QFont.DemiBold)
        header_view.setFont(header_font)
        header_families = ','.join(f"'{family}'" for family in header_font.families())
        header_qss = (
            "QHeaderView::section {"
            f"font-family: {header_families};"
            f"font-size: {TypeScale.CAPTION}px;"
            "font-weight: 600;"
            "}"
        )
        setCustomStyleSheet(self.data_table, header_qss, header_qss)

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(
            CARD_PADDING_H, CARD_PADDING_TOP, CARD_PADDING_H, CARD_PADDING_BOTTOM)
        outer_layout.setSpacing(0)

        # 卡片与趋势视图同宽，标题和表格共享一条居中的阅读轨道
        self.detail_table_content = QWidget(self)
        self.detail_table_content.setMaximumWidth(DETAIL_TABLE_MAX_WIDTH)
        self.detail_table_content.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        content_layout = QVBoxLayout(self.detail_table_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(CONTENT_SPACING)
        title_row = QHBoxLayout()
        title_row.addWidget(StrongBodyLabel("采集明细", self.detail_table_content))
        title_row.addStretch()
        self.table_hint_label = CaptionLabel(HINT_DEFAULT, self.detail_table_content)
        title_row.addWidget(self.table_hint_label)
        content_layout.addLayout(title_row)
        content_layout.addWidget(self.data_table)
        outer_layout.addStretch()
        outer_layout.addWidget(self.detail_table_content, 1)
        outer_layout.addStretch()

    def set_points(self, points: Sequence, metric_type: Optional[str],
                   total_count: Optional[int] = None, display_unit: str = '',
                   baseline_value: Optional[float] = None) -> None:
        """
        填充表格并更新提示。

        Args:
            points: 数据点（需有 timestamp / value；按时间升序，表格倒序显示，最新在前）
            metric_type: 指标类型
            total_count: 所选范围内的数据点总数；None 时按已显示条数
            display_unit: 图表当前固定的显示单位（数值列与图表、统计条同单位）
            baseline_value: 基线值；None 时"较基线"列显示 "--"
        """
        self.table_hint_label.setText(table_hint_text(len(points), total_count))
        count = len(points)

        # 大批量 setItem 前关闭界面更新，避免逐行触发重排/重绘拖慢填表
        self.data_table.setUpdatesEnabled(False)
        self.data_table.setSortingEnabled(False)
        try:
            self.data_table.setRowCount(count)
            for row in range(count):
                index = count - 1 - row
                dp = points[index]
                # 较上次/较基线按变化量自身量级自适应单位，小幅变化不会被大单位吞成 0
                previous = (format_metric_delta(metric_type, dp.value - points[index - 1].value)
                            if index > 0 else EMPTY_CELL)
                vs_baseline = (format_metric_delta(metric_type, dp.value - baseline_value)
                               if baseline_value is not None else EMPTY_CELL)
                texts = (
                    dp.timestamp.strftime(DETAIL_TIME_FORMAT),
                    format_metric_value(metric_type, dp.value, display_unit=display_unit),
                    previous,
                    vs_baseline,
                    f"{dp.value:.4f}",
                )
                for column, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    item.setFont(self._data_font)
                    item.setTextAlignment(Qt.AlignCenter)
                    self.data_table.setItem(row, column, item)
        finally:
            # 表格本就未开启排序（TableWidget 默认不排序），此处只需恢复界面更新
            self.data_table.setUpdatesEnabled(True)

    def clear(self) -> None:
        """清空表格，提示恢复默认文案。"""
        self.data_table.setRowCount(0)
        self.table_hint_label.setText(HINT_DEFAULT)

    def set_raw_values(self, visible: bool) -> None:
        """显示/隐藏原始值列（默认隐藏，只保留时间与格式化值）。"""
        self.data_table.setColumnHidden(RAW_VALUE_COLUMN, not visible)

    def raw_values_visible(self) -> bool:
        return not self.data_table.isColumnHidden(RAW_VALUE_COLUMN)

    def row_texts(self) -> list:
        """当前每行各列文本（含隐藏的原始值列），供测试与导出核对。"""
        table = self.data_table
        return [tuple(table.item(r, c).text() for c in range(table.columnCount()))
                for r in range(table.rowCount())]
