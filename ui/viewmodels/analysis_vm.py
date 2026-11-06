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
  manager.markers_changed 触发静默重查。
确认、输入名称、选择保存路径与提示都由页面负责。
"""
import itertools
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import pyqtSignal, pyqtSlot

from core.export_worker import ReportJob, ReportResult, ReportWorker
from core.history_query import (
    BASELINE_RANGE_KEY, CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY, RANGE_KEYS,
    HistorySnapshot, TaskAnalysis, build_request)
from core.monitor_manager import default_marker_kind
from core.report import gap_threshold_seconds, metadata_lines, summary_rows
from core.workers.base import LatestRequestGate
from ui.viewmodels.history_vm import HistoryViewModel

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
EXPORT_NO_DATA = 'no_data'

COMPARE_MISSING_METRIC = "对比任务没有采集该指标"


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

    @property
    def complete(self) -> bool:
        return all(s is not None for s in self.snapshots.values())


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
    """

    analysis_ready = pyqtSignal(object)
    custom_range_changed = pyqtSignal(object, object)
    view_mode_changed = pyqtSignal(str)
    time_mode_changed = pyqtSignal(str)
    compare_ready = pyqtSignal(object)
    export_busy = pyqtSignal(bool)
    export_progress = pyqtSignal(int)
    export_finished = pyqtSignal(object)
    export_failed = pyqtSignal(str)

    def __init__(self, db, manager, services, **options):
        super().__init__(db, manager, services, **options)
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
        """切换焦点指标：已有该指标的结果时直接显示，不再查库。"""
        if not self.current_task_id or metric == self.current_metric:
            return
        if metric not in self.metrics:
            return
        self.current_metric = metric
        self.selection_changed.emit(self.current_task_id, metric, not self.selection_is_auto)
        analysis = self.last_analysis
        cached = (analysis.snapshot(metric)
                  if analysis is not None and analysis.task_id == self.current_task_id
                  else None)
        if cached is not None and cached.ok and cached.points and not self.has_pending_query:
            HistoryViewModel._emit_snapshot(self, cached)
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
        super()._apply_selection(task, preferred_metric)

    # ========== 多指标查询 ==========

    def _start_query(self, auto_refresh: bool, forced: bool = False) -> None:
        task_id = self.current_task_id
        metrics = self.metrics or [self.current_metric]
        batch = _Batch(self._gate.next_seq(), task_id, self.range_key, auto_refresh,
                       {metric: None for metric in metrics})
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
        for request in requests:
            self._runner(request, self._on_part)

    @pyqtSlot(object)
    def _on_part(self, snapshot: HistorySnapshot) -> None:
        entry = self._parts.pop(snapshot.request.request_seq, None)
        if entry is None:
            return
        batch, metric = entry
        batch.snapshots[metric] = snapshot
        if batch.complete:
            self._finish_batch(batch)

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
        self._pending_analysis = TaskAnalysis(
            batch.task_id, batch.range_key, snapshots, batch.seq, batch.auto_refresh)
        try:
            self._apply_snapshot(focus, forced)
        finally:
            self._pending_analysis = None
            if not batch.auto_refresh and self._gate.is_current(batch.seq):
                self._set_busy(False)

    def _emit_snapshot(self, snapshot: HistorySnapshot) -> None:
        if self._pending_analysis is not None:
            self.last_analysis = self._pending_analysis
            self.analysis_ready.emit(self.last_analysis)
        super()._emit_snapshot(snapshot)
        self._refresh_compare(requery=not snapshot.request.auto_refresh)

    def _show_empty(self, title: str, detail: str) -> None:
        self.last_analysis = None
        super()._show_empty(title, detail)

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
