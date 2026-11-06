"""
历史数据页面

只做组装与连线：筛选条（内嵌任务选择器）、统计条、趋势图 / 采样明细、空状态卡，
加上视图模型（HistoryViewModel：选择语义、异步查询、空状态与错误）和自动刷新调度
（HistoryAutoRefresh）。控件信号转成视图模型命令，视图模型信号转成控件更新；
查库、选择规则、过期判断都不在这里。需要对话框与提示的交互（删除确认、保存/复制
图表）由页面经 services 完成。
"""
import logging
from typing import TYPE_CHECKING, Callable, List, Optional, Set

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, IndeterminateProgressBar

from core.history_query import HistorySnapshot
from ui.models.task_options import TaskOptionsModel
from ui.services import UiServices
from ui.viewmodels.history_auto_refresh import HistoryAutoRefresh
from ui.viewmodels.history_vm import (
    EMPTY_NO_TASKS, HistoryRunner, HistoryViewModel)
from ui.widgets.empty_state import EmptyStateCard
from ui.widgets.history.chart_view import ChartView
from ui.widgets.history.common import VIEWPORT_TO_CONTENT_OFFSET
from ui.widgets.history.filter_bar import VIEW_TREND, FilterBar
from ui.widgets.history.sample_table import SampleTable
from ui.widgets.history.stats_strip import StatsStrip
from ui.widgets.page_scaffold import PageScaffold
from ui.widgets.task_selector import TaskSelector
from utils.metrics import get_metric_display_name

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager
    from data.database import Database

logger = logging.getLogger(__name__)

# 区块间距：筛选条与分析区之间 16；统计条与图表/明细卡片之间 12
SECTION_SPACING = 16
CARD_SPACING = 12
# 加载超过这个时长才显示进度条，避免快速查询时进度条一闪而过
BUSY_INDICATOR_DELAY_MS = 300

NO_CHART_TO_SAVE = ("提示", "当前无图表数据可导出")
NO_CHART_TO_COPY = ("提示", "当前无图表数据可复制")
TASK_HAS_NO_DATA = ("提示", "该任务暂无采样数据")


