"""
最多四任务对比：按完整操作轮（默认）、相对时间或绝对时间对齐。

曲线累计完整区间表的增量，矩阵每轮均增沿用摘要表口径；差异说明放在悬停与报告中。
无完整轮次的任务保留矩阵行并置灰，提示切到相对时间。图例在绘图区外，长任务名换行。
导出图片固定浅底、宽 1600，完整标签保留在图片元信息和 CSV 中。
"""
import json
import math
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional, Sequence

from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPainterPath
from PyQt5.QtWidgets import (QHBoxLayout, QHeaderView, QTableWidgetItem, QVBoxLayout,
                             QWidget)
from qfluentwidgets import (Action, ComboBox, DropDownPushButton, RoundMenu, BodyLabel, CaptionLabel, CardWidget, FluentIcon,
                            StrongBodyLabel, TableWidget, TransparentToolButton)

from core.comparison import (ALIGN_ROUNDS, ALIGN_RELATIVE, ALIGN_ABSOLUTE,
                             CompareEntry, curve_for, matrix_cell, task_label)
from ui.components.chart_axes import ContextDateAxisItem
from ui.typography import ui_font, data_font, TypeScale
from ui.chart_theme import series_colors
from ui import theme_tokens as tokens
from ui.viewmodels.analysis_vm import CompareResult, analysis_origin
from ui.widgets.analysis.overlay_chart import OverlayChart, OverlaySeries
from ui.widgets.analysis.trend_panel import (
    SeriesLegend, center_line, move_legend_out, overlay_reference)
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
    """最多四任务的对比曲线与指标矩阵；主任务始终包含在对比中。"""

    task_selected = pyqtSignal(object)
    tasks_selected = pyqtSignal(list)
    close_requested = pyqtSignal()
    export_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected_ids = []
        self._result = None
        self._alignment = ALIGN_ROUNDS
        self.selector = TaskSelector(self, placeholder="选择要对比的任务")
        self.selector.hide()
        self.selector.task_selected.connect(lambda task_id, _explicit: self.task_selected.emit(task_id))
        self.selection_button = DropDownPushButton(FluentIcon.ADD, "选择对比任务（0/3）", self)
        self.selection_menu = RoundMenu(parent=self)
        self.selection_button.setMenu(self.selection_menu)
        self._actions = {}
        self.alignment_combo = ComboBox(self)
        for key, text in ((ALIGN_ROUNDS, '按轮对齐'), (ALIGN_RELATIVE, '相对时间'),
                          (ALIGN_ABSOLUTE, '绝对时间')):
            self.alignment_combo.addItem(text, userData=key)
        self.alignment_combo.currentIndexChanged.connect(self._on_alignment)
        self.export_button = DropDownPushButton(FluentIcon.SAVE, '导出对比', self)
        menu = RoundMenu(parent=self)
        for key, text in (('image', '对比图片'), ('csv', '对比 CSV')):
            action = Action(text, self)
            action.triggered.connect(lambda _=False, kind=key: self.export_requested.emit(kind))
            menu.addAction(action)
        self.export_button.setMenu(menu)
        self.close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_button.setToolTip("关闭对比")
        self.close_button.clicked.connect(self.close_requested)
        self.message_label = CaptionLabel("", self)
        self.message_label.setWordWrap(True)
        self.legend = SeriesLegend(self)
        self.legend.setWordWrap(True)
        self.legend.setMinimumWidth(0)
        self.chart = ComparisonChart(self)
        self.chart.set_chart_min_height(COMPARE_CHART_MIN_HEIGHT)
        # 保留旧测试和调用方读取逐轮值的接口；屏幕改用四任务矩阵。
        self.primary_rounds = _RoundTable("当前任务", self)
        self.other_rounds = _RoundTable("对比任务", self)
        self.primary_rounds.hide()
        self.other_rounds.hide()
        self.matrix = TableWidget(self)
        self.matrix.setEditTriggers(TableWidget.NoEditTriggers)
        self.matrix.verticalHeader().hide()
        self.matrix.setBorderVisible(True)
        self.matrix.setBorderRadius(8)
        self.matrix.setWordWrap(True)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(StrongBodyLabel("任务对比", self))
        head.addWidget(self.selection_button, 1)
        head.addWidget(self.alignment_combo)
        head.addWidget(self.export_button)
        head.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING_H, 12, CARD_PADDING_H, 16)
        layout.setSpacing(8)
        layout.addLayout(head)
        layout.addWidget(self.message_label)
        layout.addWidget(self.legend)
        layout.addWidget(self.chart)
        layout.addWidget(self.matrix)
        self.set_result(None)

    @property
    def alignment(self):
        return self._alignment

    def selected_ids(self):
        return tuple(self._selected_ids)

    def set_options(self, options, keep_id: Optional[str] = None, keep_ids=None) -> None:
        self.selector.set_options(options, keep_id=keep_id)
        wanted = list(keep_ids) if keep_ids is not None else ([keep_id] if keep_id else self._selected_ids)
        valid = {o.task_id for o in options}
        self._selected_ids = [t for t in wanted if t in valid][:3]
        self.selection_menu.clear()
        self._actions.clear()
        for option in options:
            action = Action(option.text, self)
            action.setToolTip(option.tooltip)
            action.setCheckable(True)
            action.setChecked(option.task_id in self._selected_ids)
            action.toggled.connect(lambda checked, tid=option.task_id: self._toggle_task(tid, checked))
            self.selection_menu.addAction(action)
            self._actions[option.task_id] = action
        self.selection_button.setEnabled(bool(options))
        self._sync_selection()
        if not options:
            self.message_label.setText('没有其他可对比任务；再完成一次监控后即可对比')

    def _toggle_task(self, task_id, checked):
        if checked and task_id not in self._selected_ids and len(self._selected_ids) < 3:
            self._selected_ids.append(task_id)
        elif not checked and task_id in self._selected_ids:
            self._selected_ids.remove(task_id)
        self._sync_selection()
        self.tasks_selected.emit(list(self._selected_ids))

    def _sync_selection(self):
        for task_id, action in self._actions.items():
            action.blockSignals(True)
            action.setChecked(task_id in self._selected_ids)
            action.setEnabled(task_id in self._selected_ids or len(self._selected_ids) < 3)
            action.blockSignals(False)
        self.selection_button.setText(f'选择对比任务（{len(self._selected_ids)}/3）')

    def _on_alignment(self, index):
        self._alignment = self.alignment_combo.itemData(index)
        self.set_result(self._result)

    def comparison_entries(self):
        result = self._result
        if result is None:
            return ()
        if result.entries:
            return result.entries
        primary_task = SimpleNamespace(task_id=result.primary.request.task_id,
                                       process_name='当前任务', start_time=None)
        return (CompareEntry(primary_task, {result.metric: result.primary}),
                CompareEntry(result.other_task, {result.metric: result.other} if result.other else {},
                             result.message))

    def set_result(self, result: Optional[CompareResult]) -> None:
        self._result = result
        self.export_button.setEnabled(result is not None)
        if result is None:
            self.chart.clear()
            self.legend.set_names([])
            self.matrix.setRowCount(0)
            self.primary_rounds.set_rows("当前任务", [])
            self.other_rounds.set_rows("对比任务", [])
            self.message_label.setText("选择最多三个其他任务，与当前任务一起对比；默认按完整操作轮对齐")
            return
        metric = result.metric
        entries = self.comparison_entries()
        self._selected_ids = [e.task.task_id for e in entries[1:]]
        self._sync_selection()
        curves = [curve_for(e, metric, self._alignment) for e in entries]
        series = [OverlaySeries(c.name, c.xs, c.ys, c.gaps) for c in curves if c.xs]
        peak = max((abs(y) for c in curves for y in c.ys if math.isfinite(y)), default=0.0)
        unit = get_metric_display_unit(metric, peak)
        name = get_metric_display_name(metric)
        self.chart.set_alignment(self._alignment)
        self.chart.set_overlay(series, axis_title=f'{name} 较起点变化（{unit}）',
                               formatter=lambda v: format_metric_delta(metric, v), display_unit=unit)
        move_legend_out(self.chart, self.legend)
        self.primary_rounds.set_rows('当前任务 · 分轮增量', round_rows(metric, result.primary))
        self.other_rounds.set_rows('对比任务 · 分轮增量', round_rows(metric, result.other))
        messages = [f'{c.name}：{c.message}' for c in curves if c.message]
        self.message_label.setText('；'.join(messages) or
            f'{name}：{len(entries)} 个任务；每条曲线相对各自起点，矩阵列出每轮均增与趋势判定')
        metrics = result.metrics or (metric,)
        self.matrix.setColumnCount(len(metrics) + 1)
        self.matrix.setHorizontalHeaderLabels(['任务', *[get_metric_display_name(m) for m in metrics]])
        self.matrix.setRowCount(len(entries))
        header = self.matrix.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.matrix.setColumnWidth(0, 260)
        for column in range(1, len(metrics) + 1):
            self.matrix.setColumnWidth(column, 190)
        for column in range(self.matrix.columnCount()):
            self.matrix.horizontalHeaderItem(column).setTextAlignment(
                Qt.AlignVCenter | (Qt.AlignLeft if column == 0 else Qt.AlignRight))
        for row, entry in enumerate(entries):
            dimmed = self._alignment == ALIGN_ROUNDS and not curves[row].xs
            label = task_label(entry.task)
            item = QTableWidgetItem(label)
            item.setToolTip(label)
            if dimmed:
                item.setForeground(tokens.qcolor(tokens.DIMMED_TEXT))
            self.matrix.setItem(row, 0, item)
            for column, key in enumerate(metrics, 1):
                cell = matrix_cell(entry, key)
                text = matrix_cell_text(cell)
                item = QTableWidgetItem(text)
                item.setToolTip(text + '\n' + MATRIX_BASIS)
                if dimmed:
                    item.setForeground(tokens.qcolor(tokens.DIMMED_TEXT))
                item.setTextAlignment(Qt.AlignVCenter | Qt.AlignRight)
                self.matrix.setItem(row, column, item)
            self.matrix.setRowHeight(row, 56)
        self.matrix.setFixedHeight(42 + 56 * len(entries) + 20)

    def render_image(self):
        return render_comparison_image(self.comparison_entries(),
                                       self._result.metrics or (self._result.metric,),
                                       self._result.metric, self._alignment)


