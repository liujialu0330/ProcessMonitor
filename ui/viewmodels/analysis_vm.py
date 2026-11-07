"""
分析页视图模型（无控件）

在历史数据视图模型 HistoryViewModel（任务列表、选择规则、异步查询与过期判断、自动刷新、打点变化重查）
之上扩展：
- 多指标：一次加载对任务的每个指标各发一个 run_history_query（analysis=True，带 min/max
  带与打点区间），经同一个执行器并行跑在后台线程；全部返回后合并为 TaskAnalysis，
  按"批次序号"整体判过期。当前指标（current_metric）只是"焦点"：决定明细表、区间表与
  概览条显示哪个指标，切换焦点直接用已有结果，不再查库；
- 自定义范围：概览条刷选 → set_custom_range(start, end)，范围键为 "custom"；换任务时
  自动回到"全部"（自定义起止是某个任务的绝对时刻）；
- 视图模式：小倍数 / 叠加 Δ%，绝对 / 相对时间（纯显示状态，不查库）；
- 任务对比：选第二个任务后按焦点指标查它的快照（同样异步），与当前任务的快照配对；
- 导出：报告/CSV/图片在 ReportWorker 线程里写文件，登记到线程登记表，并与清理数据库
  互斥（manager.begin_export / end_export）；
- 打点编辑：右键菜单的"在此处打点 / 设为基线 / 重命名 / 删除"经 manager 执行，
  manager.markers_changed 触发静默重查；
- 进程组任务（进程树 / 同名进程）：同一批加载里另有一个后台任务读全部成员的曲线与成员概况
  （MemberRequest → MemberBundle，整批放进一个线程），与各指标的合计快照凑齐才发布。
  "成员"选择（current_member：None 为合计）只改显示：选中某成员后 analysis_ready 与
  snapshot_ready 发的都是该成员的序列（指标键加 @成员键），切换不再查库；last_analysis、
  last_snapshot 始终是合计，报告、导出与任务对比因此不受成员选择影响。
- 逐启动段判定：被测进程重启续接过的任务，合计与成员快照都回来后再在后台线程逐段判定
  （core.history_query.load_segments，与 pmctl、证据包同一函数），随 TaskAnalysis.segments
  一起发布；没有重启的任务只有一段，直接用已有快照构造，不查库。
确认、输入名称、选择保存路径与提示都由页面负责。
"""
import itertools
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from core.report_worker import ReportJob, ReportResult, ReportWorker
from core.history_query import (
    BASELINE_RANGE_KEY, CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY, RANGE_KEYS,
    HistorySnapshot, SegmentAnalysis, TaskAnalysis, build_request, build_segment)
from core.monitor_manager import default_marker_kind
from core.report import gap_threshold_seconds, metadata_lines, summary_rows
from core.workers.base import LatestRequestGate
from core.workers.member_snapshots_worker import (
    MemberBundle, MemberRequest, MemberSnapshotsWorker, load_member_bundle)
from core.workers.segments_worker import (
    SegmentsBundle, SegmentsRequest, SegmentsWorker, load_segments_bundle)
from data.models import TARGET_PID
from ui.viewmodels.history_vm import HistoryViewModel
from utils.metrics import member_series_key, split_member_key

logger = logging.getLogger(__name__)

VIEW_SMALL = 'small'
VIEW_OVERLAY = 'overlay'
VIEW_MODES = (VIEW_SMALL, VIEW_OVERLAY)
TIME_ABSOLUTE = 'absolute'
TIME_RELATIVE = 'relative'
TIME_MODES = (TIME_ABSOLUTE, TIME_RELATIVE)

# 刷选短于这个时长视为误触（单击概览条），不切换范围
MIN_CUSTOM_RANGE_SECONDS = 2.0

RANGE_TEXTS = {
    BASELINE_RANGE_KEY: '自基线', '10m': '最近 10 分钟', '1h': '最近 1 小时',
    '6h': '最近 6 小时', '24h': '最近 24 小时', 'all': '全部',
}

# 导出被拒的原因
EXPORT_BUSY = 'busy'
EXPORT_MAINTENANCE = 'maintenance'

COMPARE_MISSING_METRIC = "对比任务没有采集该指标"
EMPTY_MEMBER_NO_DATA = ("该成员在所选范围内暂无数据", "切回“合计”，或换一个成员 / 范围查看")

# 成员加载执行器：runner(request, on_done)。on_done(bundle) 必须在主线程调用，且每个请求
# 恰好一次（失败也要回一个 error 非空的 MemberBundle）
MemberRunner = Callable[[MemberRequest, Callable[[MemberBundle], None]], None]


