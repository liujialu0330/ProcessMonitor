"""
历史数据视图模型（无控件；分析页视图模型 AnalysisViewModel 的基类）

持有单指标历史查询的业务状态：任务列表、当前任务/指标/时间范围、是否自动选中、可否删除、
加载中；对外只发信号，不持有任何控件、不弹任何提示——页面据信号刷新控件、决定提示。

两条约束：
- 选择只走一条路径：载入任务列表后的"恢复选中 / 自动跟随最新 / 用户选过不切走 /
  选中任务消失后回落或清空"集中在 reload_tasks 里，控件不再需要 blockSignals；
- 数据查询一律异步（经 worker_runner 在后台线程跑 run_history_query），结果用
  LatestRequestGate 判过期。同一事件循环轮次内的多次加载请求合并成一次查询
  （如导航跳转时"页面显示时重载 + 选中目标任务"），页面据 busy 显示加载态。
  任务列表本身（单条 SQL）仍在主线程读取。

打点变化（manager.markers_changed）时静默重查当前任务：基线、编号与统计口径都随打点变。

另有分析页共用的派生口径（纯函数）：ordered_metrics 按判定严重程度排序（持续上涨 → 波动 →
平稳 → 数据不足 → 不判定，同组按相对增速降序），is_anomalous 决定哪些指标默认画图。
"""
import logging
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from core.analysis.trend import Verdict
from core.contracts import TaskStatus
from core.history_query import (
    BASELINE_RANGE_KEY, CHART_MAX_BUCKETS, DEFAULT_RANGE_KEY, RANGE_KEYS,
    TABLE_POINT_LIMIT, HistoryRequest, HistorySnapshot, TaskAnalysis, build_request,
)
from core.history_refresh_worker import HistoryRefreshWorker
from core.workers.base import LatestRequestGate
from data.errors import StorageError

logger = logging.getLogger(__name__)

# 查询执行器：runner(request, on_done)。on_done(snapshot) 必须在主线程调用，且每个
# 请求恰好一次（失败也要回一个 error 非空的快照）
HistoryRunner = Callable[[HistoryRequest, Callable[[HistorySnapshot], None]], None]

# 空状态文案（标题, 提示）
EMPTY_NO_TASKS = ("暂无历史数据", "完成一次监控后，数据会显示在这里")
EMPTY_SELECT_TASK = ("请选择监控任务", "选定任务和指标后即可查看历史数据")
EMPTY_LOAD_FAILED = ("读取数据失败", "数据库读取出错，可稍后点击刷新重试")
EMPTY_NO_DATA_IN_RANGE = ("所选时间范围内暂无数据", "尝试切换到「全部」查看完整历史")
EMPTY_NO_DATA = ("该任务暂无数据", "产生新的采样数据后此处会自动更新")

# 错误提示标题
ERROR_LOAD_FAILED = "读取数据失败"
ERROR_TASK_MISSING = ("错误", "无法加载任务信息")

DELETE_BLOCKED_RUNNING = "任务正在运行中，请先停止任务再删除"


class HistoryWorkerRunner(QObject):
    """生产用查询执行器：每个请求一个 HistoryRefreshWorker，登记到线程登记表。

    结果经本对象（主线程）的槽转交 on_done。线程若没送出结果就结束（收尾本身出错），
    在 finished 时补一个失败快照，保证调用方不会永远等一个不会来的结果。
    """

    def __init__(self, db, threads, parent=None):
        super().__init__(parent)
        self._db = db
        self._threads = threads
        # 请求序号 → (线程, on_done)：持有线程引用直到结果送达或线程结束
        self._pending: Dict[int, tuple] = {}

    def __call__(self, request: HistoryRequest,
                 on_done: Callable[[HistorySnapshot], None]) -> None:
        worker = HistoryRefreshWorker(self._db, request)
        self._pending[request.request_seq] = (worker, on_done)
        worker.result_ready.connect(self._on_result)
        # 先连本对象的 finished 槽、再登记：同一信号按连接顺序执行，补发失败快照时
        # 线程对象还没被登记表 deleteLater
        worker.finished.connect(self._on_finished)
        self._threads.register(worker, name="历史数据查询", timeout_ms=3000)
        worker.start()

    @pyqtSlot(object)
    def _on_result(self, snapshot: HistorySnapshot) -> None:
        entry = self._pending.pop(snapshot.request.request_seq, None)
        if entry is not None:
            entry[1](snapshot)

    @pyqtSlot()
    def _on_finished(self) -> None:
        worker = self.sender()
        seq = getattr(worker, 'request_seq', None)
        entry = self._pending.pop(seq, None)
        if entry is not None:
            entry[1](HistorySnapshot(request=entry[0].request, error="后台查询异常结束"))


