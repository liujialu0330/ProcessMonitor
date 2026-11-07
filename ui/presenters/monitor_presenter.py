"""
监控页 Presenter

开始 / 等待进程启动 / 停止 / 暂停 / 恢复 / 打点（含托盘与全局快捷键的免弹窗打点）/
同配置再测 / 分析 / 关闭卡片与清除已结束的业务流程，以及 manager 各信号（任务添加、
启动、停止、数据更新、打点变化、指标不可用、错误、达到上限）的界面响应。卡片上的"较基线"由这里维护：基线值 = 基线打点之后前 5 个样本的中位数，
打点/换基线时先从库里取已有样本，不足部分随 data_updated 累积。
自动化方案（自动打点与停止条件）：开始监控时把方案存进 tasks.plan_json 并交给
AutoMarkerService 跟踪（命令行开始的任务由 IPC 命令处理器挂到同一个服务）；
"同配置再测"沿用上次的方案；按方案自动停止时提示原因，
带"停止后自动导出"的任务结束后在后台导出明细 CSV；方案到期请求阶段快照时交给
StageSnapshotWriter 在后台写报告图片与摘要 CSV（失败每个任务只提示一次）。
启动被测程序：请求带 launch 时先经 launcher 启动程序，用拿到的 PID 与创建时间建任务
（启动失败在表单启动区就近提示、不建任务）；勾选"停止时关闭"的任务在用户停止后于后台
关闭该程序；"同配置再测"对这类任务直接重新启动程序再监控。
暂停 / 恢复：卡片状态跟随 manager 的 task_paused / task_resumed（命令行暂停也同步），
本页按钮发起的那一次由按钮路径更新，信号回声不重复更新。依赖：
- manager：只用命令与只读快照接口，不接触任务线程对象；
- services：process_query / dialogs / notifier / clock / preferences；
- wait_service（可选）：等待进程启动的轮询与建任务（core.wait_service），缺省自建，
  这里只管占位卡；
- form：新建任务表单（NewTaskForm 或同接口的替身）；
- board：任务面板（TaskBoard 或同接口的替身），按 task_id 取卡片更新显示；
- host：提示挂靠的控件（页面），对话框挂在 host.window() 上；
- db（可选）：保存方案与自动导出用的数据库，缺省取 manager.db；没有时不存方案、不导出；
- snapshot_writer（可选）：阶段快照写入器，缺省自建（报告图片用 report_sheet 渲染）；
- launcher（可选）：启动 / 关闭被测程序，缺省取 services.launcher。

中文文案（表单错误、提示）集中在这里；控件只发信号、显示给定内容。
"""
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QWidget

import config
from core.automation.auto_marker import AutoMarkerService
from core.automation.plan import AutomationPlan
from core.automation.stage_snapshot import StageSnapshotWriter
from core.contracts import StopReason, UnavailableReason
from core.monitor_manager import BASELINE_SAMPLE_COUNT, default_marker_kind, median_of
from core.report import DATA_CSV_SUFFIX, gap_threshold_seconds, metadata_lines, task_basename
from core.report_worker import ReportJob, ReportWorker
from core.process_launcher import LaunchSpec, ProcessLauncher
from core.wait_service import WaitMatch, WaitService
from core.workers.terminate_worker import close_in_background
from data.errors import StorageError
from ui.formatting import (
    AUTO_EXPORT_BUSY, AUTO_EXPORT_DONE_TITLE, AUTO_EXPORT_FAILED_TITLE,
    AUTO_EXPORT_NO_DATABASE, AUTO_EXPORT_NO_TASK, AUTO_EXPORT_RANGE_TEXT, AUTO_STOP_TITLE,
    PLAN_INVALID_TITLE, READ_ONLY_MARKER_BLOCKED, READ_ONLY_TITLE, STAGE_SNAPSHOT_DONE_TITLE,
    STAGE_SNAPSHOT_FAILED_TITLE, auto_stop_text, plan_summary, stage_snapshot_failed_text,
    stop_reason_text, truncate_text, unavailable_reason_text,
)
from ui.services import UiServices
from ui.widgets.analysis.report_sheet import render_report_for
from ui.widgets.monitor.new_task_form import TaskRequest, WaitRequest
from ui.widgets.monitor.process_picker import (
    ERROR_EMPTY, ERROR_NO_MATCH, ERROR_NOT_A_NUMBER, ERROR_NOT_FOUND,
    ERROR_WAIT_NEEDS_NAME, ResolveError,
)
from utils.metrics import get_metric_display_name

logger = logging.getLogger(__name__)

# 异常结束提示的显示时长（毫秒）：被测进程崩溃是测试里最需要被注意到的事件，
# 不能像普通提示那样两秒就消失
ABNORMAL_STOP_INFOBAR_MS = 15000
UNAVAILABLE_INFOBAR_MS = 10000
RESTART_HINT_INFOBAR_MS = 5000
# 卡片"已运行"与"（N 分钟前）"的刷新间隔（毫秒）
TICK_INTERVAL_MS = 1000
# 等待进程启动的轮询间隔（毫秒）
WAIT_POLL_INTERVAL_MS = 1000

STOP_CONFIRM_HINT = "停止后不能继续记录到这个任务；要继续请用“同配置再测”。已采集的数据会保留。"

AUTO_STOP_INFOBAR_MS = 8000
AUTO_EXPORT_INFOBAR_MS = 6000
# 自动导出线程在登记表里的名称与退出时的等待上限
AUTO_EXPORT_THREAD_NAME = "自动导出"
AUTO_EXPORT_TIMEOUT_MS = 2000
BROWSE_LOG_TITLE = "选择日志文件"
BROWSE_LOG_FILTER = "日志文件 (*.log *.txt);;所有文件 (*)"
BROWSE_EXPORT_TITLE = "选择自动导出目录"
BROWSE_SNAPSHOT_TITLE = "选择阶段快照目录"
BROWSE_EXE_TITLE = "选择要启动的程序"
BROWSE_EXE_FILTER = "程序 (*.exe);;所有文件 (*)"
BROWSE_CWD_TITLE = "选择工作目录"

STAGE_SNAPSHOT_FAILED_INFOBAR_MS = 10000
STAGE_SNAPSHOT_DONE_INFOBAR_MS = 4000
LAUNCHED_NOT_MONITORED_TITLE = "程序已启动，但没能开始监控"
LAUNCHED_NOT_MONITORED_TEXT = "{name}（PID {pid}）已启动，监控任务没能创建；程序仍在运行，请稍后在进程列表里选它"
LAUNCH_MAINTENANCE_TEXT = "正在清理数据库，请稍后再试"