# 分段判定执行器：runner(request, on_done)，约定同 MemberRunner
SegmentsRunner = Callable[[SegmentsRequest, Callable[[SegmentsBundle], None]], None]


@dataclass(frozen=True)
class MemberOption:
    """成员下拉的一项：成员键、显示名（保留大小写）、出现过的实例数（未知为 None）。"""
    key: str
    name: str
    instances: Optional[int] = None


class MemberWorkerRunner(QObject):
    """生产用成员加载执行器：每个请求一个 MemberSnapshotsWorker，登记到线程登记表
    （与 HistoryWorkerRunner 同样的收尾约定：线程没送出结果就结束时补一个失败的 MemberBundle）。"""

    def __init__(self, db, threads, parent=None):
        super().__init__(parent)
        self._db = db
        self._threads = threads
        self._pending: Dict[int, tuple] = {}

    def __call__(self, request: MemberRequest,
                 on_done: Callable[[MemberBundle], None]) -> None:
        worker = MemberSnapshotsWorker(self._db, request)
        self._pending[request.request_seq] = (worker, on_done)
        worker.result_ready.connect(self._on_result)
        worker.finished.connect(self._on_finished)
        self._threads.register(worker, name="进程组成员查询", timeout_ms=3000)
        worker.start()

    @pyqtSlot(object)
    def _on_result(self, bundle: MemberBundle) -> None:
        entry = self._pending.pop(bundle.request.request_seq, None)
        if entry is not None:
            entry[1](bundle)

    @pyqtSlot()
    def _on_finished(self) -> None:
        worker = self.sender()
        entry = self._pending.pop(getattr(worker, 'request_seq', None), None)
        if entry is not None:
            entry[1](MemberBundle(entry[0].request, error="后台查询异常结束"))


class SegmentsWorkerRunner(QObject):
    """生产用分段判定执行器：每个请求一个 SegmentsWorker，登记到线程登记表（收尾约定同
    MemberWorkerRunner：线程没送出结果就结束时补一个失败的 SegmentsBundle）。"""

    def __init__(self, db, threads, parent=None):
        super().__init__(parent)
        self._db = db
        self._threads = threads
        self._pending: Dict[int, tuple] = {}

    def __call__(self, request: SegmentsRequest,
                 on_done: Callable[[SegmentsBundle], None]) -> None:
        worker = SegmentsWorker(self._db, request)
        self._pending[request.request_seq] = (worker, on_done)
        worker.result_ready.connect(self._on_result)
        worker.finished.connect(self._on_finished)
        self._threads.register(worker, name="分段判定查询", timeout_ms=3000)
        worker.start()

    @pyqtSlot(object)
    def _on_result(self, bundle: SegmentsBundle) -> None:
        entry = self._pending.pop(bundle.request.request_seq, None)
        if entry is not None:
            entry[1](bundle)

    @pyqtSlot()
    def _on_finished(self) -> None:
        worker = self.sender()
        entry = self._pending.pop(getattr(worker, 'request_seq', None), None)
        if entry is not None:
            entry[1](SegmentsBundle(entry[0].request, error="后台查询异常结束"))


def synchronous_segments_runner(db) -> SegmentsRunner:
    """同步执行分段判定的执行器（测试与注入了同步查询执行器的场合用）。"""
    def run(request: SegmentsRequest, on_done: Callable[[SegmentsBundle], None]) -> None:
        try:
            bundle = load_segments_bundle(db, request)
        except Exception as exc:
            bundle = SegmentsBundle(request, error=str(exc) or type(exc).__name__)
        if bundle is not None:
            on_done(bundle)
    return run


def synchronous_member_runner(db) -> MemberRunner:
    """同步执行成员加载的执行器（测试与注入了同步查询执行器的场合用）。"""
    def run(request: MemberRequest, on_done: Callable[[MemberBundle], None]) -> None:
        on_done(load_member_bundle(db, request))
    return run


def range_text(range_key: str, custom: Optional[Tuple[datetime, datetime]] = None,
               baseline_at: Optional[datetime] = None) -> str:
    """范围的说明文字（报告信息行与 CSV 元数据用）。"""
    if range_key == CUSTOM_RANGE_KEY and custom is not None:
        start, end = custom
        return f"自定义 {start:%m-%d %H:%M:%S} – {end:%m-%d %H:%M:%S}"
    if range_key == BASELINE_RANGE_KEY and baseline_at is not None:
        return f"自基线（{baseline_at:%H:%M:%S} 起）"
    return RANGE_TEXTS.get(range_key, range_key)


