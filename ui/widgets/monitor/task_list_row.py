"""运行任务的紧凑列表行；展开时复用原任务卡片，不复制监控状态。"""
from PyQt5.QtCore import Qt, QPointF, pyqtSignal
from PyQt5.QtGui import QPainter, QPolygonF
from PyQt5.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import (Action, CaptionLabel, CardWidget, FluentIcon, PrimaryPushButton,
                            RoundMenu, TransparentToolButton, setCustomStyleSheet)
from ui import theme_tokens as tokens
from ui.components.sparkline import LINE_COLOR
from ui.theme_tokens import qcolor
from ui.components.task_card import format_clock
from utils.metrics import format_metric_value


class _Preview(QWidget):
    def __init__(self, card, parent):
        super().__init__(parent)
        self.card = card
        self.setFixedSize(88, 28)

    def paintEvent(self, event):
        values = self.card.sparkline.series()
        if len(values) < 2:
            return
        low, high = min(values), max(values)
        span = high - low
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(qcolor(LINE_COLOR))
        painter.drawPolyline(QPolygonF([
            QPointF(i * (self.width() - 2) / (len(values) - 1) + 1,
                    14 if not span else 26 - (v - low) / span * 24)
            for i, v in enumerate(values)]))


class TaskListRow(CardWidget):
    expanded_changed = pyqtSignal(bool)

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self.card, self.expanded = card, False
        self.setFixedHeight(48)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        self.expand = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self)
        self.expand.setToolTip('展开任务详情')
        self.expand.clicked.connect(self.toggle)
        self.name = CaptionLabel(card.process_name, self)
        self.name.setMinimumWidth(30)
        self.name.setMaximumWidth(130)
        self.name.setToolTip(f'{card.process_name}\nPID {card.pid}\n{card.note}')
        self.status = CaptionLabel('', self)
        self.status.setContentsMargins(5, 2, 5, 2)
        self._status_key = None
        self.elapsed = CaptionLabel('', self)
        self.value = CaptionLabel('', self)
        self.value.setMinimumWidth(30)
        self.preview = _Preview(card, self)
        self.verdict = CaptionLabel('', self)
        self.marker = PrimaryPushButton('打点', self)
        self.marker.setToolTip('记录本轮操作（Ctrl+M 为全部任务打点）')
        self.marker.clicked.connect(lambda: card.marker_requested.emit(card.task_id))
        self.analysis = TransparentToolButton(FluentIcon.MARKET, self)
        self.analysis.setToolTip('查看分析')
        self.analysis.clicked.connect(lambda: card.history_requested.emit(card.task_id))
        self.more = TransparentToolButton(FluentIcon.MORE, self)
        self.more.setToolTip('暂停或停止')
        self.more.clicked.connect(self._menu)
        for widget in (self.expand, self.name, self.status, self.elapsed, self.value,
                       self.preview, self.verdict, self.marker, self.analysis, self.more):
            layout.addWidget(widget, 1 if widget is self.value else 0)
        self.refresh()

    def toggle(self):
        self.expanded = not self.expanded
        self.expand.setIcon(FluentIcon.UP if self.expanded else FluentIcon.CHEVRON_DOWN_MED)
        self.expanded_changed.emit(self.expanded)

    def _menu(self):
        menu = RoundMenu(parent=self)
        action = Action('恢复' if self.card.is_paused else '暂停', menu)
        action.triggered.connect(self.card._on_pause_button_clicked)
        menu.addAction(action)
        stop = Action('停止监控', menu)
        stop.triggered.connect(lambda: self.card.stop_requested.emit(self.card.task_id))
        menu.addAction(stop)
        menu.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))
        menu.deleteLater()

    def refresh(self):
        card = self.card
        width = self.name.maximumWidth()
        process = self.name.fontMetrics().elidedText(card.process_name, Qt.ElideRight, width)
        note = self.name.fontMetrics().elidedText(card.note, Qt.ElideRight, width)
        self.name.setText(process + ('\n' + note if note else ''))
        self.status.setText(card.status_label.text().replace('● ', ''))
        self.status.setTextColor(card.status_label.lightColor, card.status_label.darkColor)
        if card.status != self._status_key:
            self._status_key = card.status
            tone = 'caution' if card.is_paused or card.waiting_restart else 'success'
            setCustomStyleSheet(self.status,
                f'CaptionLabel {{ background: {tokens.pill_background(tone, False)}; border-radius: 9px; }}',
                f'CaptionLabel {{ background: {tokens.pill_background(tone, True)}; border-radius: 9px; }}')
        self.elapsed.setText(format_clock(card.running_seconds() or 0))
        metric = card.trend_metric
        text = (format_metric_value(metric, card.last_values[metric])
                if metric in card.last_values else '—')
        self.value.setText(f'{text}  {card.delta_text(metric, compact=True) if metric else ""}')
        self.value.setToolTip(card.sparkline_label.text() + '\n' + self.value.text())
        self.verdict.setText(card.live_verdict())
        self.marker.setEnabled(not card.is_paused and not card.waiting_restart)
        self.preview.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.preview.setVisible(event.size().width() >= 820)
        self.elapsed.setVisible(event.size().width() >= 720)
