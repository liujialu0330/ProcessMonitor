"""
等待进程启动的占位卡

勾选"等待进程启动后自动开始"并点开始后，任务卡片的位置先显示一张占位卡：
"等待 xxx.exe 启动…" + 不确定进度条 + [取消]。进程出现、任务创建后占位卡由
Presenter 移除，换成正式的任务卡片。
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    CaptionLabel, CardWidget, FluentIcon, IndeterminateProgressBar, PushButton,
    StrongBodyLabel,
)

from ui.formatting import truncate_text

NOTE_DISPLAY_CHARS = 40


def waiting_title(process_name: str) -> str:
    return f"等待 {process_name} 启动…"


class WaitingCard(CardWidget):
    """等待进程启动的占位卡。信号 cancel_requested(str) 携带等待 ID。"""

    cancel_requested = pyqtSignal(str)

    def __init__(self, wait_id: str, process_name: str, detail: str = '', parent=None):
        super().__init__(parent)
        self.wait_id = wait_id
        self.process_name = process_name

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.title_label = StrongBodyLabel(waiting_title(process_name), self)
        row.addWidget(self.title_label)
        self.detail_label = CaptionLabel(
            f"· {truncate_text(detail, NOTE_DISPLAY_CHARS)}" if detail else "", self)
        self.detail_label.setToolTip(detail)
        self.detail_label.setVisible(bool(detail))
        row.addWidget(self.detail_label, 1)
        if not detail:
            row.addStretch(1)

        self.cancel_button = PushButton("取消等待", self, FluentIcon.CLOSE)
        self.cancel_button.setToolTip("不再等待这个进程启动")
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self.wait_id))
        row.addWidget(self.cancel_button, 0, Qt.AlignRight)
        layout.addLayout(row)

        self.hint_label = CaptionLabel("每秒检查一次，进程一出现就按所选指标开始记录", self)
        layout.addWidget(self.hint_label)
        # 不确定进度条表示"正在等"；离屏/隐藏时不播放动画
        self.progress = IndeterminateProgressBar(self, start=False)
        layout.addWidget(self.progress)

    def showEvent(self, event):
        super().showEvent(event)
        self.progress.start()

    def hideEvent(self, event):
        self.progress.stop()
        super().hideEvent(event)
