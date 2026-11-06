"""
监控页空状态：三步引导

没有任何任务时，在任务卡片的位置说明主线流程：选进程 → 预热后打"基线" →
每轮操作后打点。比一句"暂无监控任务"更能让第一次用的人知道下一步做什么。
"""
from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, StrongBodyLabel, themeColor

from ui.typography import TypeScale, ui_font

EMPTY_TITLE = "还没有监控任务"
STEP_BADGE_SIZE = 24


def guide_steps(hotkey_text: str = '') -> List[tuple]:
    """三步引导的 (标题, 说明)；hotkey_text 为空表示全局快捷键不可用，不提它。"""
    shortcut = (f"托盘菜单或快捷键 {hotkey_text}" if hotkey_text else "托盘菜单")
    return [
        ("选择被测进程",
         "在上方搜索进程名或输入 PID，点“开始监控”；进程还没启动可勾选“等待进程启动后自动开始”"),
        ("预热后打“基线”",
         "等被测程序稳定后点卡片上的“打点”，第一个打点就是基线，之后都和它比"),
        ("每轮操作后打点",
         f"每做完一轮操作再打一次点，自动命名“第 N 轮”；{shortcut}打点不弹窗"),
    ]


class _StepBadge(QWidget):
    """圆形序号徽标（强调色描边 + 数字）。"""

    def __init__(self, number: int, parent=None):
        super().__init__(parent)
        self.number = number
        self.setFixedSize(STEP_BADGE_SIZE, STEP_BADGE_SIZE)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(themeColor())
        painter.setPen(color)
        painter.drawEllipse(1, 1, self.width() - 2, self.height() - 2)
        painter.setFont(ui_font(TypeScale.CAPTION, QFont.DemiBold))
        painter.drawText(self.rect(), Qt.AlignCenter, str(self.number))
        painter.end()


class EmptyGuideCard(CardWidget):
    """空状态卡：标题 + 横排三步。属性 title_label / step_titles / step_hints 供测试读取。"""

    def __init__(self, hotkey_text: str = '', parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self.title_label = StrongBodyLabel(EMPTY_TITLE, self)
        layout.addWidget(self.title_label)

        steps_row = QHBoxLayout()
        steps_row.setSpacing(24)
        self.step_titles = []
        self.step_hints = []
        for number, (title, hint) in enumerate(guide_steps(hotkey_text), start=1):
            column = QVBoxLayout()
            column.setSpacing(4)
            head = QHBoxLayout()
            head.setSpacing(8)
            head.addWidget(_StepBadge(number, self))
            title_label = StrongBodyLabel(title, self)
            head.addWidget(title_label, 1)
            column.addLayout(head)
            hint_label = CaptionLabel(hint, self)
            hint_label.setWordWrap(True)
            hint_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            column.addWidget(hint_label)
            column.addStretch(1)
            steps_row.addLayout(column, 1)
            self.step_titles.append(title_label)
            self.step_hints.append(hint_label)
        layout.addLayout(steps_row)

    def set_hotkey_text(self, hotkey_text: str) -> None:
        """全局打点快捷键变了（或不可用）：更新第三步的说明。"""
        for label, (_title, hint) in zip(self.step_hints, guide_steps(hotkey_text)):
            label.setText(hint)
