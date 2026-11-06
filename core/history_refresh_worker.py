"""
历史页自动刷新工作线程
运行中任务的自动刷新要执行锚点、表格最近 N 条、分桶降采样、统计、打点共五类查询；
数据量到 50 万行量级时在主线程执行会让界面周期性卡死数秒（RULES.md §5：耗时操作
不放 UI 线程）。本模块把这些只读查询搬到后台 QThread：worker 只做查询，把结果打包成
一个普通 dataclass 经信号发回，UI 槽在主线程用既有的 _update_chart/_update_table/
_update_stats 等方法应用，绝不在工作线程触碰任何 Qt 控件。

Database 的仓储每次操作都新建 sqlite 连接，可直接在工作线程调用（不共享连接对象）。
查询直接用仓储接口（失败抛 StorageError），不经门面的"吞异常返回空"——否则读取失败
会被当成"暂无数据"显示。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from PyQt5.QtCore import pyqtSignal

from core.workers.base import BackgroundTask, CancelToken

from data.errors import StorageError
from data.models import DataPoint, Marker
from data.repos.point_repo import PointQuery

logger = logging.getLogger(__name__)


@dataclass
class HistoryRefreshResult:
    """一次自动刷新的查询结果（含请求标识，供 UI 侧判断是否已过期）。"""
    # ---- 请求标识：UI 侧据此丢弃过期/已切换的结果 ----
    request_id: int
    task_id: str
    metric_type: Optional[str]
    range_key: str
    # ---- 查询结果 ----
    task_missing: bool = False           # 任务记录已不存在
    last_dt: Optional[datetime] = None   # 时间范围锚点（回填 UI 侧的 last_dt 缓存）
    table_points: List[DataPoint] = field(default_factory=list)
    stats: Optional[dict] = None
    chart_points: List[DataPoint] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    # 查询过程抛出异常时的描述；非空时其余字段无意义，UI 侧不改界面
    error: Optional[str] = None
    # 失败是否为存储故障（StorageError）：UI 侧据此提示"读取数据失败"
    storage_error: bool = False


def query_history_data(db, task_id: str, metric_type: Optional[str],
                       since: Optional[datetime], table_limit: int,
                       max_buckets: int) -> Tuple[List[DataPoint], Optional[dict],
                                                  List[DataPoint], List[Marker]]:
    """
    历史页的四类数据查询（手动加载与自动刷新共用，保证两条路径同口径）：
    表格最近 table_limit 条、统计、分桶降采样、打点。范围内没有明细点时只查明细
    （其余返回空），由调用方显示空状态。时间参数一律 datetime，失败抛 StorageError。

    Returns:
        (table_points, stats, chart_points, markers)
    """
    table_points = db.points.fetch(
        PointQuery(task_id, metric_type, since=since, limit=table_limit))
    if not table_points:
        return table_points, None, [], []
    stats = (db.analytics.legacy_stats(task_id, metric_type, since=since)
             if metric_type else None)
    chart_points = db.analytics.legacy_buckets(task_id, metric_type, max_buckets, since=since)
    markers = db.markers.list(task_id, since=since)
    return table_points, stats, chart_points, markers


def query_history_refresh(db, request_id: int, task_id: str,
                          metric_type: Optional[str], range_key: str,
                          range_seconds: Optional[int],
                          table_limit: int, max_buckets: int) -> HistoryRefreshResult:
    """
    执行自动刷新所需的全部只读查询（与 HistoryPage._load_task_data 的同步路径同口径）。
    存储失败抛 StorageError，由 HistoryRefreshWorker 转成结果的 error 字段。

    Args:
        db: Database 实例（用其仓储属性 tasks/points/analytics/markers）
        request_id/task_id/metric_type/range_key: 请求标识，原样写入结果
        range_seconds: 时间范围秒数，None 表示"全部"（不过滤）
        table_limit: 表格取最近多少条
        max_buckets: 图表分桶上限
    """
    result = HistoryRefreshResult(
        request_id=request_id, task_id=task_id,
        metric_type=metric_type, range_key=range_key)

    task_info = db.tasks.get(task_id)
    if not task_info:
        result.task_missing = True
        return result

    # 锚点：优先 tasks.last_sample_at，仅其为空才回退 MAX(timestamp) 查询
    last_dt = task_info.last_sample_at
    if last_dt is None:
        last_dt = db.analytics.last_point_timestamp(task_id)
    result.last_dt = last_dt

    since = None
    if range_seconds is not None and last_dt is not None:
        since = last_dt - timedelta(seconds=range_seconds)

    (result.table_points, result.stats,
     result.chart_points, result.markers) = query_history_data(
        db, task_id, metric_type, since, table_limit, max_buckets)
    return result


class HistoryRefreshWorker(BackgroundTask):
    """
    历史页自动刷新后台线程。

    信号约定：result_ready 是 run() 的最后一个动作，成功与失败都经它返回（失败时
    结果的 error 非空），同时也发基类的 result/failed。请求序号存于 request_seq，
    页面用 LatestRequestGate 判断结果是否过期。查询只读、不改库，取消只是让结果
    不再发出；退出时由线程登记表带超时 join 兜底。线程对象的释放由登记表挂在
    QThread.finished 上。
    """

    # 结果信号：携带 HistoryRefreshResult（成功与失败都经此返回，失败时 error 非空）
    result_ready = pyqtSignal(object)

    def __init__(self, db, request_id: int, task_id: str,
                 metric_type: Optional[str], range_key: str,
                 range_seconds: Optional[int],
                 table_limit: int, max_buckets: int, parent=None):
        super().__init__(parent, request_seq=request_id)
        self._db = db
        self._task_id = task_id
        self._metric_type = metric_type
        self._range_key = range_key
        self._range_seconds = range_seconds
        self._table_limit = table_limit
        self._max_buckets = max_buckets

    def execute(self, token: CancelToken) -> HistoryRefreshResult:
        return query_history_refresh(
            self._db, self.request_seq, self._task_id, self._metric_type,
            self._range_key, self._range_seconds,
            self._table_limit, self._max_buckets)

    def handle_result(self, value: HistoryRefreshResult) -> None:
        super().handle_result(value)
        self.result_ready.emit(value)

    def handle_failure(self, exc: Exception) -> None:
        logger.error("历史页自动刷新查询失败", exc_info=exc)
        message = self.failure_message(exc)
        self.failed.emit(message)
        self.result_ready.emit(HistoryRefreshResult(
            request_id=self.request_seq, task_id=self._task_id,
            metric_type=self._metric_type, range_key=self._range_key,
            error=message, storage_error=isinstance(exc, StorageError)))