def _defer_to_event_loop(fn: Callable[[], None]) -> None:
    QTimer.singleShot(0, fn)


# ---------- 分析页的排序与折叠（纯函数，结论条、摘要表与趋势面板共用同一口径） ----------

# 判定的严重程度：持续上涨 → 波动 → 平稳 → 数据不足；累计量 / 峰值类不做趋势判定，排最后
VERDICT_RANK = {Verdict.RISING: 0, Verdict.FLUCTUATING: 1, Verdict.STABLE: 2,
                Verdict.INSUFFICIENT: 3}
UNJUDGED_RANK = 4
# 默认展开为图表的判定（其余折叠为一行迷你图）
ANOMALOUS_VERDICTS = (Verdict.RISING, Verdict.FLUCTUATING)


def snapshot_verdict(snapshot: Optional[HistorySnapshot]) -> Optional[Verdict]:
    """快照的判定；没有统计（所选范围内无数据）按数据不足，不做趋势判定的类别为 None。"""
    if snapshot is None or not snapshot.stats:
        return Verdict.INSUFFICIENT
    judgement = snapshot.judgement
    return Verdict(judgement.verdict) if judgement is not None else None


def relative_rate(snapshot: Optional[HistorySnapshot]) -> float:
    """同组排序用的增速：拟合斜率相对起点（基线值或统计首值）的比例，取绝对值。
    各指标单位不同（KB、个），直接比斜率没有意义，比相对增速才能把"涨得最凶"的排前面；
    起点为 0 时无法相对，记为无穷大（从 0 开始上涨本身就最显眼）；不能拟合时为 0。"""
    if snapshot is None or not snapshot.stats:
        return 0.0
    judgement = snapshot.judgement
    if judgement is None or judgement.fit is None:
        return 0.0
    baseline = snapshot.baseline
    if (baseline is not None and baseline.value is not None
            and snapshot.request.baseline_only):
        reference = baseline.value
    else:
        reference = snapshot.stats['first']
    slope = abs(judgement.fit.slope_per_hour)
    if not reference:
        return float('inf') if slope else 0.0
    return slope / abs(reference)


def severity_key(snapshot: Optional[HistorySnapshot]) -> Tuple[int, float]:
    """排序键（升序即从严重到轻微）：判定严重程度，同组按相对增速绝对值降序。"""
    verdict = snapshot_verdict(snapshot)
    rank = VERDICT_RANK[verdict] if verdict is not None else UNJUDGED_RANK
    return rank, -relative_rate(snapshot)


def ordered_metrics(analysis: TaskAnalysis) -> List[str]:
    """分析结果里的指标按严重程度排序（相同时保持任务的指标顺序）。"""
    metrics = [m for m in analysis.snapshots if m]
    return sorted(metrics, key=lambda m: severity_key(analysis.snapshots[m]))


def is_anomalous(snapshot: Optional[HistorySnapshot]) -> bool:
    """是否默认展开为图表：判定为持续上涨或波动。"""
    return snapshot_verdict(snapshot) in ANOMALOUS_VERDICTS


