"""
任务对比（卡片，最小可用版本）

选第二个任务后：同一指标两条曲线叠加（各自相对起点的变化量；横轴为相对时间
T+mm:ss，起点为各自的基线或首个点），下方并排两张分轮增量表（各任务的 rounds）。
对比任务没有该指标、或读取失败时显示说明。
"""
from typing import List, Optional, Sequence

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QTableWidgetItem, QVBoxLayout,
                             QWidget)
from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, FluentIcon,
                            StrongBodyLabel, TableWidget, TransparentToolButton)

from ui.viewmodels.analysis_vm import CompareResult, analysis_origin
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.trend_panel import center_line, overlay_reference
from ui.widgets.history.common import CARD_PADDING_H
from ui.widgets.task_selector import TaskSelector
from utils.metrics import (format_metric_delta, get_metric_display_name,
                           get_metric_display_unit)

COMPARE_CHART_MIN_HEIGHT = 260
ROUND_ROW_HEIGHT = 32
NO_ROUNDS = "没有分轮数据（需要至少两个用户打点）"


def compare_series(name: str, snapshot, align_to: float) -> Optional[OverlaySeries]:
    """一个任务的对比曲线：中心线相对起点的变化量，x 平移到 align_to（主任务的原点）。"""
    origin = analysis_origin(snapshot)
    reference = overlay_reference(snapshot)
    line = center_line(snapshot)
    if origin is None or reference is None or not line:
        return None
    shift = align_to - origin.timestamp()
    return OverlaySeries(
        name, [p.timestamp.timestamp() + shift for p in line],
        [p.value - reference for p in line])


def round_rows(metric: str, snapshot) -> List[tuple]:
    """分轮增量表的行：(轮次, 增量文本)。"""
    if snapshot is None:
        return []
    return [(r.label, format_metric_delta(metric, r.delta)) for r in snapshot.rounds]


class _RoundTable(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title_label = BodyLabel(title, self)
        self.table = TableWidget(self)
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["轮次", "增量"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(ROUND_ROW_HEIGHT)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.empty_label = CaptionLabel(NO_ROUNDS, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.table)
        layout.addWidget(self.empty_label)

    def set_rows(self, title: str, rows: Sequence[tuple]) -> None:
        self.title_label.setText(title)
        self.table.setRowCount(len(rows))
        for r, texts in enumerate(rows):
            for c, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignVCenter | (Qt.AlignLeft if c == 0 else Qt.AlignRight))
                self.table.setItem(r, c, item)
        self.table.setFixedHeight(36 + max(1, len(rows)) * ROUND_ROW_HEIGHT + 4)
        self.empty_label.setVisible(not rows)

    def texts(self) -> List[tuple]:
        return [tuple(self.table.item(r, c).text() for c in range(2))
                for r in range(self.table.rowCount())]


class ComparePanel(CardWidget):
    """任务对比面板。"""

    task_selected = pyqtSignal(object)
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selector = TaskSelector(self, placeholder="选择要对比的任务")
        self.selector.task_selected.connect(lambda task_id, _explicit: self.task_selected.emit(task_id))
        self.close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_button.setToolTip("关闭对比")
        self.close_button.clicked.connect(self.close_requested)
        self.message_label = CaptionLabel("", self)
        self.chart = OverlayChart(self)
        self.chart.set_chart_min_height(COMPARE_CHART_MIN_HEIGHT)
        self.primary_rounds = _RoundTable("当前任务", self)
        self.other_rounds = _RoundTable("对比任务", self)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(StrongBodyLabel("任务对比", self))
        head.addWidget(self.selector, 1)
        head.addWidget(self.close_button)
        tables = QHBoxLayout()
        tables.setSpacing(12)
        tables.addWidget(self.primary_rounds, 1)
        tables.addWidget(self.other_rounds, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, 12, CARD_PADDING_H, 16)
        layout.setSpacing(8)
        layout.addLayout(head)
        layout.addWidget(self.message_label)
        layout.addWidget(self.chart)
        layout.addLayout(tables)
        self.set_result(None)

    def set_options(self, options, keep_id: Optional[str] = None) -> None:
        self.selector.set_options(options, keep_id=keep_id)

    def set_result(self, result: Optional[CompareResult]) -> None:
        """显示对比结果；None 清空（等待选择对比任务）。"""
        if result is None:
            self.chart.clear()
            self.primary_rounds.set_rows("当前任务", [])
            self.other_rounds.set_rows("对比任务", [])
            self.message_label.setText("选择第二个任务后，按当前焦点指标叠加对比")
            return
        metric = result.metric
        primary = result.primary
        origin = analysis_origin(primary)
        series = []
        if origin is not None:
            align = origin.timestamp()
            for name, snapshot in (("当前任务", primary), ("对比任务", result.other)):
                item = compare_series(name, snapshot, align) if snapshot is not None else None
                if item is not None:
                    series.append(item)
            self.chart.set_relative_origin(align)
        unit_name = get_metric_display_name(metric) if metric else "值"
        peak = max((abs(y) for item in series for y in item.ys), default=0.0)
        unit = get_metric_display_unit(metric, peak) if metric else ''
        self.chart.set_overlay(
            series, primary.markers,
            axis_title=f"{unit_name} 较起点变化" + (f"（{unit}）" if unit else ""),
            formatter=lambda v, m=metric: format_metric_delta(m, v), display_unit=unit)
        other_name = getattr(result.other_task, 'process_name', '对比任务')
        self.primary_rounds.set_rows("当前任务 · 分轮增量", round_rows(metric, primary))
        self.other_rounds.set_rows(f"{other_name} · 分轮增量", round_rows(metric, result.other))
        self.message_label.setText(
            result.message or f"{unit_name}：两任务各自相对起点（基线或首个点）的变化，横轴为相对时间")
