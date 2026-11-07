"""
新建任务表单的"自动化"折叠区

定时打点、日志触发打点、停止条件（运行时长 / 轮数）与停止后自动导出。控件只收集输入并
拼成 AutomationPlan，不弹对话框："浏览…"/"选择…"只发信号，由 MonitorPresenter 经
services.dialogs 选好路径后再 set_log_path / set_export_dir 填回来。

默认折叠：标题行显示当前方案的简述（未设置时为"未启用"），点击标题行展开。
各项由前面的勾选框启用，未勾选时其输入控件置灰、不进方案。
"""
from typing import List, Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon, LineEdit, PushButton, SpinBox,
    TransparentToolButton,
)

from core.automation.plan import DEFAULT_LOG_ENCODING, AutomationPlan
from ui.formatting import plan_summary
from ui.widgets.monitor.process_picker import ERROR_DARK, ERROR_LIGHT

SECTION_TITLE = "自动化"
SECTION_OFF_TEXT = "未启用"
EXPAND_TOOLTIP = "展开自动打点与停止条件设置"
COLLAPSE_TOOLTIP = "收起自动化设置"

TIMED_CHECK_TEXT = "定时打点，每"
TIMED_UNIT_TEXT = "秒打一轮"
LOG_CHECK_TEXT = "日志触发打点"
LOG_PATH_PLACEHOLDER = "日志文件的完整路径，如 D:\\logs\\app.log"
LOG_PATTERN_PLACEHOLDER = "匹配的正则，如 Case \\d+ done；命名分组 (?P<label>…) 作打点名"
LOG_PATTERN_TOOLTIP = ("日志文件新增的一行匹配即打一个点。\n"
                       "写了命名分组 label 时用它匹配到的文字作打点名称，"
                       "否则按默认名称（基线 / 第 N 轮）。")
LOG_ENCODING_TOOLTIP = "日志文件的编码"
BROWSE_TEXT = "浏览…"
STOP_TIME_CHECK_TEXT = "运行满"
STOP_TIME_UNIT_TEXT = "分钟后停止"
STOP_ROUNDS_CHECK_TEXT = "打满"
STOP_ROUNDS_UNIT_TEXT = "轮后停止"
STOP_ROUNDS_TOOLTIP = "轮数按打点计（手动与自动合计，不含基线）；第 N 轮的打点即第 N 轮结束"
EXPORT_CHECK_TEXT = "停止后自动导出明细 CSV 到"
EXPORT_DIR_PLACEHOLDER = "导出目录"
CHOOSE_DIR_TEXT = "选择…"

# 编码选项 (显示文本, 编码名)
ENCODING_CHOICES = (("UTF-8", DEFAULT_LOG_ENCODING), ("GBK", 'gbk'))

TIMED_RANGE = (1, 7 * 24 * 3600)
TIMED_DEFAULT = 600
STOP_MINUTES_RANGE = (1, 30 * 24 * 60)
STOP_MINUTES_DEFAULT = 120
STOP_ROUNDS_RANGE = (1, 100000)
STOP_ROUNDS_DEFAULT = 10


