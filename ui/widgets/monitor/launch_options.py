"""
新建任务表单的"启动被测程序"折叠区

勾选"开始时先启动程序"后，"开始"会先启动这里填的程序，再用拿到的 PID 监控它（不必先手动
打开程序、再到进程列表里找）。可填参数与工作目录，可选"停止时关闭被测程序"。

控件只收集输入并拼成 LaunchSpec，不弹对话框："浏览…"/"选择…"只发信号，由
MonitorPresenter 经 services.dialogs 选好路径后再 set_exe / set_cwd 填回来。
默认折叠：标题行显示当前设置的简述（未启用时为"未启用"），点击展开按钮展开。
"""
import os
from typing import List, Optional, Tuple

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, FluentIcon, LineEdit, PushButton, TransparentToolButton,
)

from core.process_launcher import ERR_EXE_EMPTY, LaunchSpec, join_args, split_args
from ui.widgets.monitor.process_picker import ERROR_DARK, ERROR_LIGHT

SECTION_TITLE = "启动被测程序"
SECTION_OFF_TEXT = "未启用"
SUMMARY_TEMPLATE = "开始时启动 {name}"
SUMMARY_CLOSE_SUFFIX = " · 停止时关闭"
EXPAND_TOOLTIP = "展开启动被测程序的设置"
COLLAPSE_TOOLTIP = "收起启动被测程序的设置"

ENABLE_TEXT = "开始监控时先启动这个程序（不用在进程列表里选）"
EXE_LABEL = "程序"
EXE_PLACEHOLDER = "程序的完整路径，如 D:\\app\\demo.exe"
ARGS_LABEL = "参数"
ARGS_PLACEHOLDER = "启动参数（可选），含空格的参数用双引号括起"
CWD_LABEL = "工作目录"
CWD_PLACEHOLDER = "工作目录（可选，缺省为程序所在目录）"
BROWSE_TEXT = "浏览…"
CHOOSE_DIR_TEXT = "选择…"
CLOSE_ON_STOP_TEXT = "停止监控时关闭被测程序"
CLOSE_ON_STOP_TOOLTIP = ("点“停止”（或方案的停止条件到了）后关闭这个程序：先请求它关闭窗口，"
                         "5 秒内没退出再结束进程。\n被测程序自己退出时不做任何事。")


