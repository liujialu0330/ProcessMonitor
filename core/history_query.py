"""
历史页数据查询：请求 → 快照

历史页的一次加载（用户切任务/指标/范围、进入页面、自动刷新）都归结为同一组只读
查询：锚点、打点、表格最近 N 条、聚合与判定、空洞、分桶降采样。这里把查询条件定型为
HistoryRequest、把结果定型为 HistorySnapshot，由 run_history_query 同步执行；
后台线程（core.history_refresh_worker.HistoryRefreshWorker）只是它的线程外壳。
手动加载与自动刷新走同一函数，口径天然一致，不再需要两份编排互相对照。

时间范围的语义：锚定该任务最后一次采样的时间，而非当前时刻——停止已久的任务选
"最近1小时"仍应能看到其最后一小时的数据。锚点优先取 tasks.last_sample_at（主键
查询，随数据点写入同事务维护），仅其为空（旧数据/维护缺失）时才回退 MAX(timestamp)。
"自基线"范围从基线打点时刻起；任务没有基线时等同"全部"。

两个起点：显示起点（since，图表与明细用）由范围决定；统计起点（stats_since，统计条、
回归、判定用）在存在基线时取 max(显示起点, 基线时刻)——基线之前的预热段不算进"泄漏"。
基线值取基线时刻之后 BASELINE_SAMPLES 个样本的中位数，"10 分钟"等不含基线的范围
下仍作为参考值给出。

查询直接用仓储接口（失败抛 StorageError），不经门面的"吞异常返回空"——否则读取
失败会被当成"暂无数据"显示。时间参数一律 datetime；绘图用的 epoch float 不得回流。
"""
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from core.analysis.intervals import Boundary, RoundDelta, round_deltas
from core.analysis.series import count_rises, per_second_rates
from core.analysis.trend import (
    Judgement, RateText, fit_input_from_aggregates, judge, linear_fit,
    rate_display, rate_unit_for_span)
from data.errors import StorageError
from data.models import DataPoint, Marker
from data.repos.point_repo import PointQuery
from utils.metrics import (
    VALUE_CLASS_LEVEL, VALUE_CLASS_PEAK, VALUE_CLASS_RATE, spec, value_class)

logger = logging.getLogger(__name__)

# "自基线"范围键：起点为基线打点时刻（无基线时不过滤）
BASELINE_RANGE_KEY = 'baseline'
# 时间范围预设：键 → 范围秒数；None 表示"全部"（不做时间过滤）
TIME_RANGE_SECONDS = {
    '10m': 600,
    '1h': 3600,
    '6h': 21600,
    '24h': 86400,
    'all': None,
}
# 全部合法的范围键（界面顺序）
RANGE_KEYS = (BASELINE_RANGE_KEY, *TIME_RANGE_SECONDS)
# 默认看全部数据：泄漏判断要看从开始至今的整体趋势，而不是最近一小时的局部
DEFAULT_RANGE_KEY = 'all'

# 表格取数上限：只取最近 N 条采集，避免大数据量下界面卡顿
TABLE_POINT_LIMIT = 2000
# 图表分桶上限：按行号分桶后每桶取 MIN/MAX 两点，故图表最多 2*CHART_MAX_BUCKETS 个点
CHART_MAX_BUCKETS = 2000
# 基线值 = 基线时刻之后这么多个样本的中位数（单个尖峰样本不会带偏基线）
BASELINE_SAMPLES = 5
# "每轮 +X"至少需要的用户打点数（两个打点才围出一轮完整的操作）
MIN_ROUND_MARKERS = 2

MARKER_KIND_BASELINE = 'baseline'
MARKER_KIND_USER = 'user'


