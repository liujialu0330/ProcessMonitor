"""
历史数据查询：请求 → 快照

分析页每个指标的一次加载（用户切任务/范围、进入页面、自动刷新）都归结为同一组只读
查询：锚点、打点、表格最近 N 条、聚合与判定、空洞、分桶降采样。这里把查询条件定型为
HistoryRequest、把结果定型为 HistorySnapshot，由 run_history_query 同步执行；
后台线程（core.history_refresh_worker.HistoryRefreshWorker）只是它的线程外壳。
手动加载与自动刷新走同一函数，口径天然一致，不再需要两份编排互相对照。

时间范围的语义：锚定该任务最后一次采样的时间，而非当前时刻——停止已久的任务选
"最近1小时"仍应能看到其最后一小时的数据。锚点优先取 tasks.last_sample_at（主键
查询，随数据点写入同事务维护），仅其为空（旧数据/维护缺失）时才回退 MAX(timestamp)。
"自基线"范围从基线打点时刻起；任务没有基线时等同"全部"。"自定义"范围（分析页刷选）
由调用方直接给出起止时刻，只有它允许 until。

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
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core import verdict as verdict_rules
from core.analysis.intervals import Boundary, IntervalStats, RoundDelta, round_deltas
from core.analysis.series import count_rises, merge_gaps, per_second_rates
from core.analysis.trend import (
    Judgement, RateText, fit_input_from_aggregates, judge, linear_fit,
    rate_display, rate_unit_for_span)
from data.errors import StorageError
from data.mappers import normalize_ms
from data.models import DataPoint, Marker
from data.repos.analytics_repo import Bucket, MetricAggregates
from data.repos.point_repo import PointQuery
from utils.metrics import (
    VALUE_CLASS_LEVEL, VALUE_CLASS_PEAK, VALUE_CLASS_RATE, member_series_key, spec,
    split_member_key, value_class)

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
# "自定义"范围键（分析页刷选）：起止时刻由请求直接给出，不随锚点换算
CUSTOM_RANGE_KEY = 'custom'

# 表格取数上限：只取最近 N 条采集，避免大数据量下界面卡顿
TABLE_POINT_LIMIT = 2000
# 图表分桶上限：按时间等分为不超过这么多个桶、每桶取 MIN/MAX 两个真实点（见
# AnalyticsRepo.legacy_buckets），故图表最多 2*CHART_MAX_BUCKETS 个点
CHART_MAX_BUCKETS = 2000
# 基线值 = 基线时刻之后这么多个样本的中位数（单个尖峰样本不会带偏基线）
BASELINE_SAMPLES = 5
# "每轮 +X"至少需要的用户打点数（两个打点才围出一轮完整的操作）
MIN_ROUND_MARKERS = 2
# 分析页 min/max 带的分桶上限：小倍数图宽度有限，每桶约 1–2 像素已足够
BAND_MAX_BUCKETS = 800

MARKER_KIND_BASELINE = 'baseline'
# 被测进程重启续接的系统打点：它把任务切成若干"启动段"；快照的统计与区间只看最后一段，
# 判定（TaskAnalysis.segments）逐段做
MARKER_KIND_RESTART = 'restart'
# 采集因工具重启中断后接上的系统打点：它前后的样本之间有一段没人看着，同重启一样是数据断点
MARKER_KIND_RECOVER = 'recover'
# 这两类打点处图表不连线、不算速率（间隔再短也一样）
BREAK_MARKER_KINDS = (MARKER_KIND_RESTART, MARKER_KIND_RECOVER)
MARKER_KIND_USER = 'user'
# 打点区间表补齐的首末段标签
INTERVAL_FIRST_LABEL = '开始'
INTERVAL_LAST_LABEL = '结束'


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
      关掉后统计起点与显示起点相同，基线值仍作为参考给出；
    - analysis：分析页用，额外查 min/max 带（按时间等宽分桶、不跨空洞）与打点区间统计。
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
    analysis: bool = False


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
    - value_class：指标显示类别（utils.metrics.VALUE_CLASS_*）；
    - segment_start / segment_number / segment_count：被测进程重启续接过时，快照的统计只用
      一个启动段的数据（样本在重启前后分属不同的进程，拼成一条线会把"重启后内存回落 /
      起点不同"误判成趋势）：默认范围看最后一段，自定义范围看终点所在的段。segment_start 是
      统计所在启动段的起点（没有重启为 None），segment_number 是该段为第几次启动（从 1 起），
      segment_count 是启动总次数；各段的整体判定在 TaskAnalysis.segments；
    - baseline_segment_number / baseline_comparable：基线打点落在第几次启动里；它不在统计所在
      的段时（baseline_comparable 为假）基线值属于另一个进程，不能当本段的起点——"较基线"
      一类的增量一律不显示，界面 / 报告改写"基线在第 N 段，不可比"（baseline_note）；
    - band / intervals：仅 request.analysis 时有：显示范围的时间分桶（min/max/avg）与
      按打点（基线 + 用户打点）切出的区间统计（左闭右开，最后一段闭区间）。
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
    band: List[Bucket] = field(default_factory=list)
    intervals: List[IntervalStats] = field(default_factory=list)
    segment_start: Optional[datetime] = None
    segment_number: int = 1
    segment_count: int = 1
    baseline_segment_number: int = 1

    @property
    def only_last_segment(self) -> bool:
        """统计只用了一个启动段的数据（任务发生过重启续接）。"""
        return self.segment_count > 1

    @property
    def baseline_comparable(self) -> bool:
        """基线（若有）与统计落在同一个启动段，基线值可以当作本段的起点。"""
        return self.baseline is None or self.baseline_segment_number == self.segment_number

    @property
    def baseline_in_use(self) -> bool:
        """基线值是统计的起点：有基线值、只统计基线之后，且基线与统计在同一启动段（基线在别的
        段时它是另一个进程的值，不能当起点）。分析页摘要表、增速排序与叠加参考值共用。"""
        return (self.baseline is not None and self.baseline.value is not None
                and self.request.baseline_only and self.baseline_comparable)

    @property
    def baseline_foreign(self) -> bool:
        """本该以基线为参照（有基线、只统计基线之后），但基线落在别的启动段：界面的"基线"
        与"较基线"写 "—"，说明见 baseline_note。"""
        return (self.baseline is not None and self.request.baseline_only
                and not self.baseline_comparable)

    @property
    def baseline_note(self) -> str:
        """基线与统计不在同一段时的说明："基线在第 N 段，不可比"；可比或没有基线为空串。"""
        if self.baseline_comparable:
            return ""
        return f"基线在第 {self.baseline_segment_number} 段，不可比"

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
    - 选中"全部"（或未知的范围键，含"自定义"——其起点由请求直接给出）、或该任务尚无
      数据点（锚点为空）时返回 None，即不过滤。
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
                  baseline_only: bool = True, analysis: bool = False,
                  since: Optional[datetime] = None,
                  until: Optional[datetime] = None) -> HistoryRequest:
    """组装查询条件。

    anchor 给出时（调用方缓存了锚点）直接算出 since；为空时 since 留空，由
    run_history_query 在执行时现取锚点再算——锚点查询因此也落在执行查询的线程里。
    "自基线"范围的起点依赖打点，一律由 run_history_query 现算。
    since / until 只对"自定义"范围有效（刷选的起止时刻），其余范围忽略。
    """
    if range_key == CUSTOM_RANGE_KEY:
        if since is None or until is None or until < since:
            raise ValueError("自定义范围需要 since <= until")
    else:
        since, until = compute_since(range_key, anchor), None
    return HistoryRequest(
        task_id=task_id, metric=metric, range_key=range_key,
        since=since, until=until,
        table_limit=table_limit, max_buckets=max_buckets,
        anchor=anchor, request_seq=seq, auto_refresh=auto_refresh,
        baseline_only=baseline_only, analysis=analysis)


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


def _restart_times(markers: Sequence[Marker]) -> List[datetime]:
    return sorted(_bound(m.timestamp) for m in markers if m.kind == MARKER_KIND_RESTART)


def segment_of(markers: List[Marker],
               until: Optional[datetime] = None) -> Tuple[Optional[datetime], int, int]:
    """按"重启"系统打点切出的启动段：返回 (统计所在段的起点, 该段是第几次启动, 启动总次数)。

    起点取最后一个不晚于 until（没有终点则不限）的重启打点；没有重启返回 (None, 1, 1)。
    重启打点的时刻是工具发现新进程的时刻，之前的样本都属于上一个进程。"""
    restarts = _restart_times(markers)
    count = len(restarts) + 1
    eligible = [t for t in restarts if until is None or t <= until]
    return (eligible[-1] if eligible else None), len(eligible) + 1, count


def segment_note(snapshot) -> str:
    """结论 / 依据行里附的提示："仅统计第 N 次启动后的数据"；任务没有重启过为空串。
    界面、报告与证据包共用这一句，避免各写各的。"""
    if getattr(snapshot, 'segment_count', 1) <= 1:
        return ""
    return f"仅统计第 {snapshot.segment_number} 次启动后的数据"


def _baseline_info(db, task_id: str, metric: str, marker: Marker,
                   stats_since: Optional[datetime], stats_count: int) -> BaselineInfo:
    """基线值与参与取值的样本数。统计起点就是基线时刻时样本数可由统计范围点数推出，
    否则（统计起点晚于基线）只有统计范围点数不足 BASELINE_SAMPLES 时才需要再数一次
    （point_count 按范围计数，大范围读分钟汇总，不扫样本）。"""
    value = db.analytics.value_near(
        task_id, metric, marker.timestamp, BASELINE_SAMPLES, after_only=True)
    if value is None:
        return BaselineInfo(marker, None, 0)
    if stats_since == _bound(marker.timestamp) or stats_count >= BASELINE_SAMPLES:
        after = stats_count
    else:
        after = db.analytics.point_count(task_id, metric, since=_bound(marker.timestamp))
    return BaselineInfo(marker, value, max(1, min(BASELINE_SAMPLES, after)))


def _rounds(db, task_id: str, metric: str, markers: List[Marker],
            baseline: Optional[BaselineInfo], stats_since: Optional[datetime],
            last_ts: datetime) -> List[RoundDelta]:
    """统计范围内（基线 + 用户打点）相邻两两成一轮；用户打点不足 MIN_ROUND_MARKERS 个时不算。
    每个打点处的值与基线同口径：其后 BASELINE_SAMPLES 个样本的中位数。"""
    def in_range(ts: datetime) -> bool:
        ts = _bound(ts)
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


def _bound(ts: datetime) -> datetime:
    """
    打点/基线时刻作为样本查询边界：按样本同一规则（data.mappers.to_ms，四舍五入）取整到毫秒，
    与该时刻同时采集的样本因此总落在打点之后（规则见 data.mappers 模块说明）
    """
    return normalize_ms(ts)


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
    custom = request.range_key == CUSTOM_RANGE_KEY
    if request.until is not None and not custom:
        raise ValueError("只有自定义范围支持终点 until")
    try:
        task_info = db.tasks.get(request.task_id)
        if task_info is None:
            return HistorySnapshot(request=request, task_missing=True)

        anchor = request.anchor
        if anchor is None:
            anchor = resolve_anchor(db, task_info)
            request = replace(request, anchor=anchor, since=(
                request.since if custom else compute_since(request.range_key, anchor)))

        markers = db.markers.list(request.task_id)
        baseline_marker = find_baseline(markers)
        if request.range_key == BASELINE_RANGE_KEY:
            request = replace(request, since=compute_since(
                request.range_key, anchor,
                _bound(baseline_marker.timestamp) if baseline_marker else None))
        return _query_data(db, request, task_info, anchor, markers, baseline_marker)
    except StorageError as exc:
        logger.error("历史数据读取失败: task_id=%s", request.task_id, exc_info=True)
        return HistorySnapshot(request=request, error=str(exc) or type(exc).__name__,
                               storage_error=True)


def _query_data(db, request: HistoryRequest, task_info, anchor: Optional[datetime],
                markers: List[Marker], baseline_marker: Optional[Marker]) -> HistorySnapshot:
    task_id, metric, since, until = (
        request.task_id, request.metric, request.since, request.until)
    interval = float(task_info.interval or 0.0)
    segment_start, segment_number, segment_count = segment_of(markers, until)
    baseline_segment = 1
    if baseline_marker is not None:
        at = _bound(baseline_marker.timestamp)
        baseline_segment = 1 + sum(1 for t in _restart_times(markers) if t <= at)
    base = dict(request=request, markers=markers, task_last_at=anchor,
                interval_seconds=interval,
                value_class=value_class(metric) if metric else VALUE_CLASS_LEVEL,
                segment_start=segment_start, segment_number=segment_number,
                segment_count=segment_count, baseline_segment_number=baseline_segment)

    points = db.points.fetch(PointQuery(
        task_id, metric, since=since, until=until, limit=request.table_limit))
    if not points:
        baseline = BaselineInfo(baseline_marker, None, 0) if baseline_marker else None
        return HistorySnapshot(points=points, baseline=baseline, **base)

    if not metric:
        buckets = db.analytics.legacy_buckets(task_id, None, request.max_buckets, since=since)
        if until is not None:
            buckets = [p for p in buckets if p.timestamp <= until]
        baseline = BaselineInfo(baseline_marker, None, 0) if baseline_marker else None
        return HistorySnapshot(points=points, buckets=buckets, total_count=len(points),
                               baseline=baseline, stats_since=since, **base)

    stats_since = since
    if request.baseline_only and baseline_marker is not None:
        stats_since = (_bound(baseline_marker.timestamp) if since is None
                       else max(since, _bound(baseline_marker.timestamp)))
        # 基线落在自定义范围之后：这段数据没有"基线之后"，统计退回整个显示范围
        if until is not None and stats_since > until:
            stats_since = since
    # 只统计最后一次启动之后的数据：更早的样本属于另一个进程
    if segment_start is not None and (stats_since is None or segment_start > stats_since):
        stats_since = segment_start

    agg = db.analytics.aggregates(task_id, metric, since=stats_since, until=until)
    display_agg = (agg if stats_since == since
                   else db.analytics.aggregates(task_id, metric, since=since, until=until))
    total_count = display_agg.count
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

    gaps = (db.analytics.gaps(task_id, interval, since=since, until=until, metric=metric)
            if interval > 0 else [])
    # 重启 / 续采处无论间隔多短都是断点：前后分属不同进程（或中间有一段无人看守），连线
    # 会画出假的"回落"，速率会把跨边界的差当成一段真实变化
    break_instants = tuple(m.timestamp for m in markers if m.kind in BREAK_MARKER_KINDS)
    gaps = merge_gaps(gaps, db.analytics.break_pairs(
        task_id, break_instants, since=since, until=until, metric=metric))
    buckets = _chart_points(db, request, total_count)

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
        # 基线不在当前启动段时它属于另一个进程，不能当作本段的起点
        baseline_in_segment = baseline is not None and baseline_segment == segment_number
        start = baseline.value if (baseline_in_segment and baseline.value is not None
                                   and request.baseline_only) else agg.first_value
        peak_rises = PeakRises(count_rises(values), agg.last_value - start)

    band: List[Bucket] = []
    intervals: List[IntervalStats] = []
    if request.analysis and display_agg.count:
        band = db.analytics.buckets(
            task_id, metric, min(BAND_MAX_BUCKETS, request.max_buckets),
            since=since, until=until, interval_seconds=interval if interval > 0 else None,
            break_instants=break_instants)
        first_ts = display_agg.first_ts
        if segment_start is not None and segment_start > first_ts:
            first_ts = segment_start
        intervals = _intervals(db, task_id, metric, markers, first_ts, display_agg.last_ts,
                               whole_range=display_agg)

    return HistorySnapshot(
        points=points, stats=stats, buckets=buckets, total_count=total_count,
        stats_since=stats_since, baseline=baseline, judgement=judgement, rate=rate,
        fit_unit=fit_unit, gaps=list(gaps), rate_series=rate_series,
        rate_stats=rate_stats, peak_rises=peak_rises, rounds=rounds,
        band=band, intervals=intervals, **base)


def _chart_points(db, request: HistoryRequest, total_count: int) -> List[DataPoint]:
    """图表用的降采样点（真实数据点）。有终点（自定义范围）时：范围内点数不多就直接取
    原始点，否则按起点分桶后裁掉终点之后的部分（分桶查询没有终点条件）。"""
    task_id, metric, since, until = (
        request.task_id, request.metric, request.since, request.until)
    if until is not None and total_count <= 2 * request.max_buckets:
        return db.points.fetch(PointQuery(task_id, metric, since=since, until=until))
    buckets = db.analytics.legacy_buckets(task_id, metric, request.max_buckets, since=since)
    if until is not None:
        buckets = [p for p in buckets if p.timestamp <= until]
    return buckets


def _intervals(db, task_id: str, metric: str, markers: List[Marker],
               first_ts: datetime, last_ts: datetime, *,
               whole_range: Optional[MetricAggregates] = None) -> List[IntervalStats]:
    """
    按打点（基线 + 用户打点，暂停/恢复不算）把 [first_ts, last_ts] 切成相邻区间，
    每段用一次聚合查询取起止值、峰值与点数（不把原始点读进内存）。

    口径与 core.analysis.intervals.split_intervals 一致：左闭右开、最后一段闭区间；
    首个打点晚于首点时补"开始"段，最后一个打点早于末点时补"结束"段；没有打点时整段
    为一个区间。label 取右边界的标签。
    """
    edges = [Boundary(m.timestamp, m.label, m.kind) for m in markers
             if m.kind in (MARKER_KIND_USER, MARKER_KIND_BASELINE)
             and first_ts <= _bound(m.timestamp) <= last_ts]
    if not edges or _bound(edges[0].timestamp) > first_ts:
        edges.insert(0, Boundary(first_ts, INTERVAL_FIRST_LABEL, 'start'))
    if _bound(edges[-1].timestamp) < last_ts:
        edges.append(Boundary(last_ts, INTERVAL_LAST_LABEL, 'end'))
    if len(edges) < 2:
        return []

    result: List[IntervalStats] = []
    prev_delta: Optional[float] = None
    last_index = len(edges) - 2
    for i in range(len(edges) - 1):
        left, right = edges[i], edges[i + 1]
        # 打点时刻按样本同一规则取整后作查询边界（见 _bound）；右开：终点退 1 毫秒
        start = _bound(left.timestamp)
        end = (_bound(right.timestamp) if i == last_index
               else _bound(right.timestamp) - timedelta(milliseconds=1))
        # 仅复用当前请求同一指标的完整闭区间；重启裁剪、打点分段均须重新查询。
        if (last_index == 0 and whole_range is not None and whole_range.count
                and start == whole_range.first_ts and end == whole_range.last_ts):
            agg = whole_range
        else:
            agg = (db.analytics.aggregates(task_id, metric, since=start, until=end)
                   if end >= start else None)
        duration = (right.timestamp - left.timestamp).total_seconds()
        if agg is not None and agg.count:
            start_value, end_value = agg.first_value, agg.last_value
            delta: Optional[float] = end_value - start_value
            peak: Optional[float] = agg.max_value
            rate = delta / duration * 3600.0 if duration > 0 else None
            count = agg.count
        else:
            start_value = end_value = delta = peak = rate = None
            count = 0
        result.append(IntervalStats(
            index=i, label=right.label, start=left.timestamp, end=right.timestamp,
            duration_seconds=duration, start_value=start_value, end_value=end_value,
            delta=delta, rate_per_hour=rate, peak=peak, sample_count=count,
            delta_vs_previous=(delta - prev_delta
                               if delta is not None and prev_delta is not None else None)))
        prev_delta = delta
    return result


@dataclass(frozen=True)
class TaskAnalysis:
    """分析页一次加载的结果：同一任务、同一范围下每个指标各一份快照（按任务指标顺序）。

    request_seq 是这一批查询共用的批次序号（视图模型判过期用），auto_refresh 同快照。
    """
    task_id: str
    range_key: str
    snapshots: Dict[str, HistorySnapshot]
    request_seq: int = 0
    auto_refresh: bool = False
    # 进程组任务（tree / name）的成员序列快照 {"指标@成员键": 快照}，按成员、指标的建立顺序；
    # pid 任务为空。判定把它们和合计一起看（见 core.verdict）。用 load_member_snapshots 取
    member_snapshots: Dict[str, HistorySnapshot] = field(default_factory=dict)
    # 逐启动段的判定（按段号升序，没有重启时只有一段）；整体判定取最差的一段，见 core.verdict。
    # 用 load_segments 取；空列表表示没有分段信息（旧结构 / 替身），判定退回只看 snapshots
    segments: List["SegmentAnalysis"] = field(default_factory=list)

    @property
    def metrics(self) -> List[str]:
        return list(self.snapshots)

    @property
    def member_keys(self) -> List[str]:
        """有成员序列快照的成员键（去重，保持顺序）。"""
        keys: List[str] = []
        for series_key in self.member_snapshots:
            member = split_member_key(series_key)[1]
            if member is not None and member not in keys:
                keys.append(member)
        return keys

    def member_snapshot(self, metric: Optional[str],
                        member: Optional[str]) -> Optional[HistorySnapshot]:
        """某成员某指标的快照；没有为 None。"""
        if not metric or member is None:
            return None
        return self.member_snapshots.get(member_series_key(metric, member))

    def snapshot(self, metric: Optional[str]) -> Optional[HistorySnapshot]:
        return self.snapshots.get(metric) if metric else None

    @property
    def markers(self) -> List[Marker]:
        """任务的全部打点（各快照相同，取第一份）"""
        first = next(iter(self.snapshots.values()), None)
        return list(first.markers) if first is not None else []

    @property
    def has_data(self) -> bool:
        return any(s.points for s in self.snapshots.values())


@dataclass(frozen=True)
class SegmentAnalysis:
    """一个启动段（被测进程的一次启动）的判定。

    start / end 为段的起止时刻（start 为 None 表示任务开始，end 为 None 表示仍是最后一段）；
    verdict 为 rising / stable / insufficient（insufficient 即"本段数据不足"，包括段太短、
    重启后还没有样本）；rising_metrics 是本段持续上涨的序列键（合计指标键或"指标@成员键"）；
    sample_count 为本段内各序列统计样本数的最大值；metric_verdicts 是本段每个序列的判定。
    """
    number: int
    start: Optional[datetime]
    end: Optional[datetime]
    verdict: str
    rising_metrics: Tuple[str, ...] = ()
    sample_count: int = 0
    metric_verdicts: Dict[str, str] = field(default_factory=dict)


def segment_spans(markers: Sequence[Marker]
                  ) -> List[Tuple[int, Optional[datetime], Optional[datetime]]]:
    """按"重启"打点切出各启动段 [(段号, 起点, 终点)]：第 1 段起点为 None，最后一段终点为 None；
    非最后一段的终点是下一次重启前 1 毫秒（查询的 until 含端点，重启时刻起的样本属于下一段）。"""
    restarts = _restart_times(markers)
    spans = []
    start: Optional[datetime] = None
    for number, restart in enumerate(restarts, start=1):
        spans.append((number, start, restart - timedelta(milliseconds=1)))
        start = restart
    spans.append((len(restarts) + 1, start, None))
    return spans


def build_segment(number: int, start: Optional[datetime], end: Optional[datetime],
                  snapshots: Mapping[str, HistorySnapshot]) -> SegmentAnalysis:
    """某一段内各序列（合计指标与成员序列）的快照 → 本段判定。"""
    verdicts = {key: verdict_rules.metric_verdict(s) for key, s in snapshots.items()}
    counts = [(s.stats or {}).get('count', 0) for s in snapshots.values()]
    rising = tuple(key for key, v in verdicts.items() if v == verdict_rules.VERDICT_RISING)
    return SegmentAnalysis(
        number=number, start=start, end=end,
        verdict=verdict_rules.segment_verdict(verdicts.values()),
        rising_metrics=rising, sample_count=max(counts) if counts else 0,
        metric_verdicts=verdicts)


def _segment_snapshots(db, task_id: str, keys: Sequence[str], start: Optional[datetime],
                       end: Optional[datetime],
                       is_cancelled: Optional[Callable[[], bool]]
                       ) -> Optional[Dict[str, HistorySnapshot]]:
    """某一启动段 [start, end] 内逐序列的快照（自定义范围，口径与整体查询一致）。
    查询失败抛 StorageError，取消返回 None。"""
    result: Dict[str, HistorySnapshot] = {}
    for key in keys:
        if is_cancelled is not None and is_cancelled():
            return None
        request = replace(build_request(task_id, key, DEFAULT_RANGE_KEY),
                          range_key=CUSTOM_RANGE_KEY, since=start, until=end)
        snapshot = run_history_query(db, request)
        if snapshot.error is not None:
            raise StorageError(snapshot.error)
        result[key] = snapshot
    return result


def load_segments(db, task_id: str, metrics: Sequence[str],
                  snapshots: Mapping[str, HistorySnapshot],
                  member_snapshots: Mapping[str, HistorySnapshot], *,
                  is_cancelled: Optional[Callable[[], bool]] = None,
                  reuse_last: bool = True
                  ) -> Optional[List[SegmentAnalysis]]:
    """逐启动段判定。snapshots / member_snapshots 是默认范围（统计只用最后一段）的整体查询
    结果，直接作为最后一段的依据；更早的各段按段的起止时刻（自定义范围）再查一遍。
    没有重启时只有一段，不多查。is_cancelled 为真时返回 None；查询失败抛 StorageError。

    reuse_last 为假时最后一段也按段的起止时刻再查（snapshots 统计的不是最后一段时用，如分析页
    的自定义范围落在更早的某一段里）。"""
    markers = db.markers.list(task_id)
    spans = segment_spans(markers)
    last = {**snapshots, **member_snapshots}
    if len(spans) == 1:
        return [build_segment(1, None, None, last)]
    keys = list(metrics) + member_series_keys(db, task_id, metrics)
    segments: List[SegmentAnalysis] = []
    for number, start, end in spans[:-1 if reuse_last else None]:
        found = _segment_snapshots(db, task_id, keys, start, end, is_cancelled)
        if found is None:
            return None
        segments.append(build_segment(number, start, end, found))
    if reuse_last:
        number, start, end = spans[-1]
        segments.append(build_segment(number, start, end, last))
    return segments


def member_series_keys(db, task_id: str, metrics: Sequence[str]) -> List[str]:
    """进程组任务里已有样本的成员序列键 "指标@成员键"（只含 metrics 里的指标，按序列建立
    顺序）。pid 任务与没有成员仓储的库返回空列表。"""
    members = getattr(db, 'members', None)
    if members is None:
        return []
    wanted = set(metrics)
    return [member_series_key(base, member) for base, member in members.member_series(task_id)
            if base in wanted]


def load_member_snapshots(db, task_id: str, metrics: Sequence[str], range_key: str, *,
                          analysis: bool = True,
                          is_cancelled: Optional[Callable[[], bool]] = None,
                          **request_options) -> Optional[Dict[str, HistorySnapshot]]:
    """逐成员序列跑 run_history_query（与合计序列同一函数、同一范围，口径一致）。

    返回 {序列键: 快照}；pid 任务为空字典。is_cancelled 为真时返回 None（调用方据此放弃）。
    存储故障转成快照的 error，由调用方像对合计快照一样处理。request_options 原样交给
    build_request（自定义范围的 since / until、表格与分桶上限等，与合计查询保持一致）。"""
    snapshots: Dict[str, HistorySnapshot] = {}
    for key in member_series_keys(db, task_id, metrics):
        if is_cancelled is not None and is_cancelled():
            return None
        snapshots[key] = run_history_query(
            db, build_request(task_id, key, range_key, analysis=analysis, **request_options))
    return snapshots


def _known(metric: str) -> bool:
    try:
        spec(metric)
    except KeyError:
        return False
    return True