def analysis_origin(snapshot: Optional[HistorySnapshot]) -> Optional[datetime]:
    """相对时间的原点：有基线取基线时刻，否则取显示范围的第一个点。"""
    if snapshot is None:
        return None
    if snapshot.baseline is not None:
        return snapshot.baseline.timestamp
    for series in (snapshot.band, snapshot.buckets, snapshot.points):
        if series:
            first = series[0]
            return getattr(first, 'start', None) or first.timestamp
    return None


@dataclass(frozen=True)
class CompareResult:
    """任务对比：同一指标下当前任务与对比任务的快照；other 为 None 时 message 说明原因。"""
    metric: Optional[str]
    primary: HistorySnapshot
    other_task: object
    other: Optional[HistorySnapshot] = None
    message: str = ""


@dataclass
class _Batch:
    seq: int
    task_id: str
    range_key: str
    auto_refresh: bool
    snapshots: Dict[str, Optional[HistorySnapshot]] = field(default_factory=dict)
    # 进程组任务：成员曲线还没回来（与各指标的合计快照一起凑齐才算完成）
    members_pending: bool = False
    bundle: Optional[MemberBundle] = None
    # 逐段判定：多个启动段的任务要在后台再查（segments_started 防重复发起）；
    # segments 为 None 表示没取到（失败或没做），发布时沿用同一任务上一次的
    segments_started: bool = False
    segments_pending: bool = False
    segments: Optional[List[SegmentAnalysis]] = None

    @property
    def parts_done(self) -> bool:
        return not self.members_pending and all(s is not None for s in self.snapshots.values())

    @property
    def complete(self) -> bool:
        return self.parts_done and not self.segments_pending