@dataclass(frozen=True)
class HistoryRequest:
    """一次历史查询的全部条件（不可变，可安全跨线程传递）。

    - since：显示起点；anchor 为空、或范围为"自基线"时由 run_history_query 现算，
      此时这里的值不被使用；
    - until：查询终点，预留——统计与分桶查询尚不支持终点，非空会被拒绝；
    - anchor：调用方缓存的锚点（该任务最后一次采样时间）；为空表示由查询现取；
    - request_seq：LatestRequestGate 发的序号，结果回来时据此判断是否过期；
    - auto_refresh：是否为运行中任务的定时刷新（界面据此保留用户的缩放/平移）；
    - baseline_only：存在基线时统计、回归、判定只用基线之后的数据（产品规则，默认开）；
      关掉后统计起点与显示起点相同，基线值仍作为参考给出。
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
    baseline_only: bool = True


@dataclass(frozen=True)
class BaselineInfo:
    """基线打点与基线值。value 为其后 sample_count（≤ BASELINE_SAMPLES）个样本的中位数，
    基线之后该指标没有数据时为 None、sample_count 为 0。"""
    marker: Marker
    value: Optional[float]
    sample_count: int

    @property
    def timestamp(self) -> datetime:
        return self.marker.timestamp


@dataclass(frozen=True)
class RateStats:
    """累计类指标的速率统计（原始单位/秒）：平均 = (末值 - 首值) / 跨度；
    峰值 = 图表速率序列（按分桶点相邻差计算）的最大值，没有可算的相邻段时为 None。"""
    average_per_second: Optional[float]
    peak_per_second: Optional[float]


@dataclass(frozen=True)
class PeakRises:
    """峰值类指标在统计范围内的抬升：次数按图表分桶点的相邻上升计（同一桶内的多次
    抬升合并为一次），total 为当前值减开始值（或基线值）。"""
    count: int
    total: Optional[float]


@dataclass(frozen=True)
class HistorySnapshot:
    """一次历史查询的结果。

    request 是实际执行的条件：查询现取了锚点（或"自基线"范围现算了起点）时，
    其 anchor/since 已换成解析后的值。error 非空表示查询失败（其余数据字段无意义），
    storage_error 区分存储故障与编程错误；task_missing 表示任务记录已不存在（被删除）。
    points 为空表示所选范围内没有数据——此时只带打点与基线打点，不再查统计/分桶。

    - points：显示范围内最近 table_limit 条明细；total_count 为显示范围内的总条数；
    - stats：统计范围（stats_since 起）的聚合，键 count/min/max/avg/first/first_ts/last/
      last_ts/slope_per_hour/span_seconds；范围内无数据或未选指标时为 None；
    - buckets：显示范围的分桶降采样点（真实数据点）；rate_series：累计类的每秒速率序列；
    - markers：任务的全部打点（按时间升序，含基线与暂停/恢复）——编号与基线参考不随
      范围变化，图表据显示范围自行裁剪；
    - gaps：显示范围内的空洞 (空洞前最后一点, 空洞后第一点)；
    - judgement / rate / fit_unit：水位量的趋势判定、增速显示量与其时间单位后缀；
    - rounds：统计范围内相邻打点（基线 + 用户打点）围出的各轮增量；
    - value_class：指标显示类别（utils.metrics.VALUE_CLASS_*）。
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
    total_count: int = 0
    stats_since: Optional[datetime] = None
    baseline: Optional[BaselineInfo] = None
    judgement: Optional[Judgement] = None
    rate: Optional[RateText] = None
    fit_unit: Optional[str] = None
    gaps: List[Tuple[datetime, datetime]] = field(default_factory=list)
    rate_series: List[DataPoint] = field(default_factory=list)
    rate_stats: Optional[RateStats] = None
    peak_rises: Optional[PeakRises] = None
    rounds: List[RoundDelta] = field(default_factory=list)
    value_class: str = VALUE_CLASS_LEVEL
    interval_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """查询成功且任务仍存在"""
        return self.error is None and not self.task_missing

    @property
    def user_markers(self) -> List[Marker]:
        """用户打点（不含基线与暂停/恢复），按时间升序"""
        return [m for m in self.markers if m.kind == MARKER_KIND_USER]


def compute_since(range_key: str, anchor: Optional[datetime],
                  baseline_at: Optional[datetime] = None) -> Optional[datetime]:
    """把时间范围换算成显示起点。

    - "自基线"：基线打点时刻；没有基线（baseline_at 为空）时不过滤；
    - 时钟预设：锚点往前推范围秒数；
    - 选中"全部"（或未知的范围键）、或该任务尚无数据点（锚点为空）时返回 None，即不过滤。
    """
    if range_key == BASELINE_RANGE_KEY:
        return baseline_at
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


def find_baseline(markers: List[Marker]) -> Optional[Marker]:
    """任务的基线打点：kind 为 baseline 的最后一个（按时间）；没有返回 None。"""
    found = None
    for marker in markers:
        if marker.kind == MARKER_KIND_BASELINE:
            found = marker
    return found