class LaunchOptions(QWidget):
    """"启动被测程序"折叠区。

    信号：
        browse_exe_requested(): 点击程序路径的"浏览…"
        browse_cwd_requested(): 点击工作目录的"选择…"
        changed(): 任一输入变化（标题行简述随之刷新）
    """

    browse_exe_requested = pyqtSignal()
    browse_cwd_requested = pyqtSignal()
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.toggle_button = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
        self.toggle_button.setToolTip(EXPAND_TOOLTIP)
        self.toggle_button.clicked.connect(self.toggle)
        header.addWidget(self.toggle_button)
        self.title_label = BodyLabel(SECTION_TITLE, self)
        header.addWidget(self.title_label)
        header.addSpacing(8)
        self.summary_label = CaptionLabel(SECTION_OFF_TEXT, self)
        self.summary_label.setMinimumWidth(1)
        header.addWidget(self.summary_label, 1)
        layout.addLayout(header)

        self.body = QWidget(self)
        grid = QGridLayout(self.body)
        grid.setContentsMargins(28, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        self.enable_check = CheckBox(ENABLE_TEXT, self.body)
        grid.addWidget(self.enable_check, 0, 0, 1, 3)

        grid.addWidget(CaptionLabel(EXE_LABEL, self.body), 1, 0)
        self.exe_edit = LineEdit(self.body)
        self.exe_edit.setPlaceholderText(EXE_PLACEHOLDER)
        self.exe_edit.setClearButtonEnabled(True)
        grid.addWidget(self.exe_edit, 1, 1)
        self.exe_browse_button = PushButton(BROWSE_TEXT, self.body)
        self.exe_browse_button.clicked.connect(self.browse_exe_requested)
        grid.addWidget(self.exe_browse_button, 1, 2)

        grid.addWidget(CaptionLabel(ARGS_LABEL, self.body), 2, 0)
        self.args_edit = LineEdit(self.body)
        self.args_edit.setPlaceholderText(ARGS_PLACEHOLDER)
        self.args_edit.setClearButtonEnabled(True)
        grid.addWidget(self.args_edit, 2, 1, 1, 2)

        grid.addWidget(CaptionLabel(CWD_LABEL, self.body), 3, 0)
        self.cwd_edit = LineEdit(self.body)
        self.cwd_edit.setPlaceholderText(CWD_PLACEHOLDER)
        self.cwd_edit.setClearButtonEnabled(True)
        grid.addWidget(self.cwd_edit, 3, 1)
        self.cwd_browse_button = PushButton(CHOOSE_DIR_TEXT, self.body)
        self.cwd_browse_button.clicked.connect(self.browse_cwd_requested)
        grid.addWidget(self.cwd_browse_button, 3, 2)

        self.close_check = CheckBox(CLOSE_ON_STOP_TEXT, self.body)
        self.close_check.setToolTip(CLOSE_ON_STOP_TOOLTIP)
        grid.addWidget(self.close_check, 4, 1, 1, 2)
        grid.setColumnStretch(1, 1)

        # 启动失败、没填程序等：就近显示在本区下方（与进程选择的字段错误同一配色）
        self.error_label = CaptionLabel(self.body)
        self.error_label.setTextColor(ERROR_LIGHT, ERROR_DARK)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        grid.addWidget(self.error_label, 5, 0, 1, 3)
        layout.addWidget(self.body)

        for check in (self.enable_check, self.close_check):
            check.toggled.connect(self._on_input_changed)
        for edit in (self.exe_edit, self.args_edit, self.cwd_edit):
            edit.textChanged.connect(self._on_input_changed)

        self._sync_enabled()
        self._refresh_summary()
        self.set_expanded(False)

    def focus_chain(self) -> List[QWidget]:
        """本区可获得焦点的控件，按视觉顺序（表单据此声明 Tab 顺序）。"""
        return [self.toggle_button, self.enable_check, self.exe_edit, self.exe_browse_button,
                self.args_edit, self.cwd_edit, self.cwd_browse_button, self.close_check]

    # ========== 折叠 ==========

    def set_expanded(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.toggle_button.setIcon(
            FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED)
        self.toggle_button.setToolTip(COLLAPSE_TOOLTIP if expanded else EXPAND_TOOLTIP)

    def is_expanded(self) -> bool:
        return not self.body.isHidden()

    def toggle(self) -> None:
        self.set_expanded(not self.is_expanded())

    # ========== 取值 ==========

    def is_enabled(self) -> bool:
        """是否勾选了"开始时先启动程序"。"""
        return self.enable_check.isChecked()

    def set_enabled(self, enabled: bool) -> None:
        self.enable_check.setChecked(enabled)

    def close_on_stop(self) -> bool:
        return self.close_check.isChecked()

    def exe(self) -> str:
        return self.exe_edit.text().strip()

    def set_exe(self, path: str) -> None:
        """填入程序路径（"浏览…"选好后）并勾选启动。"""
        self.exe_edit.setText(path)
        self.enable_check.setChecked(True)

    def cwd(self) -> str:
        return self.cwd_edit.text().strip()

    def set_cwd(self, path: str) -> None:
        self.cwd_edit.setText(path)

    def spec(self) -> Tuple[Optional[LaunchSpec], Optional[str]]:
        """(启动设置, 错误)：没填程序时返回 (None, 错误文案)；其余检查（文件是否存在等）
        在启动时由 launcher 做，结果同样就近显示。"""
        exe = self.exe()
        if not exe:
            return None, ERR_EXE_EMPTY
        return LaunchSpec(exe=exe, args=tuple(split_args(self.args_edit.text())),
                          cwd=self.cwd() or None), None

    def set_spec(self, spec: Optional[LaunchSpec], close_on_stop: bool = False) -> None:
        """回填启动设置（None 表示不启动：取消勾选，输入框保留原值）。"""
        if spec is None:
            self.enable_check.setChecked(False)
            return
        self.exe_edit.setText(spec.exe)
        self.args_edit.setText(join_args(spec.args))
        self.cwd_edit.setText(spec.cwd or '')
        self.close_check.setChecked(close_on_stop)
        self.enable_check.setChecked(True)

    # ========== 错误 ==========

    def set_error(self, text: str) -> None:
        """显示启动错误并展开本区（错误要让人看得见）。"""
        self.error_label.setText(text)
        self.error_label.show()
        self.set_expanded(True)

    def clear_error(self) -> None:
        self.error_label.clear()
        self.error_label.hide()

    def error_text(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ''

    # ========== 内部 ==========

    def _on_input_changed(self, *_args) -> None:
        self._sync_enabled()
        self._refresh_summary()
        if not self.error_label.isHidden():
            self.clear_error()
        self.changed.emit()

    def _sync_enabled(self) -> None:
        enabled = self.enable_check.isChecked()
        for widget in (self.exe_edit, self.exe_browse_button, self.args_edit, self.cwd_edit,
                       self.cwd_browse_button, self.close_check):
            widget.setEnabled(enabled)

    def summary_text(self) -> str:
        if not self.is_enabled():
            return SECTION_OFF_TEXT
        name = os.path.basename(self.exe()) or "（未填写程序）"
        text = SUMMARY_TEMPLATE.format(name=name)
        if self.close_on_stop():
            text += SUMMARY_CLOSE_SUFFIX
        return text

    def _refresh_summary(self) -> None:
        text = self.summary_text()
        self.summary_label.setText(text)
        self.summary_label.setToolTip(text)
