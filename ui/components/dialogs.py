"""
小型交互对话框
打点标签输入、关窗时的"有任务在监控"三选一确认。
"""
from datetime import datetime
from typing import Optional

from qfluentwidgets import (
    MessageBoxBase, SubtitleLabel, BodyLabel, CaptionLabel, LineEdit, PushButton
)

from ui.widgets.qfw_compat import InputDialogBase

# 打点标签的最大长度（超出会被输入框截断，避免图表上的竖线标签过长）
MARKER_LABEL_MAX_LENGTH = 40


class MarkerDialog(InputDialogBase):
    """打点标签输入框：默认填入"标记 N"，确定后返回标签文本。

    打点时间是用户点"打点"按钮的时刻（由调用方传入并最终写库），不是点"添加"的
    时刻：输入标签要花好几秒，按确认时刻记录会让竖线落在事件之后。
    """

    def __init__(self, default_label: str, parent=None,
                 timestamp: Optional[datetime] = None):
        super().__init__(parent)
        self.timestamp = timestamp if timestamp is not None else datetime.now()
        self.viewLayout.addWidget(SubtitleLabel("添加打点", self))
        self.viewLayout.addWidget(CaptionLabel(
            "记录点击“打点”时发生的事件，历史图表上会画出对应的竖线", self))
        self.time_label = BodyLabel(
            f"时间 {self.timestamp.strftime('%H:%M:%S')}（点击时刻）", self)
        self.viewLayout.addWidget(self.time_label)

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
        # 标签为空时不能添加（禁用后回车触发的 click 也不会生效）
        self.label_edit.textChanged.connect(self._update_yes_enabled)
        self._update_yes_enabled()
        # 回车直接确认
        self.label_edit.returnPressed.connect(self.yesButton.click)
        # 弹出后焦点给输入框并全选默认标签：直接打字即覆盖（基类默认焦点在"添加"上）
        self.set_focus_widget(self.label_edit)

    def _update_yes_enabled(self, *_args) -> None:
        self.yesButton.setEnabled(bool(self.label_edit.text().strip()))

    def get_label(self) -> str:
        """输入的标签（已去除首尾空白）。"""
        return self.label_edit.text().strip()


class CloseActionDialog(MessageBoxBase):
    """关闭主窗口时仍有任务在监控：最小化到托盘 / 停止监控并退出 / 取消。

    主按钮（强调色、默认焦点、回车触发）只给不破坏现场的动作：托盘可用时是
    "最小化到托盘"，不可用时隐藏主按钮、焦点落在"取消"。"停止监控并退出"始终是
    普通按钮，避免跑了一整夜的测试被顺手一个回车全部停掉。

    exec() 为真（点"最小化到托盘"或"停止监控并退出"）时，choice 给出具体选择。
    """

    def __init__(self, running_count: int, can_tray: bool, parent=None):
        super().__init__(parent)
        # 主按钮只在托盘可用时显示，exec() 为真且未点退出即为"最小化到托盘"
        self.choice = 'tray' if can_tray else 'cancel'

        self.viewLayout.addWidget(
            SubtitleLabel(f"有 {running_count} 个任务正在监控", self))
        hint = "退出将停止全部监控任务（已采集的数据会保存）。"
        if can_tray:
            hint += "也可以最小化到系统托盘，让监控在后台继续。"
        body = BodyLabel(hint, self)
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)

        self.cancelButton.setText("取消")

        self.tray_button: Optional[PushButton] = None
        if can_tray:
            self.yesButton.setText("最小化到托盘")
            self.tray_button = self.yesButton
        else:
            self.hideYesButton()

        self.quit_button = PushButton("停止监控并退出", self.buttonGroup)
        # 不参与 QDialog 的默认按钮机制：即使拿到焦点，回车也不会触发它
        self.quit_button.setAutoDefault(False)
        self.quit_button.clicked.connect(self._on_quit_clicked)
        self.buttonLayout.insertWidget(
            self.buttonLayout.indexOf(self.cancelButton), self.quit_button, 1)

        self.widget.setMinimumWidth(460)

    def showEvent(self, event):
        # MessageBoxBase 构造时把焦点给 yesButton；无托盘时它已隐藏，焦点改给"取消"
        super().showEvent(event)
        if self.tray_button is not None:
            self.tray_button.setFocus()
        else:
            self.cancelButton.setFocus()

    def _on_quit_clicked(self) -> None:
        self.choice = 'quit'
        self.accept()
