"""
任务面板

监控页下半部分：空状态三步引导、等待进程启动的占位卡、运行中的任务卡片，以及
下方的"已结束（N）"分组（单行卡片 + [清除全部]）。卡片按钮的信号统一由面板转发，
Presenter 只连面板，不必逐张卡片连接。

配额摘要（"运行中 2/5 · 已结束 1"）由面板算好，经 summary_changed 交给页头显示。
"""
from datetime import datetime
from typing import Dict, Optional

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CheckBox, FluentIcon, StrongBodyLabel, TransparentPushButton

from core.contracts import StopReason, TaskInfo
from ui.components.task_card import TaskCard
from ui.widgets.monitor.empty_guide import EmptyGuideCard
from ui.widgets.monitor.waiting_card import WaitingCard
from ui.widgets.monitor.task_list_row import TaskListRow

CARD_SPACING = 12
# 已结束的单行卡片排得更紧凑
FINISHED_SPACING = 8

# 卡片信号名 -> 面板信号名
_FORWARDED = (
    ('stop_requested', 'stop_requested'),
    ('pause_requested', 'pause_requested'),
    ('resume_requested', 'resume_requested'),
    ('marker_requested', 'marker_requested'),
    ('marker_menu_requested', 'marker_menu_requested'),
    ('history_requested', 'history_requested'),
    ('restart_requested', 'restart_requested'),
    ('close_requested', 'close_requested'),
)


def _column(parent: QWidget, spacing: int = CARD_SPACING) -> QVBoxLayout:
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    return layout


def summary_text(running: int, limit: int, finished: int, waiting: int = 0) -> str:
    """页头摘要："运行中 2/5 · 已结束 1"（有等待中的进程时再加"等待启动 1"）。"""
    parts = [f"运行中 {running}/{limit}"]
    if waiting:
        parts.append(f"等待启动 {waiting}")
    if finished:
        parts.append(f"已结束 {finished}")
    return " · ".join(parts)


