"""
AnalyticsRepo 两条查询路径一致：同一数据集分别强制走原始路径（path='raw'）与汇总路径
（path='rollup'），aggregates / gaps / buckets / legacy_buckets / value_near / point_count 的
结果计数与时间戳完全一致、数值相对误差 ≤ 1e-9；legacy_buckets 两条路径都等于本文件的
Python 参照实现（2.0 的图表点集定义）。

数据集覆盖：空洞（含分钟中间的空洞，汇总行在此拆开）、某指标单独缺采（metric=None 的空洞
按合并时间戳计算）、指标起止不同、范围边界切在汇总行中间、范围只含一个桶、单点、空范围。
另测自动选路径（按估算行数）与口径不符时不走汇总路径。
"""
import dataclasses
import math
import random
from datetime import datetime, timedelta

import pytest

from data.mappers import ceil_ms, from_ms, to_ms
from data.models import DataPoint, MonitorTask
from data.repos import analytics_repo
from data.repos.analytics_repo import AnalyticsRepo
from data.repos.point_repo import PointQuery

T0 = datetime(2026, 3, 1, 10, 0, 0)
MEM = 'memory_private'
CPU = 'cpu_percent'
MINUTE = 60_000


# ========== 图表点集参照实现（2.0 定义） ==========

def chart_points_reference(rows, max_points):
    """
    rows: 范围内全部样本 [(ts_ms, value)]（可含多条序列）。返回 [(ts_ms, value)]，按时间、值升序去重。
    点数 ≤ 2 × max_points 时原样返回；否则按时间等分：首末样本所在自然分钟覆盖 ≥ max_points 分钟时
    桶宽取 ceil(覆盖分钟数 / max_points) 分钟、对齐自然分钟，否则 [t0, t1] 按毫秒等分；
    每桶取最小值点与最大值点（并列取较早）。
    """
    if not rows:
        return []
    if len(rows) <= 2 * max_points:
        return sorted(set(rows))
    t0 = min(ts for ts, _v in rows)
    t1 = max(ts for ts, _v in rows)
    origin = t0 - t0 % MINUTE
    extent = (t1 - t1 % MINUTE) + MINUTE - origin
    if extent >= max_points * MINUTE:
        width = math.ceil(extent / (max_points * MINUTE)) * MINUTE

        def key(ts):
            return ((ts - ts % MINUTE) - origin) // width
    else:
        span = t1 - t0

        def key(ts):
            return 0 if span == 0 else min((ts - t0) * max_points // span, max_points - 1)
    groups = {}
    for ts, value in rows:
        groups.setdefault(key(ts), []).append((ts, value))
    picked = set()
    for members in groups.values():
        picked.add(min(members, key=lambda p: (p[1], p[0])))
        picked.add(min(members, key=lambda p: (-p[1], p[0])))
    return sorted(picked)


def reference_legacy_buckets(points, task_id, metric, max_points, since):
    """points: 任务全部 DataPoint（db.points.fetch 的结果）"""
    lo = ceil_ms(since) if since is not None else None
    rows = [(to_ms(p.timestamp), p.value) for p in points
            if metric is None or p.metric_type == metric]
    rows = [r for r in rows if lo is None or r[0] >= lo]
    return [DataPoint(task_id=task_id, timestamp=from_ms(ts), value=v, metric_type=metric or '')
            for ts, v in chart_points_reference(rows, max_points)]


# ========== 比较 ==========

def close(a, b):
    if dataclasses.is_dataclass(a) and dataclasses.is_dataclass(b):
        return type(a) is type(b) and close(dataclasses.asdict(a), dataclasses.asdict(b))
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(close(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
    return a == b


# ========== 数据集 ==========

def _task(db, task_id, interval, metrics):
    db.save_task(MonitorTask(task_id=task_id, pid=1, process_name='a.exe',
                             metric_types=list(metrics), interval=interval, start_time=T0,
                             end_time=None, status='stopped'))


def _seed_long(db):
    """
    1 秒周期约 2.5 小时：毫秒抖动；10:20:15 起 25 秒空洞（分钟中间）；11:00 起 10 分钟空洞；
    CPU 晚 5 分钟才开始、11:30 附近单独缺采 40 秒、比 MEM 早 3 分钟结束；数值带并列。
    """
    _task(db, 'long', 1.0, (MEM, CPU))
    rng = random.Random(20261107)
    points = []
    for k in range(9000):
        if 1215 <= k < 1240 or 3600 <= k < 4200:
            continue
        ts = T0 + timedelta(seconds=k, milliseconds=rng.choice([0, 0, 3, 997]))
        points.append(DataPoint('long', ts, 1000.0 + k * 0.01 + rng.randint(0, 20), MEM))
        if 300 <= k < 8820 and not 5400 <= k < 5440:
            points.append(DataPoint('long', ts, float(rng.randint(0, 100)), CPU))
    for i in range(0, len(points), 997):
        db.points.append(points[i:i + 997])
    return 'long', 1.0, (MEM, CPU)


def _seed_minute(db):
    """60 秒周期 300 点，中间 20 分钟空洞"""
    _task(db, 'minute', 60.0, (MEM,))
    db.points.append([DataPoint('minute', T0 + timedelta(minutes=i, seconds=7), float(i % 9), MEM)
                      for i in range(320) if not 100 <= i < 120])
    return 'minute', 60.0, (MEM,)


def _seed_tiny(db):
    """同一分钟内 5 点（全部只在一个汇总行里）"""
    _task(db, 'tiny', 1.0, (MEM,))
    db.points.append([DataPoint('tiny', T0 + timedelta(seconds=10 + s), float(v), MEM)
                      for s, v in enumerate([3, 1, 4, 1, 5])])
    return 'tiny', 1.0, (MEM,)


def _seed_single(db):
    _task(db, 'single', 1.0, (MEM,))
    db.points.append([DataPoint('single', T0 + timedelta(seconds=42), 7.0, MEM)])
    return 'single', 1.0, (MEM,)


SEEDS = {'long': _seed_long, 'minute': _seed_minute, 'tiny': _seed_tiny, 'single': _seed_single}


def _ranges(stamps):
    """范围组合：不限、边界切在汇总行中间、恰在分钟起点、只含一个桶、单点、空范围"""
    first, last = stamps[0], stamps[-1]
    mid = stamps[len(stamps) // 2]
    q1 = stamps[len(stamps) // 4]
    minute = mid.replace(second=0, microsecond=0)
    one = timedelta(milliseconds=1)
    return [
        (None, None), (mid, None), (None, mid), (q1, mid),
        (q1 + timedelta(seconds=17, milliseconds=500), mid - timedelta(seconds=23)),
        (minute, minute + timedelta(minutes=30)),
        (minute + timedelta(seconds=5), minute + timedelta(seconds=40)),   # 一个桶之内
        (mid, mid),                                                        # 单点
        (mid + one, mid + one),                                            # 空（毫秒之间）
        (last + timedelta(seconds=1), None),                               # 空（之后）
        (None, first - timedelta(seconds=1)),                              # 空（之前）
        (first, last),
    ]


@pytest.fixture(params=list(SEEDS))
def dataset(request, db):
    task_id, interval, metrics = SEEDS[request.param](db)
    points = db.points.fetch(PointQuery(task_id, None))
    stamps = sorted({p.timestamp for p in points})
    raw = AnalyticsRepo(db._factory, path='raw')
    rolled = AnalyticsRepo(db._factory, path='rollup')
    return db, task_id, interval, metrics, points, stamps, raw, rolled


def _same(name, fn_raw, fn_rollup):
    a, b = fn_raw(), fn_rollup()
    assert close(a, b), (name, a, b)
    return a


def test_aggregates_and_point_count_match(dataset):
    _db, task_id, _interval, metrics, _points, stamps, raw, rolled = dataset
    for since, until in _ranges(stamps):
        for metric in (None, *metrics):
            _same(('point_count', metric, since, until),
                  lambda: raw.point_count(task_id, metric, since, until),
                  lambda: rolled.point_count(task_id, metric, since, until))
        for metric in metrics:
            agg = _same(('aggregates', metric, since, until),
                        lambda: raw.aggregates(task_id, metric, since, until),
                        lambda: rolled.aggregates(task_id, metric, since, until))
            # 计数与时间戳必须完全相同（close 对整数与 datetime 本就按相等比较，这里写明）
            other = rolled.aggregates(task_id, metric, since, until)
            assert (agg.count, agg.first_ts, agg.last_ts) == \
                (other.count, other.first_ts, other.last_ts)


def test_gaps_match(dataset):
    _db, task_id, interval, metrics, _points, stamps, raw, rolled = dataset
    for since, until in _ranges(stamps):
        for metric in (None, *metrics):
            _same(('gaps', metric, since, until),
                  lambda: raw.gaps(task_id, interval, since, until, metric=metric),
                  lambda: rolled.gaps(task_id, interval, since, until, metric=metric))


def test_buckets_match(dataset):
    _db, task_id, interval, metrics, _points, stamps, raw, rolled = dataset
    for since, until in _ranges(stamps):
        for metric in metrics:
            for max_buckets in (1, 3, 7, 40, 800):
                for iv in (interval, None):
                    _same(('buckets', metric, since, until, max_buckets, iv),
                          lambda: raw.buckets(task_id, metric, max_buckets, since, until, iv),
                          lambda: rolled.buckets(task_id, metric, max_buckets, since, until, iv))


def test_legacy_buckets_match_reference(dataset):
    _db, task_id, _interval, metrics, points, stamps, raw, rolled = dataset
    for since, _until in _ranges(stamps)[:6]:
        for metric in (None, *metrics):
            for max_points in (1, 2, 7, 64, 2000):
                expected = reference_legacy_buckets(points, task_id, metric, max_points, since)
                assert raw.legacy_buckets(task_id, metric, max_points, since) == expected, \
                    ('raw', metric, since, max_points)
                assert rolled.legacy_buckets(task_id, metric, max_points, since) == expected, \
                    ('rollup', metric, since, max_points)
                assert len(expected) <= 2 * max_points


def test_value_near_matches(dataset):
    _db, task_id, _interval, metrics, _points, stamps, raw, rolled = dataset
    for metric in metrics:
        for ts in (stamps[0], stamps[len(stamps) // 2], stamps[-1] + timedelta(seconds=5)):
            for after_only in (False, True):
                _same(('value_near', metric, ts, after_only),
                      lambda: raw.value_near(task_id, metric, ts, 5, after_only),
                      lambda: rolled.value_near(task_id, metric, ts, 5, after_only))


def test_metric_none_gaps_equal_union_when_timestamps_shared(db):
    """同一周期各指标共用时间戳时，metric=None 的空洞就是各指标空洞的并集（汇总路径取并集）"""
    _task(db, 't', 1.0, (MEM, CPU))
    db.points.append([DataPoint('t', T0 + timedelta(seconds=s), float(s), m)
                      for s in range(600) if not 100 <= s < 130 and not 400 <= s < 460
                      for m in (MEM, CPU)])
    raw = AnalyticsRepo(db._factory, path='raw')
    rolled = AnalyticsRepo(db._factory, path='rollup')
    union = sorted(set(rolled.gaps('t', 1.0, metric=MEM)) | set(rolled.gaps('t', 1.0, metric=CPU)))
    assert raw.gaps('t', 1.0) == rolled.gaps('t', 1.0) == union
    assert len(union) == 2


def test_rollup_path_needs_matching_gap_rule(dataset, monkeypatch):
    """gap_factor 非默认或周期与任务不同时，强制汇总也走原始路径（口径固定在写入时）"""
    _db, task_id, interval, metrics, _points, _stamps, raw, rolled = dataset
    called = []
    monkeypatch.setattr(AnalyticsRepo, '_gaps_from_table',
                        lambda self, *a: called.append(a) or [])
    monkeypatch.setattr(AnalyticsRepo, '_rollup_buckets',
                        lambda self, *a: called.append(a) or [])
    for kwargs in ({'gap_factor': 3.0}, {'interval_seconds': interval * 2}):
        iv = kwargs.get('interval_seconds', interval)
        gf = kwargs.get('gap_factor', analytics_repo.DEFAULT_GAP_FACTOR)
        assert rolled.gaps(task_id, iv, gap_factor=gf) == raw.gaps(task_id, iv, gap_factor=gf)
        for metric in metrics:
            assert close(rolled.buckets(task_id, metric, 5, interval_seconds=iv, gap_factor=gf),
                         raw.buckets(task_id, metric, 5, interval_seconds=iv, gap_factor=gf))
    assert called == []


def test_auto_path_follows_estimated_rows(db, monkeypatch):
    task_id, interval, metrics, = _seed_minute(db)
    auto = AnalyticsRepo(db._factory)
    used = []
    real = AnalyticsRepo._rollup_sums
    monkeypatch.setattr(AnalyticsRepo, '_rollup_sums',
                        lambda self, *a: used.append(a) or real(self, *a))
    expected = AnalyticsRepo(db._factory, path='raw').aggregates(task_id, MEM)
    assert close(auto.aggregates(task_id, MEM), expected)
    assert used == []                                       # 300 行远低于阈值：原始路径
    monkeypatch.setattr(analytics_repo, 'RAW_PATH_MAX_ROWS', 299)
    assert close(auto.aggregates(task_id, MEM), expected)
    assert len(used) == 1                                   # 估算 300 行 > 299：汇总路径
    assert close(auto.aggregates(task_id, MEM, since=T0 + timedelta(minutes=200)),
                 AnalyticsRepo(db._factory, path='raw').aggregates(
                     task_id, MEM, since=T0 + timedelta(minutes=200)))
    assert len(used) == 1                                   # 后 120 行：又回到原始路径


def test_invalid_path_rejected(db):
    with pytest.raises(ValueError):
        AnalyticsRepo(db._factory, path='fast')


# ========== 整包取回（fetch_packed） ==========

def _packed_conn():
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE t (k INTEGER, v REAL)')
    return conn


def test_fetch_packed_matches_row_by_row():
    """整包取回与逐行读取逐位相同：整数、整值浮点、普通浮点、负数、0、空结果"""
    from data.repos._samples import fetch_packed
    rng = random.Random(7)
    conn = _packed_conn()
    rows = [(k, v) for k, v in enumerate(
        [0.0, 5.0, -3.0, 0.1 + 0.2, 1 / 3, 2.0 ** 53, 123456789.123456789, 1e-60, -9.87e65]
        + [math.ldexp(rng.random() + 0.5, rng.randint(-200, 200)) for _ in range(2000)])]
    conn.executemany('INSERT INTO t VALUES (?, ?)', rows)
    sql = 'SELECT k AS c0, v AS c1 FROM t WHERE k >= ?'
    assert sorted(fetch_packed(conn, sql, (0,), 'if')) == rows
    assert fetch_packed(conn, sql, (10 ** 6,), 'if') == []


@pytest.mark.parametrize('extra', [(9001, 1e300), (9002, 1e-300), (9003, None),
                                   (9004.5, 1.0), (9005, 'x')])
def test_fetch_packed_falls_back_outside_safe_range(extra):
    """浮点超出安全量级、NULL、文本、整数列里的浮点：退回逐行读取，结果不变"""
    from data.repos._samples import fetch_packed
    conn = _packed_conn()
    rows = [(1, 2.5), (2, -0.125), extra]
    conn.executemany('INSERT INTO t VALUES (?, ?)', rows)
    assert sorted(fetch_packed(conn, 'SELECT k AS c0, v AS c1 FROM t', (), 'if')) == rows


def test_fetch_packed_disabled_when_probe_fails(monkeypatch):
    """本进程 SQLite 的浮点文本化自检不通过时一律逐行读取"""
    from data.repos import _samples
    monkeypatch.setattr(_samples, '_pack_supported', None)
    monkeypatch.setattr(_samples, '_probe_pack', lambda conn: False)
    conn = _packed_conn()
    conn.executemany('INSERT INTO t VALUES (?, ?)', [(1, 0.1), (2, 0.2)])
    seen = []
    conn.set_trace_callback(seen.append)
    assert sorted(_samples.fetch_packed(conn, 'SELECT k AS c0, v AS c1 FROM t', (), 'if')) == [
        (1, 0.1), (2, 0.2)]
    assert not any('group_concat' in sql for sql in seen)
    assert _samples._pack_supported is False