# 免弹窗打点（托盘菜单 / 全局快捷键）的反馈种类
QUICK_MARKER_OK = 'ok'
QUICK_MARKER_NONE = 'none'          # 没有可打点的任务
QUICK_MARKER_FAILED = 'failed'      # 写库失败

# 托盘菜单里任务名的最大显示字数
TARGET_NOTE_CHARS = 16


def resolve_error_text(error: ResolveError) -> str:
    """进程选择错误的字段提示文案。"""
    if error.kind == ERROR_NOT_FOUND:
        return f"进程 PID {error.pid} 不存在或无法访问"
    if error.kind == ERROR_NOT_A_NUMBER:
        return "PID 必须是正整数"
    if error.kind == ERROR_NO_MATCH:
        return "输入的内容与进程列表不匹配，请从下拉列表中选择，或直接输入 PID"
    if error.kind == ERROR_EMPTY:
        return "请输入进程名或 PID，或从列表中选择进程"
    if error.kind == ERROR_WAIT_NEEDS_NAME:
        return "等待进程启动需要填写进程名（如 notepad.exe），不能用 PID"
    raise ValueError(f"未知的进程选择错误：{error.kind!r}")


@dataclass
class _BaselineTracker:
    """某任务当前基线的取值进度：每指标最多 BASELINE_SAMPLE_COUNT 个样本。
    settled 为 True 时不再从实时数据补样本（基线较早，库里的样本就是全部）。"""
    marker_id: Optional[int]
    timestamp: datetime
    samples: Dict[str, List[float]] = field(default_factory=dict)
    settled: bool = False


@dataclass
class _TaskEntry:
    """面板上一张卡片对应的任务参数（取自建卡时的快照）与界面状态。"""
    task_id: str
    pid: int
    process_name: str
    metric_types: Tuple[str, ...]
    interval: Optional[float]
    note: str
    finished: bool = False
    # 已发出停止请求、等待收尾：不再接受打点
    stopping: bool = False
    baseline: Optional[_BaselineTracker] = None
    # 自动化方案（没有为 None）；plan_saved 为 False 表示还没写进 tasks.plan_json
    # （任务行由采集线程启动时才写入，之后的周期里补存）
    plan: Optional[AutomationPlan] = None
    plan_saved: bool = True
    # 由本页启动的被测程序（"同配置再测"据此重新启动）；不是启动型任务为 None
    launch: Optional[LaunchSpec] = None
    close_on_stop: bool = False

    @property
    def display_name(self) -> str:
        return f"{self.process_name}（PID {self.pid}）"

    @property
    def active(self) -> bool:
        """还能打点（运行中或已暂停，未结束、未在停止）。"""
        return not self.finished and not self.stopping

    def target_text(self) -> str:
        """托盘菜单里的任务名："demo.exe · PID 1234 · 备注"。"""
        text = f"{self.process_name} · PID {self.pid}"
        if self.note:
            text += f" · {truncate_text(self.note, TARGET_NOTE_CHARS)}"
        return text