class TaskBoard(QWidget):
    """任务卡片容器。

    信号（参数均为 task_id）：stop_requested / pause_requested / resume_requested /
    marker_requested / history_requested / restart_requested / close_requested；
    marker_menu_requested(str, object, object)：迷你图打点刻度右键（任务、打点、全局坐标）；
    clear_finished_requested()：点"清除全部"；
    wait_cancel_requested(str)：占位卡的"取消等待"，参数为等待 ID；
    summary_changed(str)：配额摘要变了。
    """

    stop_requested = pyqtSignal(str)
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    marker_requested = pyqtSignal(str)
    marker_menu_requested = pyqtSignal(str, object, object)
    history_requested = pyqtSignal(str)
    restart_requested = pyqtSignal(str)
    close_requested = pyqtSignal(str)
    clear_finished_requested = pyqtSignal()
    wait_cancel_requested = pyqtSignal(str)
    summary_changed = pyqtSignal(str)
    export_selected_requested = pyqtSignal()
    close_selected_requested = pyqtSignal()

    def __init__(self, parent=None, *, hotkey_text: str = ''):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._cards: Dict[str, TaskCard] = {}
        self._waiting: Dict[str, WaitingCard] = {}
        self._running = 0
        self._limit = 0
        self._summary = ""
        self._view_mode = 'cards'
        self._rows = {}
        self._row_timer = QTimer(self)
        self._row_timer.setInterval(1000)
        self._row_timer.timeout.connect(self._refresh_rows)
        self._row_timer.start()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(CARD_SPACING)

        self.empty_state = EmptyGuideCard(hotkey_text, parent=self)
        layout.addWidget(self.empty_state)

        self._waiting_box = QWidget(self)
        self._waiting_layout = _column(self._waiting_box)
        layout.addWidget(self._waiting_box)

        self._running_box = QWidget(self)
        self._running_layout = _column(self._running_box)
        layout.addWidget(self._running_box)

        # 已结束（N） ........ [清除全部]
        self.finished_header = QWidget(self)
        header_layout = QHBoxLayout(self.finished_header)
        header_layout.setContentsMargins(0, 8, 0, 0)
        header_layout.setSpacing(8)
        self.finished_label = StrongBodyLabel("已结束", self.finished_header)
        header_layout.addWidget(self.finished_label)
        self.select_all_finished = CheckBox('全选', self.finished_header)
        self.select_all_finished.toggled.connect(self._select_finished)
        header_layout.addWidget(self.select_all_finished)
        header_layout.addStretch(1)
        self.export_selected_button = TransparentPushButton('导出选中', self.finished_header)
        self.export_selected_button.clicked.connect(self.export_selected_requested)
        self.close_selected_button = TransparentPushButton('关闭选中', self.finished_header)
        self.close_selected_button.clicked.connect(self.close_selected_requested)
        header_layout.addWidget(self.export_selected_button)
        header_layout.addWidget(self.close_selected_button)
        self.clear_finished_button = TransparentPushButton(
            "清除全部", self.finished_header, FluentIcon.BROOM)
        self.clear_finished_button.setToolTip("从列表移除全部已结束的卡片（数据仍保留，可在分析页查看）")
        self.clear_finished_button.clicked.connect(self.clear_finished_requested)
        header_layout.addWidget(self.clear_finished_button)
        layout.addWidget(self.finished_header)

        self._finished_box = QWidget(self)
        self._finished_layout = _column(self._finished_box, FINISHED_SPACING)
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
            target_kind=info.target_kind,
            members_truncated=info.members_truncated,
        )
        if getattr(info, 'waiting_restart', False):
            card.set_waiting_restart(True, info.restart_count, info.restart_limit, info.exit_code,
                                     paused=info.is_paused)
        self.add_card(card)
        return card

    def add_card(self, card: TaskCard, *, reconnect=True) -> None:
        """加入一张卡片（已结束的卡片放进下方分组），并转发它的按钮信号。"""
        self.remove_card(card.task_id)
        if reconnect:
            for card_signal, board_signal in _FORWARDED:
                getattr(card, card_signal).connect(getattr(self, board_signal))
        self._cards[card.task_id] = card
        if card.is_finished:
            self._finished_layout.insertWidget(0, card)
        else:
            row = TaskListRow(card, self)
            row.expanded_changed.connect(lambda _expanded: self._refresh())
            self._rows[card.task_id] = row
            self._running_layout.addWidget(row)
            self._running_layout.addWidget(card)
        self._refresh()

    def remove_card(self, task_id: str) -> None:
        """移除并释放卡片；没有这张卡片时不做任何事。"""
        card = self._cards.pop(task_id, None)
        if card is None:
            return
        row = self._rows.pop(task_id, None)
        if row is not None:
            self._running_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._running_layout.removeWidget(card)
        self._finished_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        self._refresh()

    def take_card(self, task_id):
        """暂时移出面板但保留控件，用于可撤销的“清除全部”。"""
        card = self._cards.pop(task_id, None)
        if card is not None:
            self._running_layout.removeWidget(card)
            self._finished_layout.removeWidget(card)
            card.hide()
        row = self._rows.pop(task_id, None)
        if row is not None:
            self._running_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._refresh()
        return card

    def _select_finished(self, selected):
        for card in self._cards.values():
            if card.is_finished:
                card.selection_checkbox.setChecked(selected)

    def selected_finished_ids(self):
        return [tid for tid, card in self._cards.items()
                if card.is_finished and card.selection_checkbox.isChecked()]

    def card(self, task_id: str) -> Optional[TaskCard]:
        return self._cards.get(task_id)

    def cards(self) -> Dict[str, TaskCard]:
        """全部卡片 {task_id: 卡片}（副本，按加入顺序）。"""
        return dict(self._cards)

    def finished_ids(self):
        """已结束卡片的任务 ID（按加入顺序）。"""
        return [task_id for task_id, card in self._cards.items() if card.is_finished]

    def finish_card(self, task_id: str, reason: StopReason, end_time: datetime,
                    exit_code: Optional[int] = None) -> None:
        """卡片转为"已结束"并移到下方分组（最近结束的在最上面）；exit_code 是被测进程最近一次
        退出的退出码（异常退出的卡片据此写"访问冲突 0xC0000005"）。"""
        card = self._cards.get(task_id)
        if card is None:
            return
        if exit_code is None:
            card.set_finished(reason, end_time)
        else:
            card.set_finished(reason, end_time, exit_code)
        self._running_layout.removeWidget(card)
        self._finished_layout.insertWidget(0, card)
        self._refresh()

    # ========== 等待进程启动的占位卡 ==========

    def add_waiting(self, wait_id: str, process_name: str, detail: str = '') -> WaitingCard:
        """加入一张"等待 xxx.exe 启动…"占位卡。"""
        self.remove_waiting(wait_id)
        card = WaitingCard(wait_id, process_name, detail, parent=self)
        card.cancel_requested.connect(self.wait_cancel_requested)
        self._waiting[wait_id] = card
        self._waiting_layout.addWidget(card)
        self._refresh()
        return card

    def remove_waiting(self, wait_id: str) -> None:
        card = self._waiting.pop(wait_id, None)
        if card is None:
            return
        self._waiting_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
        self._refresh()

    def waiting_card(self, wait_id: str) -> Optional[WaitingCard]:
        return self._waiting.get(wait_id)

    def waiting_ids(self):
        return list(self._waiting)

    # ========== 计数 ==========

    def set_counts(self, running: int, limit: int) -> None:
        """配额只计仍在运行的任务；已结束未关闭的卡片单独计数，不占配额。"""
        self._running, self._limit = running, limit
        self._refresh()

    def finished_count(self) -> int:
        return sum(1 for card in self._cards.values() if card.is_finished)

    def summary(self) -> str:
        """当前的配额摘要文案（与 summary_changed 发出的一致）。"""
        return self._summary

    def set_view_mode(self, mode):
        if mode not in ('cards', 'list'):
            return
        self._view_mode = mode
        self._refresh()

    def _refresh_rows(self):
        if self._view_mode == 'list':
            for row in self._rows.values():
                if not row.card.is_finished:
                    row.refresh()

    def rising_count(self):
        return sum(card.live_verdict() == '持续上涨' for card in self._cards.values()
                   if not card.is_finished)

    def set_hotkey_text(self, hotkey_text: str) -> None:
        """空状态引导里提到的打点快捷键（为空表示不可用）。"""
        self.empty_state.set_hotkey_text(hotkey_text)

    def _refresh(self) -> None:
        finished = self.finished_count()
        running_cards = len(self._cards) - finished
        for task_id, card in self._cards.items():
            card.selection_checkbox.setVisible(card.is_finished)
            row = self._rows.get(task_id)
            if row is not None:
                listed = self._view_mode == 'list' and not card.is_finished
                row.setVisible(listed)
                card.setVisible(not listed or row.expanded)
        self.empty_state.setVisible(not self._cards and not self._waiting)
        self._waiting_box.setVisible(bool(self._waiting))
        self._running_box.setVisible(running_cards > 0)
        self.finished_label.setText(f"已结束（{finished}）")
        self.finished_header.setVisible(finished > 0)
        self._finished_box.setVisible(finished > 0)

        summary = summary_text(self._running, self._limit, finished, len(self._waiting))
        if summary != self._summary:
            self._summary = summary
            self.summary_changed.emit(summary)