def build_request(task_id: str, metric: Optional[str], range_key: str, *,
                  anchor: Optional[datetime] = None,
                  table_limit: int = TABLE_POINT_LIMIT,
                  max_buckets: int = CHART_MAX_BUCKETS,
                  seq: int = 0, auto_refresh: bool = False,
                  baseline_only: bool = True) -> HistoryRequest:
    """组装查询条件。

    anchor 给出时（调用方缓存了锚点）直接算出 since；为空时 since 留空，由
    run_history_query 在执行时现取锚点再算——锚点查询因此也落在执行查询的线程里。
    "自基线"范围的起点依赖打点，一律由 run_history_query 现算。
    """
    return HistoryRequest(
        task_id=task_id, metric=metric, range_key=range_key,
        since=compute_since(range_key, anchor), until=None,
        table_limit=table_limit, max_buckets=max_buckets,
        anchor=anchor, request_seq=seq, auto_refresh=auto_refresh,
        baseline_only=baseline_only)


def _stats_dict(agg, fit) -> dict:
    """聚合 → 统计条用的 dict（键与旧 legacy_stats 一致，另加 span_seconds）。"""
    return {
        'count': agg.count,
        'min': agg.min_value,
        'max': agg.max_value,
        'avg': agg.sum_y / agg.count,
        'first': agg.first_value,
        'first_ts': agg.first_ts,
        'last': agg.last_value,
        'last_ts': agg.last_ts,
        'slope_per_hour': fit.slope_per_hour if fit is not None else None,
        'span_seconds': agg.span_seconds,
    }


def _baseline_info(db, task_id: str, metric: str, marker: Marker,
                   stats_since: Optional[datetime], stats_count: int) -> BaselineInfo:
    """基线值与参与取值的样本数。统计起点就是基线时刻时样本数可由统计范围点数推出，
    否则（统计起点晚于基线）只有统计范围点数不足 BASELINE_SAMPLES 时才需要再数一次。"""
    value = db.analytics.value_near(
        task_id, metric, marker.timestamp, BASELINE_SAMPLES, after_only=True)
    if value is None:
        return BaselineInfo(marker, None, 0)
    if stats_since == marker.timestamp or stats_count >= BASELINE_SAMPLES:
        after = stats_count
    else:
        after = db.analytics.aggregates(task_id, metric, since=marker.timestamp).count
    return BaselineInfo(marker, value, max(1, min(BASELINE_SAMPLES, after)))


def _rounds(db, task_id: str, metric: str, markers: List[Marker],
            baseline: Optional[BaselineInfo], stats_since: Optional[datetime],
            last_ts: datetime) -> List[RoundDelta]:
    """统计范围内（基线 + 用户打点）相邻两两成一轮；用户打点不足 MIN_ROUND_MARKERS 个时不算。
    每个打点处的值与基线同口径：其后 BASELINE_SAMPLES 个样本的中位数。"""
    def in_range(ts: datetime) -> bool:
        return (stats_since is None or ts >= stats_since) and ts <= last_ts

    users = [m for m in markers if m.kind == MARKER_KIND_USER and in_range(m.timestamp)]
    if len(users) < MIN_ROUND_MARKERS:
        return []
    boundaries = [Boundary(m.timestamp, m.label, m.kind) for m in users]
    known = {}
    if baseline is not None and in_range(baseline.timestamp):
        boundaries.append(Boundary(baseline.timestamp, baseline.marker.label, 'baseline'))
        known[baseline.timestamp] = baseline.value

    def value_at(ts: datetime) -> Optional[float]:
        if ts in known:
            return known[ts]
        return db.analytics.value_near(task_id, metric, ts, BASELINE_SAMPLES, after_only=True)

    return round_deltas(boundaries, value_at)


def _to_points(task_id: str, metric: str, pairs) -> List[DataPoint]:
    return [DataPoint(task_id=task_id, timestamp=ts, value=value, metric_type=metric)
            for ts, value in pairs]


def run_history_query(db, request: HistoryRequest) -> HistorySnapshot:
    """
    同步执行一次历史查询（可在任意线程调用：仓储每次操作都新建连接）。

    查询顺序：任务 →（锚点）→ 打点 → 明细；明细为空则到此为止；选了指标时再查
    统计范围聚合（统计起点与显示起点不同时另数一次显示范围总条数）→ 基线值 →
    各轮打点值 → 空洞 → 分桶。

    存储故障（StorageError）转成快照的 error/storage_error，不向外抛；其他异常
    （编程错误）照常抛出，由调用方（后台线程外壳）记录并打包。
    """
    if request.until is not None:
        raise ValueError("历史查询暂不支持终点（统计与分桶查询没有 until 条件）")
    try:
        task_info = db.tasks.get(request.task_id)
        if task_info is None:
            return HistorySnapshot(request=request, task_missing=True)

        anchor = request.anchor
        if anchor is None:
            anchor = resolve_anchor(db, task_info)
            request = replace(request, anchor=anchor,
                              since=compute_since(request.range_key, anchor))

        markers = db.markers.list(request.task_id)
        baseline_marker = find_baseline(markers)
        if request.range_key == BASELINE_RANGE_KEY:
            request = replace(request, since=compute_since(
                request.range_key, anchor,
                baseline_marker.timestamp if baseline_marker else None))
        return _query_data(db, request, task_info, anchor, markers, baseline_marker)
    except StorageError as exc:
        logger.error("历史数据读取失败: task_id=%s", request.task_id, exc_info=True)
        return HistorySnapshot(request=request, error=str(exc) or type(exc).__name__,
                               storage_error=True)