class AutomationOptions(QWidget):
    """自动化折叠区。

    信号：
        browse_log_requested(): 点击日志文件的"浏览…"
        browse_export_dir_requested(): 点击导出目录的"选择…"
        changed(): 任一输入变化（标题行简述随之刷新）
    """

    browse_log_requested = pyqtSignal()
    browse_export_dir_requested = pyqtSignal()
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 标题行：展开按钮 + "自动化" + 当前方案简述
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

        # 定时打点
        self.timed_check = CheckBox(TIMED_CHECK_TEXT, self.body)
        self.timed_spin = SpinBox(self.body)
        self.timed_spin.setRange(*TIMED_RANGE)
        self.timed_spin.setValue(TIMED_DEFAULT)
        grid.addWidget(self.timed_check, 0, 0)
        grid.addLayout(self._with_unit(self.timed_spin, TIMED_UNIT_TEXT), 0, 1, 1, 3)

        # 日志触发
        self.log_check = CheckBox(LOG_CHECK_TEXT, self.body)
        self.log_path_edit = LineEdit(self.body)
        self.log_path_edit.setPlaceholderText(LOG_PATH_PLACEHOLDER)
        self.log_path_edit.setClearButtonEnabled(True)
        self.log_browse_button = PushButton(BROWSE_TEXT, self.body)
        self.log_browse_button.clicked.connect(self.browse_log_requested)
        self.log_encoding_combo = ComboBox(self.body)
        for text, encoding in ENCODING_CHOICES:
            self.log_encoding_combo.addItem(text, userData=encoding)
        self.log_encoding_combo.setCurrentIndex(0)
        self.log_encoding_combo.setToolTip(LOG_ENCODING_TOOLTIP)
        grid.addWidget(self.log_check, 1, 0)
        grid.addWidget(self.log_path_edit, 1, 1)
        grid.addWidget(self.log_browse_button, 1, 2)
        grid.addWidget(self.log_encoding_combo, 1, 3)
        self.log_pattern_edit = LineEdit(self.body)
        self.log_pattern_edit.setPlaceholderText(LOG_PATTERN_PLACEHOLDER)
        self.log_pattern_edit.setToolTip(LOG_PATTERN_TOOLTIP)
        self.log_pattern_edit.setClearButtonEnabled(True)
        grid.addWidget(self.log_pattern_edit, 2, 1, 1, 3)

        # 停止条件
        self.stop_time_check = CheckBox(STOP_TIME_CHECK_TEXT, self.body)
        self.stop_minutes_spin = SpinBox(self.body)
        self.stop_minutes_spin.setRange(*STOP_MINUTES_RANGE)
        self.stop_minutes_spin.setValue(STOP_MINUTES_DEFAULT)
        grid.addWidget(self.stop_time_check, 3, 0)
        grid.addLayout(self._with_unit(self.stop_minutes_spin, STOP_TIME_UNIT_TEXT), 3, 1, 1, 3)

        self.stop_rounds_check = CheckBox(STOP_ROUNDS_CHECK_TEXT, self.body)
        self.stop_rounds_check.setToolTip(STOP_ROUNDS_TOOLTIP)
        self.stop_rounds_spin = SpinBox(self.body)
        self.stop_rounds_spin.setRange(*STOP_ROUNDS_RANGE)
        self.stop_rounds_spin.setValue(STOP_ROUNDS_DEFAULT)
        grid.addWidget(self.stop_rounds_check, 4, 0)
        grid.addLayout(self._with_unit(self.stop_rounds_spin, STOP_ROUNDS_UNIT_TEXT), 4, 1, 1, 3)

        # 停止后自动导出
        self.export_check = CheckBox(EXPORT_CHECK_TEXT, self.body)
        self.export_dir_edit = LineEdit(self.body)
        self.export_dir_edit.setPlaceholderText(EXPORT_DIR_PLACEHOLDER)
        self.export_dir_edit.setClearButtonEnabled(True)
        self.export_browse_button = PushButton(CHOOSE_DIR_TEXT, self.body)
        self.export_browse_button.clicked.connect(self.browse_export_dir_requested)
        grid.addWidget(self.export_check, 5, 0)
        grid.addWidget(self.export_dir_edit, 5, 1)
        grid.addWidget(self.export_browse_button, 5, 2)
        grid.setColumnStretch(1, 1)

        # 校验错误：就近显示在本区下方（与进程选择的字段错误同一配色）
        self.error_label = CaptionLabel(self.body)
        self.error_label.setTextColor(ERROR_LIGHT, ERROR_DARK)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        grid.addWidget(self.error_label, 6, 0, 1, 4)
        layout.addWidget(self.body)

        for check in self._checks():
            check.toggled.connect(self._on_input_changed)
        for spin in (self.timed_spin, self.stop_minutes_spin, self.stop_rounds_spin):
            spin.valueChanged.connect(self._on_input_changed)
        for edit in (self.log_path_edit, self.log_pattern_edit, self.export_dir_edit):
            edit.textChanged.connect(self._on_input_changed)
        self.log_encoding_combo.currentIndexChanged.connect(self._on_input_changed)

        self._sync_enabled()
        self._refresh_summary()
        self.set_expanded(False)

    def _with_unit(self, spin: SpinBox, unit: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(spin)
        row.addWidget(CaptionLabel(unit, self.body))
        row.addStretch(1)
        return row

    def _checks(self) -> List[CheckBox]:
        return [self.timed_check, self.log_check, self.stop_time_check,
                self.stop_rounds_check, self.export_check]

    def focus_chain(self) -> List[QWidget]:
        """本区可获得焦点的控件，按视觉顺序（表单据此声明 Tab 顺序）。"""
        return [self.toggle_button, self.timed_check, self.timed_spin,
                self.log_check, self.log_path_edit, self.log_browse_button,
                self.log_encoding_combo, self.log_pattern_edit,
                self.stop_time_check, self.stop_minutes_spin,
                self.stop_rounds_check, self.stop_rounds_spin,
                self.export_check, self.export_dir_edit, self.export_browse_button]

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

    # ========== 方案 ==========

    def plan(self) -> AutomationPlan:
        """按当前输入拼出方案（未勾选的项不进方案；路径与规则去首尾空白）。"""
        log_on = self.log_check.isChecked()
        export_on = self.export_check.isChecked()
        return AutomationPlan(
            timed_marker_seconds=(self.timed_spin.value()
                                  if self.timed_check.isChecked() else None),
            log_path=(self.log_path_edit.text().strip() or None) if log_on else None,
            log_pattern=(self.log_pattern_edit.text().strip() or None) if log_on else None,
            log_encoding=(self._current_encoding() if log_on else DEFAULT_LOG_ENCODING),
            stop_after_seconds=(self.stop_minutes_spin.value() * 60
                                if self.stop_time_check.isChecked() else None),
            stop_after_rounds=(self.stop_rounds_spin.value()
                               if self.stop_rounds_check.isChecked() else None),
            export_on_stop=export_on,
            export_dir=(self.export_dir_edit.text().strip() or None) if export_on else None,
        )

    def set_plan(self, plan: Optional[AutomationPlan]) -> None:
        """按方案回填各项（None 视为空方案，全部取消勾选；数值框保留原值）。"""
        plan = plan or AutomationPlan()
        self.timed_check.setChecked(plan.timed_marker_seconds is not None)
        if plan.timed_marker_seconds is not None:
            self.timed_spin.setValue(plan.timed_marker_seconds)
        self.log_check.setChecked(bool(plan.log_path or plan.log_pattern))
        self.log_path_edit.setText(plan.log_path or '')
        self.log_pattern_edit.setText(plan.log_pattern or '')
        self._select_encoding(plan.log_encoding)
        self.stop_time_check.setChecked(plan.stop_after_seconds is not None)
        if plan.stop_after_seconds is not None:
            # 方案以秒存，界面以分钟填：不足一分钟的按一分钟显示
            self.stop_minutes_spin.setValue(max(1, round(plan.stop_after_seconds / 60)))
        self.stop_rounds_check.setChecked(plan.stop_after_rounds is not None)
        if plan.stop_after_rounds is not None:
            self.stop_rounds_spin.setValue(plan.stop_after_rounds)
        self.export_check.setChecked(plan.export_on_stop)
        self.export_dir_edit.setText(plan.export_dir or '')
        self.clear_error()

    def set_log_path(self, path: str) -> None:
        """填入日志文件路径（"浏览…"选好后）并勾选日志触发。"""
        self.log_path_edit.setText(path)
        self.log_check.setChecked(True)

    def log_path(self) -> str:
        return self.log_path_edit.text().strip()

    def set_export_dir(self, path: str) -> None:
        """填入导出目录（"选择…"选好后）并勾选停止后自动导出。"""
        self.export_dir_edit.setText(path)
        self.export_check.setChecked(True)

    def export_dir(self) -> str:
        return self.export_dir_edit.text().strip()

    def _current_encoding(self) -> str:
        return self.log_encoding_combo.currentData() or DEFAULT_LOG_ENCODING

    def _select_encoding(self, encoding: str) -> None:
        encoding = encoding or DEFAULT_LOG_ENCODING
        for index in range(self.log_encoding_combo.count()):
            if (self.log_encoding_combo.itemData(index) or '').lower() == encoding.lower():
                self.log_encoding_combo.setCurrentIndex(index)
                return
        # 方案里是列表之外的编码（手写或模板导入）：加一项保留它
        self.log_encoding_combo.addItem(encoding, userData=encoding)
        self.log_encoding_combo.setCurrentIndex(self.log_encoding_combo.count() - 1)

    # ========== 校验错误 ==========

    def set_error(self, text: str) -> None:
        """显示校验错误并展开本区（错误要让人看得见）。"""
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
            # 用户在改，旧错误不再准确
            self.clear_error()
        self.changed.emit()

    def _sync_enabled(self) -> None:
        self.timed_spin.setEnabled(self.timed_check.isChecked())
        log_on = self.log_check.isChecked()
        for widget in (self.log_path_edit, self.log_browse_button,
                       self.log_encoding_combo, self.log_pattern_edit):
            widget.setEnabled(log_on)
        self.stop_minutes_spin.setEnabled(self.stop_time_check.isChecked())
        self.stop_rounds_spin.setEnabled(self.stop_rounds_check.isChecked())
        export_on = self.export_check.isChecked()
        self.export_dir_edit.setEnabled(export_on)
        self.export_browse_button.setEnabled(export_on)

    def _refresh_summary(self) -> None:
        text = plan_summary(self.plan()) or SECTION_OFF_TEXT
        self.summary_label.setText(text)
        self.summary_label.setToolTip(text)