class HistoryViewModel(QObject):
    """历史数据视图模型（分析页视图模型的基类）。

    信号：
    - tasks_changed(list, set, object)：任务列表（最新在前）重建；运行中任务 ID 集合；now
    - tasks_refreshed(list, set, object)：列表结构不变，只需原地刷新运行时长等文案
      （定时触发；刚结束的任务已换成库里带结束时间的记录）
    - metrics_changed(list)：当前任务的指标列表（空表示无任务）；默认选中由随后的
      selection_changed 给出
    - selection_changed(object, object, bool)：(task_id, metric, explicit)；task_id 为
      None 表示未选中；explicit 为 False 表示只是自动选中的最新任务
    - snapshot_ready(object)：可显示的 HistorySnapshot（有数据）；
      snapshot.request.auto_refresh 为真时页面应保留用户的缩放/平移
    - empty_state(str, str)：(标题, 提示)，页面切到空状态并清空图表/表格/统计
    - busy(bool)：用户操作触发的加载开始/结束（定时刷新不置忙）
    - error(str, str)：(标题, 内容)，需要提示用户的错误；定时刷新的连续读库失败只发一次
    - baseline_changed(bool)：当前任务是否有基线打点（"自基线"范围是否可选）
    - range_reset(str)：视图模型自行改了时间范围（选着"自基线"而基线已不存在时回到
      "全部"），页面据此同步筛选条
    """

    tasks_changed = pyqtSignal(list, set, object)
    tasks_refreshed = pyqtSignal(list, set, object)
    metrics_changed = pyqtSignal(list)
    selection_changed = pyqtSignal(object, object, bool)
    snapshot_ready = pyqtSignal(object)
    empty_state = pyqtSignal(str, str)
    busy = pyqtSignal(bool)
    error = pyqtSignal(str, str)
    baseline_changed = pyqtSignal(bool)
    range_reset = pyqtSignal(str)

    def __init__(self, db, manager, services, *,
                 worker_runner: Optional[HistoryRunner] = None,
                 defer: Optional[Callable[[Callable[[], None]], None]] = None,
                 table_limit: int = TABLE_POINT_LIMIT,
                 max_buckets: int = CHART_MAX_BUCKETS,
                 parent=None):
        """
        Args:
            db: 数据库实例（只读：任务列表在主线程读，数据查询交给 worker_runner）
            manager: 监控管理器：删除任务数据经它执行
            services: 界面层服务（用其 clock / task_status / threads）
            worker_runner: 查询执行器，默认每个请求一个后台线程；测试可注入同步执行器
            defer: 把加载请求推迟到下一轮事件循环的函数（合并同一轮内的多次请求），
                默认 QTimer.singleShot(0, fn)；测试可注入立即执行或手动排队
            table_limit / max_buckets: 表格取数上限与图表分桶上限
        """
        super().__init__(parent)
        self._db = db
        self._manager = manager
        self._services = services
        self._runner: HistoryRunner = (
            worker_runner if worker_runner is not None
            else HistoryWorkerRunner(db, services.threads, parent=self))
        self._defer = defer if defer is not None else _defer_to_event_loop
        self._table_limit = table_limit
        self._max_buckets = max_buckets

        # ---- 对外可读的状态 ----
        self.tasks: list = []
        self.current_task_id: Optional[str] = None
        self.current_metric: Optional[str] = None
        self.range_key: str = DEFAULT_RANGE_KEY
        # 当前选中只是默认选中的最新任务（用户没主动选过）：列表里出现更新的任务时
        # 自动切过去，而不是一直停在启动时的旧任务上
        self.selection_is_auto: bool = False
        self.is_busy: bool = False
        # 最近一次显示出来的快照；空状态下为 None
        self.last_snapshot: Optional[HistorySnapshot] = None
        # 当前任务是否有基线打点（最近一次查询结果）
        self.has_baseline: bool = False

        # ---- 内部状态 ----
        # 载入列表时仍在运行的任务：定时刷新它们的时长文案，结束时回库取结束时间
        self._running: Set[str] = set()
        # 锚点缓存（任务最后一次采样时间）：切范围/切指标复用；整体重载、选任务、
        # 定时刷新时失效，保证锚点跟上新数据
        self._anchor_task_id: Optional[str] = None
        self._anchor_value = None
        self._gate = LatestRequestGate()
        self._inflight: Set[int] = set()
        self._dispatch_scheduled = False
        # 所选任务在上一次定时刷新时仍在运行：转为结束时整体重载一次
        self._was_running = False
        # 定时刷新连续读库失败时只提示一次；成功取回一次结果后复位
        self._refresh_error_notified = False
        # 定时刷新是否应跳过（如用户正在看明细/悬停图表），由 HistoryAutoRefresh 设置
        self._refresh_guard: Callable[[], bool] = lambda: False
        # 打点变化触发的静默重查：结果不受"跳过定时刷新"的判断影响
        self._forced_seqs: Set[int] = set()

        # 打点增删改后重查（manager 尚无该信号时不连接，兼容旧 manager 与测试替身）
        markers_changed = getattr(manager, 'markers_changed', None)
        if markers_changed is not None:
            markers_changed.connect(self.on_markers_changed)

    # ========== 只读属性 ==========

    @property
    def current_task(self):
        """当前选中任务的记录（来自最近一次载入的列表），未选中为 None"""
        return self._find_task(self.current_task_id)

    @property
    def metrics(self) -> List[str]:
        """当前任务的指标列表"""
        task = self.current_task
        return list(task.metric_types) if task else []

    @property
    def running_ids(self) -> Set[str]:
        """载入列表时仍在运行、且尚未观察到结束的任务"""
        return set(self._running)

    @property
    def can_delete(self) -> bool:
        """当前任务可否删除：须已选中且不在运行（运行中删除会与写入竞争）"""
        return bool(self.current_task_id) and not self._current_is_running()

    @property
    def delete_hint(self) -> str:
        """删除不可用时给用户的原因（选中了运行中任务）；其余情况为空串"""
        if self.current_task_id and self._current_is_running():
            return DELETE_BLOCKED_RUNNING
        return ""

    @property
    def has_pending_query(self) -> bool:
        """是否有尚未返回的查询（含已排队、未发出的加载）"""
        return bool(self._inflight) or self._dispatch_scheduled

    # ========== 命令 ==========

    def reload_tasks(self) -> None:
        """重新载入任务列表并按选择规则恢复/切换选中，然后加载当前任务数据。"""
        self._invalidate_anchor()
        # 只作废在途结果；忙状态留给后面：要么紧接着发起新加载，要么落到空状态时复位
        self._gate.invalidate()

        prev_id = self.current_task_id
        prev_metric = self.current_metric

        load_error = None
        try:
            tasks = self._db.tasks.list_with_data()
        except StorageError as exc:
            logger.error("分析页读取任务列表失败", exc_info=True)
            tasks, load_error = [], str(exc) or type(exc).__name__

        now = self._services.clock.now()
        status = self._services.task_status
        self.tasks = list(tasks)
        self._running = {t.task_id for t in self.tasks if status.is_running(t.task_id)}
        self.tasks_changed.emit(list(self.tasks), set(self._running), now)

        if not self.tasks:
            self.cancel_pending()
            self._clear_selection()
            if load_error is not None:
                self._show_empty(*EMPTY_LOAD_FAILED)
                self.error.emit(ERROR_LOAD_FAILED, load_error)
            else:
                self._show_empty(*EMPTY_NO_TASKS)
            return

        # 之前只是默认选中、而列表最前面（最新）的任务已经换了：改为选中最新任务
        if prev_id and self.selection_is_auto and self.tasks[0].task_id != prev_id:
            prev_id, prev_metric = None, None

        if prev_id:
            task = self._find_task(prev_id)
            if task is None:
                # 用户选中的任务已不在列表（被删除或尚无数据）：清空，不静默改选其他任务
                self.cancel_pending()
                self._clear_selection()
                self._show_empty(*EMPTY_SELECT_TASK)
                return
            self._apply_selection(task, prev_metric)
        else:
            self._was_running = False
            self.selection_is_auto = True
            self._apply_selection(self.tasks[0], None)
        self._request_load()

    def refresh(self) -> None:
        """手动刷新：整体重载任务列表与当前任务数据。"""
        self.reload_tasks()

    def select_task(self, task_id: str, explicit: bool = True) -> bool:
        """
        选中任务（指标回到该任务的首指标）并加载其数据。

        目标不在当前列表里时（如导航跳转到刚产生数据的任务）先重载列表再找；
        仍找不到则清空选择、显示"请选择监控任务"。

        Returns:
            bool: 目标任务最终是否被选中
        """
        self.selection_is_auto = not explicit
        self._was_running = False
        self._invalidate_anchor()
        task = self._find_task(task_id)
        if task is None:
            self.current_task_id = task_id
            self.current_metric = None
            self.reload_tasks()
            return self.current_task_id == task_id
        self._apply_selection(task, None)
        self._request_load()
        return True

    def select_metric(self, metric: str) -> None:
        """切换当前任务的指标（不属于该任务的指标忽略）。"""
        if not self.current_task_id or metric == self.current_metric:
            return
        if metric not in self.metrics:
            return
        self.current_metric = metric
        self.selection_changed.emit(
            self.current_task_id, metric, not self.selection_is_auto)
        self._request_load()

    def set_range(self, range_key: str) -> None:
        """切换时间范围；切任务/切指标时保持不变。"""
        if range_key not in RANGE_KEYS:
            raise ValueError(f"未知的时间范围：{range_key!r}")
        if range_key == self.range_key:
            return
        self.range_key = range_key
        if self.current_task_id:
            self._request_load()

    def delete_current(self) -> bool:
        """
        经 manager 删除当前任务的全部数据，成功后重载列表。确认交互由页面负责。

        Returns:
            bool: 是否删除成功；未选中或任务运行中时直接返回 False（不调用 manager）
        """
        task_id = self.current_task_id
        if not task_id or not self.can_delete:
            return False
        if not self._manager.delete_task_data(task_id):
            return False
        self.reload_tasks()
        return True

    def on_tick(self) -> None:
        """
        自动刷新入口（页面可见时由 HistoryAutoRefresh 定时调用）：
        1. 原地刷新运行中任务的时长文案（刚结束的任务回库取结束时间）；
        2. 所选任务仍在运行：后台刷新数据（保留用户的选择与缩放）；在途查询未返回时
           跳过本次；页面要求跳过时（明细视图/悬停图表/窗口最小化）同样跳过；
        3. 所选任务刚结束：整体重载一次，让"运行中"标记、删除可用性与最终数据同步。
        """
        self._refresh_running_labels()
        task_id = self.current_task_id
        if not task_id or self._refresh_guard():
            return
        if self._services.task_status.is_running(task_id):
            self._was_running = True
            if self.has_pending_query:
                return
            self._invalidate_anchor()
            self._start_query(auto_refresh=True)
        elif self._was_running:
            self._was_running = False
            self.reload_tasks()

    @pyqtSlot(str)
    def on_markers_changed(self, task_id: str) -> None:
        """
        打点增删改（manager.markers_changed）：是当前任务就重查一次。
        用户加载在途时并入那次加载（重新发出，忙状态由新结果撤掉）；否则静默重查，
        保留缩放、不置忙，结果不因"用户正在看图"而被丢弃。
        """
        if not task_id or task_id != self.current_task_id:
            return
        if self.is_busy:
            self._request_load()
            return
        self._start_query(auto_refresh=True, forced=True)

    def cancel_pending(self) -> None:
        """让在途与已排队的查询全部作废（不等待线程，线程自行跑完后结果被丢弃）。"""
        self._dispatch_scheduled = False
        self._gate.invalidate()
        self._set_busy(False)

    def set_refresh_guard(self, guard: Callable[[], bool]) -> None:
        """设置"本次定时刷新是否跳过"的判断；结果回来时也会再判断一次。"""
        self._refresh_guard = guard

    # ========== 选择 ==========

    def _find_task(self, task_id: Optional[str]):
        if not task_id:
            return None
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def _apply_selection(self, task, preferred_metric: Optional[str]) -> None:
        """选中任务：指标优先沿用 preferred_metric，不属于该任务时回到首指标。"""
        metrics = list(task.metric_types)
        if preferred_metric in metrics:
            metric = preferred_metric
        else:
            metric = metrics[0] if metrics else None
        self.current_task_id = task.task_id
        self.current_metric = metric
        self.metrics_changed.emit(metrics)
        self.selection_changed.emit(task.task_id, metric, not self.selection_is_auto)

    def _clear_selection(self) -> None:
        self.current_task_id = None
        self.current_metric = None
        self.metrics_changed.emit([])
        self.selection_changed.emit(None, None, False)
        self._set_has_baseline(False)

    def _current_is_running(self) -> bool:
        task = self.current_task
        if task is not None and task.status == TaskStatus.RUNNING:
            return True
        return self._services.task_status.is_running(self.current_task_id)

    def _refresh_running_labels(self) -> None:
        if not self._running:
            return
        status = self._services.task_status
        ended = {tid for tid in self._running if not status.is_running(tid)}
        if ended:
            self._running -= ended
            self.tasks = [self._reread_task(t) if t.task_id in ended else t
                          for t in self.tasks]
        self.tasks_refreshed.emit(
            list(self.tasks), set(self._running), self._services.clock.now())

    def _reread_task(self, task):
        """任务刚结束：取库里带结束时间的记录；读不到就沿用旧记录。"""
        try:
            return self._db.tasks.get(task.task_id) or task
        except StorageError:
            logger.warning("回读已结束任务失败: %s", task.task_id, exc_info=True)
            return task

    # ========== 查询 ==========

    def _invalidate_anchor(self) -> None:
        self._anchor_task_id = None
        self._anchor_value = None

    def _cached_anchor(self, task_id: str):
        return self._anchor_value if self._anchor_task_id == task_id else None

    def _request_load(self) -> None:
        """用户操作触发的加载：立即作废在途结果并置忙，查询推迟到下一轮事件循环发出，
        同一轮内的多次请求只发一次（按发出时的最新状态）。"""
        self._gate.invalidate()
        self._set_busy(True)
        if self._dispatch_scheduled:
            return
        self._dispatch_scheduled = True
        self._defer(self._dispatch)

    @pyqtSlot()
    def _dispatch(self) -> None:
        if not self._dispatch_scheduled:
            return
        self._dispatch_scheduled = False
        if not self.current_task_id:
            self._set_busy(False)
            return
        self._start_query(auto_refresh=False)

    def _start_query(self, auto_refresh: bool, forced: bool = False) -> None:
        task_id = self.current_task_id
        request = build_request(
            task_id, self.current_metric, self.range_key,
            anchor=self._cached_anchor(task_id),
            table_limit=self._table_limit, max_buckets=self._max_buckets,
            seq=self._gate.next_seq(), auto_refresh=auto_refresh)
        self._inflight.add(request.request_seq)
        if forced:
            self._forced_seqs.add(request.request_seq)
        self._runner(request, self._on_snapshot)

    @pyqtSlot(object)
    def _on_snapshot(self, snapshot: HistorySnapshot) -> None:
        request = snapshot.request
        self._inflight.discard(request.request_seq)
        forced = request.request_seq in self._forced_seqs
        self._forced_seqs.discard(request.request_seq)
        if not self._gate.is_current(request.request_seq):
            return
        try:
            self._apply_snapshot(snapshot, forced)
        finally:
            # 先让页面画出结果/空状态再撤加载态；槽里若又发起了新加载（序号已变）则不撤
            if not request.auto_refresh and self._gate.is_current(request.request_seq):
                self._set_busy(False)

    def _apply_snapshot(self, snapshot: HistorySnapshot, forced: bool = False) -> None:
        request = snapshot.request
        auto = request.auto_refresh
        if (request.task_id != self.current_task_id
                or request.metric != self.current_metric
                or request.range_key != self.range_key):
            return

        if snapshot.error is not None:
            if auto:
                # 定时刷新失败：保持已显示的数据；存储故障让用户知道，但连续失败只提示一次
                logger.warning("分析页自动刷新失败，保持当前显示: %s", snapshot.error)
                if snapshot.storage_error and not self._refresh_error_notified:
                    self._refresh_error_notified = True
                    self.error.emit(ERROR_LOAD_FAILED, snapshot.error)
            else:
                self._show_empty(*EMPTY_LOAD_FAILED)
                self.error.emit(ERROR_LOAD_FAILED, snapshot.error)
            return
        if auto:
            self._refresh_error_notified = False

        if snapshot.task_missing:
            if not auto:
                self.error.emit(*ERROR_TASK_MISSING)
            return
        # 结果回来时已进入应跳过刷新的状态：丢弃，等下一次定时刷新
        if auto and not forced and self._refresh_guard():
            return

        self._anchor_task_id = request.task_id
        self._anchor_value = snapshot.task_last_at

        self._set_has_baseline(snapshot.baseline is not None)
        if request.range_key == BASELINE_RANGE_KEY and snapshot.baseline is None:
            # 基线已被删除："自基线"不可选，回到"全部"（此次结果的起点本就不过滤）
            self.range_key = DEFAULT_RANGE_KEY
            self.range_reset.emit(DEFAULT_RANGE_KEY)

        if not snapshot.points:
            self._show_empty(*(EMPTY_NO_DATA_IN_RANGE if self.range_key != 'all'
                               else EMPTY_NO_DATA))
            return
        self._emit_snapshot(snapshot)

    # ========== 输出 ==========

    def _emit_snapshot(self, snapshot: HistorySnapshot) -> None:
        """可显示的快照：记下并通知页面（子类可在此之前补发自己的信号）。"""
        self.last_snapshot = snapshot
        self.snapshot_ready.emit(snapshot)

    def _set_has_baseline(self, value: bool) -> None:
        self.has_baseline = value
        # 每次都发：页面切任务后筛选条需要按新任务重设（重复值无副作用）
        self.baseline_changed.emit(value)

    def _show_empty(self, title: str, detail: str) -> None:
        self.last_snapshot = None
        self.empty_state.emit(title, detail)

    def _set_busy(self, value: bool) -> None:
        if self.is_busy != value:
            self.is_busy = value
            self.busy.emit(value)
