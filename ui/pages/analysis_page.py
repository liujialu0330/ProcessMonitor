"""
分析页（历史数据 + 导出合并）

只做组装与连线。结论优先，自上而下：
- 工具条：任务选择（可搜索下拉）· 对比任务 · 导出报告 ▾ · 刷新 · 更多；范围（含刷选出的
  "自定义"）与视图切换（小倍数 | 叠加Δ%，绝对 | 相对时间）；
- 结论条：判定胶囊 + "2 / 5 项持续上涨：…"，右侧是统计依据与运行中任务的自动刷新状态；
- 判定摘要表：按判定严重程度排序，每个指标一行，点击行切换焦点指标；
- 进程组任务（进程树 / 同名进程）：工具条多一个"成员"下拉（合计 · 各成员），选中某成员后
  整页（结论条、摘要表、焦点图、区间表、小图）改用 指标@成员键 的序列；合计视图下摘要表下方
  有"成员结论"折叠区（每个成员一行，点击切到该成员），结论条把持续上涨的成员一并点名；
  上限占比对进程组合计与多实例成员写"—"；
- 任务对比（可选）：叠加曲线 + 并排分轮增量表；
- 焦点区：[打点区间（点击行缩放趋势图）| 采集明细] 页签，其下焦点指标大图与概览条刷选；
- 其余指标：持续上涨 / 波动的小倍数图，平稳与数据不足的折叠为一行（点一行设为焦点），
  或叠加 Δ%；右键编辑打点。
1280×820 窗口首屏能同时看到结论条、摘要表与区间表前 3 行。
响应式：页面按自身宽度（内容区）分 compact / regular / wide 三档（PageScaffold.breakpoints），
工具条、结论条、摘要表与小倍数图订阅档位变化各自调整（摘要表的列、小图的列数、范围下拉等），
页面只在这里把订阅接上，不写宽度判断。
业务状态与查询在 AnalysisViewModel（异步、多指标并行、过期丢弃）；导出交互在
AnalysisExportFlow，右键打点编辑在 MarkerEditFlow；页面只把信号接到一起。
"""
import dataclasses
import logging
from typing import TYPE_CHECKING, Callable, List, Optional, Set

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, IndeterminateProgressBar, TogglePushButton

from core.history_query import CUSTOM_RANGE_KEY, RANGE_KEYS, HistorySnapshot, TaskAnalysis
from ui.formatting import (
    GROUP_USAGE_NOTE, MEMBER_USAGE_NOTE, basis_text, build_member_rows, build_table_row,
    group_verdict_summary, member_view_summary, overall_summary, refresh_status_text,
    task_overall_verdict, task_title_text, verdict_summary)
from ui.models.task_options import TaskOptionsModel
from ui.services import UiServices
from ui.viewmodels.analysis_vm import (
    TIME_MODES, TIME_RELATIVE, VIEW_MODES, AnalysisViewModel, analysis_origin, range_text)
from ui.viewmodels.history_auto_refresh import HistoryAutoRefresh
from ui.viewmodels.history_vm import EMPTY_NO_TASKS, HistoryRunner, ordered_metrics
from ui.widgets.analysis.compare_panel import ComparePanel
from ui.widgets.analysis.reference_controls import ReferenceControls
from ui.widgets.analysis.task_library import TaskLibraryPanel
from ui.widgets.analysis.export_flow import AnalysisExportFlow
from ui.widgets.analysis.interval_table import LowerViews
from ui.widgets.analysis.marker_menu import MarkerEditFlow
from ui.widgets.analysis.member_selector import MemberSelector
from ui.widgets.analysis.member_verdicts import MemberVerdictPanel
from ui.widgets.analysis.report_sheet import build_report_text, render_report_image
from ui.widgets.analysis.summary_table import SummaryTable
from ui.widgets.analysis.toolbar import AnalysisToolbar
from ui.widgets.analysis.trend_panel import TrendPanel, center_line
from ui.widgets.analysis.verdict_banner import VerdictBanner
from ui.widgets.empty_state import EmptyStateCard
from ui.widgets.history.chart_view import chart_display_unit
from ui.widgets.page_scaffold import PageScaffold
from ui.widgets.task_selector import TaskSelector
from ui.window_state import AnalysisMemory
from utils.metrics import PROCESS_COUNT, VALUE_CLASS_RATE, split_member_key

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager
    from data.database import Database