class AnalysisViewModel(HistoryViewModel):
    """分析页视图模型（信号在 HistoryViewModel 的基础上增加）：

    - analysis_ready(object)：TaskAnalysis，紧接着会发焦点指标的 snapshot_ready
    - custom_range_changed(object, object)：自定义范围 (start, end)；清除时为 (None, None)
    - view_mode_changed(str) / time_mode_changed(str)
    - compare_ready(object)：CompareResult，关闭对比时为 None
    - export_busy(bool)：导出线程开始/结束
    - export_progress(int)：已处理的数据点数
    - export_finished(object)：ReportResult
    - export_failed(str)：失败说明
    - members_changed(list)：进程组任务的成员选项（MemberOption，按显示名排序）；非进程组或
      没有成员时为空列表
    - member_selection_changed(object)：当前成员键，None 为合计

    analysis_ready 带的是"当前显示的"分析结果：合计视图是 last_analysis 本身，成员视图是只含
    该成员序列（键为 指标@成员键）的 TaskAnalysis（member_snapshots 仍是全部成员）。
    """

    analysis_ready = pyqtSignal(object)
    members_changed = pyqtSignal(list)
    member_selection_changed = pyqtSignal(object)
    custom_range_changed = pyqtSignal(object, object)
    view_mode_changed = pyqtSignal(str)
    time_mode_changed = pyqtSignal(str)
    compare_ready = pyqtSignal(object)
    export_busy = pyqtSignal(bool)
    export_progress = pyqtSignal(int)
    export_finished = pyqtSignal(object)
    export_failed = pyqtSignal(str)

    def __init__(self, db, manager, services, *, member_runner: Optional[MemberRunner] = None,
                 segments_runner: Optional[SegmentsRunner] = None, **options):
        super().__init__(db, manager, services, **options)
        # 注入了查询执行器（测试）而没给成员 / 分段执行器时，它们也同步加载；生产环境走后台线程
        injected = options.get('worker_runner') is not None
        if member_runner is None:
            member_runner = (synchronous_member_runner(db) if injected
                             else MemberWorkerRunner(db, services.threads, parent=self))
        self._member_runner: MemberRunner = member_runner
        if segments_runner is None:
            segments_runner = (synchronous_segments_runner(db) if injected
                               else SegmentsWorkerRunner(db, services.threads, parent=self))
        self._segments_runner: SegmentsRunner = segments_runner
        self._segment_parts: Dict[int, _Batch] = {}
        self.current_member: Optional[str] = None
        self.member_options: List[MemberOption] = []
        self._pending_bundle: Optional[MemberBundle] = None
        self.view_mode: str = VIEW_SMALL
        self.time_mode: str = TIME_ABSOLUTE
        self.custom_range: Optional[Tuple[datetime, datetime]] = None
        self.last_analysis: Optional[TaskAnalysis] = None
        self.compare_task_id: Optional[str] = None
        self.last_compare: Optional[CompareResult] = None
        # 执行器按请求序号区分在途请求：各指标的子请求用独立递增的序号，批次另有序号
        self._part_seq = itertools.count(1)
        self._parts: Dict[int, Tuple[_Batch, str]] = {}
        self._pending_analysis: Optional[TaskAnalysis] = None
        self._member_parts: Dict[int, _Batch] = {}
        self._compare_gate = LatestRequestGate()
        self._compare_parts: Dict[int, Tuple[int, object]] = {}
        self._report_worker: Optional[ReportWorker] = None

    # ========== 只读属性 ==========

    @property
    def export_worker(self) -> Optional[ReportWorker]:
        """进行中的导出线程（没有导出时为 None）。"""
        return self._report_worker

    def is_maintenance_running(self) -> bool:
        """是否正在清理数据库（导出会与之抢锁，页面据此先拒绝）。"""
        return self._manager.is_maintenance_running()

    def range_description(self) -> str:
        baseline = self.last_snapshot.baseline if self.last_snapshot else None
        return range_text(self.range_key, self.custom_range,
                          baseline.timestamp if baseline is not None else None)

    # ========== 范围与视图 ==========

    def set_range(self, range_key: str) -> None:
        """切换预设范围（自定义范围请用 set_custom_range）；会清除自定义范围。"""
        if range_key == CUSTOM_RANGE_KEY:
            if self.custom_range is None:
                raise ValueError("没有自定义范围：请先刷选")
            return
        if range_key not in RANGE_KEYS:
            raise ValueError(f"未知的时间范围：{range_key!r}")
        if self.custom_range is not None:
            self.custom_range = None
            self.custom_range_changed.emit(None, None)
        super().set_range(range_key)

    def set_custom_range(self, start: datetime, end: datetime) -> bool:
        """刷选出的统计范围（两端含）。过短视为误触，返回 False 且不改变范围。"""
        if end < start:
            start, end = end, start
        if (end - start).total_seconds() < MIN_CUSTOM_RANGE_SECONDS:
            return False
        if self.custom_range == (start, end) and self.range_key == CUSTOM_RANGE_KEY:
            return True
        self.custom_range = (start, end)
        self.range_key = CUSTOM_RANGE_KEY
        self.custom_range_changed.emit(start, end)
        if self.current_task_id:
            self._request_load()
        return True

    def set_view_mode(self, mode: str) -> None:
        if mode not in VIEW_MODES:
            raise ValueError(f"未知的视图：{mode!r}")
        if mode != self.view_mode:
            self.view_mode = mode
            self.view_mode_changed.emit(mode)

    def set_time_mode(self, mode: str) -> None:
        if mode not in TIME_MODES:
            raise ValueError(f"未知的时间模式：{mode!r}")
        if mode != self.time_mode:
            self.time_mode = mode
            self.time_mode_changed.emit(mode)

    def select_metric(self, metric: str) -> None:
        """切换焦点指标：已有该指标的结果时直接显示，不再查库。成员视图里表格与图上用的是
        序列键（指标@成员键），这里一并认；该成员没有这个指标的数据时忽略。"""
        metric = split_member_key(metric)[0]
        if not self.current_task_id or metric == self.current_metric:
            return
        if metric not in self.metrics:
            return
        if self.current_member is not None and self.last_analysis is not None:
            shown = self.last_analysis.member_snapshot(metric, self.current_member)
            if shown is None or not shown.points:
                return
        self.current_metric = metric
        self.selection_changed.emit(self.current_task_id, metric, not self.selection_is_auto)
        analysis = self.last_analysis
        cached = (analysis.snapshot(metric)
                  if analysis is not None and analysis.task_id == self.current_task_id
                  else None)
        if cached is not None and cached.ok and cached.points and not self.has_pending_query:
            self._publish_snapshot(cached)
            self._refresh_compare()
        else:
            self._request_load()

    def _apply_selection(self, task, preferred_metric: Optional[str]) -> None:
        if self.range_key == CUSTOM_RANGE_KEY and task.task_id != self.current_task_id:
            # 自定义起止是上一个任务的绝对时刻，换任务后回到"全部"
            self.custom_range = None
            self.range_key = DEFAULT_RANGE_KEY
            self.custom_range_changed.emit(None, None)
            self.range_reset.emit(DEFAULT_RANGE_KEY)
        if task.task_id != self.current_task_id and self.compare_task_id == task.task_id:
            self.set_compare_task(None)
        if task.task_id != self.current_task_id:
            self._reset_members()
        super()._apply_selection(task, preferred_metric)

    def _clear_selection(self) -> None:
        self._reset_members()
        super()._clear_selection()

    # ========== 多指标查询 ==========

    def _start_query(self, auto_refresh: bool, forced: bool = False) -> None:
        task_id = self.current_task_id
        metrics = self.metrics or [self.current_metric]
        group = self.is_group_task
        batch = _Batch(self._gate.next_seq(), task_id, self.range_key, auto_refresh,
                       {metric: None for metric in metrics}, members_pending=group)
        self._inflight.add(batch.seq)
        if forced:
            self._forced_seqs.add(batch.seq)
        since, until = self.custom_range or (None, None)
        anchor = self._cached_anchor(task_id)
        requests = []
        for metric in metrics:
            request = build_request(
                task_id, metric, self.range_key, anchor=anchor,
                table_limit=self._table_limit, max_buckets=self._max_buckets,
                seq=next(self._part_seq), auto_refresh=auto_refresh, analysis=True,
                since=since, until=until)
            self._parts[request.request_seq] = (batch, metric)
            requests.append(request)
        member_request = None
        if group:
            member_request = MemberRequest(
                task_id, tuple(metrics), self.range_key, since=since, until=until,
                table_limit=self._table_limit, max_buckets=self._max_buckets,
                request_seq=next(self._part_seq))
            self._member_parts[member_request.request_seq] = batch
        for request in requests:
            self._runner(request, self._on_part)
        if member_request is not None:
            self._member_runner(member_request, self._on_member_part)

    @pyqtSlot(object)
    def _on_member_part(self, bundle: MemberBundle) -> None:
        batch = self._member_parts.pop(bundle.request.request_seq, None)
        if batch is None:
            return
        batch.members_pending = False
        batch.bundle = bundle
        self._advance(batch)

    @pyqtSlot(object)
    def _on_part(self, snapshot: HistorySnapshot) -> None:
        entry = self._parts.pop(snapshot.request.request_seq, None)
        if entry is None:
            return
        batch, metric = entry
        batch.snapshots[metric] = snapshot
        self._advance(batch)

    def _advance(self, batch: _Batch) -> None:
        """合计与成员都回来后：多个启动段的任务先去后台做逐段判定，其余直接发布。"""
        if not batch.parts_done or batch.segments_pending:
            return
        if not batch.segments_started:
            batch.segments_started = True
            if self._start_segments(batch):
                return
        self._finish_batch(batch)

    def _start_segments(self, batch: _Batch) -> bool:
        """准备本批的逐段判定。只有一个启动段（没有重启）时用已有快照直接构造，不查库；
        有多个启动段则发给后台执行器，返回 True 表示要等它的结果。"""
        snapshots = dict(batch.snapshots)
        usable = [s for s in snapshots.values() if s.error is None and not s.task_missing]
        if not usable or len(usable) != len(snapshots) or not self._gate.is_current(batch.seq):
            return False
        members, _bundle = self._members_of(batch)
        focus = usable[0]
        if focus.segment_count <= 1:
            batch.segments = [build_segment(1, None, None, {**snapshots, **members})]
            return False
        request = SegmentsRequest(
            batch.task_id, tuple(snapshots), snapshots, members,
            # 统计落在最后一段才能把这批快照当作最后一段的依据（自定义范围可能落在更早的段）
            reuse_last=focus.segment_number == focus.segment_count,
            request_seq=next(self._part_seq))
        self._segment_parts[request.request_seq] = batch
        batch.segments_pending = True
        self._segments_runner(request, self._on_segments_part)
        return True

    @pyqtSlot(object)
    def _on_segments_part(self, bundle: SegmentsBundle) -> None:
        batch = self._segment_parts.pop(bundle.request.request_seq, None)
        if batch is None:
            return
        batch.segments_pending = False
        if bundle.error is None:
            batch.segments = bundle.segments
        else:
            logger.warning("逐段判定加载失败，沿用上一次的结果: %s", bundle.error)
        self._finish_batch(batch)

    def _segments_of(self, batch: _Batch) -> List[SegmentAnalysis]:
        """一批加载的逐段判定；没取到时沿用同一任务上一次的（一次读库失败不应让分段信息
        凭空消失），没有就为空（判定退回只看当前快照）。"""
        if batch.segments is not None:
            return batch.segments
        previous = self.last_analysis
        if previous is not None and previous.task_id == batch.task_id:
            return list(previous.segments)
        return []

    def _finish_batch(self, batch: _Batch) -> None:
        self._inflight.discard(batch.seq)
        forced = batch.seq in self._forced_seqs
        self._forced_seqs.discard(batch.seq)
        if not self._gate.is_current(batch.seq):
            return
        snapshots: Dict[str, HistorySnapshot] = dict(batch.snapshots)
        focus = snapshots.get(self.current_metric) or next(iter(snapshots.values()))
        failed = next((s for s in snapshots.values() if s.error is not None), None)
        if failed is not None and focus.error is None:
            # 任一指标读取失败都按整批失败处理（摘要表不能缺行却不说明）
            focus = replace(focus, error=failed.error, storage_error=failed.storage_error)
        members, bundle = self._members_of(batch)
        self._pending_analysis = TaskAnalysis(
            batch.task_id, batch.range_key, snapshots, batch.seq, batch.auto_refresh,
            member_snapshots=members, segments=self._segments_of(batch))
        self._pending_bundle = bundle
        try:
            self._apply_snapshot(focus, forced)
        finally:
            self._pending_analysis = None
            self._pending_bundle = None
            if not batch.auto_refresh and self._gate.is_current(batch.seq):
                self._set_busy(False)

    def _emit_snapshot(self, snapshot: HistorySnapshot) -> None:
        if self._pending_analysis is not None:
            self.last_analysis = self._pending_analysis
            self._reconcile_members(self._pending_bundle)
            if not self._publish_analysis():
                # 选着的成员在这个范围内没有数据：页面已切到空状态，合计结果留着供切回
                self.last_snapshot = snapshot
                return
        self._publish_snapshot(snapshot)
        self._refresh_compare(requery=not snapshot.request.auto_refresh)

    def _show_empty(self, title: str, detail: str) -> None:
        self.last_analysis = None
        self._reset_members()
        super()._show_empty(title, detail)

    # ========== 进程组：成员 ==========

    @property
    def is_group_task(self) -> bool:
        """当前任务是否进程组任务（进程树 / 同名进程）。"""
        task = self.current_task
        return task is not None and getattr(task, 'target_kind', TARGET_PID) != TARGET_PID

    def series_key(self, metric: Optional[str]) -> Optional[str]:
        """指标在当前视图里的键：合计视图是指标本身，成员视图是 指标@成员键。"""
        if metric is None or self.current_member is None:
            return metric
        return member_series_key(metric, self.current_member)

    def member_name(self, member: Optional[str]) -> str:
        """成员的显示名（保留大小写）；未知时退回成员键。"""
        option = next((o for o in self.member_options if o.key == member), None)
        return option.name if option is not None else (member or "")

    def member_instances(self, member: Optional[str]) -> Optional[int]:
        """成员出现过的不同实例数（含已退出的）；未知为 None。"""
        option = next((o for o in self.member_options if o.key == member), None)
        return option.instances if option is not None else None

    def select_member(self, member: Optional[str]) -> bool:
        """选择要看的成员（None 为合计）。不是当前任务的成员时忽略并返回 False；切换只改显示，
        用已加载的成员曲线重画整页，不再查库。"""
        if member is not None and all(o.key != member for o in self.member_options):
            return False
        if member == self.current_member:
            return True
        self.current_member = member
        self.member_selection_changed.emit(member)
        if self.last_analysis is not None and self.last_analysis.task_id == self.current_task_id:
            self._republish()
        return True

    def _reset_members(self) -> None:
        """换任务 / 清空选择：回到合计并清空成员选项。"""
        if self.member_options:
            self.member_options = []
            self.members_changed.emit([])
        if self.current_member is not None:
            self.current_member = None
            self.member_selection_changed.emit(None)

    def _members_of(self, batch: _Batch):
        """一批加载里的成员曲线与成员概况；成员加载失败时沿用同一任务上一次的结果
        （一次读库失败不应让成员选择凭空消失）。"""
        bundle = batch.bundle
        if bundle is not None and bundle.error is None:
            return dict(bundle.snapshots), bundle
        if bundle is not None:
            logger.warning("成员曲线加载失败，沿用上一次的结果: %s", bundle.error)
        previous = self.last_analysis
        if previous is not None and previous.task_id == batch.task_id:
            return dict(previous.member_snapshots), None
        return {}, None

    def _reconcile_members(self, bundle: Optional[MemberBundle]) -> None:
        """按最新的成员曲线刷新成员选项；当前选中的成员已不存在时回到合计。"""
        analysis = self.last_analysis
        keys = analysis.member_keys if analysis is not None else []
        if bundle is not None:
            known = {m.member_key: (m.name, m.instance_count) for m in bundle.summaries}
        else:
            known = {o.key: (o.name, o.instances) for o in self.member_options}
        options = []
        for key in keys:
            name, instances = known.get(key, (None, None))
            options.append(MemberOption(key, name or key, instances))
        options.sort(key=lambda o: (o.name.casefold(), o.key))
        if options != self.member_options:
            self.member_options = options
            self.members_changed.emit(list(options))
        if self.current_member is not None and self.current_member not in keys:
            self.current_member = None
            self.member_selection_changed.emit(None)

    def view_analysis(self, auto_refresh: Optional[bool] = None) -> Optional[TaskAnalysis]:
        """当前显示的分析结果：合计视图就是 last_analysis；成员视图只含该成员的序列
        （键为 指标@成员键，缺数据的指标不在其中），member_snapshots 仍是全部成员。"""
        analysis = self.last_analysis
        member = self.current_member
        if analysis is None or member is None:
            return analysis
        shown = {}
        for metric in analysis.snapshots:
            snapshot = analysis.member_snapshot(metric, member)
            if snapshot is not None and snapshot.points:
                shown[member_series_key(metric, member)] = snapshot
        return TaskAnalysis(
            analysis.task_id, analysis.range_key, shown, analysis.request_seq,
            analysis.auto_refresh if auto_refresh is None else auto_refresh,
            member_snapshots=analysis.member_snapshots, segments=analysis.segments)

    def shown_snapshots(self) -> List[HistorySnapshot]:
        """当前视图里有数据的快照（页面算相对时间原点用）。"""
        view = self.view_analysis()
        return [s for s in view.snapshots.values() if s.points] if view is not None else []

    def _settle_focus(self) -> None:
        """成员视图里该成员没有焦点指标的数据时，焦点改到它第一个有数据的指标。"""
        analysis, member = self.last_analysis, self.current_member
        if analysis is None or member is None:
            return
        shown = analysis.member_snapshot(self.current_metric, member)
        if shown is not None and shown.points:
            return
        for metric in analysis.snapshots:
            candidate = analysis.member_snapshot(metric, member)
            if candidate is not None and candidate.points:
                self.current_metric = metric
                self.selection_changed.emit(
                    self.current_task_id, metric, not self.selection_is_auto)
                return

    def _publish_analysis(self, auto_refresh: Optional[bool] = None) -> bool:
        """发布当前视图的分析结果；成员视图里该成员没有任何数据时改发空状态并返回 False
        （last_analysis 保留，切回合计可直接重画）。"""
        self._settle_focus()
        view = self.view_analysis(auto_refresh)
        if self.current_member is not None and not view.snapshots:
            self.last_snapshot = None
            self.empty_state.emit(*EMPTY_MEMBER_NO_DATA)
            return False
        self.analysis_ready.emit(view)
        return True

    def _shown_snapshot(self, snapshot: HistorySnapshot) -> HistorySnapshot:
        """页面该画的焦点快照：成员视图换成该成员同一指标的序列。"""
        analysis, member = self.last_analysis, self.current_member
        if member is None or analysis is None:
            return snapshot
        shown = analysis.member_snapshot(self.current_metric, member)
        return shown if shown is not None and shown.points else snapshot

    def _publish_snapshot(self, snapshot: HistorySnapshot) -> None:
        """记下合计的焦点快照，把页面该画的（合计或成员的）发给页面。"""
        self.last_snapshot = snapshot
        self.snapshot_ready.emit(self._shown_snapshot(snapshot))

    def _republish(self) -> None:
        """切换成员后用已加载的结果重画整页（不保留缩放：换了一套曲线）。"""
        analysis = self.last_analysis
        if analysis is None or not self._publish_analysis(auto_refresh=False):
            return
        focus = analysis.snapshot(self.current_metric) or next(iter(analysis.snapshots.values()))
        self._publish_snapshot(focus)

    # ========== 任务对比 ==========

    def compare_candidates(self) -> list:
        """可选作对比的任务：当前列表里除当前任务以外的任务（最新在前）。"""
        return [t for t in self.tasks if t.task_id != self.current_task_id]

    def set_compare_task(self, task_id: Optional[str]) -> None:
        """选择对比任务（None 关闭对比）。"""
        if task_id == self.current_task_id:
            task_id = None
        self.compare_task_id = task_id
        self._compare_gate.invalidate()
        if task_id is None:
            self.last_compare = None
            self.compare_ready.emit(None)
            return
        self._refresh_compare(requery=True)

    def _refresh_compare(self, requery: bool = True) -> None:
        primary = self.last_snapshot
        if self.compare_task_id is None or primary is None:
            return
        other_task = self._find_task(self.compare_task_id)
        if other_task is None:
            self.set_compare_task(None)
            return
        metric = primary.request.metric
        last = self.last_compare
        if (not requery and last is not None and last.metric == metric
                and last.other_task is not None
                and last.other_task.task_id == other_task.task_id):
            self._emit_compare(replace(last, primary=primary))
            return
        if metric not in other_task.metric_types:
            self._emit_compare(CompareResult(metric, primary, other_task,
                                             message=COMPARE_MISSING_METRIC))
            return
        range_key = self.range_key if self.range_key in RANGE_KEYS else DEFAULT_RANGE_KEY
        request = build_request(
            other_task.task_id, metric, range_key, table_limit=self._table_limit,
            max_buckets=self._max_buckets, seq=next(self._part_seq), analysis=True)
        self._compare_parts[request.request_seq] = (self._compare_gate.next_seq(), other_task)
        self._runner(request, self._on_compare_part)

    @pyqtSlot(object)
    def _on_compare_part(self, snapshot: HistorySnapshot) -> None:
        entry = self._compare_parts.pop(snapshot.request.request_seq, None)
        if entry is None or not self._compare_gate.is_current(entry[0]):
            return
        primary = self.last_snapshot
        if primary is None:
            return
        message = "" if snapshot.ok else (snapshot.error or "对比任务读取失败")
        self._emit_compare(CompareResult(
            snapshot.request.metric, primary, entry[1],
            snapshot if snapshot.ok else None, message))

    def _emit_compare(self, result: CompareResult) -> None:
        self.last_compare = result
        self.compare_ready.emit(result)

    # ========== 打点编辑 ==========

    def next_marker_label(self) -> str:
        return self._manager.next_marker_label(self.current_task_id)

    def add_marker_at(self, timestamp: datetime, label: str, default_label: str) -> bool:
        """在指定时刻补加打点；类型按点击时的默认标签决定（默认是"基线"即为基线）。"""
        if not self.current_task_id:
            return False
        kind = default_marker_kind(default_label, label)
        return self._manager.add_marker(
            self.current_task_id, label, timestamp, kind=kind) is not None

    def set_baseline_marker(self, marker) -> bool:
        return bool(self.current_task_id) and self._manager.set_baseline(
            self.current_task_id, marker.id)

    def rename_marker(self, marker, label: str) -> bool:
        return self._manager.rename_marker(marker.id, label, self.current_task_id)

    def delete_marker(self, marker) -> bool:
        return self._manager.delete_marker(marker.id, self.current_task_id)

    # ========== 导出 ==========

    def report_job(self, *, image=None, image_path: Optional[str] = None,
                   csv_path: Optional[str] = None,
                   summary_path: Optional[str] = None) -> Optional[ReportJob]:
        """按当前任务与分析结果组装导出任务；未选任务时为 None。"""
        task = self.current_task
        if task is None:
            return None
        running = self._current_is_running()
        end_time = None if running else (task.end_time or task.last_sample_at)
        analysis = self.last_analysis
        rows = summary_rows(analysis.snapshots) if analysis is not None else []
        # 采集次数取任务列表里的记录（不在主线程另查库）；运行中任务以导出时刻为准略有出入
        metadata = metadata_lines(
            task, exported_at=self._services.clock.now(),
            range_text=self.range_description(), sample_count=task.sample_count or None,
            end_time=end_time)
        return ReportJob(
            task=task, csv_path=csv_path, summary_path=summary_path,
            image_path=image_path, image=image, metadata=tuple(metadata),
            summary_rows=tuple(tuple(r) for r in rows),
            gap_seconds=gap_threshold_seconds(task.interval))

    def start_export(self, job: ReportJob,
                     worker_factory: Optional[Callable] = None) -> Optional[str]:
        """
        启动导出线程。被拒时返回原因（EXPORT_BUSY / EXPORT_MAINTENANCE），成功返回 None。
        与清理数据库互斥：维护进行中或登记失败都拒绝。
        """
        if self._report_worker is not None:
            return EXPORT_BUSY
        if self._manager.is_maintenance_running() or not self._manager.begin_export():
            return EXPORT_MAINTENANCE
        factory = worker_factory or ReportWorker
        worker = factory(self._db, job)
        self._report_worker = worker
        worker.progress.connect(self.export_progress)
        worker.result.connect(self._on_export_result)
        worker.failed.connect(self.export_failed)
        # 注销挂在 QThread.finished 上：取消（退出时）不发结果/失败，finished 仍会触发
        worker.finished.connect(lambda w=worker: self._on_export_thread_finished(w))
        self._services.threads.register(
            worker, name="导出报告", cancel=worker.cancel, timeout_ms=2000)
        self.export_busy.emit(True)
        worker.start()
        return None

    @pyqtSlot(object)
    def _on_export_result(self, result: ReportResult) -> None:
        self.export_finished.emit(result)

    def _on_export_thread_finished(self, worker) -> None:
        self._manager.end_export()
        if self._report_worker is worker:
            self._report_worker = None
            self.export_busy.emit(False)

    def chart_snapshots(self) -> List[HistorySnapshot]:
        """当前分析结果里有数据的快照（按任务指标顺序）。"""
        analysis = self.last_analysis
        if analysis is None:
            return []
        return [s for s in analysis.snapshots.values() if s.points]
