"""
AnalyticsRepo 用例：聚合、按时间分桶（不跨空洞）、空洞、基线取值、计数，
以及 legacy_* 与拆分前 Database 实现（本文件内冻结的旧 SQL）逐字段一致。

用现有 Database 建库造数据，再用本文件内的 _Factory 桩（与 Database 两个连接
上下文管理器同实现）驱动仓储。
"""
import math
import random
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from data.repos.analytics_repo import (
    DEFAULT_GAP_FACTOR, AnalyticsRepo, Bucket, MetricAggregates)

T0 = datetime(2026, 3, 1, 10, 0, 0)
MEM = 'memory_rss'
CPU = 'cpu_percent'


class _Factory:
    """ConnectionFactory 契约的测试桩"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn.cursor()
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
        finally:
            conn.close()


@pytest.fixture
def repo(db):
    return AnalyticsRepo(_Factory(db.db_path))


def _seed(db, series, task_id='t1', interval=1.0):
    """series: {metric: [(datetime, value), ...]}；建任务并写入数据点"""
    db.save_task(MonitorTask(
        task_id=task_id, pid=1, process_name='p.exe', metric_types=list(series),
        interval=interval, start_time=T0, end_time=None, status='running'))
    points = [DataPoint(task_id=task_id, timestamp=ts, value=v, metric_type=m)
              for m, pts in series.items() for ts, v in pts]
    if points:
        assert db.save_data_points(points)
    return task_id


def _series(seconds, func):
    return [(T0 + timedelta(seconds=s), float(func(s))) for s in seconds]


# ========== aggregates ==========

def _py_aggregates(points):
    first_ts = points[0][0]
    xs = [(ts - first_ts).total_seconds() for ts, _ in points]
    ys = [v for _, v in points]
    return dict(
        count=len(points), sum_x=sum(xs), sum_y=sum(ys),
        sum_xx=sum(x * x for x in xs), sum_xy=sum(x * y for x, y in zip(xs, ys)),
        sum_yy=sum(y * y for y in ys))


def test_aggregates_match_python(db, repo):
    rng = random.Random(7)
    # 毫秒级时间戳（SQLite julianday 精度为毫秒）
    seconds = [i * 1.5 + rng.randrange(0, 400) / 1000.0 for i in range(80)]
    mem = _series(seconds, lambda s: 1e8 + rng.uniform(-5e5, 5e5) + s * 1000)
    cpu = _series(seconds, lambda s: rng.uniform(0, 100))
    tid = _seed(db, {MEM: mem, CPU: cpu})

    agg = repo.aggregates(tid, MEM)
    expected = _py_aggregates(mem)
    assert agg.task_id == tid and agg.metric == MEM
    assert agg.count == expected['count']
    assert agg.first_ts == mem[0][0] and agg.last_ts == mem[-1][0]
    assert agg.first_value == mem[0][1] and agg.last_value == mem[-1][1]
    assert agg.min_value == min(v for _, v in mem)
    assert agg.max_value == max(v for _, v in mem)
    assert agg.span_seconds == (mem[-1][0] - mem[0][0]).total_seconds()
    for key in ('sum_x', 'sum_y', 'sum_xx', 'sum_xy', 'sum_yy'):
        assert math.isclose(getattr(agg, key), expected[key], rel_tol=1e-12), key


def test_aggregates_since_until_inclusive(db, repo):
    pts = _series(range(20), lambda s: s * 2)
    tid = _seed(db, {MEM: pts})
    since, until = pts[5][0], pts[14][0]

    agg = repo.aggregates(tid, MEM, since=since, until=until)
    sub = pts[5:15]
    assert agg.count == 10
    assert agg.first_ts == since and agg.last_ts == until
    assert agg.first_value == 10.0 and agg.last_value == 28.0
    expected = _py_aggregates(sub)
    for key in ('sum_x', 'sum_y', 'sum_xx', 'sum_xy', 'sum_yy'):
        assert math.isclose(getattr(agg, key), expected[key], rel_tol=1e-12), key

    # since == until 恰好落在一个点上
    one = repo.aggregates(tid, MEM, since=since, until=since)
    assert one.count == 1 and one.span_seconds == 0.0 and one.sum_x == 0.0


def test_aggregates_empty(db, repo):
    pts = _series(range(5), lambda s: s)
    tid = _seed(db, {MEM: pts})
    for agg in (repo.aggregates(tid, MEM, since=T0 + timedelta(hours=1)),
                repo.aggregates(tid, CPU),
                repo.aggregates('missing', MEM)):
        assert agg.count == 0
        assert agg.first_ts is None and agg.last_value is None and agg.min_value is None
        assert agg.sum_x == agg.sum_yy == agg.span_seconds == 0.0


def test_gap_factor_default_is_2_5(db, repo):
    """默认空洞倍数 2.5（方案 §6.3）：1 秒周期下间隔 2.4 秒不算空洞，2.6 秒算"""
    assert DEFAULT_GAP_FACTOR == 2.5
    tid = _seed(db, {MEM: _series([0, 1, 3.4, 4.4, 7.0, 8.0], lambda s: s)})
    assert repo.gaps(tid, 1.0) == [(T0 + timedelta(seconds=4.4), T0 + timedelta(seconds=7))]
    # 不指定周期时按间隔中位数（1 秒）估计，同一判定
    buckets = repo.buckets(tid, MEM, 1)
    assert [(b.start, b.end) for b in buckets] == [
        (T0, T0 + timedelta(seconds=4.4)), (T0 + timedelta(seconds=7), T0 + timedelta(seconds=8))]


def test_analytics_repo_does_not_import_core():
    """数据层不得依赖 core：拟合输入的转换在 core.analysis.trend"""
    import data.repos.analytics_repo as module
    source = open(module.__file__, encoding='utf-8').read()
    assert not re.search(r'^\s*(from|import)\s+core', source, re.M)
    assert not hasattr(MetricAggregates, 'to_fit_input')


def test_metric_aggregates_is_frozen(db, repo):
    tid = _seed(db, {MEM: _series(range(3), lambda s: s)})
    agg = repo.aggregates(tid, MEM)
    assert isinstance(agg, MetricAggregates)
    with pytest.raises(Exception):
        agg.count = 99


# ========== buckets ==========

def _check_bucket_stats(buckets, points):
    """每桶的统计量与按桶 [start, end] 切出的原始点一致，且桶覆盖全部点"""
    total = 0
    for b in buckets:
        inside = [v for ts, v in points if b.start <= ts <= b.end]
        assert b.count == len(inside)
        assert b.min == min(inside) and b.max == max(inside)
        assert math.isclose(b.avg, statistics.fmean(inside), rel_tol=1e-12)
        assert b.last == [v for ts, v in points if ts == b.end][0]
        total += b.count
    assert total == len(points)
    starts = [b.start for b in buckets]
    assert starts == sorted(starts)
    for prev, nxt in zip(buckets, buckets[1:]):
        assert prev.end < nxt.start


@pytest.mark.parametrize('max_buckets', [1, 3, 10, 50, 100, 500])
def test_buckets_count_and_stats(db, repo, max_buckets):
    rng = random.Random(max_buckets)
    pts = _series(range(100), lambda s: rng.uniform(0, 1000))
    tid = _seed(db, {MEM: pts})

    buckets = repo.buckets(tid, MEM, max_buckets)
    assert all(isinstance(b, Bucket) for b in buckets)
    assert 1 <= len(buckets) <= max_buckets
    if max_buckets <= len(pts):
        # 无空洞、点均匀时恰好等分为 max_buckets 个非空桶
        assert len(buckets) == max_buckets
    _check_bucket_stats(buckets, pts)


def test_buckets_equal_time_width_not_row_number(db, repo):
    # 前 10 秒每 0.1 秒一点（100 点），后 10 秒每 1 秒一点（11 点）；
    # 周期显式给 1 秒（阈值 2 秒），全程没有空洞
    dense = [i / 10 for i in range(100)]
    sparse = [10 + i for i in range(11)]
    pts = _series(dense + sparse, lambda s: s)
    tid = _seed(db, {MEM: pts})

    buckets = repo.buckets(tid, MEM, 3, interval_seconds=1.0)
    _check_bucket_stats(buckets, pts)
    # 20 秒等分 3 段：[0, 6.67) [6.67, 13.33) [13.33, 20]；按行号分则约 37/37/37
    assert [b.count for b in buckets] == [67, 37, 7]


def test_buckets_do_not_cross_gap(db, repo):
    # 0..49 秒、70..119 秒，中间缺 20 秒
    seconds = list(range(50)) + list(range(70, 120))
    pts = _series(seconds, lambda s: s % 7)
    tid = _seed(db, {MEM: pts})
    gap_start, gap_end = T0 + timedelta(seconds=49), T0 + timedelta(seconds=70)

    for max_buckets in (2, 3, 4, 7, 30):
        buckets = repo.buckets(tid, MEM, max_buckets)       # interval 自动估计为 1 秒
        assert len(buckets) <= max_buckets
        _check_bucket_stats(buckets, pts)
        for b in buckets:
            assert b.end <= gap_start or b.start >= gap_end, (max_buckets, b)


@pytest.mark.parametrize('seed', range(6))
def test_buckets_never_cross_gaps_randomized(db, repo, seed):
    # 0.5 秒周期、±20 毫秒抖动，随机插入 2~5 个 2~30 秒的空洞
    rng = random.Random(seed)
    gap_after = set(rng.sample(range(20, 380), rng.randint(2, 5)))
    t, seconds = 0.0, []
    for i in range(400):
        seconds.append(round(t + rng.uniform(-0.02, 0.02), 3))
        t += 0.5 + (rng.uniform(2, 30) if i in gap_after else 0)
    pts = _series(seconds, lambda s: rng.uniform(0, 100))
    tid = _seed(db, {MEM: pts}, interval=0.5)

    gaps = repo.gaps(tid, 0.5, metric=MEM)
    assert len(gaps) == len(gap_after)
    for max_buckets in (1, 2, 5, 13, 40, 400, 1000):
        buckets = repo.buckets(tid, MEM, max_buckets)     # 周期由中位间隔估计
        assert len(buckets) <= max(max_buckets, len(gaps) + 1)
        _check_bucket_stats(buckets, pts)
        for b in buckets:
            for g_start, g_end in gaps:
                assert b.end <= g_start or b.start >= g_end, (max_buckets, b)
        assert buckets == repo.buckets(tid, MEM, max_buckets, interval_seconds=0.5)


def test_buckets_more_gaps_than_buckets_keeps_segments(db, repo):
    seconds = list(range(10)) + list(range(30, 40))
    pts = _series(seconds, lambda s: s)
    tid = _seed(db, {MEM: pts})
    # 段数 >= max_buckets：不跨空洞优先，每段一桶
    buckets = repo.buckets(tid, MEM, 1)
    assert [(b.start, b.end, b.count) for b in buckets] == [
        (T0, T0 + timedelta(seconds=9), 10),
        (T0 + timedelta(seconds=30), T0 + timedelta(seconds=39), 10),
    ]


def test_buckets_explicit_interval_and_gap_factor(db, repo):
    seconds = list(range(10)) + list(range(30, 40))
    pts = _series(seconds, lambda s: s)
    tid = _seed(db, {MEM: pts})
    # 周期给到 30 秒时 21 秒的间隔不算空洞，一桶装下全部
    assert len(repo.buckets(tid, MEM, 1, interval_seconds=30.0)) == 1
    # gap_factor 足够大同理
    assert len(repo.buckets(tid, MEM, 1, interval_seconds=1.0, gap_factor=25)) == 1
    assert len(repo.buckets(tid, MEM, 1, interval_seconds=1.0, gap_factor=2)) == 2


def test_buckets_since_until_and_edge_cases(db, repo):
    pts = _series(range(60), lambda s: s)
    tid = _seed(db, {MEM: pts})
    since, until = pts[10][0], pts[29][0]
    buckets = repo.buckets(tid, MEM, 5, since=since, until=until)
    assert buckets[0].start == since and buckets[-1].end == until
    _check_bucket_stats(buckets, pts[10:30])

    assert repo.buckets(tid, CPU, 10) == []
    single = repo.buckets(tid, MEM, 10, since=until, until=until)
    assert [(b.count, b.min, b.last) for b in single] == [(1, 29.0, 29.0)]
    with pytest.raises(ValueError):
        repo.buckets(tid, MEM, 0)
    with pytest.raises(ValueError):
        repo.buckets(tid, MEM, 10, interval_seconds=0)


# ========== gaps ==========

def test_gaps_single_gap_boundaries(db, repo):
    # 1 秒周期，第 20..29 秒缺失（中间缺 10 秒）
    seconds = list(range(20)) + list(range(30, 50))
    tid = _seed(db, {MEM: _series(seconds, lambda s: s), CPU: _series(seconds, lambda s: 1)})
    expected = [(T0 + timedelta(seconds=19), T0 + timedelta(seconds=30))]
    assert repo.gaps(tid, 1.0) == expected
    assert repo.gaps(tid, 1.0, metric=MEM) == expected
    # 范围裁掉空洞一侧后不再有空洞
    assert repo.gaps(tid, 1.0, until=T0 + timedelta(seconds=19)) == []
    assert repo.gaps(tid, 1.0, since=T0 + timedelta(seconds=30)) == []
    assert repo.gaps(tid, 1.0, since=T0 + timedelta(seconds=19),
                     until=T0 + timedelta(seconds=30)) == expected


def test_gaps_none_and_factor(db, repo):
    tid = _seed(db, {MEM: _series(range(30), lambda s: s)})
    assert repo.gaps(tid, 1.0) == []
    assert repo.gaps('missing', 1.0) == []

    # 间隔恰为 3 秒（缺 2 个点）：factor=2 时是空洞，factor=3 时 3 > 3 不成立
    tid2 = _seed(db, {MEM: _series([0, 1, 2, 5, 6, 7], lambda s: s)}, task_id='t2')
    assert repo.gaps(tid2, 1.0) == [(T0 + timedelta(seconds=2), T0 + timedelta(seconds=5))]
    assert repo.gaps(tid2, 1.0, gap_factor=2.9) == [
        (T0 + timedelta(seconds=2), T0 + timedelta(seconds=5))]
    assert repo.gaps(tid2, 1.0, gap_factor=3.0) == []
    assert repo.gaps(tid2, 1.5) == []
    with pytest.raises(ValueError):
        repo.gaps(tid2, 0)
    with pytest.raises(ValueError):
        repo.gaps(tid2, 1.0, gap_factor=0)


def test_gaps_millisecond_timestamps(db, repo):
    seconds = [0.0, 0.5, 1.0, 1.5, 2.999, 3.5]
    tid = _seed(db, {MEM: _series(seconds, lambda s: s)})
    # 1.5 → 2.999 间隔 1.499 秒，阈值 1.5（0.5 秒周期 × 3）
    assert repo.gaps(tid, 0.5, gap_factor=3) == []
    assert repo.gaps(tid, 0.5, gap_factor=2.99) == [
        (T0 + timedelta(seconds=1.5), T0 + timedelta(seconds=2.999))]


# ========== value_near ==========

@pytest.fixture
def near_task(db):
    # 每秒一点，值 = 秒数 × 10，第 5 秒放一个尖峰
    values = {s: s * 10.0 for s in range(10)}
    values[5] = 1000.0
    return _seed(db, {MEM: [(T0 + timedelta(seconds=s), v) for s, v in values.items()]})


def test_value_near_odd_even(repo, near_task):
    at = T0 + timedelta(seconds=5)
    # n=5：第 3..7 秒 → 30, 40, 1000, 60, 70 → 中位数 60
    assert repo.value_near(near_task, MEM, at, n=5) == 60.0
    # n=1：最近一点就是尖峰本身
    assert repo.value_near(near_task, MEM, at, n=1) == 1000.0
    # n=4：距离相同时较早的点优先 → 第 4、5、6、3 秒 → 30,40,60,1000 → (40+60)/2
    assert repo.value_near(near_task, MEM, at, n=4) == 50.0
    # 落在两点之间：4.4 秒最近的两个是 4、5 秒
    assert repo.value_near(near_task, MEM, T0 + timedelta(seconds=4.4), n=2) == 520.0


def test_value_near_after_only(repo, near_task):
    at = T0 + timedelta(seconds=6)
    # 只取 ts 及之后：6,7,8,9 秒（不足 5 个）→ 60,70,80,90 → 75
    assert repo.value_near(near_task, MEM, at, n=5, after_only=True) == 75.0
    assert repo.value_near(near_task, MEM, at, n=1, after_only=True) == 60.0
    # 与 core.analysis.intervals.value_near 的结果一致
    from core.analysis.intervals import value_near as pure_value_near
    pts = [(T0 + timedelta(seconds=s), (1000.0 if s == 5 else s * 10.0)) for s in range(10)]
    for n in (1, 2, 3, 5, 8, 20):
        for sec in (-3, 0, 2.5, 5, 7.7, 12):
            ts = T0 + timedelta(seconds=sec)
            for after_only in (False, True):
                assert repo.value_near(near_task, MEM, ts, n=n, after_only=after_only) == \
                    pure_value_near(pts, ts, n, after_only=after_only), (n, sec, after_only)


def test_value_near_insufficient_and_empty(repo, near_task):
    # 全部只有 10 个点，n=20 用全部点
    assert repo.value_near(near_task, MEM, T0, n=20) == statistics.median(
        [s * 10.0 for s in range(10) if s != 5] + [1000.0])
    assert repo.value_near(near_task, MEM, T0 + timedelta(hours=1), after_only=True) is None
    assert repo.value_near(near_task, CPU, T0) is None
    with pytest.raises(ValueError):
        repo.value_near(near_task, MEM, T0, n=0)


# ========== 计数与锚点 ==========

def test_counts_and_last_timestamp(db, repo):
    seconds = range(15)
    tid = _seed(db, {MEM: _series(seconds, lambda s: s),
                     CPU: _series(list(seconds)[:10], lambda s: s)})
    assert repo.point_count(tid) == db.get_data_point_count(tid) == 25
    assert repo.point_count(tid, MEM) == db.get_data_point_count(tid, MEM) == 15
    assert repo.point_count(tid, 'nope') == 0
    assert repo.sample_count(tid) == db.get_sample_count(tid) == 15
    assert repo.sample_count('missing') == 0
    assert repo.last_point_timestamp(tid) == db.get_last_point_timestamp(tid) \
        == T0 + timedelta(seconds=14)
    assert repo.last_point_timestamp('missing') is None


def test_sample_count_legacy_schema_fallback(tmp_path):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY);
        CREATE TABLE data_points (id INTEGER PRIMARY KEY, task_id TEXT, timestamp TEXT,
                                  value REAL, metric_type TEXT);
        INSERT INTO tasks VALUES ('t');
        INSERT INTO data_points (task_id, timestamp, value, metric_type) VALUES
            ('t', '2026-01-01T00:00:00', 1, 'a'), ('t', '2026-01-01T00:00:00', 2, 'b'),
            ('t', '2026-01-01T00:00:01', 3, 'a');
    ''')
    conn.close()
    assert AnalyticsRepo(_Factory(path)).sample_count('t') == 2


# ========== legacy_* 与旧 Database 实现一致 ==========
#
# Database 门面现在直接委托 legacy_*，拿门面比对已是同义反复；下面两个函数是
# 阶段 3 拆分前 Database.get_metric_stats / get_task_data_points_bucketed 的 SQL 原样冻结，
# 作为等价性的参照实现（不随生产代码修改）。

def _old_where(task_id, metric, since_iso):
    where = 'task_id = ?'
    params = [task_id]
    if metric is not None:
        where += ' AND metric_type = ?'
        params.append(metric)
    if since_iso is not None:
        where += ' AND timestamp >= ?'
        params.append(since_iso)
    return where, params


def _old_metric_stats(db_path, task_id, metric, since_iso=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        where, params = _old_where(task_id, metric, since_iso)
        first_row = conn.execute(
            f'SELECT timestamp, value FROM data_points WHERE {where} '
            f'ORDER BY timestamp ASC LIMIT 1', params).fetchone()
        if first_row is None:
            return None
        last_row = conn.execute(
            f'SELECT timestamp, value FROM data_points WHERE {where} '
            f'ORDER BY timestamp DESC LIMIT 1', params).fetchone()
        agg = conn.execute(f'''
            SELECT COUNT(*) AS cnt, MIN(v) AS min_v, MAX(v) AS max_v,
                   AVG(v) AS avg_v, SUM(x) AS sx, SUM(x * x) AS sxx,
                   SUM(v) AS sy, SUM(x * v) AS sxy
            FROM (
                SELECT value AS v, (julianday(timestamp) - julianday(?)) * 24.0 AS x
                FROM data_points WHERE {where}
            )
        ''', [first_row['timestamp'], *params]).fetchone()
    finally:
        conn.close()
    first_ts = datetime.fromisoformat(first_row['timestamp'])
    last_ts = datetime.fromisoformat(last_row['timestamp'])
    slope = None
    count = agg['cnt']
    if count >= 2 and last_ts > first_ts:
        denominator = count * agg['sxx'] - agg['sx'] ** 2
        if denominator > 0:
            slope = (count * agg['sxy'] - agg['sx'] * agg['sy']) / denominator
    return {'count': count, 'min': agg['min_v'], 'max': agg['max_v'], 'avg': agg['avg_v'],
            'first': first_row['value'], 'first_ts': first_ts,
            'last': last_row['value'], 'last_ts': last_ts, 'slope_per_hour': slope}


def _old_bucketed(db_path, task_id, metric, max_buckets, since_iso=None):
    where, params = _old_where(task_id, metric, since_iso)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f'''
            WITH numbered AS (
                SELECT id, timestamp, value,
                       ROW_NUMBER() OVER (ORDER BY timestamp ASC, id ASC) - 1 AS rn,
                       COUNT(*) OVER () AS total
                FROM data_points WHERE {where}
            ),
            bucketed AS (
                SELECT id, timestamp, value,
                       CASE WHEN total <= ? THEN rn
                            ELSE MIN((rn * ?) / total, ? - 1) END AS bucket
                FROM numbered
            ),
            mins AS (
                SELECT bucket, timestamp, value, ROW_NUMBER() OVER (
                    PARTITION BY bucket ORDER BY value ASC, timestamp ASC) AS rk
                FROM bucketed
            ),
            maxs AS (
                SELECT bucket, timestamp, value, ROW_NUMBER() OVER (
                    PARTITION BY bucket ORDER BY value DESC, timestamp ASC) AS rk
                FROM bucketed
            )
            SELECT timestamp, value FROM mins WHERE rk = 1
            UNION
            SELECT timestamp, value FROM maxs WHERE rk = 1
            ORDER BY timestamp ASC
        ''', (*params, max_buckets, max_buckets, max_buckets)).fetchall()
    finally:
        conn.close()
    return [DataPoint(task_id=task_id, timestamp=datetime.fromisoformat(r['timestamp']),
                      value=r['value'], metric_type=metric or '') for r in rows]


def _dataset(name):
    rng = random.Random(name)
    if name == 'linear':
        return {MEM: _series(range(50), lambda s: 100 + 2.5 * s)}
    if name == 'noisy_with_gaps':
        seconds = [s + rng.randrange(0, 1000) / 1000 for s in range(300) if not 120 <= s < 160]
        return {MEM: _series(seconds, lambda s: 5e8 + rng.gauss(0, 1e6) + s * 3e4),
                CPU: _series(seconds, lambda s: rng.uniform(0, 100))}
    if name == 'single':
        return {MEM: _series([0], lambda s: 42.0)}
    if name == 'two_points':
        return {MEM: _series([0, 7], lambda s: [1.0, 9.0][s == 7])}
    if name == 'constant':
        return {MEM: _series(range(40), lambda s: 7.0)}
    if name == 'microseconds':
        # 带非毫秒对齐的微秒：旧实现与新实现都经 julianday，同样只到毫秒
        base = [T0 + timedelta(seconds=s, microseconds=rng.randrange(0, 10 ** 6))
                for s in range(0, 600, 3)]
        return {MEM: [(ts, rng.uniform(-50, 50)) for ts in base]}
    if name == 'duplicates':
        # 尖峰与重复值：检验桶内 min/max 并列时的取舍
        vals = [5, 5, 9, 1, 9, 1, 5, 5, 3, 3, 9, 1] * 20
        return {MEM: _series(range(len(vals)), lambda s: vals[s])}
    raise KeyError(name)


DATASETS = ['linear', 'noisy_with_gaps', 'single', 'two_points', 'constant',
            'microseconds', 'duplicates']


def _since_options(series):
    pts = series[MEM]
    mid = pts[len(pts) // 2][0]
    return [None, pts[0][0], mid, mid + timedelta(milliseconds=1),
            pts[-1][0], pts[-1][0] + timedelta(seconds=1), T0 - timedelta(days=1)]


@pytest.mark.parametrize('name', DATASETS)
def test_legacy_stats_equals_old_database(db, repo, name):
    series = _dataset(name)
    tid = _seed(db, series)
    for metric in (MEM, CPU, 'absent'):
        for since in _since_options(series):
            old = _old_metric_stats(db.db_path, tid, metric,
                                    since.isoformat() if since else None)
            new = repo.legacy_stats(tid, metric, since=since)
            # 门面委托后与仓储逐字段相同
            assert db.get_metric_stats(
                tid, metric, since_iso=since.isoformat() if since else None) == new
            if old is None:
                assert new is None, (metric, since)
                continue
            assert new.keys() == old.keys()
            for key in ('count', 'min', 'max', 'first', 'first_ts', 'last', 'last_ts'):
                assert new[key] == old[key], (key, metric, since)
            assert math.isclose(new['avg'], old['avg'], rel_tol=1e-12), (metric, since)
            if old['slope_per_hour'] is None:
                assert new['slope_per_hour'] is None, (metric, since)
            else:
                # 旧实现的 x 是未取整的儒略日差（每点约 4e-5 秒舍入噪声），新实现取整到毫秒，
                # 两者斜率差的量级 ≈ (值域/跨度) × (1e-4 秒/跨度)，据此给绝对容差
                span_s = (new['last_ts'] - new['first_ts']).total_seconds()
                scale = (new['max'] - new['min']) / (span_s / 3600.0)
                assert math.isclose(new['slope_per_hour'], old['slope_per_hour'],
                                    rel_tol=1e-9, abs_tol=scale * 1e-4 / span_s), \
                    (metric, since)


@pytest.mark.parametrize('name', DATASETS)
@pytest.mark.parametrize('max_buckets', [1, 2, 7, 64, 2000])
def test_legacy_buckets_equals_old_database(db, repo, name, max_buckets):
    series = _dataset(name)
    tid = _seed(db, series)
    for metric in (MEM, CPU, None):
        for since in _since_options(series):
            old = _old_bucketed(db.db_path, tid, metric, max_buckets,
                                since.isoformat() if since else None)
            new = repo.legacy_buckets(tid, metric, max_buckets, since=since)
            assert new == old, (metric, since)
            assert len(new) <= 2 * max_buckets


# ========== 参数类型与错误包装 ==========

def test_time_params_reject_numbers(db, repo):
    tid = _seed(db, {MEM: _series(range(3), lambda s: s)})
    epoch = T0.timestamp()
    calls = [
        lambda: repo.aggregates(tid, MEM, since=epoch),
        lambda: repo.aggregates(tid, MEM, until=int(epoch)),
        lambda: repo.buckets(tid, MEM, 10, since=epoch),
        lambda: repo.gaps(tid, 1.0, until=epoch),
        lambda: repo.value_near(tid, MEM, epoch),
        lambda: repo.value_near(tid, MEM, None),
        lambda: repo.legacy_stats(tid, MEM, since=epoch),
        lambda: repo.legacy_buckets(tid, MEM, 10, since=T0.isoformat()),
    ]
    for call in calls:
        with pytest.raises(TypeError):
            call()


def _all_queries(r):
    return [
        lambda: r.aggregates('t', MEM),
        lambda: r.buckets('t', MEM, 10),
        lambda: r.gaps('t', 1.0),
        lambda: r.value_near('t', MEM, T0),
        lambda: r.point_count('t'),
        lambda: r.sample_count('t'),
        lambda: r.last_point_timestamp('t'),
        lambda: r.legacy_stats('t', MEM),
        lambda: r.legacy_buckets('t', MEM, 10),
    ]


def test_storage_error_on_corrupt_file(tmp_path):
    path = tmp_path / 'corrupt.db'
    path.write_bytes(b'this is not a sqlite database' * 200)
    for call in _all_queries(AnalyticsRepo(_Factory(str(path)))):
        with pytest.raises(StorageError) as info:
            call()
        assert isinstance(info.value.__cause__, sqlite3.Error)


class _ReadOnlyMissingFactory(_Factory):
    """只读打开一个不存在的库文件：sqlite3 在 connect 阶段就失败"""

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(f'file:{self.db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def test_storage_error_on_unopenable_readonly(tmp_path):
    repo = AnalyticsRepo(_ReadOnlyMissingFactory(str(tmp_path / 'missing.db')))
    for call in _all_queries(repo):
        with pytest.raises(StorageError) as info:
            call()
        assert isinstance(info.value.__cause__, sqlite3.OperationalError)


def test_storage_error_on_missing_table(tmp_path):
    path = str(tmp_path / 'empty.db')
    sqlite3.connect(path).close()
    with pytest.raises(StorageError):
        AnalyticsRepo(_Factory(path)).aggregates('t', MEM)