MATRIX_BASIS = ('每轮均增沿用摘要表口径：打点边界参考值的增量均值。'
                '按轮折线累计完整区间表的首末样本差，采样边界可能使两者略有差异。')


VERDICT_TEXT = {'rising': '持续上涨', 'stable': '平稳', 'fluctuating': '波动',
                'insufficient': '数据不足', 'not_judged': '不作趋势判定'}


def matrix_cell_text(cell):
    if cell.message:
        return cell.message
    mean = format_metric_delta(cell.metric, cell.mean_delta) if cell.mean_delta is not None else '—'
    return f'{mean}/轮 · {VERDICT_TEXT.get(cell.verdict, cell.verdict)}'


class ComparisonAxis(ContextDateAxisItem):
    rounds = False

    def tickValues(self, minVal, maxVal, size):
        if self.rounds:
            step = max(1, math.ceil((maxVal - minVal) / max(2, size / 90)))
            return [(step, list(range(max(0, math.ceil(minVal / step) * step), math.floor(maxVal) + 1, step)))]
        return super().tickValues(minVal, maxVal, size)

    def tickStrings(self, values, scale, spacing):
        if self.rounds:
            return ['起点' if value == 0 else f'第 {value:g} 轮' for value in values]
        return super().tickStrings(values, scale, spacing)


