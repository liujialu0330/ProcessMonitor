"""
新建监控任务表单

进程选择 + 采集间隔 + 测试备注 + 指标摘要/选指标（旁边按指标数与采集间隔实时提示存储
增长，见 core.storage_estimate）+ "等待进程启动后自动开始" + "自动化"折叠区（自动打点、
停止条件与阶段快照，见 automation_options）+ "启动被测程序"折叠区（先启动程序再监控，见
launch_options）+ 开始按钮。
表单只收集输入、发出请求，不调用 manager，也不弹对话框：选指标、选日志文件与导出目录、
校验文案、重复 PID 确认、等待进程启动的轮询等流程都在 MonitorPresenter 里；只有自动化
方案的校验在提交时就地完成，错误显示在自动化区下方；启动程序的错误（没填程序、启动失败）
显示在启动区下方。勾选了"开始时先启动程序"时不看进程选择、也不走等待进程启动。

已有任务时表单可折叠成一行（只剩目标类型、进程选择与开始按钮），把纵向空间让给任务卡片；
行尾的展开按钮可随时展开其余字段。

目标类型（进程选择左边的分段控件）：单个进程（默认，行为与旧版一致）/ 进程树（所选进程
为根，监控它及其全部后代）/ 同名进程（取所选进程的进程名，也可只输入进程名，进程此刻
不在运行也行）。进程组任务各有一份指标选择（对组不适用的峰值类与优先级剔除，始终带进程
总数），存储预估按成员数估算；同名进程没有"等待启动"与"启动被测程序"（它本来就随时
可以开始，且没有单一的被启动进程），进程树两者都可用。
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel, CardWidget, CheckBox, FluentIcon, LineEdit, PrimaryPushButton,
    PushButton, SegmentedWidget, SpinBox, StrongBodyLabel, TransparentToolButton,
)

from core.automation.plan import AutomationPlan
from core.process_launcher import LaunchSpec
from core.storage_estimate import (
    GROUP_TYPICAL_MEMBERS, estimate, estimate_tooltip, format_estimate,
)
from data.models import TARGET_NAME, TARGET_PID, TARGET_TREE
from ui.services.preferences import MonitorPreferences
from ui.services.process_query import ProcessQuery
from ui.widgets.monitor.automation_options import AutomationOptions
from ui.widgets.monitor.launch_options import LaunchOptions
from ui.widgets.monitor.process_picker import (
    ERROR_GROUP_NEEDS_NAME, ProcessPicker, ResolveError,
)
from utils.metrics import (
    LEAK_CHECK_PRESET, PROCESS_COUNT, filter_group_metrics, get_metric_display_name,
)

NOTE_MAX_LENGTH = 100
START_TEXT = "开始监控"
WAIT_START_TEXT = "等待并开始"
LAUNCH_START_TEXT = "启动并监控"
WAIT_OPTION_TEXT = "等待进程启动后自动开始"
WAIT_OPTION_TIP = ("勾选后在上方输入进程名（如 notepad.exe）再点开始，"
                   "进程一出现就按所选指标开始记录")
WAIT_OPTION_NAME_KIND_TIP = "同名进程任务不需要等待：没有匹配的进程也会开始，进程出现后自动纳入"
LAUNCH_NAME_KIND_TIP = "同名进程监控的是全部同名进程，没有单一的被启动进程；要先启动程序请选“进程树”"
NAME_PLACEHOLDER = "输入进程名（如 chrome.exe），或选一个进程取它的名字"

# 目标类型：(取值, 分段控件文案, 悬停说明)
KIND_OPTIONS = (
    (TARGET_PID, "单个进程", "监控一个进程"),
    (TARGET_TREE, "进程树", "监控所选进程（根）及其全部子孙进程：看合计，也按进程名分成员"),
    (TARGET_NAME, "同名进程", "监控全部同名进程（如 chrome.exe 的所有实例）；进程此刻不在运行也能开始"),
)


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
    # 先启动被测程序再监控（此时 pid / create_time 由启动结果填入，表单里先给 0 / None）
    launch: Optional[LaunchSpec] = None
    # 用户停止任务后关闭由 launch 启动的程序
    close_on_stop: bool = False
    # 监控目标类型：pid 单个进程 / tree 以 pid 为根的进程树 / name 全部同名进程
    # （name 时 pid 为 0、name 是要监控的进程名）
    target_kind: str = TARGET_PID


@dataclass(frozen=True)
class WaitRequest:
    """一次"等待进程启动后自动开始"请求：按进程名等待，出现即用这些参数建任务。"""
    name: str
    metrics: Tuple[str, ...]
    interval: float
    note: str = ''
    plan: Optional[AutomationPlan] = None
    # 出现后按哪种目标建任务（pid 或 tree；同名进程不需要等待）
    target_kind: str = TARGET_PID


def metric_summary(metrics: Sequence[str], group: bool = False) -> Tuple[str, str]:
    """已选指标的摘要文案与完整列表（tooltip）。group 为真时是进程组任务的选择：
    始终含进程总数，泄漏排查预设的判断不把它算在内。"""
    names = [get_metric_display_name(m) for m in metrics]
    summary = f"已选 {len(names)} 项" if names else "未选择指标"
    compared = {m for m in metrics if not (group and m == PROCESS_COUNT)}
    if names and compared == set(LEAK_CHECK_PRESET):
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
        browse_snapshot_dir_requested(): "自动化"区阶段快照目录的"选择…"
        browse_exe_requested(): "启动被测程序"区程序路径的"浏览…"
        browse_cwd_requested(): "启动被测程序"区工作目录的"选择…"
        target_kind_changed(str): 目标类型（pid / tree / name）变了
    """

    start_requested = pyqtSignal(object)
    wait_requested = pyqtSignal(object)
    target_invalid = pyqtSignal(object)
    choose_metrics_requested = pyqtSignal()
    browse_log_requested = pyqtSignal()
    browse_export_dir_requested = pyqtSignal()
    browse_snapshot_dir_requested = pyqtSignal()
    browse_exe_requested = pyqtSignal()
    browse_cwd_requested = pyqtSignal()
    target_kind_changed = pyqtSignal(str)

    def __init__(self, process_query: ProcessQuery, preferences: MonitorPreferences,
                 parent=None):
        super().__init__(parent)
        self._metrics: List[str] = []
        # 进程组任务（进程树 / 同名进程）的指标选择；None 表示还没单独选过，随单进程的
        # 选择派生（剔除对组不适用的指标、补上进程总数）
        self._group_metrics: Optional[List[str]] = None
        self._kind = TARGET_PID
        # 不能开始监控的原因（如数据库只读）；None 表示可以开始
        self._start_blocked: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.title_label = StrongBodyLabel("新建监控任务", self)
        layout.addWidget(self.title_label)

        # 第一行：目标类型 | 进程选择 | 展开/收起 | 开始。折叠后只剩这一行
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.kind_segmented = SegmentedWidget(self)
        for key, text, tip in KIND_OPTIONS:
            self.kind_segmented.addItem(routeKey=key, text=text)
            self.kind_segmented.items[key].setToolTip(tip)
        self.kind_segmented.setCurrentItem(TARGET_PID)
        self.kind_segmented.currentItemChanged.connect(self._on_kind_item_changed)
        top_row.addWidget(self.kind_segmented, 0, Qt.AlignTop)
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
        # 指标摘要 + 存储预估（"已选 5 项    每小时约 18,000 行 · 12 小时约 4 MB"）
        metric_info = QHBoxLayout()
        metric_info.setSpacing(16)
        self.metric_summary_label = CaptionLabel(self.details)
        metric_info.addWidget(self.metric_summary_label)
        self.storage_label = CaptionLabel(self.details)
        # 窄宽度下允许截断（完整说明在悬停提示里），不撑大表单的最小宽度
        self.storage_label.setMinimumWidth(1)
        metric_info.addWidget(self.storage_label, 1)
        grid.addLayout(metric_info, 0, 3)
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
        self.wait_checkbox.setToolTip(WAIT_OPTION_TIP)
        self.wait_checkbox.toggled.connect(self._on_wait_toggled)
        grid.addWidget(self.wait_checkbox, 2, 1, 1, 3)

        # 自动化：定时 / 日志触发打点、停止条件、停止后导出（默认折叠）
        self.automation = AutomationOptions(self.details)
        self.automation.browse_log_requested.connect(self.browse_log_requested)
        self.automation.browse_export_dir_requested.connect(self.browse_export_dir_requested)
        self.automation.browse_snapshot_dir_requested.connect(
            self.browse_snapshot_dir_requested)
        grid.addWidget(self.automation, 3, 0, 1, 4)

        # 启动被测程序：先启动再监控（默认折叠）
        self.launch_options = LaunchOptions(self.details)
        self.launch_options.browse_exe_requested.connect(self.browse_exe_requested)
        self.launch_options.browse_cwd_requested.connect(self.browse_cwd_requested)
        self.launch_options.changed.connect(self._on_launch_changed)
        grid.addWidget(self.launch_options, 4, 0, 1, 4)
        layout.addWidget(self.details)

        self.interval_spinbox.valueChanged.connect(self._refresh_storage_estimate)
        self.set_metrics_summary([])
        self._declare_tab_order()

    def _declare_tab_order(self) -> None:
        """Tab 顺序：进程 → 间隔 → 指标 → 备注 → 等待选项 → 开始 → 展开按钮 → 自动化区 →
        启动区。开始按钮紧跟常用字段（填完回车/Tab 即到），两个折叠区按显示顺序放在最后。"""
        chain = [self.picker.combo.line_edit, self.interval_spinbox, self.metric_button,
                 self.note_input, self.wait_checkbox, self.start_button, self.expand_button,
                 *self.automation.focus_chain(), *self.launch_options.focus_chain()]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    # ========== 指标 ==========

    def set_metrics_summary(self, metrics: Sequence[str]) -> None:
        """设置本表单当前目标类型的已选指标（新建任务将使用它们）并刷新摘要。
        进程组任务的选择里对组不适用的指标会被剔除，进程总数始终在末尾。"""
        if self._is_group():
            self._group_metrics = self._with_process_count(filter_group_metrics(metrics))
        else:
            self._metrics = list(metrics)
        self._refresh_metric_summary()

    def metrics(self) -> List[str]:
        """当前目标类型下新建任务要用的指标（进程组任务：已剔除对组不适用的，含进程总数）。"""
        if not self._is_group():
            return list(self._metrics)
        if self._group_metrics is None:
            return self._with_process_count(filter_group_metrics(self._metrics))
        return list(self._group_metrics)

    @staticmethod
    def _with_process_count(metrics: Sequence[str]) -> List[str]:
        result = [m for m in metrics if m != PROCESS_COUNT]
        result.append(PROCESS_COUNT)
        return result

    def _refresh_metric_summary(self) -> None:
        summary, details = metric_summary(self.metrics(), group=self._is_group())
        self.metric_summary_label.setText(summary)
        self.metric_summary_label.setToolTip(details)
        self._refresh_storage_estimate()

    def storage_estimate_text(self) -> str:
        return self.storage_label.text()

    def _refresh_storage_estimate(self, *_args) -> None:
        """按当前指标数与采集间隔刷新存储预估；没选指标时清空。进程组任务按
        GROUP_TYPICAL_MEMBERS 种成员估算（每种成员另加一份序列）。"""
        count, interval = len(self.metrics()), self.interval()
        members = GROUP_TYPICAL_MEMBERS if self._is_group() else 0
        self.storage_label.setText(format_estimate(estimate(count, interval, members), members))
        self.storage_label.setToolTip(estimate_tooltip(count, interval, members))

    # ========== 目标类型 ==========

    def target_kind(self) -> str:
        """当前目标类型：pid（单个进程）/ tree（进程树）/ name（同名进程）。"""
        return self._kind

    def set_target_kind(self, kind: str) -> None:
        """程序切换目标类型（与用户点分段控件同一效果，也发 target_kind_changed）。"""
        if kind not in (key for key, _text, _tip in KIND_OPTIONS):
            raise ValueError(f"未知的监控目标类型 {kind!r}")
        self.kind_segmented.blockSignals(True)
        try:
            self.kind_segmented.setCurrentItem(kind)
        finally:
            self.kind_segmented.blockSignals(False)
        self._apply_kind(kind)

    def _is_group(self) -> bool:
        return self._kind != TARGET_PID

    def _on_kind_item_changed(self, kind: str) -> None:
        self._apply_kind(kind)

    def _apply_kind(self, kind: str) -> None:
        if kind == self._kind:
            return
        self._kind = kind
        self.picker.set_placeholder(NAME_PLACEHOLDER if kind == TARGET_NAME else None)
        self.picker.clear_error()
        self._refresh_metric_summary()
        self._sync_start_mode()
        self.target_kind_changed.emit(kind)

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
        self._sync_start_mode()

    def _sync_start_mode(self) -> None:
        """按"启动程序 / 等待进程 / 直接开始"三种方式同步开始按钮文案与进程选择的提示。
        启动程序优先：勾选它时等待选项不可用（程序由本次启动，用不着等）。同名进程既不等待
        也不启动程序（见模块说明），两个选项置灰并说明原因。"""
        name_kind = self._kind == TARGET_NAME
        if name_kind and self.wait_checkbox.isChecked():
            self.wait_checkbox.setChecked(False)   # 再次进入本方法时已不勾选
            return
        launching = self.launch_enabled()
        self.wait_checkbox.setEnabled(not launching and not name_kind)
        self.wait_checkbox.setToolTip(WAIT_OPTION_NAME_KIND_TIP if name_kind else WAIT_OPTION_TIP)
        self.launch_options.setEnabled(not name_kind)
        self.launch_options.setToolTip(LAUNCH_NAME_KIND_TIP if name_kind else "")
        waiting = self.wait_enabled() and not launching
        # 等待中的进程本来就不在列表里、启动的程序还没运行、同名进程允许输入此刻不存在的
        # 进程名：都不提示"未找到"
        self.picker.set_no_match_hint_enabled(not waiting and not launching and not name_kind)
        if launching:
            self.start_button.setText(LAUNCH_START_TEXT)
        else:
            self.start_button.setText(WAIT_START_TEXT if waiting else START_TEXT)
        self._sync_start_tooltip()

    def _sync_start_tooltip(self) -> None:
        if self._start_blocked:
            self.start_button.setToolTip(self._start_blocked)
        elif self.launch_enabled():
            self.start_button.setToolTip("先启动所填的程序，再监控它")
        elif self._kind == TARGET_NAME:
            self.start_button.setToolTip("监控全部同名进程；此刻没有也能开始，出现后自动纳入")
        else:
            self.start_button.setToolTip(
                "进程出现后自动开始记录" if self.wait_enabled() else "")

    # ========== 启动被测程序 ==========

    def launch_enabled(self) -> bool:
        """本次开始是否先启动程序：其余字段折叠时不算（藏起来的设置不生效），同名进程不适用。"""
        return (not self.is_collapsed() and self._kind != TARGET_NAME
                and self.launch_options.is_enabled())

    def launch_exe(self) -> str:
        return self.launch_options.exe()

    def set_launch_exe(self, path: str) -> None:
        self.launch_options.set_exe(path)

    def launch_cwd(self) -> str:
        return self.launch_options.cwd()

    def set_launch_cwd(self, path: str) -> None:
        self.launch_options.set_cwd(path)

    def set_launch_spec(self, spec: Optional[LaunchSpec], close_on_stop: bool = False) -> None:
        """回填启动设置；有设置时展开其余字段与启动区，让下一次"开始"看得见它。"""
        self.launch_options.set_spec(spec, close_on_stop)
        if spec is not None:
            self._show_details(True)
            self.launch_options.set_expanded(True)

    def show_launch_error(self, text: str) -> None:
        """在启动区下方就近显示启动错误（并展开它）。"""
        if self.is_collapsed():
            self._show_details(True)
        self.launch_options.set_error(text)

    def launch_error_text(self) -> str:
        return self.launch_options.error_text()

    def _on_launch_changed(self) -> None:
        self._sync_start_mode()

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

    def set_snapshot_dir(self, path: str) -> None:
        self.automation.set_snapshot_dir(path)

    def snapshot_dir(self) -> str:
        return self.automation.snapshot_dir()

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
        自动化设置有误时在自动化区就近显示错误、不发请求。勾选了"开始时先启动程序"时发出
        带 launch 的开始请求（不看进程选择），没填程序就近提示。"""
        if self.launch_enabled():
            self._submit_launch()
            return
        if self._kind == TARGET_NAME:
            self._submit_by_name()
            return
        if self.wait_enabled():
            name = self.picker.resolve_name()
            if isinstance(name, ResolveError):
                self.target_invalid.emit(name)
                return
            ok, plan = self._checked_plan()
            if not ok:
                return
            self.wait_requested.emit(WaitRequest(
                name=name, metrics=tuple(self.metrics()), interval=self.interval(),
                note=self.note(), plan=plan, target_kind=self._kind))
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
            metrics=tuple(self.metrics()), interval=self.interval(), note=self.note(),
            plan=plan, target_kind=self._kind))

    def _submit_by_name(self) -> None:
        """同名进程：只要进程名（选中的进程取其名，也可手输），进程不必此刻在运行。"""
        name = self.picker.resolve_name(ERROR_GROUP_NEEDS_NAME)
        if isinstance(name, ResolveError):
            self.target_invalid.emit(name)
            return
        ok, plan = self._checked_plan()
        if not ok:
            return
        self.start_requested.emit(TaskRequest(
            pid=0, name=name, create_time=None, metrics=tuple(self.metrics()),
            interval=self.interval(), note=self.note(), plan=plan, target_kind=TARGET_NAME))

    def _submit_launch(self) -> None:
        spec, error = self.launch_options.spec()
        if spec is None:
            self.show_launch_error(error or "")
            return
        ok, plan = self._checked_plan()
        if not ok:
            return
        self.launch_options.clear_error()
        self.start_requested.emit(TaskRequest(
            pid=0, name=spec.name, create_time=None,
            metrics=tuple(self.metrics()), interval=self.interval(), note=self.note(),
            plan=plan, launch=spec, close_on_stop=self.launch_options.close_on_stop(),
            target_kind=self._kind))

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
        仍生效会让下一次"开始"的行为出人意料（"启动被测程序"在折叠时同样不生效）。"""
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
        self._sync_start_mode()
        self.expand_button.setIcon(
            FluentIcon.UP if visible else FluentIcon.CHEVRON_DOWN_MED)
        self.expand_button.setToolTip(
            "收起" if visible else "展开采集间隔、指标、备注与自动化")
