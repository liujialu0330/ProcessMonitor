"""
任务面板

监控页下半部分：配额标题（"监控任务 · n/5 运行中 · k 个已结束"）、空状态、
运行中的任务卡片，以及下方的"已结束"分组。卡片按钮的信号统一由面板转发，
Presenter 只连面板，不必逐张卡片连接。
"""
from datetime import datetime
from typing import Dict, Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, StrongBodyLabel

from core.contracts import StopReason, TaskInfo
from ui.components.task_card import TaskCard
from ui.widgets.empty_state import EmptyStateCard

CARD_SPACING = 12

EMPTY_TITLE = "暂无监控任务"
EMPTY_HINT = "在上方选择被测进程开始记录；每轮操作后点\"打点\"，即可量化每轮的增长"

# 卡片信号名 -> 面板信号名
_FORWARDED = (
    ('stop_requested', 'stop_requested'),
    ('pause_requested', 'pause_requested'),
    ('resume_requested', 'resume_requested'),
    ('marker_requested', 'marker_requested'),
    ('history_requested', 'history_requested'),
    ('restart_requested', 'restart_requested'),
    ('close_requested', 'close_requested'),
)


def _column(parent: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(CARD_SPACING)
    return layout


class TaskBoard(QWidget):
    """任务卡片容器。

    信号（参数均为 task_id）：stop_requested / pause_requested / resume_requested /
    marker_requested / history_requested / restart_requested / close_requested。
    """

    stop_requested = pyqtSignal(str)
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    marker_requested = pyqtSignal(str)
    history_requested = pyqtSignal(str)
    restart_requested = pyqtSignal(str)
    close_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._cards: Dict[str, TaskCard] = {}
        self._running = 0
        self._limit = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(CARD_SPACING)

        self.count_label = StrongBodyLabel(self)
        layout.addWidget(self.count_label)

        self.empty_state = EmptyStateCard(
            FluentIcon.SPEED_HIGH, EMPTY_TITLE, EMPTY_HINT, parent=self)
        layout.addWidget(self.empty_state)

        self._running_box = QWidget(self)
        self._running_layout = _column(self._running_box)
        layout.addWidget(self._running_box)

        self.finished_label = CaptionLabel("已结束", self)
        layout.addWidget(self.finished_label)
        self._finished_box = QWidget(self)
        self._finished_layout = _column(self._finished_box)
        layout.addWidget(self._finished_box)

        self._refresh()

    # ========== 卡片 ==========

    def create_card(self, info: TaskInfo) -> TaskCard:
        """按任务快照建卡片并加入运行中区域。"""
        card = TaskCard(
            task_id=info.task_id,
            process_name=info.process_name,
            pid=info.pid,
            metric_types=list(info.metric_types),
            parent=self,
            interval=info.interval,
            note=info.note,
        )
        self.add_card(card)
        return card

    def add_card(self, card: TaskCard) -> None:
        """加入一张卡片（已结束的卡片放进下方分组），并转发它的按钮信号。"""
        self.remove_card(card.task_id)
        for card_signal, board_signal in _FORWARDED:
            getattr(card, card_signal).connect(getattr(self, board_signal))
        self._cards[card.task_id] = card
        if card.is_finished:
            self._finished_layout.insertWidget(0, card)
        else:
            self._running_layout.addWidget(card)
        self._refresh()

    def remove_card(self, task_id: str) -> None:
        """移除并释放卡片；没有这张卡片时不做任何事。"""
        card = self._cards.pop(task_id, None)
        if card is None:
            return
        self._running_layout.removeWidget(card)
        self._finished_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        self._refresh()

    def card(self, task_id: str) -> Optional[TaskCard]:
        return self._cards.get(task_id)

    def cards(self) -> Dict[str, TaskCard]:
        """全部卡片 {task_id: 卡片}（副本，按加入顺序）。"""
        return dict(self._cards)

    def finish_card(self, task_id: str, reason: StopReason, end_time: datetime) -> None:
        """卡片转为"已结束"并移到下方分组（最近结束的在最上面）。"""
        card = self._cards.get(task_id)
        if card is None:
            return
        card.set_finished(reason, end_time)
        self._running_layout.removeWidget(card)
        self._finished_layout.insertWidget(0, card)
        self._refresh()

    # ========== 计数 ==========

    def set_counts(self, running: int, limit: int) -> None:
        """配额只计仍在运行的任务；已结束未关闭的卡片单独计数，不占配额。"""
        self._running, self._limit = running, limit
        self._refresh()

    def finished_count(self) -> int:
        return sum(1 for card in self._cards.values() if card.is_finished)

    def _refresh(self) -> None:
        finished = self.finished_count()
        text = f"监控任务 · {self._running}/{self._limit} 运行中"
        if finished:
            text += f" · {finished} 个已结束"
        self.count_label.setText(text)
        self.empty_state.setVisible(not self._cards)
        self._running_box.setVisible(len(self._cards) > finished)
        self.finished_label.setVisible(finished > 0)
        self._finished_box.setVisible(finished > 0)
