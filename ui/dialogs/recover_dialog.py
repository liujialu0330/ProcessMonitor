"""
"继续监控"对话框

上次运行被强杀、崩溃或断电，而这些任务的被测进程仍在运行：列出它们，让用户勾选要接着采集
的（默认全选）。选中的任务沿用原任务 ID 继续记录，中断期间是数据空洞；不继续的任务按"上次
异常结束"照常收尾（结束时间取最后一次采样）。

对话框只收集选择；执行续采在 core.session_recovery.apply_decision。
"""
from datetime import datetime
from typing import List, Optional, Sequence

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QListWidgetItem
from qfluentwidgets import BodyLabel, ListWidget, MessageBoxBase, SubtitleLabel

from core.session_recovery import RecoveryCandidate

TITLE = "发现上次未结束的监控任务"
HINT = ("程序上次没有正常退出，下面这些任务的被测进程仍在运行。勾选后可继续监控："
        "任务沿用原来的记录，中断期间的数据是空洞；不勾选的任务将按已结束处理。")
YES_TEXT = "继续监控"
CANCEL_TEXT = "全部放弃"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TARGET_KIND_TEXTS = {'tree': '进程树', 'name': '同名进程'}
# 列表高度随任务数在 [MIN, MAX] 之间取（每行约 ROW_HEIGHT），任务多了在列表内滚动
LIST_ROW_HEIGHT = 38
LIST_MIN_HEIGHT = 120
LIST_MAX_HEIGHT = 260


def candidate_text(candidate: RecoveryCandidate) -> str:
    """一行文字：进程名 · PID · 上次采样时间（备注）。"""
    parts = [candidate.process_name]
    if candidate.target_kind in TARGET_KIND_TEXTS:
        parts.append(TARGET_KIND_TEXTS[candidate.target_kind])
    if candidate.pid:
        parts.append(f"PID {candidate.pid}")
    if candidate.last_sample_at is not None:
        parts.append(f"最后采样 {_time(candidate.last_sample_at)}")
    text = " · ".join(parts)
    if candidate.note:
        text += f"（{candidate.note}）"
    return text


def _time(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


class RecoverDialog(MessageBoxBase):
    """多选列表，全部默认勾选。exec() 为真表示点了"继续监控"。

    列表用 Fluent 主题的 ListWidget（深色主题下不会是白底黑字），单行过长时右侧省略、
    完整文字放在悬停提示里，不出现横向滚动条。"""

    def __init__(self, candidates: Sequence[RecoveryCandidate], parent=None):
        super().__init__(parent)
        self._candidates = list(candidates)
        self.viewLayout.addWidget(SubtitleLabel(TITLE, self))
        hint = BodyLabel(HINT, self)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)

        self.list_widget = ListWidget(self)
        self.list_widget.setFixedHeight(min(LIST_MAX_HEIGHT, max(
            LIST_MIN_HEIGHT, LIST_ROW_HEIGHT * len(self._candidates) + 8)))
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setTextElideMode(Qt.ElideRight)
        self.list_widget.setWordWrap(False)
        for candidate in self._candidates:
            text = candidate_text(candidate)
            item = QListWidgetItem(text)
            item.setToolTip(text)
            item.setData(Qt.UserRole, candidate.task_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.list_widget.addItem(item)
        self.list_widget.itemChanged.connect(self._update_yes_enabled)
        self.viewLayout.addWidget(self.list_widget)

        self.yesButton.setText(YES_TEXT)
        self.cancelButton.setText(CANCEL_TEXT)
        self.widget.setMinimumWidth(520)
        self._update_yes_enabled()

    def _update_yes_enabled(self, *_args) -> None:
        # 一个都没勾时"继续监控"没有意义：置灰，用户要放弃就点"全部放弃"
        self.yesButton.setEnabled(bool(self.selected_task_ids()))

    def selected_task_ids(self) -> List[str]:
        """已勾选的任务 ID（列表顺序）。"""
        ids: List[str] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.checkState() == Qt.Checked:
                ids.append(item.data(Qt.UserRole))
        return ids


def ask_recover(candidates: Sequence[RecoveryCandidate], parent=None) -> List[str]:
    """弹出对话框，返回用户选择续采的任务 ID；点"全部放弃"或关闭返回空列表。"""
    dialog = RecoverDialog(candidates, parent)
    if dialog.exec():
        return dialog.selected_task_ids()
    return []


def default_prompt(parent: Optional[object] = None):
    """生成给组合根用的提示函数：candidates -> 选中的任务 ID 列表。"""
    return lambda candidates: ask_recover(candidates, parent)
