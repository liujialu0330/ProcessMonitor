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
业务状态与查询在 AnalysisViewModel（异步、多指标并行、过期丢弃）；导出交互在
AnalysisExportFlow，右键打点编辑在 MarkerEditFlow；页面只把信号接到一起。
"""
import dataclasses
import logging
from typing import TYPE_CHECKING, Callable, List, Optional, Set

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, IndeterminateProgressBar

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
from ui.widgets.history.common import VIEWPORT_TO_CONTENT_OFFSET
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
        super().__init__("分析", parent=parent, spacing=SECTION_SPACING)
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
        self.auto_refresh = HistoryAutoRefresh(self.vm, self._should_skip_refresh, parent=self)
        self.marker_flow = MarkerEditFlow(self.vm, services, self, parent=self)
        self.export_flow = AnalysisExportFlow(
            self.vm, services, self, self.render_report, parent=self)
        self._connect()

    # ========== 组装 ==========

    def _init_ui(self) -> None:
        self.task_selector = TaskSelector(placeholder="选择要分析的监控任务")
        self.toolbar = AnalysisToolbar()
        self.toolbar.set_task_selector(self.task_selector)
        self.member_selector = MemberSelector()
        self.toolbar.set_member_selector(self.member_selector)
        self.add_section(self.toolbar)
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
        预设时间范围（刷选出的自定义范围是绝对时刻，不记）、视图与时间模式。"""
        vm = self.vm
        task_id = vm.current_task_id if not vm.selection_is_auto else None
        range_key = vm.range_key if vm.range_key != CUSTOM_RANGE_KEY else ""
        return AnalysisMemory(task_id=task_id or "", range_key=range_key,
                              view_mode=vm.view_mode, time_mode=vm.time_mode)

    def restore_memory(self, memory: AnalysisMemory) -> None:
        """启动时恢复上次的范围、视图与任务（任务在页面第一次显示、载入列表时才选中；
        已被删除就静默退回默认选择）。记录里不认识的取值一律忽略。"""
        vm = self.vm
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
        text = build_report_text(vm.current_task, vm.last_analysis, vm.range_description(),
                                 self.services.clock.now())
        return render_report_image(text, vm.last_analysis, origin=self._origin_epoch())

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
        self.compare_panel.set_options(options, keep_id=self.vm.compare_task_id)

    def _should_skip_refresh(self) -> bool:
        """定时刷新跳过：用户正在看图（悬停/拖动）、主窗口已最小化。"""
        if self.trend_panel.is_interacting():
            return True
        window = self.window()
        return window is not None and window.isMinimized()

    # ========== 视图模型 → 控件 ==========

    def _on_tasks_changed(self, tasks: list, running: Set[str], now) -> None:
        options = self._task_options.rebuild(tasks, running, now)
        self.task_selector.set_options(options, keep_id=self.vm.current_task_id)
        self.task_selector.setEnabled(bool(options))
        if self.compare_panel.isVisible():
            self._sync_compare_options()
        self._sync_delete_state()

    def _on_tasks_refreshed(self, tasks: list, running: Set[str], now) -> None:
        latest = {task.task_id: task for task in tasks}
        changed = self._task_options.refresh_running(now, running, latest)
        options = self._task_options.options()
        for index in changed:
            self.task_selector.update_option(index, options[index])
        self._sync_delete_state()

    def _on_selection_changed(self, task_id: Optional[str], metric: Optional[str],
                              _explicit: bool) -> None:
        self.task_selector.select_by_id(task_id, silent=True)
        self.summary_table.select_metric(self.vm.series_key(metric))
        self.toolbar.set_members_visible(self.vm.is_group_task)
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
        self.trend_panel.update_columns(self._content_width())
        self.trend_panel.set_analysis(analysis, keep_view=analysis.auto_refresh,
                                      order=order, focus=focus)

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
        if self.vm.delete_current():
            notifier.success(self, "删除成功", "该任务的历史数据已删除")
        else:
            notifier.error(self, "删除失败", "删除任务数据失败，请查看日志")

    @staticmethod
    def _delete_title(task) -> str:
        """确认框里的任务称呼：pid 任务沿用"进程名 (PID: n)"，进程组任务写目标类型。"""
        if getattr(task, 'target_kind', 'pid') == 'pid':
            return f"{task.process_name} (PID: {task.pid})"
        return task_title_text(task)

    # ========== 事件 ==========

    def _content_width(self) -> int:
        return self.viewport().width() - VIEWPORT_TO_CONTENT_OFFSET

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = self._content_width()
        self.toolbar.update_layout(width)
        self.trend_panel.update_columns(width)

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

