# 冻结副本：v4（提交 7c7464b）的 data/repos/analytics_repo.py，供 tests/test_storage_v5.py 做 v4/v5 结果对照。
# 不随当前代码演进修改。
"""
分析仓储：聚合、按时间分桶、空洞检测、基线取值等只读查询

时间参数一律是 datetime，内部用 isoformat() 转成与写入端（PointRepo.append）
同格式的 ISO 字符串再与 data_points.timestamp（TEXT）比较；传 float/int 直接 TypeError，
从根上杜绝"epoch float 与 ISO 串做字典序比较、过滤静默失效"。

所有 sqlite3 异常包成 StorageError 向上抛（原始异常挂在 __cause__），是否降级由调用方决定。

相对时间 x 的取法：SQLite 内部以整数毫秒保存儒略日，julianday() 返回的双精度值
（约 2.46e6）再相减会带入约 4e-5 秒的舍入噪声，这里乘 86400000 后 ROUND 回整数毫秒，
得到精确到毫秒的相对时间，空洞判定与分桶边界因此是确定的。
"""
import json
import math
import sqlite3
import statistics
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional, Tuple

from data.models import DataPoint
from data.repos._guard import storage_errors

# 空洞判定倍数：相邻采样间隔 > 2.5 × 采集周期视为空洞（架构重构方案 §6.3 的统一口径）
DEFAULT_GAP_FACTOR = 2.5

# 相对锚点（绑定参数 ?）的毫秒数，整数值的 REAL
_MS_EXPR = 'ROUND((julianday(timestamp) - julianday(?)) * 86400000.0)'
# 只需要毫秒差值、不需要以首点为原点时用的固定锚点（省一次取首点的查询）
_MS_ANCHOR = '2000-01-01T00:00:00'


def _threshold(interval_seconds: float, gap_factor: float) -> float:
    """空洞阈值（秒）= gap_factor × interval_seconds，两者都须 > 0"""
    if interval_seconds is None or interval_seconds <= 0:
        raise ValueError(f'interval_seconds 必须 > 0，实际为 {interval_seconds!r}')
    if gap_factor <= 0:
        raise ValueError(f'gap_factor 必须 > 0，实际为 {gap_factor!r}')
    return float(interval_seconds) * float(gap_factor)


def _gap_rows(conn: sqlite3.Connection, pts_cte: str, pts_params: list,
              threshold: float) -> list:
    """pts(ts, ms) 中相邻间隔 > threshold 秒的位置：prev/pms 为空洞前一点，ts/ms 为空洞后一点"""
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


def _iso(value: Optional[datetime], name: str) -> Optional[str]:
    """datetime → 与写入端一致的 ISO 串；None 原样返回；其余类型 TypeError"""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f'{name} 必须是 datetime 或 None，实际为 {type(value).__name__}')
    return value.isoformat()


def _where(task_id: str, metric: Optional[str], since: Optional[str],
           until: Optional[str]) -> Tuple[str, list]:
    where = 'task_id = ?'
    params: list = [task_id]
    if metric is not None:
        where += ' AND metric_type = ?'
        params.append(metric)
    if since is not None:
        where += ' AND timestamp >= ?'
        params.append(since)
    if until is not None:
        where += ' AND timestamp <= ?'
        params.append(until)
    return where, params


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