class HistoryPage(PageScaffold):
    """历史数据页面（db 只读；删除任务数据经 manager）。"""

    def __init__(self, db: "Database", manager: "MonitorManager",
                 services: UiServices, parent=None, *,
                 worker_runner: Optional[HistoryRunner] = None,
                 defer: Optional[Callable[[Callable[[], None]], None]] = None,
                 **vm_options):
        """
        Args:
            db / manager / services: 与其他页面相同的注入
            worker_runner / defer / vm_options: 透传给 HistoryViewModel（测试注入同步
                执行器、立即执行的 defer、表格与分桶上限）；生产用默认值
        """
        super().__init__("历史数据", parent=parent, spacing=SECTION_SPACING)
        self.setObjectName("historyPage")
        self.db, self.manager, self.services = db, manager, services

        self.vm = HistoryViewModel(
            db, manager, services, worker_runner=worker_runner, defer=defer,
            parent=self, **vm_options)
        self._task_options = TaskOptionsModel()
        self._init_ui()
        self.auto_refresh = HistoryAutoRefresh(
            self.vm, self._should_skip_refresh, parent=self)
        self._connect()

    # ========== 组装 ==========

    def _init_ui(self) -> None:
        self.task_selector = TaskSelector(placeholder="选择要查看的监控任务")
        self.filter_bar = FilterBar()
        self.filter_bar.set_task_selector(self.task_selector)
        self.add_section(self.filter_bar)

        self.stats_strip = StatsStrip()
        self.chart_view = ChartView()
        self.sample_table = SampleTable()
        self.view_stack = QStackedWidget()
        self.view_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view_stack.addWidget(self.chart_view)
        self.view_stack.addWidget(self.sample_table)

        self.analysis_page = QWidget()
        analysis_layout = QVBoxLayout(self.analysis_page)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.setSpacing(CARD_SPACING)
        analysis_layout.addWidget(self.stats_strip)
        analysis_layout.addWidget(self.view_stack, 1)

        self.empty_state_card = EmptyStateCard(FluentIcon.HISTORY, *EMPTY_NO_TASKS)

        # 不要给内容栈设最小高度：显式值会覆盖布局算出的最小尺寸（统计条 + 图表），
        # 小窗口下滚动区不再出现滚动条，图表被压出卡片、横轴被裁掉
        self.content_stack = QStackedWidget()
        self.content_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.content_stack.addWidget(self.empty_state_card)
        self.content_stack.addWidget(self.analysis_page)

        # 加载条放在内容区顶部；隐藏时仍占位，显示/隐藏不推动下方卡片
        self.busy_bar = IndeterminateProgressBar(start=False)
        policy = self.busy_bar.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.busy_bar.setSizePolicy(policy)
        self.busy_bar.setVisible(False)
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.setInterval(BUSY_INDICATOR_DELAY_MS)
        self._busy_timer.timeout.connect(self._show_busy_bar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)
        content_layout.addWidget(self.busy_bar)
        content_layout.addWidget(self.content_stack, 1)
        self.add_section(content, 1)

    def _connect(self) -> None:
        vm, bar = self.vm, self.filter_bar
        # 控件信号 → 视图模型命令（程序设置控件时一律不发这些信号）
        self.task_selector.task_selected.connect(self._on_task_selected)
        bar.metric_selected.connect(vm.select_metric)
        bar.range_changed.connect(vm.set_range)
        bar.refresh_clicked.connect(vm.refresh)
        bar.view_changed.connect(self._on_view_changed)
        bar.raw_values_toggled.connect(self.sample_table.set_raw_values)
        bar.delete_clicked.connect(self._on_delete_clicked)
        self.chart_view.save_requested.connect(self._save_chart_as_png)
        self.chart_view.copy_requested.connect(self._copy_chart_image)
        # 视图模型信号 → 控件
        vm.tasks_changed.connect(self._on_tasks_changed)
        vm.tasks_refreshed.connect(self._on_tasks_refreshed)
        vm.metrics_changed.connect(self._on_metrics_changed)
        vm.selection_changed.connect(self._on_selection_changed)
        vm.snapshot_ready.connect(self._on_snapshot)
        vm.empty_state.connect(self._on_empty_state)
        vm.busy.connect(self._on_busy)
        vm.error.connect(lambda title, content: self.services.notifier.error(
            self, title, content))

    # ========== 公开接口 ==========

    @property
    def current_task_id(self) -> Optional[str]:
        return self.vm.current_task_id

    @property
    def current_metric(self) -> Optional[str]:
        return self.vm.current_metric

    def select_task(self, task_id: str) -> None:
        """选中指定任务并载入其数据（主窗口从监控页"查看历史"跳转时调用）。

        任务尚无采样数据时不在列表中：清空选择并提示，不静默选中其他任务。
        """
        if not self.vm.select_task(task_id, explicit=True):
            self.services.notifier.info(self, *TASK_HAS_NO_DATA)

    # ========== 控件 → 视图模型 ==========

    def _on_task_selected(self, task_id: Optional[str], explicit: bool) -> None:
        if task_id is not None:
            self.vm.select_task(task_id, explicit)

    def _on_view_changed(self, key: str) -> None:
        self.view_stack.setCurrentWidget(
            self.chart_view if key == VIEW_TREND else self.sample_table)

    def _should_skip_refresh(self) -> bool:
        """定时刷新是否跳过：明细视图（图表/统计不可见）、用户正在看图（悬停/拖动）、
        主窗口已最小化。"""
        if self.filter_bar.current_view() != VIEW_TREND or self.chart_view.is_interacting():
            return True
        window = self.window()
        return window is not None and window.isMinimized()

    # ========== 视图模型 → 控件 ==========

    def _on_tasks_changed(self, tasks: list, running: Set[str], now) -> None:
        options = self._task_options.rebuild(tasks, running, now)
        # 选中项随后由 selection_changed 给出；这里先按旧选中恢复，避免闪成占位文字
        self.task_selector.set_options(options, keep_id=self.vm.current_task_id)
        self.task_selector.setEnabled(bool(options))
        self._sync_delete_state()

    def _on_tasks_refreshed(self, tasks: list, running: Set[str], now) -> None:
        latest = {task.task_id: task for task in tasks}
        changed = self._task_options.refresh_running(now, running, latest)
        options = self._task_options.options()
        for index in changed:
            self.task_selector.update_option(index, options[index])
        self._sync_delete_state()

    def _on_metrics_changed(self, metrics: List[str]) -> None:
        self.filter_bar.set_metrics(
            [(key, get_metric_display_name(key)) for key in metrics],
            self.vm.current_metric)

    def _on_selection_changed(self, task_id: Optional[str], metric: Optional[str],
                              _explicit: bool) -> None:
        self.task_selector.select_by_id(task_id, silent=True)
        if metric and self.filter_bar.current_metric() != metric:
            self._on_metrics_changed(self.vm.metrics)
        self._sync_delete_state()

    def _sync_delete_state(self) -> None:
        self.filter_bar.set_delete_enabled(self.vm.can_delete, self.vm.delete_hint)

    def _on_snapshot(self, snapshot: HistorySnapshot) -> None:
        request, stats = snapshot.request, snapshot.stats
        # 先画图：统计条与明细沿用图表按数据选出的显示单位
        self.chart_view.set_series(
            snapshot.buckets, request.metric, snapshot.markers,
            keep_view=request.auto_refresh)
        unit = self.chart_view.display_unit
        self.stats_strip.set_values(stats, request.metric, unit)
        self.sample_table.set_points(
            snapshot.points, request.metric,
            stats['count'] if stats else None, unit)
        self.content_stack.setCurrentWidget(self.analysis_page)

    def _on_empty_state(self, title: str, hint: str) -> None:
        self.chart_view.clear()
        self.stats_strip.clear()
        self.sample_table.clear()
        self.empty_state_card.set_message(title, hint)
        self.content_stack.setCurrentWidget(self.empty_state_card)

    def _on_busy(self, busy: bool) -> None:
        self.filter_bar.refresh_button.setEnabled(not busy)
        if busy:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self.busy_bar.stop()
            self.busy_bar.setVisible(False)

    def _show_busy_bar(self) -> None:
        self.busy_bar.setVisible(True)
        self.busy_bar.start()

    # ========== 需要对话框的操作 ==========

    def _on_delete_clicked(self) -> None:
        """删除此任务数据：确认（默认焦点在取消）→ 视图模型经 manager 删除并重载。"""
        task, notifier = self.vm.current_task, self.services.notifier
        if task is None:
            return
        if not self.vm.can_delete:
            notifier.warning(self, "提示", self.vm.delete_hint, duration=2000)
            return
        confirmed = self.services.dialogs.confirm(
            self.window(), "确认删除",
            f"确定要删除任务「{task.process_name} (PID: {task.pid})」的全部历史数据吗？"
            "此操作不可恢复。", "删除")
        if not confirmed:
            return
        if self.vm.delete_current():
            notifier.success(self, "删除成功", "该任务的历史数据已删除")
        else:
            notifier.error(self, "删除失败", "删除任务数据失败，请查看日志")

    def _save_chart_as_png(self) -> None:
        notifier = self.services.notifier
        if not self.chart_view.has_data():
            notifier.warning(self, *NO_CHART_TO_SAVE, duration=2000)
            return
        task, metric = self.vm.current_task, self.vm.current_metric
        process_name = (task.process_name if task else "task").replace('.exe', '')
        metric_name = get_metric_display_name(metric) if metric else "指标"
        stamp = self.services.clock.now().strftime('%Y%m%d%H%M%S')
        save_path = self.services.dialogs.ask_save_path(
            self, "保存图表为图片", f"{process_name}_{metric_name}_{stamp}.png",
            "PNG 图片 (*.png)")
        if not save_path:
            return
        try:
            self.chart_view.export_png(save_path)
        except Exception:
            logger.error("保存图表图片失败", exc_info=True)
            notifier.error(self, "保存失败", "保存图表图片失败，请查看日志")
            return
        notifier.success(self, "保存成功", f"图表已保存到 {save_path}")

    def _copy_chart_image(self) -> None:
        notifier = self.services.notifier
        if not self.chart_view.has_data():
            notifier.warning(self, *NO_CHART_TO_COPY, duration=2000)
            return
        try:
            self.services.dialogs.copy_pixmap(self.chart_view.grab_pixmap())
        except Exception:
            logger.error("复制图表到剪贴板失败", exc_info=True)
            notifier.error(self, "复制失败", "复制图表到剪贴板失败，请查看日志")
            return
        notifier.success(self, "复制成功", "图表已复制到剪贴板")

    # ========== 事件 ==========

    def resizeEvent(self, event):
        """按视口宽度换算出卡片内容宽度，更新筛选条换行与统计条列数。"""
        super().resizeEvent(event)
        content_width = self.viewport().width() - VIEWPORT_TO_CONTENT_OFFSET
        self.filter_bar.update_layout(content_width)
        self.stats_strip.update_columns(content_width)

    def showEvent(self, event):
        """每次显示都重新载入任务列表，并开始运行中任务的自动刷新计时。"""
        super().showEvent(event)
        self.vm.reload_tasks()
        self.auto_refresh.start()

    def hideEvent(self, event):
        """隐藏时停止自动刷新，并让在途查询作废（不阻塞等待线程）。"""
        super().hideEvent(event)
        self.auto_refresh.stop()
