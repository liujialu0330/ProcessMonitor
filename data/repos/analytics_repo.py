"""
分析仓储：聚合、按时间分桶、空洞检测、基线取值等只读查询

样本存在 samples(series_id, ts_ms, value)，ts_ms 为 UTC 毫秒整数；时间参数一律是 datetime，
在 data.repos._samples.ms_range 统一换成整数毫秒（since 向上取整、until 向下取整），传
float/int 直接 TypeError，杜绝"把秒级 epoch 当毫秒比较、过滤静默失效"。
所有相对时间、间隔都用整数毫秒相减，空洞判定与分桶边界因此是确定的；返回给调用方的
时间一律经 data.mappers.from_ms 转回本地 naive datetime。

两条查询路径（结果相同：计数与时间戳完全一致，浮点只差求和顺序带来的舍入）：
- 原始路径：直接扫 samples；
- 汇总路径：读分钟汇总 rollup_1m 与写时空洞 series_gaps（规则见 data.repos.rollup），
  范围两端不完整的汇总行（以及分桶时跨桶边界的汇总行）回到 samples 取原始样本。
范围内样本数（按 rollup_1m 的 SUM(count) 估算，一次主键范围查询）超过 RAW_PATH_MAX_ROWS
时走汇总路径。依赖空洞口径的查询（gaps、buckets）只有在空洞口径与写入时相同
（gap_factor 为默认值、interval_seconds 等于任务的采集周期）时才走汇总路径。
构造参数 path 可强制某条路径（测试比对两条路径用；强制汇总时口径不符仍走原始）。

每次查询在一个读事务里完成（_connect 显式 BEGIN）：汇总路径要先后读 rollup_1m、series_gaps
与两端的原始样本，WAL 下各语句默认各自取快照，期间采集线程追加的样本会让前后几条语句看到
不同的数据（如桶数按旧样本算、端点又读到新样本）；同一读事务内所有语句看到同一快照。

所有 sqlite3 异常包成 StorageError 向上抛（原始异常挂在 __cause__），是否降级由调用方决定。
"""
import bisect
import json
import math
import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from data.mappers import from_ms, to_ms
from data.models import DataPoint
from data.repos import rollup
from data.repos._guard import storage_errors
from data.repos._samples import (check_time, fetch_packed, in_clause, ms_range, series_id,
                                  task_series)
from data.repos.rollup import DEFAULT_GAP_FACTOR, MINUTE_MS  # noqa: F401  DEFAULT_GAP_FACTOR 对外沿用此处

# 范围内样本数超过它时走汇总路径（原始路径扫 20 万行约几十毫秒，再多就该读汇总）
RAW_PATH_MAX_ROWS = 200_000

PATH_RAW = 'raw'
PATH_ROLLUP = 'rollup'


def _threshold(interval_seconds: float, gap_factor: float) -> float:
    """空洞阈值（秒）= gap_factor × interval_seconds，两者都须 > 0"""
    if interval_seconds is None or interval_seconds <= 0:
        raise ValueError(f'interval_seconds 必须 > 0，实际为 {interval_seconds!r}')
    if gap_factor <= 0:
        raise ValueError(f'gap_factor 必须 > 0，实际为 {gap_factor!r}')
    return float(interval_seconds) * float(gap_factor)


def _gap_rows(conn: sqlite3.Connection, pts_cte: str, pts_params: list,
              threshold: float) -> list:
    """
    pts(ts, ms) 中相邻间隔 > threshold 秒的位置：prev/pms 为空洞前一点，ts/ms 为空洞后一点。
    ts 为绝对毫秒，ms 为相对锚点的毫秒（两者之差是常数，间隔相同）。
    """
    return conn.execute(f'''
        WITH {pts_cte},
        d AS (
            SELECT ts, ms,
                   LAG(ts) OVER (ORDER BY ts) AS prev,
                   LAG(ms) OVER (ORDER BY ts) AS pms
            FROM pts
        )
        SELECT prev, pms, ts, ms FROM d
        WHERE pms IS NOT NULL AND (ms - pms) / 1000.0 > ?
        ORDER BY ts
    ''', [*pts_params, threshold]).fetchall()


