"""
历史页自动刷新工作线程
运行中任务的自动刷新要执行锚点、表格最近 N 条、分桶降采样、统计、打点共五类查询；
数据量到 50 万行量级时在主线程执行会让界面周期性卡死数秒（RULES.md §5：耗时操作
不放 UI 线程）。本模块把这些只读查询搬到后台 QThread：worker 只做查询，把结果打包成
一个普通 dataclass 经信号发回，UI 槽在主线程用既有的 _update_chart/_update_table/
_update_stats 等方法应用，绝不在工作线程触碰任何 Qt 控件。

Database 每次操作都新建 sqlite 连接，可直接在工作线程调用（不共享连接对象）。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal

from data.models import DataPoint, Marker

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
    # 查询过程抛出异常时的描述；非空时其余字段无意义，UI 侧只记日志不改界面
    error: Optional[str] = None


def query_history_refresh(db, request_id: int, task_id: str,
                          metric_type: Optional[str], range_key: str,
                          range_seconds: Optional[int],
                          table_limit: int, max_buckets: int) -> HistoryRefreshResult:
    """
    执行自动刷新所需的全部只读查询（与 HistoryPage._load_task_data 的同步路径同口径）。

    Args:
        db: Database 实例（或同接口的假对象）
        request_id/task_id/metric_type/range_key: 请求标识，原样写入结果
        range_seconds: 时间范围秒数，None 表示"全部"（不过滤）
        table_limit: 表格取最近多少条
        max_buckets: 图表分桶上限
    """
    result = HistoryRefreshResult(
        request_id=request_id, task_id=task_id,
        metric_type=metric_type, range_key=range_key)

    task_info = db.get_task(task_id)
    if not task_info:
        result.task_missing = True
        return result

    # 锚点：优先 tasks.last_sample_at，仅其为空才回退 MAX(timestamp) 查询
    last_dt = task_info.last_sample_at
    if last_dt is None:
        last_dt = db.get_last_point_timestamp(task_id)
    result.last_dt = last_dt

    since_iso = None
    if range_seconds is not None and last_dt is not None:
        since_iso = (last_dt - timedelta(seconds=range_seconds)).isoformat()

    result.table_points = db.get_task_data_points(
        task_id, metric_type=metric_type, limit=table_limit, since_iso=since_iso)
    if not result.table_points:
        # 范围内无数据：UI 侧据此显示空状态，无需再查统计/分桶
        return result

    result.stats = (db.get_metric_stats(task_id, metric_type, since_iso=since_iso)
                    if metric_type else None)
    result.chart_points = db.get_task_data_points_bucketed(
        task_id, metric_type=metric_type, max_buckets=max_buckets, since_iso=since_iso)
    result.markers = db.get_markers(task_id, since_iso=since_iso)
    return result


class HistoryRefreshWorker(QThread):
    """
    历史页自动刷新后台线程（持引用防 GC，用法参照 core/export_worker.py）。

    信号约定：result_ready 是 run() 的最后一个动作；UI 侧应把 QThread.finished 接到
    deleteLater，而不是在 result_ready 的槽里释放线程对象。查询无取消机制（只读、
    不改库），退出时由主窗口带超时 join 兜底。
    """

    # 结果信号：携带 HistoryRefreshResult（成功与失败都经此返回，失败时 error 非空）
    result_ready = pyqtSignal(object)

    def __init__(self, db, request_id: int, task_id: str,
                 metric_type: Optional[str], range_key: str,
                 range_seconds: Optional[int],
                 table_limit: int, max_buckets: int, parent=None):
        super().__init__(parent)
        self._db = db
        self._request_id = request_id
        self._task_id = task_id
        self._metric_type = metric_type
        self._range_key = range_key
        self._range_seconds = range_seconds
        self._table_limit = table_limit
        self._max_buckets = max_buckets

    def run(self):
        try:
            result = query_history_refresh(
                self._db, self._request_id, self._task_id, self._metric_type,
                self._range_key, self._range_seconds,
                self._table_limit, self._max_buckets)
        except Exception as e:
            logger.error("历史页自动刷新查询失败", exc_info=True)
            result = HistoryRefreshResult(
                request_id=self._request_id, task_id=self._task_id,
                metric_type=self._metric_type, range_key=self._range_key,
                error=str(e) or type(e).__name__)
        self.result_ready.emit(result)