class MonitorPresenter(QObject):
    """监控页业务流程。

    信号：
        history_requested(str): 卡片"分析"：请求切到分析页并选中该任务（主窗口处理）
        task_ended_abnormally(str, str): 被测进程自行结束（不是用户停止）：(标题, 内容)，
            主窗口不在前台时据此弹托盘气泡
        quick_marker_feedback(str, str, str): 免弹窗打点的结果 (种类, 标题, 内容)，
            种类见 QUICK_MARKER_*；主窗口据此弹托盘气泡
    """

    history_requested = pyqtSignal(str)
    task_ended_abnormally = pyqtSignal(str, str)
    quick_marker_feedback = pyqtSignal(str, str, str)

    def __init__(self, manager, services: UiServices, form, board, host: QWidget,
                 parent: Optional[QObject] = None, *,
                 task_limit: int = config.MAX_MONITOR_TASKS,
                 db=None, auto_marker: Optional[AutoMarkerService] = None,
                 export_worker_factory: Optional[Callable] = None,
                 wait_service: Optional[WaitService] = None,
                 snapshot_writer: Optional[StageSnapshotWriter] = None,
                 launcher=None):
        super().__init__(parent)
        self.manager = manager
        self.services = services
        self.form = form
        self.board = board
        self.host = host
        self.task_limit = task_limit
        # 方案落库（tasks.plan_json）与自动导出读库用的数据库；缺省取 manager 持有的实例
        self._db = db if db is not None else getattr(manager, 'db', None)
        self._export_worker_factory = export_worker_factory or ReportWorker
        # 进行中的自动导出线程（持有引用直到线程结束）
        self._export_workers: List[Any] = []

        self._tasks: Dict[str, _TaskEntry] = {}
        # 启动 / 关闭被测程序；勾选"停止时关闭"的任务 {task_id: (PID, 创建时间)}
        self.launcher = (launcher if launcher is not None
                         else getattr(services, 'launcher', None) or ProcessLauncher())
        self._close_targets: Dict[str, Tuple[int, Optional[float]]] = {}
        # 本页按钮正在发起暂停 / 恢复的任务：manager 随之发的 task_paused / task_resumed
        # 是这次操作的回声，按钮路径已更新卡片，不再重复
        self._commanding: Optional[str] = None
        # 数据库只读（由更高版本创建）时的原因文案：不能开始监控、不能打点
        self._read_only_reason: Optional[str] = None
        # 等待进程启动：按名轮询与建任务交给 WaitService，这里只管占位卡 {等待 ID: 请求}
        self.wait_service = (wait_service if wait_service is not None
                             else WaitService(manager, services.process_query,
                                              services.clock, self))
        self._waits: Dict[str, WaitRequest] = {}
        # 当前显示中的错误提示 {(task_id, 内容): 提示对象}：错误提示不自动消失，
        # 同一任务同一错误重复上报时不再叠加
        self._error_bars: Dict[Tuple[str, str], Any] = {}

        # 每秒刷新运行中卡片的"已运行"等时间文案（有运行中卡片时才走）
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(TICK_INTERVAL_MS)
        self._tick_timer.timeout.connect(self.tick)

        # 有等待中的进程时每秒查一次是否已启动
        self._wait_timer = QTimer(self)
        self._wait_timer.setInterval(WAIT_POLL_INTERVAL_MS)
        self._wait_timer.timeout.connect(self.poll_waits)

        # 新建任务沿用上次选择的指标（默认"泄漏排查"预设）
        self.form.set_metrics_summary(self.services.preferences.last_metrics())
        self.form.set_collapsed(False)
        self._sync_counts()

        self.form.start_requested.connect(self.on_start_requested)
        self.form.wait_requested.connect(self.on_wait_requested)
        self.form.target_invalid.connect(self.on_target_invalid)
        self.form.choose_metrics_requested.connect(self.on_choose_metrics)

        self.board.stop_requested.connect(self.on_stop)
        self.board.pause_requested.connect(self.on_pause)
        self.board.resume_requested.connect(self.on_resume)
        self.board.marker_requested.connect(self.on_marker)
        self.board.history_requested.connect(self.history_requested)
        self.board.restart_requested.connect(self.on_restart)
        self.board.close_requested.connect(self.on_close)
        self.board.clear_finished_requested.connect(self.on_clear_finished)
        self.board.wait_cancel_requested.connect(self.on_wait_cancel)

        manager.task_added.connect(self.on_task_added)
        manager.task_started.connect(self.on_task_started)
        manager.task_stopped.connect(self.on_task_stopped)
        manager.data_updated.connect(self.on_data_updated)
        manager.markers_changed.connect(self.on_markers_changed)
        manager.error_occurred.connect(self.on_error)
        manager.metrics_unavailable.connect(self.on_metrics_unavailable)
        manager.task_limit_reached.connect(self.on_limit_reached)
        manager.task_paused.connect(self.on_task_paused)
        manager.task_resumed.connect(self.on_task_resumed)
        # 被测目标元数据由 manager 在任务启动后后台探测并落库（无窗口模式同样有）；
        # 这里只把摘要行补进卡片的悬停提示。替身管理器可以没有这个信号
        target_info_ready = getattr(manager, 'target_info_ready', None)
        if target_info_ready is not None:
            target_info_ready.connect(self.on_target_info_ready)

        # 自动打点与停止条件：每个采集周期经 data_updated、另由每秒的 tick 兜底驱动
        self.auto_marker = (auto_marker if auto_marker is not None
                            else AutoMarkerService(manager, services.clock, self))
        self.auto_marker.auto_stopped.connect(self.on_auto_stopped)
        self.auto_marker.export_requested.connect(self.on_export_requested)
        self.form.browse_log_requested.connect(self.on_browse_log)
        self.form.browse_export_dir_requested.connect(self.on_browse_export_dir)
        self.form.browse_snapshot_dir_requested.connect(self.on_browse_snapshot_dir)
        self.form.browse_exe_requested.connect(self.on_browse_exe)
        self.form.browse_cwd_requested.connect(self.on_browse_cwd)

        # 阶段快照：方案到期 → 后台写报告图片与摘要 CSV（命令行开始的任务挂在同一个自动
        # 打点服务上，也经这里写）
        self.snapshot_writer = (snapshot_writer if snapshot_writer is not None
                                else StageSnapshotWriter(self._db, manager, services.threads,
                                                         services.clock, self,
                                                         renderer=render_report_for))
        self.auto_marker.stage_snapshot_due.connect(self.on_stage_snapshot_due)
        self.snapshot_writer.failed.connect(self.on_stage_snapshot_failed)
        self.snapshot_writer.written.connect(self.on_stage_snapshot_written)

    # ========== 查询（供页面与测试） ==========

    def task_ids(self):
        """面板上各卡片的任务 ID（按加入顺序）。"""
        return list(self._tasks)

    def shown_errors(self) -> Set[Tuple[str, str]]:
        """仍在显示中的错误提示键 (task_id, 内容)。"""
        return set(self._error_bars)

    def active_task_ids(self) -> List[str]:
        """还能打点的任务（运行中或已暂停），按加入顺序。"""
        return [task_id for task_id, entry in self._tasks.items() if entry.active]

    def marker_targets(self) -> List[Tuple[str, str]]:
        """托盘"打点"菜单的候选任务 [(task_id, 显示名)]。"""
        return [(task_id, self._tasks[task_id].target_text())
                for task_id in self.active_task_ids()]

    def wait_ids(self) -> List[str]:
        """等待中的进程启动请求 ID（按加入顺序）。"""
        return list(self._waits)

    @property
    def read_only(self) -> bool:
        """数据库是否只读（只读时拒绝开始监控与打点）"""
        return self._read_only_reason is not None

    def set_read_only(self, reason: Optional[str]) -> None:
        """数据库只读（主窗口按 Database.read_only 设置）：禁用开始按钮并在 tooltip 写明原因，
        之后的开始、等待、同配置再测与打点请求都被拒绝并就近提示。reason 为 None 时恢复。"""
        self._read_only_reason = reason or None
        self.form.set_start_blocked(self._read_only_reason)

    def _reject_if_read_only(self) -> bool:
        """只读时就近提示并返回 True"""
        if self._read_only_reason is None:
            return False
        self._warn(self._read_only_reason, title=READ_ONLY_TITLE)
        return True

    def next_marker_label(self, task_id: str) -> Optional[str]:
        """下一个打点的默认名称（基线 / 第 N 轮）；面板上没有该任务时返回 None。"""
        if task_id not in self._tasks:
            return None
        return self.manager.next_marker_label(task_id)

    # ========== 公共小工具 ==========

    def _dialog_parent(self) -> QWidget:
        return self.host.window()

    def _warn(self, content: str, title: str = "提示", duration: int = 3000):
        self.services.notifier.warning(self.host, title, content, duration)

    def _sync_counts(self) -> None:
        self.board.set_counts(self.manager.running_task_count(), self.task_limit)

    def _has_items(self) -> bool:
        return bool(self._tasks) or bool(self._waits)

    def _set_has_tasks(self, had_tasks: bool) -> None:
        """面板从空到有卡片（含等待占位卡）时折叠表单，从有到空时展开；
        其余时候不动用户的展开状态。"""
        has_tasks = self._has_items()
        if has_tasks != had_tasks:
            self.form.set_collapsed(has_tasks)
        self._sync_tick_timer()

    def _sync_tick_timer(self) -> None:
        running = any(not entry.finished for entry in self._tasks.values())
        if running and not self._tick_timer.isActive():
            self._tick_timer.start()
        elif not running and self._tick_timer.isActive():
            self._tick_timer.stop()

    def tick(self) -> None:
        """刷新运行中卡片的时间文案，并驱动自动打点服务（定时器每秒调用；测试可直接调用）。
        自动打点也随每个采集周期驱动，这里保证长周期与暂停时也能准时打点、按时长停止。"""
        now = self.services.clock.now()
        for task_id, entry in self._tasks.items():
            if entry.finished:
                continue
            card = self.board.card(task_id)
            if card is not None:
                card.tick(now)
        self.auto_marker.tick()

    # ========== 新建任务 ==========

    def on_choose_metrics(self) -> None:
        """弹出指标多选对话框；确定后记住本次选择。"""
        selected = self.services.dialogs.choose_metrics(
            self._dialog_parent(), self.form.metrics())
        if selected is None:
            return
        metrics = list(selected)
        self.services.preferences.remember_last_metrics(metrics)
        self.form.set_metrics_summary(metrics)

    def on_target_invalid(self, error: ResolveError) -> None:
        self.form.show_error(resolve_error_text(error))

    def on_start_requested(self, request: TaskRequest) -> Optional[str]:
        if self._reject_if_read_only():
            return None
        if not request.metrics:
            self._warn("请先选择要监控的指标")
            return None
        return self.start_monitoring(request)

    def start_monitoring(self, request: TaskRequest) -> Optional[str]:
        """创建并启动任务；同一 PID 已有运行中任务时先确认。返回任务 ID，
        取消或创建失败返回 None；数据库只读或自动化方案有误时拒绝并提示。

        带方案时：建任务后记下方案、尝试写入 tasks.plan_json（任务行要等采集线程启动才
        写入，写不进去就在之后的采集周期里补存）、交给自动打点服务，再启动任务——
        服务以 task_started 的时刻起算。"""
        if self._reject_if_read_only():
            return None
        plan = request.plan
        if plan is not None and plan.is_empty():
            plan = None
        if plan is not None:
            errors = plan.validate(request.interval)
            if errors:
                self._warn("；".join(errors), title=PLAN_INVALID_TITLE)
                return None
        if request.launch is not None:
            # 先启动被测程序：拿到的 PID 是新进程，不会与已有任务重复
            request = self._launch_target(request)
            if request is None:
                return None
        elif (self.manager.has_running_task_for_pid(request.pid)
                and not self._confirm_duplicate_pid(request)):
            return None
        task_id = self.manager.create_task(
            request.pid, request.name, list(request.metrics), request.interval,
            note=request.note, create_time=request.create_time)
        if not task_id:
            if request.launch is not None:
                logger.warning("被测程序已启动但没能创建任务: pid=%s", request.pid)
                self._warn(LAUNCHED_NOT_MONITORED_TEXT.format(name=request.name, pid=request.pid),
                           title=LAUNCHED_NOT_MONITORED_TITLE, duration=RESTART_HINT_INFOBAR_MS)
            return task_id
        if request.launch is not None:
            self._remember_launch(task_id, request)
        if plan is not None:
            self._apply_plan(task_id, plan)
        self.manager.start_task(task_id)
        return task_id

    # ========== 启动被测程序 ==========

    def _launch_target(self, request: TaskRequest) -> Optional[TaskRequest]:
        """启动请求里的程序，返回填好 PID / 进程名 / 创建时间的请求；失败就近提示并返回 None。
        先确认任务建得起来（没满、没在清理数据库），不留下没人监控的程序。"""
        can_add = getattr(self.manager, 'can_add_task', None)
        if can_add is not None and not can_add():
            self.on_limit_reached()
            return None
        if self.manager.is_maintenance_running():
            self._warn(LAUNCH_MAINTENANCE_TEXT)
            return None
        result = self.launcher.launch(request.launch)
        if not result.ok:
            self.form.show_launch_error(result.error or "")
            return None
        return replace(request, pid=result.pid, name=result.name or request.launch.name,
                       create_time=result.create_time)

    def _remember_launch(self, task_id: str, request: TaskRequest) -> None:
        entry = self._tasks.get(task_id)
        if entry is not None:
            entry.launch = request.launch
            entry.close_on_stop = request.close_on_stop
        if request.close_on_stop:
            self._close_targets[task_id] = (request.pid, request.create_time)

    def is_launched(self, task_id: str) -> bool:
        """该卡片的任务是否由本页启动了被测程序。"""
        entry = self._tasks.get(task_id)
        return entry is not None and entry.launch is not None

    def _close_launched(self, task_id: str, reason: StopReason) -> None:
        """勾选了"停止时关闭"的启动型任务结束：只有用户停止（含方案的停止条件）才关闭
        程序；被测进程自己退出、PID 复用等不动。关闭在后台线程进行（先 WM_CLOSE 再等待）。"""
        target = self._close_targets.pop(task_id, None)
        if target is None or reason is not StopReason.USER:
            return
        pid, create_time = target
        close_in_background(self.launcher, pid, create_time, self.services.threads)

    def on_browse_exe(self) -> None:
        """启动区程序路径的"浏览…"。"""
        current = self.form.launch_exe()
        path = self.services.dialogs.ask_open_file(
            self._dialog_parent(), BROWSE_EXE_TITLE,
            os.path.dirname(current) if current else '', BROWSE_EXE_FILTER)
        if path:
            self.form.set_launch_exe(path)

    def on_browse_cwd(self) -> None:
        """启动区工作目录的"选择…"。"""
        start = self.form.launch_cwd() or os.path.dirname(self.form.launch_exe())
        path = self.services.dialogs.ask_directory(
            self._dialog_parent(), BROWSE_CWD_TITLE, start)
        if path:
            self.form.set_launch_cwd(path)

    # ========== 自动化方案 ==========

    def plan_of(self, task_id: str) -> Optional[AutomationPlan]:
        """卡片对应任务的自动化方案；没有方案或不在面板上返回 None。"""
        entry = self._tasks.get(task_id)
        return entry.plan if entry is not None else None

    def _apply_plan(self, task_id: str, plan: AutomationPlan) -> None:
        entry = self._tasks.get(task_id)
        if entry is not None:
            entry.plan = plan
            entry.plan_saved = False
            card = self.board.card(task_id)
            if card is not None:
                # 悬停卡片即可看到这个任务的自动化设置
                card.setToolTip(plan_summary(plan))
        self._save_plan(task_id, plan)
        self.auto_marker.attach(task_id, plan)

    def _save_plan(self, task_id: str, plan: AutomationPlan) -> bool:
        """把方案写进 tasks.plan_json；任务行尚不存在或写库失败返回 False（稍后再试）。"""
        store = getattr(self._db, 'tasks', None)
        entry = self._tasks.get(task_id)
        if store is None:
            if entry is not None:
                entry.plan_saved = True     # 没有可写的数据库（降级或测试），不再重试
            return False
        try:
            saved = bool(store.save_plan(task_id, plan.to_json()))
        except StorageError:
            logger.error("保存自动化方案失败: task_id=%s", task_id, exc_info=True)
            saved = False
        if entry is not None:
            entry.plan_saved = saved
        return saved

    def _retry_save_plan(self, task_id: str) -> None:
        entry = self._tasks.get(task_id)
        if entry is not None and entry.plan is not None and not entry.plan_saved:
            self._save_plan(task_id, entry.plan)

    def on_auto_stopped(self, task_id: str, reason: str) -> None:
        """按方案自动停止：卡片转"正在停止"并提示原因（任务随后照常经 task_stopped 结束，
        停止原因是"用户停止"，不按异常结束提示）。"""
        entry = self._tasks.get(task_id)
        if entry is None:
            return
        entry.stopping = True
        card = self.board.card(task_id)
        if card is not None:
            card.set_stopping()
        self.services.notifier.info(
            self.host, AUTO_STOP_TITLE,
            f"{entry.display_name}：{auto_stop_text(reason, entry.plan)}",
            AUTO_STOP_INFOBAR_MS)

    def on_browse_log(self) -> None:
        """自动化区日志文件的"浏览…"。"""
        path = self.services.dialogs.ask_open_file(
            self._dialog_parent(), BROWSE_LOG_TITLE,
            os.path.dirname(self.form.log_path()), BROWSE_LOG_FILTER)
        if path:
            self.form.set_log_path(path)

    def on_browse_export_dir(self) -> None:
        """自动化区导出目录的"选择…"。"""
        path = self.services.dialogs.ask_directory(
            self._dialog_parent(), BROWSE_EXPORT_TITLE, self.form.export_dir())
        if path:
            self.form.set_export_dir(path)

    def on_browse_snapshot_dir(self) -> None:
        """自动化区阶段快照目录的"选择…"。"""
        path = self.services.dialogs.ask_directory(
            self._dialog_parent(), BROWSE_SNAPSHOT_TITLE, self.form.snapshot_dir())
        if path:
            self.form.set_snapshot_dir(path)

    # ========== 阶段快照 ==========

    def on_stage_snapshot_due(self, task_id: str, directory: str, index: int) -> bool:
        """方案到了写阶段快照的时刻：交给写入器在后台写（上一次没写完时它会跳过本次）。"""
        return self.snapshot_writer.write(task_id, directory, index)

    def on_stage_snapshot_failed(self, task_id: str, message: str) -> None:
        """阶段快照没写出来（写入器保证每个任务只来一次）：提示原因，采集不受影响。"""
        entry = self._tasks.get(task_id)
        name = entry.display_name if entry is not None else task_id
        self.services.notifier.warning(
            self.host, STAGE_SNAPSHOT_FAILED_TITLE, stage_snapshot_failed_text(name, message),
            STAGE_SNAPSHOT_FAILED_INFOBAR_MS)

    def on_stage_snapshot_written(self, task_id: str, paths) -> None:
        entry = self._tasks.get(task_id)
        name = entry.display_name if entry is not None else task_id
        files = "、".join(os.path.basename(p) for p in (paths or ()))
        self.services.notifier.info(
            self.host, STAGE_SNAPSHOT_DONE_TITLE, f"{name}：{files}",
            STAGE_SNAPSHOT_DONE_INFOBAR_MS)

    # ========== 停止后自动导出 ==========

    def on_export_requested(self, task_id: str, export_dir: str) -> bool:
        """带"停止后自动导出"的任务已结束（数据已全部落库）：在后台把明细 CSV（含元数据、
        已运行秒数、打点列与空洞空行）写到方案的导出目录，与清理数据库互斥。
        返回是否已启动导出。

        报告图片与摘要 CSV 依赖分析页的离屏渲染与分析结果，不在这里生成。"""
        entry = self._tasks.get(task_id)
        name = entry.display_name if entry is not None else task_id
        db = self._db
        if db is None:
            self._export_failed(name, AUTO_EXPORT_NO_DATABASE)
            return False
        task = db.get_task(task_id)
        if task is None or task.start_time is None:
            self._export_failed(name, AUTO_EXPORT_NO_TASK)
            return False
        if self.manager.is_maintenance_running() or not self.manager.begin_export():
            self._export_failed(name, AUTO_EXPORT_BUSY)
            return False
        try:
            os.makedirs(export_dir, exist_ok=True)
        except OSError as exc:
            self.manager.end_export()
            self._export_failed(name, f"无法创建导出目录：{exc}")
            return False
        csv_path = _unique_path(os.path.join(export_dir, task_basename(task) + DATA_CSV_SUFFIX))
        job = ReportJob(
            task=task, csv_path=csv_path,
            metadata=tuple(metadata_lines(
                task, exported_at=self.services.clock.now(), range_text=AUTO_EXPORT_RANGE_TEXT,
                sample_count=task.sample_count or None,
                end_time=task.end_time or task.last_sample_at)),
            gap_seconds=gap_threshold_seconds(task.interval))
        worker = self._export_worker_factory(db, job)
        self._export_workers.append(worker)
        worker.result.connect(lambda result, n=name: self._export_done(n, result))
        worker.failed.connect(lambda message, n=name: self._export_failed(n, message))
        # 注销导出挂在 QThread.finished 上：取消（退出时）不发结果/失败，finished 仍会触发
        worker.finished.connect(lambda w=worker: self._export_thread_finished(w))
        self.services.threads.register(worker, name=AUTO_EXPORT_THREAD_NAME,
                                       cancel=worker.cancel, timeout_ms=AUTO_EXPORT_TIMEOUT_MS)
        worker.start()
        return True

    def _export_thread_finished(self, worker) -> None:
        self.manager.end_export()
        if worker in self._export_workers:
            self._export_workers.remove(worker)

    def _export_done(self, name: str, result) -> None:
        paths = getattr(result, 'paths', ()) or ()
        self.services.notifier.success(
            self.host, AUTO_EXPORT_DONE_TITLE, f"{name}：{paths[0] if paths else ''}",
            AUTO_EXPORT_INFOBAR_MS)

    def _export_failed(self, name: str, message: str) -> None:
        logger.warning("自动导出未完成: %s：%s", name, message)
        self.services.notifier.error(self.host, AUTO_EXPORT_FAILED_TITLE, f"{name}：{message}")

    def _confirm_duplicate_pid(self, request: TaskRequest) -> bool:
        return self.services.dialogs.confirm(
            self._dialog_parent(), "已在监控该进程",
            f"{request.name}（PID {request.pid}）已有一个运行中的监控任务。\n"
            "仍要再创建一个吗？",
            "继续创建")

    # ========== 等待进程启动 ==========

    def on_wait_requested(self, request: WaitRequest) -> Optional[str]:
        """登记一次等待：面板放占位卡，立刻查一次（进程已在运行就直接开始），
        之后每秒查一次。返回等待 ID；没选指标或数据库只读时拒绝并返回 None。"""
        if self._reject_if_read_only():
            return None
        if not request.metrics:
            self._warn("请先选择要监控的指标")
            return None
        had_tasks = self._has_items()
        wait_id = self.wait_service.add(request, starter=self._start_waited)
        self._waits[wait_id] = request
        self.board.add_waiting(wait_id, request.name, request.note)
        self._set_has_tasks(had_tasks)
        self.poll_waits()
        self._sync_wait_timer()
        return wait_id

    def on_wait_cancel(self, wait_id: str) -> None:
        """占位卡的"取消等待"。"""
        self._drop_wait(wait_id)

    def _drop_wait(self, wait_id: str) -> None:
        self.wait_service.cancel(wait_id)
        if self._waits.pop(wait_id, None) is None:
            return
        had_tasks = True
        self.board.remove_waiting(wait_id)
        self._set_has_tasks(had_tasks)
        self._sync_wait_timer()

    def _sync_wait_timer(self) -> None:
        if self._waits and not self._wait_timer.isActive():
            self._wait_timer.start()
        elif not self._waits and self._wait_timer.isActive():
            self._wait_timer.stop()

    def poll_waits(self) -> None:
        """查一遍各等待中的进程（定时器每秒调用；测试可直接调用）：出现即移除占位卡，
        按请求参数建任务。同名进程有多个时取最新启动的那个（WaitService 负责查找）。"""
        self.wait_service.poll()

    def _start_waited(self, wait_id: str, match: WaitMatch) -> Optional[str]:
        """WaitService 等到了进程：先移除占位卡再按请求开始监控（与手动开始走同一流程）。"""
        request = match.request
        self._drop_wait(wait_id)
        return self.start_monitoring(TaskRequest(
            pid=match.pid, name=match.name, create_time=match.create_time,
            metrics=request.metrics, interval=request.interval, note=request.note,
            plan=request.plan))

    # ========== manager 信号 ==========

    def on_task_added(self, task_id: str) -> None:
        info = self.manager.get_task_info(task_id)
        if info is None:
            return
        had_tasks = self._has_items()
        self._tasks[task_id] = _TaskEntry(
            task_id=task_id, pid=info.pid, process_name=info.process_name,
            metric_types=tuple(info.metric_types), interval=info.interval, note=info.note)
        self.board.create_card(info)
        self._sync_counts()
        self._set_has_tasks(had_tasks)

    def on_task_started(self, task_id: str) -> None:
        """卡片开始计时。不弹"已启动"提示：卡片就在眼前，结果直接可见。
        由命令行（IPC）开始的任务不经本页，方案由命令处理器挂到同一个自动打点服务并负责
        落库；这里只把方案记到卡片上（悬停提示与"同配置再测"沿用）。"""
        card = self.board.card(task_id)
        if card is not None:
            card.set_started_at(self.services.clock.now())
        self._adopt_external_plan(task_id)
        self._sync_counts()

    def _adopt_external_plan(self, task_id: str) -> None:
        entry = self._tasks.get(task_id)
        if entry is None or entry.plan is not None:
            return
        plan_of = getattr(self.auto_marker, 'plan_of', None)
        plan = plan_of(task_id) if plan_of is not None else None
        if plan is None:
            return
        entry.plan = plan
        entry.plan_saved = True
        card = self.board.card(task_id)
        if card is not None:
            card.setToolTip(plan_summary(plan))

    def on_target_info_ready(self, task_id: str, info) -> None:
        """被测目标元数据探测完成：卡片悬停提示追加"被测程序 / 命令行 / 环境"摘要行。"""
        card = self.board.card(task_id)
        set_summary = getattr(card, 'set_target_summary', None)
        if set_summary is None or info is None:
            return
        try:
            set_summary(info.summary_lines())
        except Exception:
            logger.warning("更新卡片的被测目标摘要失败: task_id=%s", task_id, exc_info=True)

    def on_task_stopped(self, task_id: str, reason: StopReason) -> None:
        """卡片保留并转为"已结束"，同时请 manager 移除 core 侧任务。remove_task 不阻塞：
        线程若还没退出，manager 会在它 finished 后再释放对象。"""
        entry = self._tasks.get(task_id)
        if entry is not None:
            entry.finished = True
            # 任务行此时一定已落库：一个周期都没采到就结束时，方案在这里补存
            self._retry_save_plan(task_id)
            self.board.finish_card(task_id, reason, self.services.clock.now())
            self.form.forget_process_if(entry.pid)
            self._sync_tick_timer()

        self.manager.remove_task(task_id)
        self._sync_counts()
        self._close_launched(task_id, reason)

        reason_text = stop_reason_text(reason)
        if reason is StopReason.USER:
            # 用户自己点的停止：卡片当场转为已结束，不再弹提示
            return

        # 被测进程崩溃/退出等自行结束：提示要显眼且停留更久，并通知主窗口在后台时补托盘气泡
        name = entry.display_name if entry is not None else task_id
        title = "监控任务异常结束"
        content = f"{name}：{reason_text}"
        self.services.notifier.warning(self.host, title, content, ABNORMAL_STOP_INFOBAR_MS)
        self.task_ended_abnormally.emit(title, content)

    def on_data_updated(self, task_id: str, values: dict) -> None:
        """每采集周期一次：卡片采集次数为内存自增，基线样本在内存里累积，不查库。"""
        card = self.board.card(task_id)
        if card is None:
            return
        card.update_values(values, self.services.clock.now())
        card.increment_count()
        self._feed_baseline(task_id, values)
        self._retry_save_plan(task_id)

    # ========== 基线 ==========

    def _feed_baseline(self, task_id: str, values: dict) -> None:
        """把一批实时样本计入尚未采够的基线；某指标凑满 N 个时把中位数交给卡片。"""
        entry = self._tasks.get(task_id)
        tracker = entry.baseline if entry is not None else None
        if tracker is None or tracker.settled:
            return
        card = self.board.card(task_id)
        for metric, value in values.items():
            samples = tracker.samples.setdefault(metric, [])
            if len(samples) >= BASELINE_SAMPLE_COUNT:
                continue
            samples.append(value)
            if len(samples) == BASELINE_SAMPLE_COUNT and card is not None:
                card.set_baseline_value(metric, median_of(samples))
        if all(len(tracker.samples.get(m, ())) >= BASELINE_SAMPLE_COUNT
               for m in entry.metric_types):
            tracker.settled = True

    def baseline_values(self, task_id: str) -> Optional[Dict[str, float]]:
        """某任务卡片当前已算出的基线值 {指标: 值}；无基线返回 None。
        样本凑满 N 个（或基线较早、库里的样本就是全部）的指标才有值。"""
        entry = self._tasks.get(task_id)
        if entry is None or entry.baseline is None:
            return None
        tracker = entry.baseline
        return {metric: median_of(samples) for metric, samples in tracker.samples.items()
                if samples and (len(samples) >= BASELINE_SAMPLE_COUNT or tracker.settled)}

    def on_markers_changed(self, task_id: str) -> None:
        """打点有变化（本页打点、暂停/恢复，或分析页改名/删除/换基线）：刷新卡片刻度；
        基线换了就重新取基线值，基线没了就回到"比开始时"。"""
        entry = self._tasks.get(task_id)
        card = self.board.card(task_id)
        if entry is None or card is None:
            return
        markers = self.manager.list_markers(task_id)
        card.set_markers(markers)

        baseline = self.manager.get_baseline(task_id, markers)
        if baseline is None:
            if entry.baseline is not None:
                entry.baseline = None
                card.clear_baseline()
            return
        current = entry.baseline
        if (current is not None and current.marker_id == baseline.id
                and current.timestamp == baseline.timestamp):
            return

        seed = self.manager.baseline_samples(task_id, baseline)
        tracker = _BaselineTracker(marker_id=baseline.id, timestamp=baseline.timestamp)
        if seed is not None:
            tracker.samples = {m: list(v) for m, v in seed.samples.items()}
            tracker.settled = seed.settled or entry.finished
        entry.baseline = tracker
        card.set_baseline(baseline.timestamp, self.baseline_values(task_id))

    def on_metrics_unavailable(self, task_id: str,
                               reasons: Dict[str, UnavailableReason]) -> None:
        """部分指标无法采集：卡片标"不可用"，并弹出一次警告。"""
        card = self.board.card(task_id)
        if card is None:
            return
        card.mark_unavailable(reasons)

        items = "、".join(
            f"{get_metric_display_name(metric)}（{unavailable_reason_text(reason)}）"
            for metric, reason in reasons.items())
        content = f"{items}，这些指标不会记录数据。"
        if UnavailableReason.ACCESS_DENIED in reasons.values():
            content += "以管理员身份运行本工具可采集全部指标。"
        self.services.notifier.warning(
            self.host, "部分指标无法采集", content, UNAVAILABLE_INFOBAR_MS)

    def on_error(self, task_id: str, error_msg: str) -> None:
        """错误提示不自动消失；同一任务的同一错误仍在显示时不再叠加
        （采集异常可能每个周期都上报一次）。"""
        key = (task_id, error_msg)
        if key in self._error_bars:
            return
        bar = self.services.notifier.error(self.host, "错误", error_msg)
        if bar is not None:
            self._error_bars[key] = bar
            bar.closedSignal.connect(lambda k=key: self._error_bars.pop(k, None))

    def on_limit_reached(self) -> None:
        self.services.notifier.warning(
            self.host, "提示", f"最多只能同时监控 {self.task_limit} 个任务", 2000)

    # ========== 卡片按钮 ==========

    def on_stop(self, task_id: str) -> None:
        """确认后发出停止请求（非阻塞），卡片等 task_stopped 再转为已结束。
        停止窗口期内 manager 可能返回 False（任务已在停止），界面容忍不报错。"""
        entry = self._tasks.get(task_id)
        if entry is None or entry.finished:
            return
        confirmed = self.services.dialogs.confirm(
            self._dialog_parent(), "停止监控",
            f"确定要停止对 {entry.display_name} 的监控吗？\n{STOP_CONFIRM_HINT}",
            "停止")
        if not confirmed:
            return
        if self.manager.stop_task(task_id):
            entry.stopping = True
            card = self.board.card(task_id)
            if card is not None:
                card.set_stopping()

    def on_pause(self, task_id: str) -> None:
        """manager 确认暂停成功后才更新卡片；任务正在停止等导致失败时不做任何事。"""
        if self._command(self.manager.pause_task, task_id):
            self._show_paused(task_id, True)
            # 自动打点立即以此刻为暂停点，不等下一次 tick
            self.auto_marker.process(task_id)

    def on_resume(self, task_id: str) -> None:
        if self._command(self.manager.resume_task, task_id):
            self._show_paused(task_id, False)
            # 定时打点从恢复的这一刻重新计时
            self.auto_marker.process(task_id)

    def _command(self, command: Callable[[str], bool], task_id: str) -> bool:
        """执行本页发起的暂停 / 恢复；执行期间 manager 发出的同名信号视为回声。"""
        self._commanding = task_id
        try:
            return bool(command(task_id))
        finally:
            self._commanding = None

    def _show_paused(self, task_id: str, paused: bool) -> None:
        card = self.board.card(task_id)
        if card is not None:
            card.set_paused(paused)

    def on_task_paused(self, task_id: str) -> None:
        """别处（命令行、托盘等）暂停了任务：同步卡片。本页按钮那一次已由按钮路径更新。"""
        if task_id != self._commanding:
            self._show_paused(task_id, True)

    def on_task_resumed(self, task_id: str) -> None:
        if task_id != self._commanding:
            self._show_paused(task_id, False)

    def on_marker(self, task_id: str) -> None:
        """打点：弹出名称输入框（默认名称按序列：基线 / 第 N 轮），经 manager 写库，
        成功后提示打点时间。默认名称是"基线"时，用户改了名称也仍记为基线。"""
        entry = self._tasks.get(task_id)
        if entry is None or entry.finished:
            return
        if self._read_only_reason is not None:
            self._warn(READ_ONLY_MARKER_BLOCKED, title=READ_ONLY_TITLE)
            return
        # 打点时间取点击按钮的这一刻：输入名称要花好几秒，按确认时刻记录会让
        # 竖线落在事件之后，短周期下甚至把增长归到下一轮
        clicked_at = self.services.clock.now()
        default_label = self.manager.next_marker_label(task_id)
        label = self.services.dialogs.ask_marker_label(
            self._dialog_parent(), default_label, clicked_at,
            process_name=entry.process_name, pid=entry.pid)
        if label is None:
            return
        if not label.strip():
            self._warn("打点名称不能为空")
            return

        marker = self.manager.add_marker(
            task_id, label, clicked_at, kind=default_marker_kind(default_label, label))
        if marker is None:
            self.services.notifier.error(self.host, "打点失败", "未能保存打点，请查看日志")
            return
        self.services.notifier.success(
            self.host, "已打点",
            f"{marker.label} · {marker.timestamp.strftime('%H:%M:%S')}", 2000)

    # ========== 免弹窗打点（托盘菜单 / 全局快捷键） ==========

    def quick_marker(self, task_id: Optional[str] = None) -> List[Any]:
        """不弹窗，直接用默认名称（基线 / 第 N 轮）在当前时刻打点，返回写入的打点。

        task_id 给出时只打这个任务（托盘子菜单）；为 None 时打全部运行中/已暂停的
        任务（全局快捷键、只有一个任务时的托盘菜单）——同一轮操作对各被测进程是同一个
        时刻。结果经 quick_marker_feedback 交给主窗口弹托盘气泡。
        """
        if self._read_only_reason is not None:
            # 托盘与全局快捷键也不能写只读库（正常情况下只读时本来就没有运行中的任务）
            self.quick_marker_feedback.emit(
                QUICK_MARKER_FAILED, READ_ONLY_TITLE, READ_ONLY_MARKER_BLOCKED)
            return []
        ids = self.active_task_ids()
        if task_id is not None:
            ids = [task_id] if task_id in ids else []
        if not ids:
            self.quick_marker_feedback.emit(
                QUICK_MARKER_NONE, "没有可打点的任务", "请先在监控页开始监控")
            return []

        when = self.services.clock.now()
        added: List[Tuple[_TaskEntry, Any]] = []
        failed: List[_TaskEntry] = []
        for tid in ids:
            entry = self._tasks[tid]
            label = self.manager.next_marker_label(tid)
            marker = self.manager.add_marker(
                tid, label, when, kind=default_marker_kind(label, label))
            if marker is None:
                failed.append(entry)
            else:
                added.append((entry, marker))

        clock_text = when.strftime('%H:%M:%S')
        if failed:
            names = "、".join(entry.process_name for entry in failed)
            self.quick_marker_feedback.emit(
                QUICK_MARKER_FAILED, "打点失败", f"{names}：未能保存打点，请查看日志")
        if len(added) == 1:
            entry, marker = added[0]
            self.quick_marker_feedback.emit(
                QUICK_MARKER_OK, f"已打点：{marker.label} {clock_text}",
                f"{entry.process_name} · PID {entry.pid}")
        elif added:
            self.quick_marker_feedback.emit(
                QUICK_MARKER_OK, f"已打点：{len(added)} 个任务 {clock_text}",
                "；".join(f"{entry.process_name}：{marker.label}" for entry, marker in added))
        return [marker for _entry, marker in added]

    def on_restart(self, task_id: str) -> Optional[str]:
        """已结束卡片的"同配置再测"：本页启动过被测程序的任务直接重新启动它再监控；
        其余按进程名找到唯一的运行进程时，以相同指标/间隔/备注新建任务；找不到或有多个时
        把进程名填入搜索框，由用户自己选。"""
        entry = self._tasks.get(task_id)
        if entry is None:
            return None
        if entry.launch is not None:
            # 启动型任务：直接重新启动同一个程序再监控（沿用参数与"停止时关闭"）
            return self.start_monitoring(TaskRequest(
                pid=0, name=entry.launch.name, create_time=None,
                metrics=entry.metric_types,
                interval=entry.interval or self.form.interval(), note=entry.note,
                plan=entry.plan, launch=entry.launch, close_on_stop=entry.close_on_stop))

        # 精确匹配（不区分大小写）并排除本程序自身：被测的同名进程（如 python.exe）
        # 退出后，不能因为只剩本程序同名而自动监控起自己
        matches = self.services.process_query.find_pids_by_name(entry.process_name)
        if len(matches) == 1:
            pid, create_time = matches[0]
            return self.start_monitoring(TaskRequest(
                pid=pid, name=entry.process_name, create_time=create_time,
                metrics=entry.metric_types,
                interval=entry.interval or self.form.interval(), note=entry.note,
                plan=entry.plan))

        self.form.fill_search(entry.process_name)
        if entry.plan is not None:
            # 交给用户自己选进程时，把上次的自动化方案填回表单，"开始"时一并沿用
            self.form.set_automation_plan(entry.plan)
        if matches:
            self._warn(
                f"找到 {len(matches)} 个 {entry.process_name} 进程，"
                "请在上方列表中选择要监控的一个",
                title="请选择进程", duration=RESTART_HINT_INFOBAR_MS)
        else:
            self._warn(
                f"当前没有运行中的 {entry.process_name}；可勾选“等待进程启动后自动开始”再点开始",
                title="未找到进程", duration=RESTART_HINT_INFOBAR_MS)
        return None

    def on_close(self, task_id: str) -> None:
        """从列表移除卡片（历史数据仍保留）。"""
        had_tasks = self._has_items()
        self._tasks.pop(task_id, None)
        self.board.remove_card(task_id)
        self.snapshot_writer.forget(task_id)
        self._sync_counts()
        self._set_has_tasks(had_tasks)

    def on_clear_finished(self) -> None:
        """"已结束"分组的"清除全部"：移除全部已结束卡片（数据仍保留在分析页）。"""
        finished = [task_id for task_id, entry in self._tasks.items() if entry.finished]
        if not finished:
            return
        had_tasks = self._has_items()
        for task_id in finished:
            self._tasks.pop(task_id, None)
            self.board.remove_card(task_id)
            self.snapshot_writer.forget(task_id)
        self._sync_counts()
        self._set_has_tasks(had_tasks)


def _unique_path(path: str) -> str:
    """目标已存在时在扩展名前加 _2、_3……：自动导出没人确认覆盖，不能覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"