class ComparisonChart(OverlayChart):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.time_axis = ComparisonAxis(orientation='bottom')
        self.chart_widget.getPlotItem().setAxisItems({'bottom': self.time_axis})
        self._alignment = ALIGN_ROUNDS
        self.time_axis.setTickFont(data_font(TypeScale.CHART_TICK))
        self.time_axis.label.setFont(ui_font(TypeScale.CAPTION))
        self.time_axis.setStyle(maxTickLevel=0, tickTextOffset=6)
        self.apply_theme()

    def set_alignment(self, alignment):
        self._alignment = alignment
        self.time_axis.rounds = alignment == ALIGN_ROUNDS
        self.set_relative_origin(0.0 if alignment == ALIGN_RELATIVE else None)
        self.time_axis.picture = None
        self.time_axis.update()
        self.chart_widget.setLabel('bottom', {ALIGN_ROUNDS: '完整操作轮', ALIGN_RELATIVE: '相对时间',
                                              ALIGN_ABSOLUTE: '绝对时间'}[alignment])

    def hover_text(self, index):
        if self._alignment != ALIGN_ROUNDS:
            return super().hover_text(index)
        x = self._x[index]
        values = self.values_at(x)
        return ' · '.join([f'第 {x:g} 轮', *[
            f'{s.name} {self._formatter(value) if value is not None and math.isfinite(value) else "—"}'
            for s, value in zip(self._series, values)]])