def _weighted_median(hist: List[Tuple[float, int]]) -> float:
    """按 (取值, 次数) 升序直方图求中位数；总数为偶数时取中间两值平均"""
    total = sum(c for _, c in hist)
    ranks = ((total + 1) // 2, (total + 2) // 2)   # 1 起的名次
    picked = []
    seen = 0
    for value, count in hist:
        seen += count
        while len(picked) < 2 and seen >= ranks[len(picked)]:
            picked.append(value)
        if len(picked) == 2:
            break
    return (picked[0] + picked[1]) / 2.0


def _segment_buckets(spans: List[float], width: float) -> int:
    """
    各段按桶宽 width 切分后桶数之和的上界：每段 floor(段跨度 / width) + 1。
    与 SQL 里 CAST((ms - 段首) / w AS INTEGER) 对段末点的运算逐位相同。
    """
    return sum(math.floor(s / width) + 1 for s in spans)


def _bucket_width(spans: List[float], max_buckets: int) -> float:
    """
    使总桶数 <= max_buckets 的最小桶宽（单位同 spans），二分求得。
    段数 >= max_buckets 或各段都是单点时返回 inf（每段一桶）。
    """
    covered = sum(spans)
    nseg = len(spans)
    if nseg >= max_buckets or covered <= 0:
        return math.inf
    lo = covered / max_buckets          # 小于它必然超出上限
    hi = covered / (max_buckets - nseg)  # Σ(s/hi + 1) = max_buckets，必然可行
    while _segment_buckets(spans, hi) > max_buckets:   # 只防舍入，通常不进循环
        hi *= 1.0 + 1e-12
    for _ in range(200):
        if hi - lo <= hi * 1e-12:
            break
        mid = (lo + hi) / 2.0
        if _segment_buckets(spans, mid) <= max_buckets:
            hi = mid
        else:
            lo = mid
    return hi


def _sid_list(sids: Sequence[int]) -> str:
    """series_id IN (...) 的字面量列表（series_id 来自库里的整数主键，直接内联）"""
    return ','.join(str(int(sid)) for sid in sids)


def _chart_buckets(t0: int, t1: int, lo: int, hi: int, max_points: int
                   ) -> List[Tuple[int, int]]:
    """
    图表降采样的时间桶 [起, 止]（毫秒闭区间，已裁到 [lo, hi]），按时间升序。

    - 首末样本所在自然分钟覆盖的时长 ≥ max_points 分钟：桶宽取能把它分成不超过 max_points
      份的最小整分钟数，桶边界对齐自然分钟——汇总行不跨分钟，因此每个汇总行整体落在一个桶里；
    - 否则把 [t0, t1] 按毫秒等分为 max_points 份：样本 t 落在第
      min((t - t0) × max_points // (t1 - t0), max_points - 1) 个桶（t1 == t0 时只有一个桶）。
    """
    origin = rollup.minute_floor(t0)
    extent = rollup.minute_floor(t1) + MINUTE_MS - origin
    edges: List[Tuple[int, int]] = []
    if extent >= max_points * MINUTE_MS:
        width = -(-extent // (max_points * MINUTE_MS)) * MINUTE_MS
        start = origin
        while start <= t1:
            edges.append((start, start + width - 1))
            start += width
    else:
        span = t1 - t0
        if span <= 0:
            edges.append((t0, t1))
        else:
            n = max_points
            for b in range(n):
                left = t0 + (b * span + n - 1) // n
                right = t1 if b == n - 1 else t0 + ((b + 1) * span + n - 1) // n - 1
                if left <= right:
                    edges.append((left, right))
    return [(max(a, lo), min(b, hi)) for a, b in edges if max(a, lo) <= min(b, hi)]


@dataclass(frozen=True)
class MetricAggregates:
    """
    某任务某指标在 [since, until] 内的聚合量。
    x 为相对 first_ts 的秒数（毫秒精度），y 为指标值；count == 0 时可选字段为 None、各和为 0。
    转成趋势拟合输入用 core.analysis.trend.fit_input_from_aggregates（数据层不依赖 core）。
    """
    task_id: str
    metric: str
    count: int
    first_ts: Optional[datetime]
    last_ts: Optional[datetime]
    first_value: Optional[float]
    last_value: Optional[float]
    min_value: Optional[float]
    max_value: Optional[float]
    sum_x: float
    sum_y: float
    sum_xx: float
    sum_xy: float
    sum_yy: float
    span_seconds: float


@dataclass(frozen=True)
class Bucket:
    """
    一个时间桶。start/end 是桶内首末两个真实数据点的时间（不是理论桶边界），
    因此相邻桶之间若隔着空洞，start/end 不会伸进空洞里。
    """
    start: datetime
    end: datetime
    min: float
    max: float
    avg: float
    last: float
    count: int


def _empty_aggregates(task_id: str, metric: str) -> MetricAggregates:
    return MetricAggregates(
        task_id=task_id, metric=metric, count=0,
        first_ts=None, last_ts=None, first_value=None, last_value=None,
        min_value=None, max_value=None,
        sum_x=0.0, sum_y=0.0, sum_xx=0.0, sum_xy=0.0, sum_yy=0.0,
        span_seconds=0.0)


# 汇总行整体落在 [lo, hi] 内的条件（参数：series_id, lo - 1 分钟, hi, lo, hi）。
# bucket_ms <= first_ms < bucket_ms + 1 分钟，故只需扫 bucket_ms ∈ (lo - 1 分钟, hi]
_FULL_ROWS = ('series_id = ? AND bucket_ms > ? AND bucket_ms <= ? '
              'AND first_ms >= ? AND last_ms <= ?')


def _full_params(sid: int, lo: int, hi: int) -> tuple:
    return (sid, lo - MINUTE_MS, hi, lo, hi)


def _edge_ranges(lo: int, hi: int, first_full: Optional[int], last_full: Optional[int]
                 ) -> List[Tuple[int, int]]:
    """[lo, hi] 中不被完整汇总行覆盖的两端（完整汇总行覆盖 [first_full, last_full] 内的全部样本）"""
    if first_full is None:
        return [(lo, hi)]
    ranges = []
    if first_full > lo:
        ranges.append((lo, first_full - 1))
    if last_full < hi:
        ranges.append((last_full + 1, hi))
    return ranges


class AnalyticsRepo:
    """只读分析查询。factory 需提供 connect() 上下文管理器（row_factory = sqlite3.Row）。"""

    def __init__(self, factory, path: Optional[str] = None):
        """
        Args:
            factory: 连接工厂
            path: None 按范围内样本数自动选路径；'raw' 一律原始路径；'rollup' 只要空洞口径
                允许就走汇总路径（测试比对两条路径用）
        """
        if path not in (None, PATH_RAW, PATH_ROLLUP):
            raise ValueError(f'path 只能是 None、{PATH_RAW!r} 或 {PATH_ROLLUP!r}，实际为 {path!r}')
        self._factory = factory
        self._path = path

    @contextmanager
    def _connect(self, what: str) -> Iterator[sqlite3.Connection]:
        """
        连接上下文：显式 BEGIN 开读事务（WAL 下首条读语句取快照，块内各语句共用），块结束时
        由连接工厂 COMMIT（异常时 ROLLBACK）。只读连接（query_only）同样允许 BEGIN。
        块内 sqlite3.Error 经 storage_errors 统一改抛 StorageError。
        """
        with storage_errors(what):
            with self._factory.connect() as conn:
                conn.execute('BEGIN')
                yield conn

    # ========== 路径选择 ==========

    def _use_rollup(self, conn: sqlite3.Connection, sids: Sequence[int],
                    lo: int, hi: int) -> bool:
        """范围内样本数（按汇总行估算）是否超过 RAW_PATH_MAX_ROWS；path 参数优先"""
        if self._path is not None:
            return self._path == PATH_ROLLUP
        row = conn.execute(f'''
            SELECT SUM(count) FROM rollup_1m
            WHERE series_id IN ({_sid_list(sids)}) AND bucket_ms > ? AND bucket_ms <= ?
        ''', (lo - MINUTE_MS, hi)).fetchone()
        return (row[0] or 0) > RAW_PATH_MAX_ROWS

    def _gap_rule_matches(self, conn: sqlite3.Connection, task_id: str,
                          interval_seconds: Optional[float], gap_factor: float) -> bool:
        """查询的空洞口径是否与写入 series_gaps / 汇总桶键时相同"""
        if self._path == PATH_RAW or interval_seconds is None:
            return False
        if gap_factor != DEFAULT_GAP_FACTOR:
            return False
        row = conn.execute('SELECT interval FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
        if row is None or rollup.gap_threshold(row[0]) is None:
            return False
        return float(row[0]) == float(interval_seconds)

    # ========== 公共片段 ==========

    @staticmethod
    def _bounds(conn: sqlite3.Connection, sids: Sequence[int], lo: int, hi: int
                ) -> Tuple[Optional[int], Optional[int]]:
        """这些序列在 [lo, hi] 内的首末样本时间（逐序列走主键两端）"""
        parts = ' UNION ALL '.join(
            f'SELECT (SELECT MIN(ts_ms) FROM samples WHERE series_id = {int(sid)} '
            f'AND ts_ms BETWEEN ?1 AND ?2) AS f, '
            f'(SELECT MAX(ts_ms) FROM samples WHERE series_id = {int(sid)} '
            f'AND ts_ms BETWEEN ?1 AND ?2) AS l' for sid in sids)
        row = conn.execute(f'SELECT MIN(f), MAX(l) FROM ({parts})', (lo, hi)).fetchone()
        return row[0], row[1]

    @staticmethod
    def _raw_count(conn: sqlite3.Connection, sid: int, ranges: List[Tuple[int, int]]) -> int:
        total = 0
        for a, b in ranges:
            total += conn.execute('SELECT COUNT(*) FROM samples WHERE series_id = ? '
                                  'AND ts_ms BETWEEN ? AND ?', (sid, a, b)).fetchone()[0]
        return total

    def _rollup_count(self, conn: sqlite3.Connection, sid: int, lo: int, hi: int) -> int:
        """[lo, hi] 内的样本数：完整汇总行求和 + 两端原始样本计数"""
        full = conn.execute(f'SELECT SUM(count), MIN(first_ms), MAX(last_ms) FROM rollup_1m '
                            f'WHERE {_FULL_ROWS}', _full_params(sid, lo, hi)).fetchone()
        count = full[0] or 0
        return count + self._raw_count(conn, sid, _edge_ranges(lo, hi, full[1], full[2]))

    # ========== 聚合 ==========

    def aggregates(self, task_id: str, metric: str,
                   since: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> MetricAggregates:
        """[since, until]（两端含等于）内的点数、首末点、极值与拟合所需各阶和"""
        lo, hi = ms_range(since, until)
        with self._connect('读取指标聚合') as conn:
            sid = series_id(conn, task_id, metric)
            if sid is None:
                return _empty_aggregates(task_id, metric)
            rng = (sid, lo, hi)
            first_row = conn.execute(
                'SELECT ts_ms, value FROM samples WHERE series_id = ? '
                'AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms ASC LIMIT 1', rng).fetchone()
            if first_row is None:
                return _empty_aggregates(task_id, metric)
            last_row = conn.execute(
                'SELECT ts_ms, value FROM samples WHERE series_id = ? '
                'AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms DESC LIMIT 1', rng).fetchone()
            origin = first_row['ts_ms']
            if self._use_rollup(conn, [sid], lo, hi):
                sums = self._rollup_sums(conn, sid, lo, hi, origin)
            else:
                sums = self._raw_sums(conn, sid, [(lo, hi)], origin)

        cnt, min_v, max_v, sx, sy, sxx, sxy, syy = sums
        first_ts = from_ms(first_row['ts_ms'])
        last_ts = from_ms(last_row['ts_ms'])
        return MetricAggregates(
            task_id=task_id, metric=metric, count=cnt,
            first_ts=first_ts, last_ts=last_ts,
            first_value=first_row['value'], last_value=last_row['value'],
            min_value=min_v, max_value=max_v,
            sum_x=float(sx), sum_y=float(sy),
            sum_xx=float(sxx), sum_xy=float(sxy), sum_yy=float(syy),
            span_seconds=(last_ts - first_ts).total_seconds(),
        )

    @staticmethod
    def _raw_sums(conn: sqlite3.Connection, sid: int, ranges: List[Tuple[int, int]],
                  origin: int) -> tuple:
        """原始样本在若干互不重叠的区间内的 (计数, 最小, 最大, Σx, Σy, Σx², Σxy, Σy²)，x 相对 origin"""
        if not ranges:
            return 0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0
        parts = ' UNION ALL '.join(
            'SELECT value AS v, (ts_ms - ?) / 1000.0 AS x FROM samples '
            'WHERE series_id = ? AND ts_ms BETWEEN ? AND ?' for _ in ranges)
        params = [p for a, b in ranges for p in (origin, sid, a, b)]
        row = conn.execute(f'''
            SELECT COUNT(*), MIN(v), MAX(v), SUM(x), SUM(v), SUM(x * x), SUM(x * v), SUM(v * v)
            FROM ({parts})
        ''', params).fetchone()
        return (row[0], row[1], row[2], row[3] or 0.0, row[4] or 0.0, row[5] or 0.0,
                row[6] or 0.0, row[7] or 0.0)

    def _rollup_sums(self, conn: sqlite3.Connection, sid: int, lo: int, hi: int,
                     origin: int) -> tuple:
        """
        汇总路径的各阶和：完整落在范围内的汇总行直接合并，各阶和平移到以 origin 为原点——
        对桶 d = (bucket_ms - origin) / 1000：Σx = n·d + Σx₀，Σx² = n·d² + 2d·Σx₀ + Σx²₀，
        Σxy = d·Σy + Σxy₀；范围两端不完整的部分从 samples 取。
        """
        full = conn.execute(f'''
            SELECT SUM(count), MIN(first_ms), MAX(last_ms), MIN(min_value), MAX(max_value),
                   SUM(count * d + sum_x), SUM(sum_y),
                   SUM(count * d * d + 2.0 * d * sum_x + sum_xx),
                   SUM(d * sum_y + sum_xy), SUM(sum_yy)
            FROM (SELECT *, (bucket_ms - ?) / 1000.0 AS d FROM rollup_1m WHERE {_FULL_ROWS})
        ''', (origin, *_full_params(sid, lo, hi))).fetchone()
        edges = self._raw_sums(conn, sid, _edge_ranges(lo, hi, full[1], full[2]), origin)
        if full[0] is None:
            return edges
        mins = [v for v in (full[3], edges[1]) if v is not None]
        maxs = [v for v in (full[4], edges[2]) if v is not None]
        return (full[0] + edges[0], min(mins), max(maxs),
                full[5] + edges[3], full[6] + edges[4], full[7] + edges[5],
                full[8] + edges[6], full[9] + edges[7])

    # ========== 空洞与分桶 ==========

    def gaps(self, task_id: str, interval_seconds: float,
             since: Optional[datetime] = None, until: Optional[datetime] = None,
             metric: Optional[str] = None,
             gap_factor: float = DEFAULT_GAP_FACTOR) -> List[Tuple[datetime, datetime]]:
        """
        相邻两个采样时刻间隔 > gap_factor × interval_seconds 的空洞，按时间升序。
        每项是 (空洞前最后一个点的时间, 空洞后第一个点的时间)。
        metric 为 None 时按任务全部指标的不同时间戳计算（同一周期多指标共用时间戳）。

        空洞口径与写入时相同（gap_factor 为默认值、interval 与任务一致）时直接读 series_gaps，
        与范围内样本多少无关：它与按样本逐对比较的结果逐项相同（不变式），且任何范围都更快；
        否则（或 path='raw'）按样本用 LAG 逐对比较。
        """
        threshold = _threshold(interval_seconds, gap_factor)
        lo, hi = ms_range(since, until)
        with self._connect('读取空洞') as conn:
            series = task_series(conn, task_id, metric)
            if not series:
                return []
            sids = [sid for sid, _m in series]
            if self._gap_rule_matches(conn, task_id, interval_seconds, gap_factor):
                pairs = self._gaps_from_table(conn, sids, lo, hi, threshold)
                return [(from_ms(a), from_ms(b)) for a, b in pairs]
            where, params = in_clause(sids)
            distinct = 'DISTINCT ' if len(series) > 1 else ''
            pts_cte = f'''
                pts AS (
                    SELECT {distinct}ts_ms AS ts, ts_ms AS ms
                    FROM samples WHERE {where} AND ts_ms BETWEEN ? AND ?
                )
            '''
            rows = _gap_rows(conn, pts_cte, [*params, lo, hi], threshold)
        return [(from_ms(r['prev']), from_ms(r['ts'])) for r in rows]

    @staticmethod
    def _series_gaps(conn: sqlite3.Connection, sid: int, lo: int, hi: int
                     ) -> List[Tuple[int, int]]:
        """series_gaps 中两端都在 [lo, hi] 内的空洞（即范围内相邻样本构成的空洞）"""
        return [(r[0], r[1]) for r in conn.execute(
            'SELECT start_ms, end_ms FROM series_gaps WHERE series_id = ? '
            'AND start_ms >= ? AND end_ms <= ? ORDER BY start_ms', (sid, lo, hi))]

    def _gaps_from_table(self, conn: sqlite3.Connection, sids: Sequence[int], lo: int, hi: int,
                         threshold: float) -> List[Tuple[int, int]]:
        """
        从 series_gaps 读空洞。多条序列时求"所有序列的不同时间戳"上的空洞：每条序列在范围内
        有样本的部分是 [首, 末] 去掉自己的空洞后的若干闭区间，把各序列的这些区间合并，
        合并结果之间的间隙即合并时间戳上相邻两点的间隔，超过阈值的就是空洞（与按不同时间戳
        计算逐项相同；同一周期各指标共用时间戳时就是各序列空洞的并集）。
        """
        if len(sids) == 1:
            return self._series_gaps(conn, sids[0], lo, hi)
        covered: List[Tuple[int, int]] = []
        for sid in sids:
            first, last = self._bounds(conn, [sid], lo, hi)
            if first is None:
                continue
            start = first
            for a, b in self._series_gaps(conn, sid, lo, hi):
                covered.append((start, a))
                start = b
            covered.append((start, last))
        covered.sort()
        result: List[Tuple[int, int]] = []
        reach = None
        for a, b in covered:
            if reach is not None and a > reach and rollup.is_gap(a - reach, threshold):
                result.append((reach, a))
            reach = b if reach is None else max(reach, b)
        return result

    def buckets(self, task_id: str, metric: str, max_buckets: int,
                since: Optional[datetime] = None, until: Optional[datetime] = None,
                interval_seconds: Optional[float] = None,
                gap_factor: float = DEFAULT_GAP_FACTOR) -> List[Bucket]:
        """
        按时间等宽分桶（GROUP BY 时间桶），且任何桶都不跨空洞（空洞判定同 gaps）。

        做法：先找出空洞（LAG），把序列切成若干连续段；所有段共用一个桶宽 w，
        每段从自己的首点起按 w 切桶（桶号 = floor((t - 段首) / w)），
        段内最多 floor(段跨度 / w) + 1 个桶；w 取使各段桶数之和 ≤ max_buckets 的最小值
        （二分求得）。没有空洞时即把 [first, last] 等分为 max_buckets 段；空洞本身不占桶；
        空桶不返回，故实际桶数可能少于上限。
        段数 ≥ max_buckets 时（空洞极多）每段恰好一桶，此时桶数 = 段数，会超过
        max_buckets——不跨空洞优先于桶数上限。

        interval_seconds 为 None 时取相邻点正间隔的中位数作为采集周期估计
        （中位数不受少数空洞拉大影响）；不足两个不同时刻时视为没有空洞。

        空洞口径与写入时相同时空洞一律读 series_gaps（同 gaps）。汇总路径（口径相同、范围内样本多、
        且桶宽 ≥ 1 分钟时）：整体落在一个桶里的汇总行直接合并，跨桶边界或范围边界的汇总行回到
        samples 取原始样本。
        """
        if not isinstance(max_buckets, int) or isinstance(max_buckets, bool) or max_buckets < 1:
            raise ValueError(f'max_buckets 必须是 >= 1 的整数，实际为 {max_buckets!r}')
        if interval_seconds is not None:
            threshold = _threshold(interval_seconds, gap_factor)
        else:
            _threshold(1.0, gap_factor)   # 只校验 gap_factor
            threshold = None
        lo, hi = ms_range(since, until)

        with self._connect('读取分桶') as conn:
            sid = series_id(conn, task_id, metric)
            if sid is None:
                return []
            t0, t1 = self._bounds(conn, [sid], lo, hi)
            if t0 is None:
                return []
            rows = None
            gap_pairs = None
            if self._gap_rule_matches(conn, task_id, interval_seconds, gap_factor):
                gap_pairs = self._series_gaps(conn, sid, lo, hi)
            if gap_pairs is not None and self._use_rollup(conn, [sid], lo, hi):
                starts = [0.0] + [b - t0 for _a, b in gap_pairs]
                ends = [a - t0 for a, _b in gap_pairs] + [t1 - t0]
                width_ms = _bucket_width([e - s for s, e in zip(starts, ends)], max_buckets)
                if width_ms >= MINUTE_MS:
                    rows = self._rollup_buckets(conn, sid, lo, hi, t0, width_ms)
            if rows is None:
                rows = self._raw_buckets(conn, sid, lo, hi, t0, t1, threshold,
                                         gap_factor, max_buckets, gap_pairs)

        # 各桶时间上互不重叠、段按时间先后排列，按 start_ts 排序即 (段, 桶号) 的顺序
        return [
            Bucket(start=from_ms(start_ts), end=from_ms(end_ts), min=min_v, max=max_v,
                   avg=avg_v, last=last_v, count=cnt)
            for start_ts, end_ts, min_v, max_v, avg_v, cnt, last_v in sorted(rows)
        ]

    @staticmethod
    def _raw_buckets(conn: sqlite3.Connection, sid: int, lo: int, hi: int, t0: int, t1: int,
                     threshold: Optional[float], gap_factor: float, max_buckets: int,
                     gap_pairs: Optional[List[Tuple[int, int]]] = None) -> list:
        """按样本分桶；gap_pairs 给定（读自 series_gaps）时不再扫样本找空洞"""
        rng = (sid, lo, hi)
        anchor = t0
        last_ms = t1 - anchor
        # ts 为绝对毫秒（取首末与回查末值用），ms 为相对首点的毫秒（分桶用）
        pts_cte = '''
            pts AS (
                SELECT ts_ms AS ts, value AS v, ts_ms - ? AS ms
                FROM samples WHERE series_id = ? AND ts_ms BETWEEN ? AND ?
            )
        '''
        pts_params = [anchor, *rng]

        if gap_pairs is not None:
            need_scan = False
        elif threshold is None:
            # 正间隔的直方图通常只有少数几种取值，中位数在 Python 里按权重求，
            # 同时可知最大间隔，超过阈值才需要再扫一遍定位空洞
            hist = [(r['dms'], r['cnt']) for r in conn.execute(f'''
                WITH {pts_cte},
                d AS (SELECT ms - LAG(ms) OVER (ORDER BY ts) AS dms FROM pts)
                SELECT dms, COUNT(*) AS cnt FROM d WHERE dms > 0
                GROUP BY dms ORDER BY dms
            ''', pts_params).fetchall()]
            if hist:
                threshold = _weighted_median(hist) / 1000.0 * gap_factor
                need_scan = hist[-1][0] / 1000.0 > threshold
            else:
                need_scan = False
        else:
            need_scan = True
        if gap_pairs is not None:
            gap_rows = [{'prev': a, 'pms': a - anchor, 'ts': b, 'ms': b - anchor}
                        for a, b in gap_pairs]
        else:
            gap_rows = (_gap_rows(conn, pts_cte, pts_params, threshold)
                        if need_scan else [])

        starts = [0.0] + [r['ms'] for r in gap_rows]
        ends = [r['pms'] for r in gap_rows] + [last_ms]
        width_ms = _bucket_width([e - s for s, e in zip(starts, ends)], max_buckets)

        if gap_rows:
            # 段首 = 至今遇到的最后一个"空洞后首点"的 ms（单个窗口函数，按主键顺序扫描）
            s0_expr = '''COALESCE(MAX(CASE WHEN ts IN (SELECT value FROM json_each(?))
                                           THEN ms END)
                                  OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING), 0.0)'''
            s0_params = [json.dumps([r['ts'] for r in gap_rows])]
        else:
            s0_expr, s0_params = '0.0', []
        # 每桶的 last 用 end_ts 回主键取值（桶数有限），避免再做一次窗口排序
        return fetch_packed(conn, f'''
            WITH {pts_cte},
            seg AS (SELECT ts, v, ms, {s0_expr} AS s0 FROM pts),
            agg AS (
                SELECT s0, CAST((ms - s0) / ? AS INTEGER) AS b,
                       MIN(ts) AS start_ts, MAX(ts) AS end_ts,
                       MIN(v) AS min_v, MAX(v) AS max_v, AVG(v) AS avg_v,
                       COUNT(*) AS cnt
                FROM seg
                GROUP BY s0, b
            )
            SELECT start_ts AS c0, end_ts AS c1, min_v AS c2, max_v AS c3, avg_v AS c4,
                   cnt AS c5,
                   (SELECT value FROM samples
                    WHERE series_id = ? AND ts_ms = agg.end_ts) AS c6
            FROM agg
        ''', [*pts_params, *s0_params, width_ms, sid], 'iifffif')

    @staticmethod
    def _rollup_buckets(conn: sqlite3.Connection, sid: int, lo: int, hi: int, t0: int,
                        width_ms: float) -> list:
        """
        汇总路径的 min/max 带：每个汇总行的段首（相对 t0）= 最后一个起点早于其首样本的空洞的
        end_ms，不早于 t0；桶号与原始路径同一算式（整数毫秒差除以 w 后截断）。整体落在一个桶里
        且在范围内的汇总行直接参与合并，其余行（跨桶边界或范围边界）展开为原始样本。
        """
        # r 物化：段首是逐行的标量子查询，不物化会在 c、u 里每次引用都重算一遍
        return fetch_packed(conn, '''
            WITH r AS MATERIALIZED (
                SELECT r.count AS cnt, r.first_ms, r.last_ms, r.min_value, r.max_value, r.sum_y,
                       MAX(?3, COALESCE(
                           (SELECT g.end_ms FROM series_gaps g
                            WHERE g.series_id = ?1 AND g.start_ms < r.first_ms
                            ORDER BY g.start_ms DESC LIMIT 1), ?3)) - ?3 AS s0
                FROM rollup_1m r
                WHERE r.series_id = ?1 AND r.bucket_ms > ?4 - 60000 AND r.bucket_ms <= ?5
            ),
            c AS (
                SELECT *, CAST(((first_ms - ?3) - s0) / ?2 AS INTEGER) AS b1,
                          CAST(((last_ms - ?3) - s0) / ?2 AS INTEGER) AS b2,
                          (first_ms >= ?4 AND last_ms <= ?5) AS inside
                FROM r
            ),
            u AS (
                SELECT s0, b1 AS b, cnt, first_ms AS f, last_ms AS l,
                       min_value AS mn, max_value AS mx, sum_y AS sy
                FROM c WHERE inside AND b1 = b2
                UNION ALL
                SELECT c.s0, CAST(((s.ts_ms - ?3) - c.s0) / ?2 AS INTEGER), 1,
                       s.ts_ms, s.ts_ms, s.value, s.value, s.value
                FROM c CROSS JOIN samples s
                WHERE NOT (c.inside AND c.b1 = c.b2) AND s.series_id = ?1
                  AND s.ts_ms BETWEEN MAX(c.first_ms, ?4) AND MIN(c.last_ms, ?5)
            ),
            agg AS (
                SELECT s0, b, MIN(f) AS start_ts, MAX(l) AS end_ts, MIN(mn) AS min_v,
                       MAX(mx) AS max_v, SUM(sy) AS sy, SUM(cnt) AS cnt
                FROM u GROUP BY s0, b
            )
            SELECT start_ts AS c0, end_ts AS c1, min_v AS c2, max_v AS c3, sy / cnt AS c4,
                   cnt AS c5,
                   (SELECT value FROM samples
                    WHERE series_id = ?1 AND ts_ms = agg.end_ts) AS c6
            FROM agg
        ''', (sid, width_ms, t0, lo, hi), 'iifffif')

    # ========== 基线取值 ==========

    def value_near(self, task_id: str, metric: str, ts: datetime, n: int = 5,
                   after_only: bool = False) -> Optional[float]:
        """
        时间上最接近 ts 的 n 个点的中位数（n 为偶数时取中间两值平均），用作基线等参考值。
        距离相同时较早的点优先；点数不足 n 时用全部可用点；没有可用点返回 None。
        after_only=True 时只取时间 >= ts 的点（"基线值取其后 n 个样本中位数"）。
        语义与 core.analysis.intervals.value_near 一致，只是直接走主键取候选点。
        """
        if ts is None:
            raise TypeError('ts 不能为 None')
        check_time(ts, 'ts')
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError(f'n 必须是 >= 1 的整数，实际为 {n!r}')
        # ts 是时刻类边界，与样本同一取整规则（见 data.mappers 模块说明）：取整后不早于它的
        # 样本算 ts 及之后，其余在 ts 之前
        pivot = to_ms(ts)
        with self._connect('读取基线值') as conn:
            sid = series_id(conn, task_id, metric)
            if sid is None:
                return None
            after = conn.execute(
                'SELECT ts_ms, value FROM samples WHERE series_id = ? AND ts_ms >= ? '
                'ORDER BY ts_ms ASC LIMIT ?', (sid, pivot, n)).fetchall()
            before = [] if after_only else conn.execute(
                'SELECT ts_ms, value FROM samples WHERE series_id = ? AND ts_ms < ? '
                'ORDER BY ts_ms DESC LIMIT ?', (sid, pivot, n)).fetchall()
        candidates = sorted(
            ((from_ms(r['ts_ms']), r['value']) for r in [*before, *after]),
            key=lambda p: p[0])
        if not candidates:
            return None
        nearest = sorted(candidates, key=lambda p: abs((p[0] - ts).total_seconds()))[:n]
        return float(statistics.median(v for _, v in nearest))

    # ========== 计数与锚点 ==========

    def point_count(self, task_id: str, metric: Optional[str] = None,
                    since: Optional[datetime] = None,
                    until: Optional[datetime] = None) -> int:
        """[since, until] 内的数据点行数（两端不限时为全部）；metric 为 None 时统计任务全部指标"""
        lo, hi = ms_range(since, until)
        with self._connect('读取数据点数量') as conn:
            series = task_series(conn, task_id, metric)
            if not series:
                return 0
            sids = [sid for sid, _m in series]
            if self._use_rollup(conn, sids, lo, hi):
                return sum(self._rollup_count(conn, sid, lo, hi) for sid in sids)
            where, params = in_clause(sids)
            row = conn.execute(
                f'SELECT COUNT(*) AS cnt FROM samples WHERE {where} AND ts_ms BETWEEN ? AND ?',
                [*params, lo, hi]).fetchone()
        return row['cnt'] if row else 0

    def sample_count(self, task_id: str) -> int:
        """
        采集次数（同一时间戳的多指标数据点算一次），读任务行上写入时累加的 sample_count；
        库仍停留在旧结构（迁移失败、tasks 缺该列）时回退为按不同时间戳计数
        （有 samples 表按样本，否则按旧 data_points）。任务不存在返回 0。
        """
        with self._connect('读取采集次数') as conn:
            try:
                row = conn.execute(
                    'SELECT sample_count FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
                return (row['sample_count'] or 0) if row else 0
            except sqlite3.OperationalError as e:
                if 'no such column' not in str(e):
                    raise
                if _has_table(conn, 'samples'):
                    row = conn.execute('''
                        SELECT COUNT(DISTINCT ts_ms) AS cnt FROM samples
                        WHERE series_id IN (SELECT series_id FROM series WHERE task_id = ?)
                    ''', (task_id,)).fetchone()
                else:
                    row = conn.execute(
                        'SELECT COUNT(DISTINCT timestamp) AS cnt FROM data_points '
                        'WHERE task_id = ?', (task_id,)).fetchone()
                return row['cnt'] if row else 0

    def last_point_timestamp(self, task_id: str) -> Optional[datetime]:
        """
        任务全部指标里最新数据点的时间（分析页时间范围的锚点）；无数据点返回 None。
        逐序列取主键末端再求最大，不扫样本。
        """
        with self._connect('读取最新数据点时间') as conn:
            row = conn.execute('''
                SELECT MAX((SELECT MAX(ts_ms) FROM samples s
                            WHERE s.series_id = se.series_id)) AS latest
                FROM series se WHERE se.task_id = ?
            ''', (task_id,)).fetchone()
        if not row or row['latest'] is None:
            return None
        return from_ms(row['latest'])

    # ========== 旧接口等价实现（供 Database 门面原样委托） ==========

    def legacy_stats(self, task_id: str, metric: str,
                     since: Optional[datetime] = None) -> Optional[dict]:
        """
        与 Database.get_metric_stats 结构、数值一致的 dict；范围内无数据返回 None。

        字段对应（左为 dict 键，右为 MetricAggregates 字段）：
            count → count            min → min_value         max → max_value
            avg → sum_y / count      first → first_value     first_ts → first_ts
            last → last_value        last_ts → last_ts
            slope_per_hour → 3600 × 最小二乘斜率（按秒）；count < 2、跨度为 0
                             或 x 无方差时为 None（同旧实现的判定）
        与旧实现的差别只在出错时：旧实现吞异常返回 None，这里抛 StorageError。
        """
        agg = self.aggregates(task_id, metric, since=since)
        if agg.count == 0:
            return None
        n = agg.count
        slope = None
        if n >= 2 and agg.last_ts > agg.first_ts:
            denominator = n * agg.sum_xx - agg.sum_x ** 2
            if denominator > 0:
                slope = (n * agg.sum_xy - agg.sum_x * agg.sum_y) / denominator * 3600.0
        return {
            'count': n,
            'min': agg.min_value,
            'max': agg.max_value,
            'avg': agg.sum_y / n,
            'first': agg.first_value,
            'first_ts': agg.first_ts,
            'last': agg.last_value,
            'last_ts': agg.last_ts,
            'slope_per_hour': slope,
        }

    def legacy_buckets(self, task_id: str, metric: Optional[str], max_points: int,
                       since: Optional[datetime] = None) -> List[DataPoint]:
        """
        图表用的降采样（Database.get_task_data_points_bucketed 的实现）：返回真实数据点，
        按时间升序（同一时刻按值升序），去掉 (时间, 值) 完全相同的重复点。

        2.0 起按时间等分（1.x 为按行号等分）：范围内点数不超过 2 × max_points 时原样返回全部点
        （分桶取最值也最多返回这么多点，不如直接给全）；否则把 [首点, 末点] 按时间分成不超过
        max_points 个等宽桶（时长够长时桶宽取整分钟并对齐自然分钟，见 _chart_buckets），
        每桶取最小值点与最大值点（并列时取较早的），故最多返回 2 × max_points 个点。
        metric 为 None 时对任务全部指标一起分桶（同一时刻多指标各是一个点）。
        """
        if not isinstance(max_points, int) or isinstance(max_points, bool) or max_points < 1:
            raise ValueError(f'max_points 必须是 >= 1 的整数，实际为 {max_points!r}')
        lo, hi = ms_range(since, None)
        with self._connect('读取分桶数据点') as conn:
            series = task_series(conn, task_id, metric)
            if not series:
                return []
            sids = [sid for sid, _m in series]
            t0, t1 = self._bounds(conn, sids, lo, hi)
            if t0 is None:
                return []
            use_rollup = self._use_rollup(conn, sids, lo, hi)
            total = (sum(self._rollup_count(conn, sid, lo, hi) for sid in sids) if use_rollup
                     else conn.execute(
                         f'SELECT COUNT(*) FROM samples WHERE series_id IN ({_sid_list(sids)}) '
                         f'AND ts_ms BETWEEN ? AND ?', (lo, hi)).fetchone()[0])
            if total <= 2 * max_points:
                pairs = sorted(fetch_packed(
                    conn, f'SELECT DISTINCT ts_ms AS c0, value AS c1 FROM samples '
                          f'WHERE series_id IN ({_sid_list(sids)}) AND ts_ms BETWEEN ? AND ?',
                    (lo, hi), 'if'))
            else:
                edges = _chart_buckets(t0, t1, lo, hi, max_points)
                if use_rollup and self._edges_minute_aligned(t0, t1, max_points):
                    picks = self._rollup_extremes(conn, sids, lo, hi, edges)
                else:
                    picks = self._raw_extremes(conn, sids, edges)
                pairs = sorted({p for pick in picks for p in pick})
        return [
            DataPoint(task_id=task_id, timestamp=from_ms(ms), value=value,
                      metric_type=metric or '')
            for ms, value in pairs
        ]

    @staticmethod
    def _edges_minute_aligned(t0: int, t1: int, max_points: int) -> bool:
        extent = rollup.minute_floor(t1) + MINUTE_MS - rollup.minute_floor(t0)
        return extent >= max_points * MINUTE_MS

    @staticmethod
    def _raw_extremes(conn: sqlite3.Connection, sids: Sequence[int],
                      edges: List[Tuple[int, int]]) -> List[Tuple[Tuple[int, float], ...]]:
        """
        每个时间桶的 ((最小值时间, 最小值), (最大值时间, 最大值))，并列取较早的；空桶不返回。
        每桶按主键范围各扫两遍（先求最值、再找最值最早出现的时间），不排序。mm 必须物化：
        否则 mm.mn 会被展开成标量子查询、在内层逐行重算，退化为平方复杂度。
        """
        where = f'series_id IN ({_sid_list(sids)})'
        rows = fetch_packed(conn, f'''
            WITH bk AS (
                SELECT json_extract(value, '$[0]') AS a, json_extract(value, '$[1]') AS b
                FROM json_each(?)
            ),
            mm AS MATERIALIZED (
                SELECT a, b,
                       (SELECT MIN(value) FROM samples WHERE {where}
                        AND ts_ms BETWEEN bk.a AND bk.b) AS mn,
                       (SELECT MAX(value) FROM samples WHERE {where}
                        AND ts_ms BETWEEN bk.a AND bk.b) AS mx
                FROM bk
            )
            SELECT (SELECT MIN(ts_ms) FROM samples WHERE {where}
                    AND ts_ms BETWEEN mm.a AND mm.b AND value = mm.mn) AS c0, mn AS c1,
                   (SELECT MIN(ts_ms) FROM samples WHERE {where}
                    AND ts_ms BETWEEN mm.a AND mm.b AND value = mm.mx) AS c2, mx AS c3
            FROM mm WHERE mn IS NOT NULL
        ''', (json.dumps(edges),), 'ifif')
        return [((r[0], r[1]), (r[2], r[3])) for r in rows]

    def _rollup_extremes(self, conn: sqlite3.Connection, sids: Sequence[int], lo: int, hi: int,
                         edges: List[Tuple[int, int]]
                         ) -> List[Tuple[Tuple[int, float], ...]]:
        """
        _raw_extremes 的汇总路径（桶边界对齐自然分钟时）：桶内完整落在范围里的汇总行按
        min_value/min_ms、max_value/max_ms 取最值点；范围两端不完整的汇总行取原始样本，
        在 Python 里并入所属的桶。
        """
        where = f'series_id IN ({_sid_list(sids)})'
        # 桶 [a, b] 被裁到 lo 时 a 可能落在分钟中间，而同一分钟的汇总行 bucket_ms 可能早于 a，
        # 按 a 所在分钟的起点查（该分钟里早于 lo 的行由 first_ms >= lo 排除）
        cond = (f'{where} AND bucket_ms BETWEEN bk.f AND bk.b '
                f'AND first_ms >= ?2 AND last_ms <= ?3')
        rows = fetch_packed(conn, f'''
            WITH bk AS (
                SELECT json_extract(value, '$[0]') AS a, json_extract(value, '$[1]') AS f,
                       json_extract(value, '$[2]') AS b
                FROM json_each(?1)
            ),
            mm AS MATERIALIZED (
                SELECT a, f, b,
                       (SELECT MIN(min_value) FROM rollup_1m WHERE {cond}) AS mn,
                       (SELECT MAX(max_value) FROM rollup_1m WHERE {cond}) AS mx
                FROM bk
            )
            SELECT a AS c0,
                   (SELECT MIN(min_ms) FROM rollup_1m WHERE {cond.replace('bk.', 'mm.')}
                    AND min_value = mm.mn) AS c1, mn AS c2,
                   (SELECT MIN(max_ms) FROM rollup_1m WHERE {cond.replace('bk.', 'mm.')}
                    AND max_value = mm.mx) AS c3, mx AS c4
            FROM mm WHERE mn IS NOT NULL
        ''', (json.dumps([(a, rollup.minute_floor(a), b) for a, b in edges]), lo, hi), 'iifif')
        best: Dict[int, List] = {r[0]: [r[2], r[1], r[4], r[3]] for r in rows}
        starts = [a for a, _b in edges]
        for sid in sids:
            full = conn.execute(f'SELECT MIN(first_ms), MAX(last_ms) FROM rollup_1m '
                                f'WHERE {_FULL_ROWS}', _full_params(sid, lo, hi)).fetchone()
            for a, b in _edge_ranges(lo, hi, full[0], full[1]):
                for ts, value in conn.execute(
                        'SELECT ts_ms, value FROM samples WHERE series_id = ? '
                        'AND ts_ms BETWEEN ? AND ?', (sid, a, b)):
                    key = _bucket_start(starts, ts)
                    cur = best.get(key)
                    if cur is None:
                        best[key] = [value, ts, value, ts]
                        continue
                    if value < cur[0] or (value == cur[0] and ts < cur[1]):
                        cur[0], cur[1] = value, ts
                    if value > cur[2] or (value == cur[2] and ts < cur[3]):
                        cur[2], cur[3] = value, ts
        return [((v[1], v[0]), (v[3], v[2])) for _k, v in sorted(best.items())]


def _bucket_start(starts: List[int], ts: int) -> int:
    """ts 所在桶的起点（starts 升序，ts 不早于第一个起点）"""
    return starts[bisect.bisect_right(starts, ts) - 1]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None
