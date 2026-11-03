"""
小型交互对话框
打点标签输入、关窗时的"有任务在监控"三选一确认。
"""
from typing import Optional

from qfluentwidgets import (
    MessageBoxBase, SubtitleLabel, BodyLabel, CaptionLabel, LineEdit, PushButton
)

# 打点标签的最大长度（超出会被输入框截断，避免图表上的竖线标签过长）
MARKER_LABEL_MAX_LENGTH = 40


class MarkerDialog(MessageBoxBase):
    """打点标签输入框：默认填入"标记 N"，确定后返回标签文本。"""

    def __init__(self, default_label: str, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("添加打点", self))
        self.viewLayout.addWidget(CaptionLabel(
            "记录当前时刻发生的事件，历史图表上会画出对应的竖线", self))

        self.label_edit = LineEdit(self)
        self.label_edit.setText(default_label)
        self.label_edit.setMaxLength(MARKER_LABEL_MAX_LENGTH)
        self.label_edit.setPlaceholderText("标签，如：开始第 3 轮建模")
        self.label_edit.setClearButtonEnabled(True)
        self.label_edit.selectAll()
        self.viewLayout.addWidget(self.label_edit)

        self.yesButton.setText("添加")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(380)
        # 回车直接确认
        self.label_edit.returnPressed.connect(self.yesButton.click)

    def showEvent(self, event):
        # MessageBoxBase 默认把焦点给"添加"按钮：用户直接打字会落空、回车按默认标签提交。
        # 显示后把焦点交给输入框（默认标签已全选，打字即覆盖）
        super().showEvent(event)
        self.label_edit.setFocus()
        self.label_edit.selectAll()

    def get_label(self) -> str:
        """输入的标签（已去除首尾空白）。"""
        return self.label_edit.text().strip()


class CloseActionDialog(MessageBoxBase):
    """关闭主窗口时仍有任务在监控：停止监控并退出 / 最小化到托盘 / 取消。

    exec() 为真（点"停止监控并退出"或"最小化到托盘"）时，choice 给出具体选择。
    """

    def __init__(self, running_count: int, can_tray: bool, parent=None):
        super().__init__(parent)
        self.choice = 'quit'

        self.viewLayout.addWidget(
            SubtitleLabel(f"有 {running_count} 个任务正在监控", self))
        hint = "退出将停止全部监控任务（已采集的数据会保存）。"
        if can_tray:
            hint += "也可以最小化到系统托盘，让监控在后台继续。"
        body = BodyLabel(hint, self)
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)

        self.yesButton.setText("停止监控并退出")
        self.cancelButton.setText("取消")

        self.tray_button: Optional[PushButton] = None
        if can_tray:
            self.tray_button = PushButton("最小化到托盘", self)
            self.tray_button.clicked.connect(self._on_tray_clicked)
            self.buttonLayout.insertWidget(1, self.tray_button, 1)

        self.widget.setMinimumWidth(460)

    def _on_tray_clicked(self) -> None:
        self.choice = 'tray'
        self.accept()