def render_comparison_image(entries, metrics, focus_metric, alignment):
    """固定浅底报告；矩阵展开成任务×指标行，宽屏与多指标都不截断。"""
    width = 1600
    height = 650 + (len(entries) * (len(metrics) + 1)) * 38
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#ffffff'))
    image.setText('任务信息', json.dumps([{'task_id': entry.task.task_id,
                                        'label': task_label(entry.task)} for entry in entries],
                                      ensure_ascii=False))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor('#202020'))
        painter.setFont(ui_font(TypeScale.PAGE_TITLE))
        painter.drawText(QRectF(40, 28, 1520, 45), Qt.AlignLeft | Qt.AlignVCenter, '任务对比报告')
        painter.setFont(ui_font(TypeScale.BODY))
        painter.drawText(QRectF(40, 78, 1520, 32), Qt.AlignLeft,
                         f'{get_metric_display_name(focus_metric)} · ' +
                         {ALIGN_ROUNDS: '按完整操作轮对齐', ALIGN_RELATIVE: '相对时间对齐',
                          ALIGN_ABSOLUTE: '绝对时间对齐'}[alignment])
        curves = [curve_for(e, focus_metric, alignment) for e in entries]
        palette = series_colors(dark=False)
        for index, curve in enumerate(curves):
            painter.setPen(QColor(palette[index % len(palette)]))
            painter.drawText(QRectF(40, 116 + index * 28, 1520, 28), Qt.AlignLeft,
                             painter.fontMetrics().elidedText(
                                 curve.name + (f'（{curve.message}）' if curve.message else ''),
                                 Qt.ElideMiddle, 1510))
        rect = QRectF(120, 250, 1400, 300)
        valid = [(x, y) for c in curves for x, y in zip(c.xs, c.ys) if math.isfinite(y)]
        painter.setPen(QColor('#b0b0b0'))
        painter.drawRect(rect)
        if valid:
            xmin, xmax = min(x for x, _ in valid), max(x for x, _ in valid)
            ymin, ymax = min(0, min(y for _, y in valid)), max(0, max(y for _, y in valid))
            dx, dy = max(xmax - xmin, 1), max(ymax - ymin, 1)
            painter.setPen(QColor('#303030'))
            painter.drawText(QRectF(8, 242, 106, 35), Qt.AlignRight, format_metric_delta(focus_metric, ymax))
            painter.drawText(QRectF(8, 527, 106, 35), Qt.AlignRight, format_metric_delta(focus_metric, ymin))
            for index, curve in enumerate(curves):
                painter.setPen(QPen(QColor(palette[index % len(palette)]), 2.5))
                path = QPainterPath()
                started = False
                previous = None
                for x, y in zip(curve.xs, curve.ys):
                    if not math.isfinite(y):
                        started, previous = False, None
                        continue
                    if previous is not None and any(previous <= a and x >= b for a, b in curve.gaps):
                        started = False
                    point = QPointF(rect.left() + (x - xmin) / dx * rect.width(),
                                    rect.bottom() - (y - ymin) / dy * rect.height())
                    path.lineTo(point) if started else path.moveTo(point)
                    started, previous = True, x
                painter.drawPath(path)
            painter.setPen(QColor('#303030'))
            painter.drawText(QRectF(120, 560, 1400, 32), Qt.AlignCenter,
                             (f'{datetime.fromtimestamp(xmin):%m-%d %H:%M:%S} — '
                              f'{datetime.fromtimestamp(xmax):%m-%d %H:%M:%S}')
                             if alignment == ALIGN_ABSOLUTE else
                             f'{xmin:g} — {xmax:g}' + (' 轮' if alignment == ALIGN_ROUNDS else ' 秒'))
        painter.setPen(QColor('#505050'))
        painter.drawText(QRectF(40, 592, 1520, 28), Qt.AlignLeft, MATRIX_BASIS)
        y = 630
        for entry in entries:
            painter.setPen(QColor('#202020'))
            painter.drawText(QRectF(40, y, 1520, 36), Qt.AlignLeft,
                             painter.fontMetrics().elidedText(task_label(entry.task), Qt.ElideMiddle, 1510))
            y += 38
            for metric in metrics:
                painter.setPen(QColor('#505050'))
                painter.drawText(QRectF(64, y, 1490, 36), Qt.AlignLeft,
                                 get_metric_display_name(metric) + '：' + matrix_cell_text(matrix_cell(entry, metric)))
                y += 38
    finally:
        painter.end()
    return image
