"""
历史页数据查询：请求 → 快照

历史页的一次加载（用户切任务/指标/范围、进入页面、自动刷新）都归结为同一组只读
查询：锚点、表格最近 N 条、统计、分桶降采样、打点。这里把查询条件定型为
HistoryRequest、把结果定型为 HistorySnapshot，由 run_history_query 同步执行；
后台线程（core.history_refresh_worker.HistoryRefreshWorker）只是它的线程外壳。
手动加载与自动刷新走同一函数，口径天然一致，不再需要两份编排互相对照。

时间范围的语义：锚定该任务最后一次采样的时间，而非当前时刻——停止已久的任务选
"最近1小时"仍应能看到其最后一小时的数据。锚点优先取 tasks.last_sample_at（主键
查询，随数据点写入同事务维护），仅其为空（旧数据/维护缺失）时才回退 MAX(timestamp)。

查询直接用仓储接口（失败抛 StorageError），不经门面的"吞异常返回空"——否则读取
失败会被当成"暂无数据"显示。时间参数一律 datetime；绘图用的 epoch float 不得回流。
"""
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from data.errors import StorageError
from data.models import DataPoint, Marker
from data.repos.point_repo import PointQuery

logger = logging.getLogger(__name__)

# 时间范围预设：键 → 范围秒数；None 表示"全部"（不做时间过滤）
TIME_RANGE_SECONDS = {
    '10m': 600,
    '1h': 3600,
    '6h': 21600,
    '24h': 86400,
    'all': None,
}
# 默认看全部数据：泄漏判断要看从开始至今的整体趋势，而不是最近一小时的局部
DEFAULT_RANGE_KEY = 'all'

# 表格取数上限：只取最近 N 条采集，避免大数据量下界面卡顿
TABLE_POINT_LIMIT = 2000
# 图表分桶上限：按行号分桶后每桶取 MIN/MAX 两点，故图表最多 2*CHART_MAX_BUCKETS 个点
CHART_MAX_BUCKETS = 2000


@dataclass(frozen=True)
class HistoryRequest:
    """一次历史查询的全部条件（不可变，可安全跨线程传递）。

    - since：查询起点；anchor 为空时由 run_history_query 解析锚点后重算，
      此时这里的值不被使用；
    - until：查询终点，预留——统计与分桶查询尚不支持终点，非空会被拒绝；
    - anchor：调用方缓存的锚点（该任务最后一次采样时间）；为空表示由查询现取；
    - request_seq：LatestRequestGate 发的序号，结果回来时据此判断是否过期；
    - auto_refresh：是否为运行中任务的定时刷新（界面据此保留用户的缩放/平移）。
    """
    task_id: str
    metric: Optional[str]
    range_key: str
    since: Optional[datetime]
    until: Optional[datetime]
    table_limit: int
    max_buckets: int
    anchor: Optional[datetime]
    request_seq: int
    auto_refresh: bool = False


@dataclass(frozen=True)
class HistorySnapshot:
    """一次历史查询的结果。

    request 是实际执行的条件：查询现取了锚点时，其 anchor/since 已换成解析后的值。
    error 非空表示查询失败（其余数据字段无意义），storage_error 区分存储故障与
    编程错误；task_missing 表示任务记录已不存在（被删除）。points 为空表示所选
    范围内没有数据——此时不再查统计/分桶/打点。
    """
    request: HistoryRequest
    points: List[DataPoint] = field(default_factory=list)
    stats: Optional[dict] = None
    buckets: List[DataPoint] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    # 本次查询使用的锚点（任务最后一次采样时间），供调用方回填缓存
    task_last_at: Optional[datetime] = None
    error: Optional[str] = None
    storage_error: bool = False
    task_missing: bool = False

    @property
    def ok(self) -> bool:
        """查询成功且任务仍存在"""
        return self.error is None and not self.task_missing


def compute_since(range_key: str, anchor: Optional[datetime]) -> Optional[datetime]:
    """把时间范围换算成查询起点：锚点往前推范围秒数。

    选中"全部"（或未知的范围键）、或该任务尚无数据点（锚点为空）时返回 None，即不过滤。
    """
    seconds = TIME_RANGE_SECONDS.get(range_key)
    if seconds is None or anchor is None:
        return None
    return anchor - timedelta(seconds=seconds)


def resolve_anchor(db, task_info) -> Optional[datetime]:
    """任务的时间范围锚点：优先 tasks.last_sample_at，为空才回退 MAX(timestamp) 查询。
    读库失败抛 StorageError。"""
    anchor = task_info.last_sample_at
    if anchor is None:
        anchor = db.analytics.last_point_timestamp(task_info.task_id)
    return anchor


def build_request(task_id: str, metric: Optional[str], range_key: str, *,
                  anchor: Optional[datetime] = None,
                  table_limit: int = TABLE_POINT_LIMIT,
                  max_buckets: int = CHART_MAX_BUCKETS,
                  seq: int = 0, auto_refresh: bool = False) -> HistoryRequest:
    """组装查询条件。

    anchor 给出时（调用方缓存了锚点）直接算出 since；为空时 since 留空，由
    run_history_query 在执行时现取锚点再算——锚点查询因此也落在执行查询的线程里。
    """
    return HistoryRequest(
        task_id=task_id, metric=metric, range_key=range_key,
        since=compute_since(range_key, anchor), until=None,
        table_limit=table_limit, max_buckets=max_buckets,
        anchor=anchor, request_seq=seq, auto_refresh=auto_refresh)


def query_history_data(db, task_id: str, metric_type: Optional[str],
                       since: Optional[datetime], table_limit: int,
                       max_buckets: int) -> Tuple[List[DataPoint], Optional[dict],
                                                  List[DataPoint], List[Marker]]:
    """
    四类数据查询：表格最近 table_limit 条、统计、分桶降采样、打点。范围内没有明细点
    时只查明细（其余返回空），由调用方显示空状态。失败抛 StorageError。

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


def run_history_query(db, request: HistoryRequest) -> HistorySnapshot:
    """
    同步执行一次历史查询（可在任意线程调用：仓储每次操作都新建连接）。

    存储故障（StorageError）转成快照的 error/storage_error，不向外抛；其他异常
    （编程错误）照常抛出，由调用方（后台线程外壳）记录并打包。
    """
    if request.until is not None:
        raise ValueError("历史查询暂不支持终点（统计与分桶查询没有 until 条件）")
    try:
        task_info = db.tasks.get(request.task_id)
        if task_info is None:
            return HistorySnapshot(request=request, task_missing=True)

        anchor, since = request.anchor, request.since
        if anchor is None:
            anchor = resolve_anchor(db, task_info)
            since = compute_since(request.range_key, anchor)
            request = replace(request, anchor=anchor, since=since)

        points, stats, buckets, markers = query_history_data(
            db, request.task_id, request.metric, since,
            request.table_limit, request.max_buckets)
    except StorageError as exc:
        logger.error("历史数据读取失败: task_id=%s", request.task_id, exc_info=True)
        return HistorySnapshot(request=request, error=str(exc) or type(exc).__name__,
                               storage_error=True)

    return HistorySnapshot(
        request=request, points=points, stats=stats, buckets=buckets,
        markers=markers, task_last_at=anchor)
