"""
新建监控任务表单

进程选择 + 采集间隔 + 测试备注 + 指标摘要/选指标 + "等待进程启动后自动开始" +
"自动化"折叠区（自动打点与停止条件，见 automation_options）+ 开始按钮。
表单只收集输入、发出请求，不调用 manager，也不弹对话框：选指标、选日志文件与导出目录、
校验文案、重复 PID 确认、等待进程启动的轮询等流程都在 MonitorPresenter 里；只有自动化
方案的校验在提交时就地完成，错误显示在自动化区下方。

已有任务时表单可折叠成一行（只剩进程选择与开始按钮），把纵向空间让给任务卡片；
行尾的展开按钮可随时展开其余字段。
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel, CardWidget, CheckBox, FluentIcon, LineEdit, PrimaryPushButton,
    PushButton, SpinBox, StrongBodyLabel, TransparentToolButton,
)

from core.automation.plan import AutomationPlan
from ui.services.preferences import MonitorPreferences
from ui.services.process_query import ProcessQuery
from ui.widgets.monitor.automation_options import AutomationOptions
from ui.widgets.monitor.process_picker import ProcessPicker, ResolveError
from utils.metrics import LEAK_CHECK_PRESET, get_metric_display_name

NOTE_MAX_LENGTH = 100
START_TEXT = "开始监控"
WAIT_START_TEXT = "等待并开始"
WAIT_OPTION_TEXT = "等待进程启动后自动开始"


@dataclass(frozen=True)
class TaskRequest:
    """一次"开始监控"请求：目标进程 + 任务参数。"""
    pid: int
    name: str
    create_time: Optional[float]
    metrics: Tuple[str, ...]
    interval: float
    note: str = ''
    # 自动化方案；没有设置任何自动化时为 None
    plan: Optional[AutomationPlan] = None


@dataclass(frozen=True)
class WaitRequest:
    """一次"等待进程启动后自动开始"请求：按进程名等待，出现即用这些参数建任务。"""
    name: str
    metrics: Tuple[str, ...]
    interval: float
    note: str = ''
    plan: Optional[AutomationPlan] = None


def metric_summary(metrics: Sequence[str]) -> Tuple[str, str]:
    """已选指标的摘要文案与完整列表（tooltip）。"""
    names = [get_metric_display_name(m) for m in metrics]
    summary = f"已选 {len(names)} 项" if names else "未选择指标"
    if names and set(metrics) == set(LEAK_CHECK_PRESET):
        summary += "（泄漏排查预设）"
    details = "、".join(names) if names else "请选择至少一项监控指标"
    return summary, details


class NewTaskForm(CardWidget):
    """新建监控任务表单。

    信号：
        start_requested(object): 点击开始（或备注框回车）且目标进程有效，参数为 TaskRequest
        wait_requested(object): 勾选了"等待进程启动后自动开始"时点击开始，参数为 WaitRequest
        target_invalid(object): 点击开始但无法确定目标进程，参数为 ResolveError
        choose_metrics_requested(): 点击"选择指标"
        browse_log_requested(): "自动化"区日志文件的"浏览…"
        browse_export_dir_requested(): "自动化"区导出目录的"选择…"
    """

    start_requested = pyqtSignal(object)
    wait_requested = pyqtSignal(object)
    target_invalid = pyqtSignal(object)
    choose_metrics_requested = pyqtSignal()
    browse_log_requested = pyqtSignal()
    browse_export_dir_requested = pyqtSignal()

    def __init__(self, process_query: ProcessQuery, preferences: MonitorPreferences,
                 parent=None):
        super().__init__(parent)
        self._metrics: List[str] = []
        # 不能开始监控的原因（如数据库只读）；None 表示可以开始
        self._start_blocked: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.title_label = StrongBodyLabel("新建监控任务", self)
        layout.addWidget(self.title_label)

        # 第一行：进程选择 | 展开/收起 | 开始。折叠后只剩这一行
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.picker = ProcessPicker(process_query, self)
        top_row.addWidget(self.picker, 1)

        self.expand_button = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self)
        self.expand_button.setToolTip("展开采集间隔、指标、备注与自动化")
        self.expand_button.clicked.connect(self._toggle_details)
        self.expand_button.hide()
        top_row.addWidget(self.expand_button, 0, Qt.AlignTop)

        self.start_button = PrimaryPushButton(START_TEXT, self, FluentIcon.PLAY)
        self.start_button.setFixedWidth(116)
        self.start_button.clicked.connect(self.submit)
        top_row.addWidget(self.start_button, 0, Qt.AlignTop)
        layout.addLayout(top_row)

        # 其余字段：采集间隔、指标；等待进程启动；备注
        self.details = QWidget(self)
        grid = QGridLayout(self.details)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(12)

        grid.addWidget(CaptionLabel("采集间隔（秒）", self.details), 0, 0)
        self.interval_spinbox = SpinBox(self.details)
        # 取值范围只在应用配置里定义一份，经偏好服务读取
        self.interval_spinbox.setRange(*preferences.interval_range())
        self.interval_spinbox.setValue(preferences.default_interval())
        # 宽度交给 SpinBox 的尺寸提示：紧凑布局压缩它会把数值编辑区压成 0
        grid.addWidget(self.interval_spinbox, 0, 1)

        self.metric_button = PushButton("选择指标", self.details, FluentIcon.CHECKBOX)
        self.metric_button.setFixedWidth(112)
        self.metric_button.clicked.connect(self.choose_metrics_requested)
        grid.addWidget(self.metric_button, 0, 2)
        self.metric_summary_label = CaptionLabel(self.details)
        grid.addWidget(self.metric_summary_label, 0, 3)
        grid.setColumnStretch(3, 1)

        # 测试备注：随任务落库，分析页用它区分各次测试
        grid.addWidget(CaptionLabel("测试备注", self.details), 1, 0)
        self.note_input = LineEdit(self.details)
        self.note_input.setPlaceholderText("测试备注（可选，如：建模 200 次）；回车开始监控")
        self.note_input.setMaxLength(NOTE_MAX_LENGTH)
        self.note_input.setClearButtonEnabled(True)
        self.note_input.returnPressed.connect(self.submit)
        grid.addWidget(self.note_input, 1, 1, 1, 3)

        # 被测程序还没启动：按进程名等它出现，出现即开始（不必盯着手动点开始）
        self.wait_checkbox = CheckBox(WAIT_OPTION_TEXT, self.details)
        self.wait_checkbox.setToolTip("勾选后在上方输入进程名（如 notepad.exe）再点开始，"
                                      "进程一出现就按所选指标开始记录")
        self.wait_checkbox.toggled.connect(self._on_wait_toggled)
        grid.addWidget(self.wait_checkbox, 2, 1, 1, 3)

        # 自动化：定时 / 日志触发打点、停止条件、停止后导出（默认折叠）
        self.automation = AutomationOptions(self.details)
        self.automation.browse_log_requested.connect(self.browse_log_requested)
        self.automation.browse_export_dir_requested.connect(self.browse_export_dir_requested)
        grid.addWidget(self.automation, 3, 0, 1, 4)
        layout.addWidget(self.details)

        self.set_metrics_summary([])
        self._declare_tab_order()

    def _declare_tab_order(self) -> None:
        """Tab 顺序：进程 → 间隔 → 指标 → 备注 → 等待选项 → 开始 → 展开按钮 → 自动化区。
        开始按钮紧跟常用字段（填完回车/Tab 即到），自动化区放在最后。"""
        chain = [self.picker.combo.line_edit, self.interval_spinbox, self.metric_button,
                 self.note_input, self.wait_checkbox, self.start_button, self.expand_button,
                 *self.automation.focus_chain()]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    # ========== 指标 ==========

    def set_metrics_summary(self, metrics: Sequence[str]) -> None:
        """设置本表单的已选指标（新建任务将使用它们）并刷新摘要。"""
        self._metrics = list(metrics)
        summary, details = metric_summary(self._metrics)
        self.metric_summary_label.setText(summary)
        self.metric_summary_label.setToolTip(details)

    def metrics(self) -> List[str]:
        return list(self._metrics)

    # ========== 其他字段 ==========

    def interval(self) -> float:
        return float(self.interval_spinbox.value())

    def set_default_interval(self, seconds: int) -> None:
        """设置页的默认采集间隔变了：直接覆盖输入框的值（不影响已创建的任务）。"""
        self.interval_spinbox.setValue(seconds)

    def note(self) -> str:
        return self.note_input.text().strip()

    # ========== 等待进程启动 ==========

    def wait_enabled(self) -> bool:
        return self.wait_checkbox.isChecked()

    def set_wait_enabled(self, enabled: bool) -> None:
        self.wait_checkbox.setChecked(enabled)

    def _on_wait_toggled(self, checked: bool) -> None:
        # 等待中的进程本来就不在列表里，不提示"未找到"
        self.picker.set_no_match_hint_enabled(not checked)
        self.start_button.setText(WAIT_START_TEXT if checked else START_TEXT)
        self._sync_start_tooltip()

    def _sync_start_tooltip(self) -> None:
        if self._start_blocked:
            self.start_button.setToolTip(self._start_blocked)
        else:
            self.start_button.setToolTip(
                "进程出现后自动开始记录" if self.wait_enabled() else "")

    # ========== 自动化 ==========

    def automation_plan(self) -> Optional[AutomationPlan]:
        """本次开始要用的自动化方案：其余字段折叠（自动化区看不见）或什么都没设置时为
        None——藏起来的设置不生效，与折叠时取消"等待进程启动"同理。"""
        if self.is_collapsed():
            return None
        plan = self.automation.plan()
        return None if plan.is_empty() else plan

    def set_automation_plan(self, plan: Optional[AutomationPlan]) -> None:
        """回填自动化方案（"同配置再测"找不到唯一进程、交给用户自己选时）：有方案时
        展开其余字段与自动化区，让下一次"开始"看得见、也确实带上它。"""
        self.automation.set_plan(plan)
        if plan is not None and not plan.is_empty():
            self._show_details(True)
            self.automation.set_expanded(True)

    def show_automation_error(self, text: str) -> None:
        """在自动化区下方就近显示校验错误（并展开它）。"""
        if self.is_collapsed():
            self._show_details(True)
        self.automation.set_error(text)

    def set_log_path(self, path: str) -> None:
        self.automation.set_log_path(path)

    def set_export_dir(self, path: str) -> None:
        self.automation.set_export_dir(path)

    def log_path(self) -> str:
        return self.automation.log_path()

    def export_dir(self) -> str:
        return self.automation.export_dir()

    def _checked_plan(self) -> Tuple[bool, Optional[AutomationPlan]]:
        """(是否可以提交, 方案)：方案有误时就近显示错误并返回 (False, None)。"""
        plan = self.automation_plan()
        if plan is None:
            return True, None
        errors = plan.validate(self.interval())
        if errors:
            self.show_automation_error("；".join(errors))
            return False, None
        self.automation.clear_error()
        return True, plan

    # ========== 禁止开始 ==========

    def set_start_blocked(self, reason: Optional[str]) -> None:
        """禁止或恢复"开始监控"：reason 非空时禁用开始按钮并把原因放进 tooltip。

        备注框回车仍会发出请求，由 MonitorPresenter 就近提示原因（表单不弹提示）。
        """
        self._start_blocked = reason or None
        self.start_button.setEnabled(self._start_blocked is None)
        self._sync_start_tooltip()

    def start_blocked_reason(self) -> Optional[str]:
        return self._start_blocked

    # ========== 提交 ==========

    def submit(self) -> None:
        """按当前输入发出开始请求（勾选等待时发出等待请求）；目标无效时发出 target_invalid，
        自动化设置有误时在自动化区就近显示错误、不发请求。"""
        if self.wait_enabled():
            name = self.picker.resolve_name()
            if isinstance(name, ResolveError):
                self.target_invalid.emit(name)
                return
            ok, plan = self._checked_plan()
            if not ok:
                return
            self.wait_requested.emit(WaitRequest(
                name=name, metrics=tuple(self._metrics), interval=self.interval(),
                note=self.note(), plan=plan))
            return
        target = self.picker.resolve()
        if isinstance(target, ResolveError):
            self.target_invalid.emit(target)
            return
        ok, plan = self._checked_plan()
        if not ok:
            return
        self.start_requested.emit(TaskRequest(
            pid=target.pid, name=target.name, create_time=target.create_time,
            metrics=tuple(self._metrics), interval=self.interval(), note=self.note(),
            plan=plan))

    def show_error(self, text: str) -> None:
        self.picker.set_error(text)

    def forget_process_if(self, pid: int) -> bool:
        """被测进程退出后，若进程选择仍指向它则清掉并刷新列表（见 ProcessPicker.forget_if）。"""
        return self.picker.forget_if(pid)

    def fill_search(self, text: str) -> None:
        """把文本填进进程搜索框并聚焦（按名找不到唯一进程时交给用户自己选）。"""
        self.picker.set_text(text)
        self.picker.focus_search()

    # ========== 折叠 ==========

    def set_collapsed(self, collapsed: bool) -> None:
        """有任务时折叠成一行（只显示进程选择与开始），行尾的展开按钮可展开其余字段；
        没有任务时完整展开且不显示展开按钮。折叠时取消"等待进程启动"：选项藏起来后
        仍生效会让下一次"开始"的行为出人意料。"""
        if collapsed:
            self.set_wait_enabled(False)
        self.expand_button.setVisible(collapsed)
        self._show_details(not collapsed)

    def is_collapsed(self) -> bool:
        return self.details.isHidden()

    def _toggle_details(self) -> None:
        self._show_details(self.details.isHidden())

    def _show_details(self, visible: bool) -> None:
        self.details.setVisible(visible)
        self.title_label.setVisible(visible)
        self.expand_button.setIcon(
            FluentIcon.UP if visible else FluentIcon.CHEVRON_DOWN_MED)
        self.expand_button.setToolTip(
            "收起" if visible else "展开采集间隔、指标、备注与自动化")