class AnalyticsRepo:
    """只读分析查询。factory 需提供 connect() 上下文管理器（row_factory = sqlite3.Row）。"""

    def __init__(self, factory):
        self._factory = factory

    @contextmanager
    def _connect(self, what: str) -> Iterator[sqlite3.Connection]:
        """连接上下文；块内 sqlite3.Error 经 storage_errors 统一改抛 StorageError"""
        with storage_errors(what):
            with self._factory.connect() as conn:
                yield conn

    # ========== 聚合 ==========

    def aggregates(self, task_id: str, metric: str,
                   since: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> MetricAggregates:
        """[since, until]（两端含等于）内的点数、首末点、极值与拟合所需各阶和"""
        where, params = _where(task_id, metric, _iso(since, 'since'), _iso(until, 'until'))
        with self._connect('读取指标聚合') as conn:
            first_row = conn.execute(
                f'SELECT timestamp, value FROM data_points WHERE {where} '
                f'ORDER BY timestamp ASC, id ASC LIMIT 1', params).fetchone()
            if first_row is None:
                return MetricAggregates(
                    task_id=task_id, metric=metric, count=0,
                    first_ts=None, last_ts=None, first_value=None, last_value=None,
                    min_value=None, max_value=None,
                    sum_x=0.0, sum_y=0.0, sum_xx=0.0, sum_xy=0.0, sum_yy=0.0,
                    span_seconds=0.0)
            last_row = conn.execute(
                f'SELECT timestamp, value FROM data_points WHERE {where} '
                f'ORDER BY timestamp DESC, id DESC LIMIT 1', params).fetchone()
            agg = conn.execute(f'''
                SELECT COUNT(*) AS cnt, MIN(v) AS min_v, MAX(v) AS max_v,
                       SUM(x) AS sx, SUM(v) AS sy, SUM(x * x) AS sxx,
                       SUM(x * v) AS sxy, SUM(v * v) AS syy
                FROM (
                    SELECT value AS v, {_MS_EXPR} / 1000.0 AS x
                    FROM data_points WHERE {where}
                )
            ''', [first_row['timestamp'], *params]).fetchone()

        first_ts = datetime.fromisoformat(first_row['timestamp'])
        last_ts = datetime.fromisoformat(last_row['timestamp'])
        return MetricAggregates(
            task_id=task_id, metric=metric, count=agg['cnt'],
            first_ts=first_ts, last_ts=last_ts,
            first_value=first_row['value'], last_value=last_row['value'],
            min_value=agg['min_v'], max_value=agg['max_v'],
            sum_x=float(agg['sx']), sum_y=float(agg['sy']),
            sum_xx=float(agg['sxx']), sum_xy=float(agg['sxy']), sum_yy=float(agg['syy']),
            span_seconds=(last_ts - first_ts).total_seconds(),
        )

    # ========== 空洞与分桶 ==========

    def gaps(self, task_id: str, interval_seconds: float,
             since: Optional[datetime] = None, until: Optional[datetime] = None,
             metric: Optional[str] = None,
             gap_factor: float = DEFAULT_GAP_FACTOR) -> List[Tuple[datetime, datetime]]:
        """
        相邻两个采样时刻间隔 > gap_factor × interval_seconds 的空洞，按时间升序。
        每项是 (空洞前最后一个点的时间, 空洞后第一个点的时间)。
        metric 为 None 时按任务全部指标的不同时间戳计算（同一周期多指标共用时间戳）。
        """
        threshold = _threshold(interval_seconds, gap_factor)
        where, params = _where(task_id, metric, _iso(since, 'since'), _iso(until, 'until'))
        distinct = 'DISTINCT ' if metric is None else ''
        pts_cte = f'''
            pts AS (
                SELECT {distinct}timestamp AS ts, {_MS_EXPR} AS ms
                FROM data_points WHERE {where}
            )
        '''
        with self._connect('读取空洞') as conn:
            rows = _gap_rows(conn, pts_cte, [_MS_ANCHOR, *params], threshold)
        return [(datetime.fromisoformat(r['prev']), datetime.fromisoformat(r['ts']))
                for r in rows]

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
        """
        if not isinstance(max_buckets, int) or isinstance(max_buckets, bool) or max_buckets < 1:
            raise ValueError(f'max_buckets 必须是 >= 1 的整数，实际为 {max_buckets!r}')
        if interval_seconds is not None:
            threshold = _threshold(interval_seconds, gap_factor)
        else:
            _threshold(1.0, gap_factor)   # 只校验 gap_factor
        where, params = _where(task_id, metric, _iso(since, 'since'), _iso(until, 'until'))

        with self._connect('读取分桶') as conn:
            # MIN 与 MAX 分成两个子查询，各自走索引一端，不必扫描整个范围
            bounds = conn.execute(f'''
                SELECT (SELECT MIN(timestamp) FROM data_points WHERE {where}) AS t0,
                       (SELECT MAX(timestamp) FROM data_points WHERE {where}) AS t1
            ''', [*params, *params]).fetchone()
            if bounds is None or bounds['t0'] is None:
                return []
            anchor = bounds['t0']
            last_ms = conn.execute(
                'SELECT ROUND((julianday(?) - julianday(?)) * 86400000.0) AS ms',
                (bounds['t1'], anchor)).fetchone()['ms']
            pts_cte = f'''
                pts AS (
                    SELECT timestamp AS ts, value AS v, {_MS_EXPR} AS ms
                    FROM data_points WHERE {where}
                )
            '''
            pts_params = [anchor, *params]

            if interval_seconds is None:
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
            gap_rows = (_gap_rows(conn, pts_cte, pts_params, threshold)
                        if need_scan else [])

            starts = [0.0] + [r['ms'] for r in gap_rows]
            ends = [r['pms'] for r in gap_rows] + [last_ms]
            width_ms = _bucket_width([e - s for s, e in zip(starts, ends)], max_buckets)

            if gap_rows:
                # 段首 = 至今遇到的最后一个"空洞后首点"的 ms（单个窗口函数，按索引顺序扫描）
                s0_expr = '''COALESCE(MAX(CASE WHEN ts IN (SELECT value FROM json_each(?))
                                               THEN ms END)
                                      OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING), 0.0)'''
                s0_params = [json.dumps([r['ts'] for r in gap_rows])]
            else:
                s0_expr, s0_params = '0.0', []
            # 每桶的 last 用 end_ts 回索引取值（桶数有限），避免再做一次窗口排序
            rows = conn.execute(f'''
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
                SELECT start_ts, end_ts, min_v, max_v, avg_v, cnt,
                       (SELECT value FROM data_points
                        WHERE task_id = ? AND metric_type = ? AND timestamp = agg.end_ts
                        ORDER BY id DESC LIMIT 1) AS last_v
                FROM agg
                ORDER BY s0, b
            ''', [*pts_params, *s0_params, width_ms, task_id, metric]).fetchall()

        return [
            Bucket(start=datetime.fromisoformat(r['start_ts']),
                   end=datetime.fromisoformat(r['end_ts']),
                   min=r['min_v'], max=r['max_v'], avg=r['avg_v'],
                   last=r['last_v'], count=r['cnt'])
            for r in rows
        ]

    # ========== 基线取值 ==========

    def value_near(self, task_id: str, metric: str, ts: datetime, n: int = 5,
                   after_only: bool = False) -> Optional[float]:
        """
        时间上最接近 ts 的 n 个点的中位数（n 为偶数时取中间两值平均），用作基线等参考值。
        距离相同时较早的点优先；点数不足 n 时用全部可用点；没有可用点返回 None。
        after_only=True 时只取 timestamp >= ts 的点（"基线值取其后 n 个样本中位数"）。
        语义与 core.analysis.intervals.value_near 一致，只是直接走索引取候选点。
        """
        ts_iso = _iso(ts, 'ts')
        if ts_iso is None:
            raise TypeError('ts 不能为 None')
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError(f'n 必须是 >= 1 的整数，实际为 {n!r}')
        base = 'SELECT timestamp, value FROM data_points WHERE task_id = ? AND metric_type = ?'
        with self._connect('读取基线值') as conn:
            after = conn.execute(
                f'{base} AND timestamp >= ? ORDER BY timestamp ASC, id ASC LIMIT ?',
                (task_id, metric, ts_iso, n)).fetchall()
            before = [] if after_only else conn.execute(
                f'{base} AND timestamp < ? ORDER BY timestamp DESC, id DESC LIMIT ?',
                (task_id, metric, ts_iso, n)).fetchall()
        candidates = sorted(
            ((datetime.fromisoformat(r['timestamp']), r['value']) for r in [*before, *after]),
            key=lambda p: p[0])
        if not candidates:
            return None
        nearest = sorted(candidates, key=lambda p: abs((p[0] - ts).total_seconds()))[:n]
        return float(statistics.median(v for _, v in nearest))

    # ========== 计数与锚点 ==========

    def point_count(self, task_id: str, metric: Optional[str] = None) -> int:
        """数据点行数；metric 为 None 时统计任务全部指标"""
        where, params = _where(task_id, metric, None, None)
        with self._connect('读取数据点数量') as conn:
            row = conn.execute(
                f'SELECT COUNT(*) AS cnt FROM data_points WHERE {where}', params).fetchone()
        return row['cnt'] if row else 0

    def sample_count(self, task_id: str) -> int:
        """
        采集次数（同一时间戳的多指标数据点算一次），读任务行上写入时累加的 sample_count；
        旧库 tasks 缺该列时回退为按不同时间戳计数。任务不存在返回 0。
        """
        with self._connect('读取采集次数') as conn:
            try:
                row = conn.execute(
                    'SELECT sample_count FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
                return (row['sample_count'] or 0) if row else 0
            except sqlite3.OperationalError as e:
                if 'no such column' not in str(e):
                    raise
                row = conn.execute(
                    'SELECT COUNT(DISTINCT timestamp) AS cnt FROM data_points '
                    'WHERE task_id = ?', (task_id,)).fetchone()
                return row['cnt'] if row else 0

    def last_point_timestamp(self, task_id: str) -> Optional[datetime]:
        """任务全部指标里最新数据点的时间（分析页时间范围的锚点）；无数据点返回 None"""
        with self._connect('读取最新数据点时间') as conn:
            row = conn.execute(
                'SELECT MAX(timestamp) AS latest FROM data_points WHERE task_id = ?',
                (task_id,)).fetchone()
        if not row or row['latest'] is None:
            return None
        return datetime.fromisoformat(row['latest'])

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
        与 Database.get_task_data_points_bucketed 语义一致的降采样：按行号等分为
        max_points 个桶（即旧接口的 max_buckets），每桶取最小值行与最大值行，
        合并去重后按时间升序输出，故最多返回 2 × max_points 个点。
        metric 为 None 时对任务全部指标一起分桶（同旧接口）。
        """
        if not isinstance(max_points, int) or isinstance(max_points, bool) or max_points < 1:
            raise ValueError(f'max_points 必须是 >= 1 的整数，实际为 {max_points!r}')
        where, params = _where(task_id, metric, _iso(since, 'since'), None)
        with self._connect('读取分桶数据点') as conn:
            rows = conn.execute(f'''
                WITH numbered AS (
                    SELECT id, timestamp, value,
                           ROW_NUMBER() OVER (ORDER BY timestamp ASC, id ASC) - 1 AS rn,
                           COUNT(*) OVER () AS total
                    FROM data_points
                    WHERE {where}
                ),
                bucketed AS (
                    SELECT id, timestamp, value,
                           CASE WHEN total <= ? THEN rn
                                ELSE MIN((rn * ?) / total, ? - 1)
                           END AS bucket
                    FROM numbered
                ),
                mins AS (
                    SELECT bucket, timestamp, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY bucket ORDER BY value ASC, timestamp ASC
                           ) AS rk
                    FROM bucketed
                ),
                maxs AS (
                    SELECT bucket, timestamp, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY bucket ORDER BY value DESC, timestamp ASC
                           ) AS rk
                    FROM bucketed
                )
                SELECT timestamp, value FROM mins WHERE rk = 1
                UNION
                SELECT timestamp, value FROM maxs WHERE rk = 1
                ORDER BY timestamp ASC
            ''', (*params, max_points, max_points, max_points)).fetchall()
        return [
            DataPoint(task_id=task_id, timestamp=datetime.fromisoformat(r['timestamp']),
                      value=r['value'], metric_type=metric or '')
            for r in rows
        ]