def _query_data(db, request: HistoryRequest, task_info, anchor: Optional[datetime],
                markers: List[Marker], baseline_marker: Optional[Marker]) -> HistorySnapshot:
    task_id, metric, since = request.task_id, request.metric, request.since
    interval = float(task_info.interval or 0.0)
    base = dict(request=request, markers=markers, task_last_at=anchor,
                interval_seconds=interval,
                value_class=value_class(metric) if metric else VALUE_CLASS_LEVEL)

    points = db.points.fetch(PointQuery(task_id, metric, since=since, limit=request.table_limit))
    if not points:
        baseline = BaselineInfo(baseline_marker, None, 0) if baseline_marker else None
        return HistorySnapshot(points=points, baseline=baseline, **base)

    if not metric:
        buckets = db.analytics.legacy_buckets(task_id, None, request.max_buckets, since=since)
        baseline = BaselineInfo(baseline_marker, None, 0) if baseline_marker else None
        return HistorySnapshot(points=points, buckets=buckets, total_count=len(points),
                               baseline=baseline, stats_since=since, **base)

    stats_since = since
    if request.baseline_only and baseline_marker is not None:
        stats_since = (baseline_marker.timestamp if since is None
                       else max(since, baseline_marker.timestamp))

    agg = db.analytics.aggregates(task_id, metric, since=stats_since)
    total_count = (agg.count if stats_since == since
                   else db.analytics.aggregates(task_id, metric, since=since).count)
    fit_input = fit_input_from_aggregates(agg)

    baseline = None
    if baseline_marker is not None:
        baseline = _baseline_info(db, task_id, metric, baseline_marker, stats_since, agg.count)

    stats = judgement = rate = fit_unit = None
    rounds: List[RoundDelta] = []
    kind = spec(metric).kind if _known(metric) else None
    vclass = base['value_class']
    if fit_input is not None:
        fit = linear_fit(fit_input)
        stats = _stats_dict(agg, fit)
        fit_unit = rate_unit_for_span(agg.span_seconds)[0]
        if vclass == VALUE_CLASS_LEVEL:
            judgement = judge(fit_input, interval)
            rate = rate_display(judgement.fit, agg.span_seconds, kind)
        rounds = _rounds(db, task_id, metric, markers, baseline, stats_since, agg.last_ts)

    gaps = (db.analytics.gaps(task_id, interval, since=since, metric=metric)
            if interval > 0 else [])
    buckets = db.analytics.legacy_buckets(task_id, metric, request.max_buckets, since=since)

    rate_series: List[DataPoint] = []
    rate_stats = peak_rises = None
    if vclass == VALUE_CLASS_RATE:
        rate_series = _to_points(task_id, metric, per_second_rates(
            [(p.timestamp, p.value) for p in buckets], gaps))
        if stats is not None:
            span = agg.span_seconds
            average = (agg.last_value - agg.first_value) / span if span > 0 else None
            in_stats = [p.value for p in rate_series
                        if stats_since is None or p.timestamp > stats_since]
            rate_stats = RateStats(average, max(in_stats) if in_stats else None)
    elif vclass == VALUE_CLASS_PEAK and stats is not None:
        values = [p.value for p in buckets if stats_since is None or p.timestamp >= stats_since]
        start = baseline.value if (baseline is not None and baseline.value is not None
                                   and request.baseline_only) else agg.first_value
        peak_rises = PeakRises(count_rises(values), agg.last_value - start)

    return HistorySnapshot(
        points=points, stats=stats, buckets=buckets, total_count=total_count,
        stats_since=stats_since, baseline=baseline, judgement=judgement, rate=rate,
        fit_unit=fit_unit, gaps=list(gaps), rate_series=rate_series,
        rate_stats=rate_stats, peak_rises=peak_rises, rounds=rounds, **base)


def _known(metric: str) -> bool:
    try:
        spec(metric)
    except KeyError:
        return False
    return True