logger = logging.getLogger(__name__)

SECTION_SPACING = 16
CARD_SPACING = 12
BUSY_INDICATOR_DELAY_MS = 300
# 结论条"自动刷新 · N 秒前"的走字间隔
REFRESH_STATUS_TICK_MS = 1000
TASK_HAS_NO_DATA = ("提示", "该任务暂无采样数据")


class AnalysisPage(PageScaffold):
    """分析页（db 只读；打点编辑、删除任务数据与导出登记经 manager）。"""

    def __init__(self, db: "Database", manager: "MonitorManager",
                 services: UiServices, parent=None, *,
                 worker_runner: Optional[HistoryRunner] = None,
                 defer: Optional[Callable[[Callable[[], None]], None]] = None,
                 **vm_options):
        """worker_runner / defer / vm_options 透传给 AnalysisViewModel（测试注入同步执行器）。"""
        super().__init__("分析", "查看趋势与每轮变化，对比测试结果并导出证据", parent=parent, spacing=SECTION_SPACING)
        self.setObjectName("analysisPage")
        self.db, self.manager, self.services = db, manager, services
        self.vm = AnalysisViewModel(
            db, manager, services, worker_runner=worker_runner, defer=defer,
            parent=self, **vm_options)
        self._task_options = TaskOptionsModel()
        self._compare_options = TaskOptionsModel()
        # 最近一次拿到分析结果的时刻（结论条的"N 秒前"）
        self._analysis_at = None
        self._init_ui()
        self.breakpoints.tier_changed.connect(self._apply_tier)
        self._apply_tier(self.breakpoints.tier)
        self.auto_refresh = HistoryAutoRefresh(self.vm, self._should_skip_refresh, parent=self)
        self.marker_flow = MarkerEditFlow(self.vm, services, self, parent=self)
        self.export_flow = AnalysisExportFlow(
            self.vm, services, self, self.render_report, parent=self)
        self._connect()
        self._set_tab_order()

    # ========== 组装 ==========

    def _init_ui(self) -> None:
        self.task_selector = TaskSelector(placeholder="选择要分析的监控任务")
        self.task_selector.set_grouped(True)
        self.toolbar = AnalysisToolbar()
        self.toolbar.set_task_selector(self.task_selector)
        self.member_selector = MemberSelector()
        self.toolbar.set_member_selector(self.member_selector)
        self.add_section(self.toolbar)
        self.reference_controls = ReferenceControls(self.manager, self.services)
        self.task_library = TaskLibraryPanel(self.manager, self.services)
        self.add_section(self.reference_controls)
        self.add_section(self.task_library)
        # 编辑与批量管理按需展开，首屏优先显示当前任务的结论和区间数据。
        self.reference_toggle = TogglePushButton(FluentIcon.TAG, '场景与参考', self)
        self.reference_toggle.setToolTip('编辑测试场景、版本和参考基线')
        self.reference_toggle.toggled.connect(self.reference_controls.setVisible)
        self.add_header_widget(self.reference_toggle)
        self.reference_controls.hide()
        self.task_library.layout().removeWidget(self.task_library.toggle_button)
        self.add_header_widget(self.task_library.toggle_button)
        self.task_library.toggle_button.clicked.connect(self._sync_task_library_visibility)
        self.task_library.hide()
        self.analysis_hint = CaptionLabel('')
        self.analysis_hint.setWordWrap(True)
        self.analysis_hint.hide()
        self.add_section(self.analysis_hint)
        self.export_status_label = CaptionLabel("")
        self.export_status_label.setVisible(False)
        self.add_section(self.export_status_label)

        self.verdict_banner = VerdictBanner()
        # 依据并入结论条，摘要表不再单列标题与依据行
        self.summary_table = SummaryTable(show_header=False)
        self.member_panel = MemberVerdictPanel()
        self.compare_panel = ComparePanel()
        self.compare_panel.setVisible(False)
        self.lower_views = LowerViews()
        self.interval_table = self.lower_views.interval_table
        self.sample_table = self.lower_views.sample_table
        self.trend_panel = TrendPanel()

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(CARD_SPACING)
        for widget, stretch in ((self.verdict_banner, 0), (self.summary_table, 0),
                                (self.member_panel, 0), (self.compare_panel, 0), (self.lower_views, 0),
                                (self.trend_panel, 1)):
            body_layout.addWidget(widget, stretch)

        self.empty_state_card = EmptyStateCard(FluentIcon.HISTORY, *EMPTY_NO_TASKS)
        self.content_stack = QStackedWidget()
        self.content_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.content_stack.addWidget(self.empty_state_card)
        self.content_stack.addWidget(self.body)

        self.busy_bar = IndeterminateProgressBar(start=False)
        policy = self.busy_bar.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.busy_bar.setSizePolicy(policy)
        self.busy_bar.setVisible(False)
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.setInterval(BUSY_INDICATOR_DELAY_MS)
        self._busy_timer.timeout.connect(self._show_busy_bar)
        self._refresh_status_timer = QTimer(self)
        self._refresh_status_timer.setInterval(REFRESH_STATUS_TICK_MS)
        self._refresh_status_timer.timeout.connect(self.update_refresh_status)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)
        content_layout.addWidget(self.busy_bar)
        content_layout.addWidget(self.content_stack, 1)
        self.add_section(content, 1)

    def _connect(self) -> None:
        vm, bar, trend = self.vm, self.toolbar, self.trend_panel
        # 控件 → 视图模型
        self.task_selector.task_selected.connect(self._on_task_selected)
        self.reference_controls.identity_changed.connect(self._refresh_task_identity)
        self.reference_controls.compare_requested.connect(self._compare_reference)
        self.task_library.task_selected.connect(self.select_task)
        self.task_library.changed.connect(vm.reload_tasks)
        vm.reference_ready.connect(self._on_reference_ready)

        bar.range_changed.connect(self._on_range_changed)
        bar.view_mode_changed.connect(vm.set_view_mode)
        bar.time_mode_changed.connect(vm.set_time_mode)
        bar.refresh_clicked.connect(vm.refresh)
        bar.compare_toggled.connect(self._set_compare_open)
        bar.export_requested.connect(self.export_flow.run)
        bar.raw_values_toggled.connect(self.sample_table.set_raw_values)
        bar.delete_clicked.connect(self._on_delete_clicked)
        self.summary_table.metric_selected.connect(vm.select_metric)
        self.member_selector.member_selected.connect(vm.select_member)
        self.member_panel.member_selected.connect(vm.select_member)
        self.compare_panel.task_selected.connect(vm.set_compare_task)
        self.compare_panel.tasks_selected.connect(vm.set_compare_tasks)
        self.compare_panel.export_requested.connect(self._export_comparison)
        self.compare_panel.close_requested.connect(lambda: self._set_compare_open(False))
        trend.region_selected.connect(vm.set_custom_range)
        trend.context_menu_requested.connect(self.marker_flow.show_menu)
        trend.metric_activated.connect(vm.select_metric)
        self.interval_table.interval_activated.connect(
            lambda start, end: trend.zoom_to(start.timestamp(), end.timestamp()))
        self.verdict_banner.copy_requested.connect(self.export_flow.copy_conclusion)
        self.export_flow.status_changed.connect(self._on_export_status)
        # 视图模型 → 控件
        vm.tasks_changed.connect(self._on_tasks_changed)
        vm.tasks_refreshed.connect(self._on_tasks_refreshed)
        vm.delete_busy_changed.connect(lambda _busy: self._sync_delete_state())
        vm.delete_succeeded.connect(lambda _task_id: self.services.notifier.success(
            self, '删除成功', '该任务的历史数据已删除'))
        vm.delete_failed.connect(lambda message: self.services.notifier.error(
            self, '删除失败', message))
        vm.selection_changed.connect(self._on_selection_changed)
        vm.analysis_ready.connect(self._on_analysis)
        vm.members_changed.connect(self._on_members_changed)
        vm.member_selection_changed.connect(self._on_member_selection)
        vm.snapshot_ready.connect(self._on_snapshot)
        vm.empty_state.connect(self._on_empty_state)
        vm.busy.connect(self._on_busy)
        vm.baseline_changed.connect(bar.set_baseline_available)
        vm.range_reset.connect(bar.set_range)
        vm.custom_range_changed.connect(self._on_custom_range)
        vm.view_mode_changed.connect(self._on_view_mode)
        vm.time_mode_changed.connect(self._on_time_mode)
        vm.compare_ready.connect(self.compare_panel.set_result)
        vm.export_busy.connect(lambda busy: bar.set_export_enabled(not busy))
        vm.error.connect(lambda title, content: self.services.notifier.error(self, title, content))

    def _set_tab_order(self):
        controls = [self.reference_toggle, self.task_library.toggle_button,
                    self.task_selector.search_edit, self.task_selector.combo,
                    self.toolbar.compare_button, self.toolbar.export_button,
                    self.toolbar.refresh_button, self.toolbar.more_button,
                    self.toolbar.range_combo, self.toolbar.view_menu_button,
                    self.reference_controls.scenario_edit, self.reference_controls.build_edit,
                    self.reference_controls.save_button, self.reference_controls.reference_button,
                    self.reference_controls.star_button, self.reference_controls.compare_button,
                    self.summary_table.table,
                    self.interval_table.table]
        for first, second in zip(controls, controls[1:]):
            QWidget.setTabOrder(first, second)

    def _sync_task_library_visibility(self) -> None:
        self.task_library.setVisible(not self.task_library.body.isHidden())

    def _apply_tier(self, tier: str) -> None:
        """档位变化：各控件自己决定怎么调整（页面不判断宽度）。"""
        self.toolbar.set_tier(tier)
        self.verdict_banner.set_tier(tier)
        self.summary_table.set_tier(tier)
        self.trend_panel.set_tier(tier)
        self.interval_table.set_tier(tier)

    # ========== 公开接口 ==========

    @property
    def current_task_id(self) -> Optional[str]:
        return self.vm.current_task_id

    @property
    def current_metric(self) -> Optional[str]:
        return self.vm.current_metric

    def select_task(self, task_id: str) -> None:
        """选中指定任务并载入其数据（主窗口从监控页"分析"跳转时调用）。
        任务尚无采样数据时不在列表中：清空选择并提示，不静默选中其他任务。"""
        if not self.vm.select_task(task_id, explicit=True):
            self.services.notifier.info(self, *TASK_HAS_NO_DATA)

    def memory_snapshot(self) -> AnalysisMemory:
        """要记住的状态：用户亲手选过的任务（默认自动选中的最新任务不记，下次仍自动跟随）、
        预设时间范围（刷选出的自定义范围是绝对时刻，不记）、视图与时间模式、摘要表用户勾选的列。"""
        vm = self.vm
        task_id = vm.current_task_id if not vm.selection_is_auto else None
        range_key = vm.range_key if vm.range_key != CUSTOM_RANGE_KEY else ""
        return AnalysisMemory(task_id=task_id or "", range_key=range_key,
                              view_mode=vm.view_mode, time_mode=vm.time_mode,
                              summary_columns=self.summary_table.user_columns())

    def restore_memory(self, memory: AnalysisMemory) -> None:
        """启动时恢复上次的范围、视图与任务（任务在页面第一次显示、载入列表时才选中；
        已被删除就静默退回默认选择）。记录里不认识的取值一律忽略。"""
        vm = self.vm
        self.summary_table.set_user_columns(memory.summary_columns)
        if memory.range_key in RANGE_KEYS:
            self.toolbar.set_range(memory.range_key)
            vm.set_range(memory.range_key)
        if memory.view_mode in VIEW_MODES:
            vm.set_view_mode(memory.view_mode)
        if memory.time_mode in TIME_MODES:
            vm.set_time_mode(memory.time_mode)
        if memory.task_id:
            vm.restore_selection(memory.task_id)

    def render_report(self) -> QImage:
        """按当前分析结果离屏渲染报告图片（导出与复制共用）。"""
        vm = self.vm
        analysis = vm.export_analysis()
        text = build_report_text(vm.current_task, analysis, vm.range_description(),
                                 self.services.clock.now())
        return render_report_image(text, analysis, origin=self._origin_epoch())

    def refresh(self):
        self.vm.refresh()

    def export_report(self):
        return self.export_flow.run('report')

    def copy_report_image(self):
        return self.export_flow.run('copy')

    def focus_search(self):
        search = getattr(self.task_selector, 'search_edit', None)
        (search if search is not None else self.task_selector.combo).setFocus()

    def select_range_index(self, index):
        from ui.widgets.analysis.toolbar import RANGE_OPTIONS
        if not 0 <= index < len(RANGE_OPTIONS):
            return
        key = RANGE_OPTIONS[index][0]
        item = self.toolbar.range_segmented.items[key]
        if item.isEnabled():
            self.toolbar.set_range(key)
            if key != CUSTOM_RANGE_KEY:
                self.vm.set_range(key)

    def _export_comparison(self, kind):
        return self.export_flow.run_comparison(kind, self.compare_panel)

    # ========== 控件 → 视图模型 ==========

    def _on_task_selected(self, task_id: Optional[str], explicit: bool) -> None:
        if task_id is not None:
            self.vm.select_task(task_id, explicit)

    def _on_range_changed(self, key: str) -> None:
        if key != CUSTOM_RANGE_KEY:
            self.vm.set_range(key)

    def _set_compare_open(self, opened: bool) -> None:
        self.toolbar.set_compare_checked(opened)
        self.compare_panel.setVisible(opened)
        if opened:
            self._sync_compare_options()
        else:
            self.vm.set_compare_task(None)

    def _sync_compare_options(self) -> None:
        options = self._compare_options.rebuild(
            self.vm.compare_candidates(), self.vm.running_ids, self.services.clock.now())
        self.compare_panel.set_options(options, keep_id=self.vm.compare_task_id,
                                       keep_ids=self.vm.compare_task_ids)

    def _should_skip_refresh(self) -> bool:
        """定时刷新跳过：用户正在看图（悬停/拖动）、主窗口已最小化。"""
        if self.trend_panel.is_interacting():
            return True
        window = self.window()
        return window is not None and window.isMinimized()

    # ========== 视图模型 → 控件 ==========

    def _on_tasks_changed(self, tasks: list, running: Set[str], now) -> None:
        self.task_library.set_tasks(tasks)
        options = self._task_options.rebuild(tasks, running, now)
        self.task_selector.set_options(options, keep_id=self.vm.current_task_id)
        self.task_selector.setEnabled(bool(options))
        if self.compare_panel.isVisible():
            self._sync_compare_options()
        self._sync_delete_state()

    def _on_tasks_refreshed(self, tasks: list, running: Set[str], now) -> None:
        self.task_library.refresh_tasks(tasks)
        latest = {task.task_id: task for task in tasks}
        changed = self._task_options.refresh_running(now, running, latest)
        options = self._task_options.options()
        for index in changed:
            self.task_selector.update_option(index, options[index])
        self._sync_delete_state()

    def _on_selection_changed(self, task_id: Optional[str], metric: Optional[str],
                              _explicit: bool) -> None:
        self.task_selector.select_by_id(task_id, silent=True)
        self.reference_controls.set_task(self.vm.current_task)
        reference = self.vm.last_reference
        current = self.vm.current_task
        keep_reference = (reference is not None and current is not None
                          and reference.task_id == task_id
                          and reference.scenario == current.scenario
                          and self.vm.current_member is None)
        # 换焦点指标复用同一份分析，不会再次发 analysis_ready；参考列也应复用。
        self.summary_table.set_reference(reference if keep_reference else None)
        self.summary_table.select_metric(self.vm.series_key(metric))
        self.toolbar.set_members_visible(self.vm.is_group_task)
        if self.compare_panel.isVisible():
            self._sync_compare_options()
        self._sync_delete_state()

    def _on_members_changed(self, options: list) -> None:
        self.member_selector.set_options(options, keep=self.vm.current_member)
        self.toolbar.set_members_visible(self.vm.is_group_task)

    def _on_member_selection(self, member: Optional[str]) -> None:
        self.member_selector.select(member)

    def _sync_delete_state(self) -> None:
        self.toolbar.set_delete_enabled(self.vm.can_delete, self.vm.delete_hint)

    def _origin_epoch(self) -> Optional[float]:
        if self.vm.time_mode != TIME_RELATIVE or self.vm.last_analysis is None:
            return None
        snapshot = next((s for s in self.vm.shown_snapshots()), None)
        origin = analysis_origin(snapshot)
        return origin.timestamp() if origin is not None else None

    def _on_analysis(self, analysis: TaskAnalysis) -> None:
        """analysis 是当前视图的结果：合计视图的指标键是指标本身，成员视图是 指标@成员键。"""
        vm = self.vm
        order = ordered_metrics(analysis)
        snapshots = [(m, analysis.snapshots[m]) for m in order]
        basis = next((basis_text(s) for _m, s in snapshots if s.stats), "")
        rows = self._table_rows(snapshots, analysis)
        focus = vm.series_key(vm.current_metric)
        self.summary_table.set_rows(rows, basis, focus=focus)
        self.verdict_banner.set_summary(self._banner_summary(snapshots, analysis))
        self.verdict_banner.set_basis(basis)
        self._analysis_at = self.services.clock.now()
        self.update_refresh_status()
        self.trend_panel.set_relative_origin(self._origin_epoch())
        self.trend_panel.set_analysis(analysis, keep_view=analysis.auto_refresh,
                                      order=order, focus=focus)
        self._update_analysis_hint(analysis)
        vm.refresh_reference(vm.last_analysis)


    def _refresh_task_identity(self, task_id):
        self.vm.reload_tasks()
        if self.vm.current_task_id == task_id:
            self.reference_controls.set_task(self.vm.current_task)
            self.vm.refresh_reference(self.vm.last_analysis)

    def _compare_reference(self, task_id):
        self._set_compare_open(True)
        self.vm.set_compare_task(task_id)

    def _on_reference_ready(self, comparison):
        self.reference_controls.set_result(comparison)
        self.summary_table.set_reference(comparison if self.vm.current_member is None else None)

    def _update_analysis_hint(self, analysis):
        snapshots = list(analysis.snapshots.values())
        hints = []
        if snapshots and all(s.baseline is None for s in snapshots):
            hints.append('尚未设置基线：预热稳定后打一次点，或在趋势图上右键设为基线')
        judged = [s for s in snapshots if s.judgement is not None]
        if judged and all(str(s.judgement.verdict) == 'insufficient' for s in judged):
            snapshot = judged[0]
            stats = snapshot.stats or {}
            count = stats.get('count', 0)
            span = stats.get('span_seconds', 0)
            required = max(600, 30 * snapshot.interval_seconds)
            hints.append(f'趋势数据不足：已有 {count} / 30 个样本，'
                         f'跨度 {span / 60:.1f} / {required / 60:.1f} 分钟')
        self.analysis_hint.setText('；'.join(hints))
        self.analysis_hint.setVisible(bool(hints))

    def _table_rows(self, snapshots: list, analysis: TaskAnalysis) -> list:
        """摘要表各行。进程组的上限占比写"—"（GDI/USER 的 10000 是单个进程的口径）：合计
        一律如此，成员在有多个实例时如此；单实例成员保留单进程口径。成员视图的行用
        指标@成员键作 metric，点击与焦点就都落在成员的序列上。"""
        vm = self.vm
        member = vm.current_member
        if member is None:
            note = GROUP_USAGE_NOTE if vm.is_group_task else None
            return [build_table_row(m, s, usage_note=note) for m, s in snapshots]
        count = self._member_instance_count(analysis, member)
        note = MEMBER_USAGE_NOTE.format(count=count) if count > 1 else None
        return [dataclasses.replace(
                    build_table_row(split_member_key(m)[0], s, usage_note=note), metric=m)
                for m, s in snapshots]

    def _member_instance_count(self, analysis: TaskAnalysis, member: str) -> int:
        """成员当前的实例数：取"进程总数@成员"最后一个值；任务没采这个指标时退回出现过
        的实例数（偏保守：宁可不写占比也不拿合计去比单进程上限）。"""
        snapshot = analysis.snapshot(f"{PROCESS_COUNT}@{member}")
        if snapshot is not None and snapshot.stats:
            return int(round(snapshot.stats['last']))
        return self.vm.member_instances(member) or 1

    def _banner_summary(self, snapshots: list, analysis: TaskAnalysis):
        """结论条：合计视图在组任务里并入成员结论（合计或任一成员持续上涨即算上涨，点名
        上涨的成员）；成员视图在主句前点名成员；pid 任务不变。合计视图最后再叠加整体判定
        （逐启动段、中途崩溃，与 pmctl、证据包同一口径）。"""
        vm = self.vm
        items = [(split_member_key(m)[0], s if s.points else None) for m, s in snapshots]
        summary = verdict_summary(items)
        if vm.current_member is not None:
            self.member_panel.clear()
            return member_view_summary(vm.member_name(vm.current_member), summary)
        if vm.is_group_task:
            rows = self._member_rows()
            self.member_panel.set_rows(rows)
            summary = group_verdict_summary(summary, rows)
        else:
            self.member_panel.clear()
        return overall_summary(summary, analysis, task_overall_verdict(vm.current_task, analysis))

    def _member_rows(self) -> list:
        vm = self.vm
        names = {o.key: o.name for o in vm.member_options}
        instances = {o.key: o.instances for o in vm.member_options}
        return build_member_rows(vm.last_analysis, names, instances)

    def _on_snapshot(self, snapshot: HistorySnapshot) -> None:
        """snapshot 的指标键在成员视图里是 指标@成员键。"""
        metric = snapshot.request.metric
        self.trend_panel.set_focus(metric)
        chart = self.trend_panel.charts().get(metric)
        if chart is not None and not chart.is_rate:
            unit = chart.display_unit
        else:
            unit = chart_display_unit(metric, [p.value for p in snapshot.buckets])
        self.interval_table.set_intervals(metric, snapshot.intervals, snapshot.markers, unit)
        baseline = snapshot.baseline
        self.sample_table.set_points(
            snapshot.points, metric, snapshot.total_count or None, unit,
            baseline_value=baseline.value if baseline is not None else None)
        if self.vm.range_key != CUSTOM_RANGE_KEY:
            rate = snapshot.value_class == VALUE_CLASS_RATE
            self.trend_panel.overview.set_series(
                snapshot.rate_series if rate else center_line(snapshot))
        self.summary_table.select_metric(metric)
        self.content_stack.setCurrentWidget(self.body)

    def _on_empty_state(self, title: str, hint: str) -> None:
        self.analysis_hint.hide()
        self.reference_controls.set_task(self.vm.current_task)
        self.summary_table.set_reference(None)
        self._analysis_at = None
        self.verdict_banner.clear()
        self.summary_table.clear()
        self.member_panel.clear()
        self.trend_panel.clear()
        self.lower_views.clear()
        self.empty_state_card.set_message(title, hint)
        self.content_stack.setCurrentWidget(self.empty_state_card)

    def _on_custom_range(self, start, end) -> None:
        if start is None:
            self.toolbar.set_custom_range_text(None)
            self.trend_panel.overview.set_region(None)
            return
        self.toolbar.set_custom_range_text(range_text(CUSTOM_RANGE_KEY, (start, end)))
        self.toolbar.set_range(CUSTOM_RANGE_KEY)
        self.trend_panel.overview.set_region((start, end))

    def _on_view_mode(self, mode: str) -> None:
        self.toolbar.set_view_mode(mode)
        self.trend_panel.set_view_mode(mode)

    def _on_time_mode(self, mode: str) -> None:
        self.toolbar.set_time_mode(mode)
        self.trend_panel.set_relative_origin(self._origin_epoch())

    def _on_busy(self, busy: bool) -> None:
        self.toolbar.refresh_button.setEnabled(not busy)
        if busy:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self.busy_bar.stop()
            self.busy_bar.setVisible(False)

    def update_refresh_status(self) -> None:
        """结论条右侧的"自动刷新 · N 秒前"：只对运行中任务显示（定时走字，结果到达时也更新）。"""
        vm = self.vm
        running = vm.current_task_id is not None and vm.current_task_id in vm.running_ids
        if not running or self._analysis_at is None:
            self.verdict_banner.set_refresh_status("")
            return
        elapsed = (self.services.clock.now() - self._analysis_at).total_seconds()
        self.verdict_banner.set_refresh_status(refresh_status_text(elapsed))

    def _show_busy_bar(self) -> None:
        self.busy_bar.setVisible(True)
        self.busy_bar.start()

    def _on_export_status(self, text: str) -> None:
        self.export_status_label.setText(text)
        self.export_status_label.setVisible(bool(text))

    # ========== 需要对话框的操作 ==========

    def _on_delete_clicked(self) -> None:
        """删除此任务数据：确认（默认焦点在取消）→ 视图模型经 manager 删除并重载。"""
        task, notifier = self.vm.current_task, self.services.notifier
        if task is None:
            return
        if not self.vm.can_delete:
            notifier.warning(self, "提示", self.vm.delete_hint, duration=2000)
            return
        if not self.services.dialogs.confirm(
                self.window(), "确认删除",
                f"确定要删除任务「{self._delete_title(task)}」的全部历史数据吗？"
                "此操作不可恢复。", "删除"):
            return
        reference = bool(getattr(task, 'is_reference', False))
        if reference and not self.services.dialogs.confirm(
                self.window(), '删除场景参考',
                f'该任务是场景「{task.scenario}」的参考。删除后，该场景将没有参考任务。', '删除参考'):
            return
        self.vm.delete_current(expected_task_id=task.task_id, allow_reference=reference)

    @staticmethod
    def _delete_title(task) -> str:
        """确认框里的任务称呼：pid 任务沿用"进程名 (PID: n)"，进程组任务写目标类型。"""
        if getattr(task, 'target_kind', 'pid') == 'pid':
            return f"{task.process_name} (PID: {task.pid})"
        return task_title_text(task)

    # ========== 事件 ==========

    def showEvent(self, event):
        """每次显示都重新载入任务列表，并开始运行中任务的自动刷新计时。"""
        super().showEvent(event)
        self.vm.reload_tasks()
        self.auto_refresh.start()
        self._refresh_status_timer.start()

    def hideEvent(self, event):
        """隐藏时停止自动刷新，并让在途查询作废（不阻塞等待线程）。"""
        super().hideEvent(event)
        self.auto_refresh.stop()
        self._refresh_status_timer.stop()

