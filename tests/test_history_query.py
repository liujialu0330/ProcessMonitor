"""
历史查询（core.history_query）：请求组装、锚点与起点口径、查询编排、基线与统计起点、
判定/增速/每轮、空洞与累计量速率、失败归类。

起点/锚点的期望值取自重构前页面里 _compute_since 的实际输出（锚点为任务最后
一次采样 BASE+4s；last_sample_at 为空时回退 MAX(timestamp) 结果相同；任务无数据点
或选"全部"/未知范围时不过滤），写死在 SINCE_CASES 里逐一对照。
"""
import uuid
from datetime import timedelta

import pytest

from core.analysis.trend import Verdict
from core.history_query import (
    BASELINE_RANGE_KEY, CHART_MAX_BUCKETS, DEFAULT_RANGE_KEY, RANGE_KEYS,
    TABLE_POINT_LIMIT, TIME_RANGE_SECONDS, HistoryRequest, HistorySnapshot, PeakRises,
    build_request, compute_since, run_history_query,
)
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from tests.history_dataset import SIMPLE_BASE as BASE, _seed_task
from tests.history_dataset import (
    BASE as NET_BASE, MAX_BUCKETS as NET_BUCKETS, QUERY_CASES,
    TABLE_LIMIT as NET_LIMIT, _expected_calls, _record_db_queries, _seed_query_dataset,
)
from utils.metrics import (
    VALUE_CLASS_LEVEL, VALUE_CLASS_PEAK, VALUE_CLASS_RATE, MetricType)

MEM = MetricType.MEMORY_PRIVATE

# (范围, 有数据, 期望起点相对 BASE 的秒数；None 表示不过滤) —— 旧页面 _compute_since 的输出
SINCE_CASES = [
    ('10m', True, 4 - 600),
    ('1h', True, 4 - 3600),
    ('6h', True, 4 - 21600),
    ('24h', True, 4 - 86400),
    ('all', True, None),
    ('bogus', True, None),
    ('10m', False, None),
    ('1h', False, None),
    ('6h', False, None),
    ('24h', False, None),
    ('all', False, None),
]


def _null_last_sample_at(db, task_id):
    with db._get_connection() as conn:
        conn.execute('UPDATE tasks SET last_sample_at = NULL WHERE task_id = ?', (task_id,))


def _request(task_id, range_key='all', **overrides):
    return build_request(task_id, overrides.pop('metric', MEM), range_key, **overrides)


# ---------- 常量与纯函数 ----------

def test_presets_match_page_constants():
    assert TIME_RANGE_SECONDS == {
        '10m': 600, '1h': 3600, '6h': 21600, '24h': 86400, 'all': None}
    assert DEFAULT_RANGE_KEY == 'all'
    assert RANGE_KEYS == ('baseline', '10m', '1h', '6h', '24h', 'all')
    assert (TABLE_POINT_LIMIT, CHART_MAX_BUCKETS) == (2000, 2000)


def test_compute_since_baseline_range():
    baseline_at = BASE + timedelta(minutes=3)
    anchor = BASE + timedelta(hours=1)
    assert compute_since(BASELINE_RANGE_KEY, anchor, baseline_at) == baseline_at
    assert compute_since(BASELINE_RANGE_KEY, anchor, None) is None
    # 时钟预设不受基线影响
    assert compute_since('10m', anchor, baseline_at) == anchor - timedelta(minutes=10)


def test_build_request_leaves_baseline_since_to_query():
    request = build_request("t", MEM, BASELINE_RANGE_KEY, anchor=BASE)
    assert request.since is None and request.baseline_only is True


@pytest.mark.parametrize("range_key, has_data, offset", SINCE_CASES)
def test_compute_since_matches_old_page(range_key, has_data, offset):
    anchor = BASE + timedelta(seconds=4) if has_data else None
    expected = None if offset is None else BASE + timedelta(seconds=offset)
    assert compute_since(range_key, anchor) == expected


def test_build_request_with_anchor_computes_since():
    anchor = BASE + timedelta(seconds=4)
    request = build_request("t", MEM, '1h', anchor=anchor, table_limit=7,
                            max_buckets=9, seq=42)
    assert request == HistoryRequest(
        task_id="t", metric=MEM, range_key='1h',
        since=anchor - timedelta(hours=1), until=None, table_limit=7,
        max_buckets=9, anchor=anchor, request_seq=42, auto_refresh=False)


def test_build_request_without_anchor_leaves_since_to_query():
    request = build_request("t", MEM, '1h', seq=1, auto_refresh=True)
    assert request.anchor is None and request.since is None
    assert request.auto_refresh is True
    assert (request.table_limit, request.max_buckets) == (TABLE_POINT_LIMIT, CHART_MAX_BUCKETS)


def test_request_is_immutable():
    request = build_request("t", MEM, 'all')
    with pytest.raises(Exception):
        request.metric = "x"


# ---------- 锚点解析（在执行查询的线程里） ----------

@pytest.mark.parametrize("null_last_sample", [False, True], ids=["last_sample_at", "max_fallback"])
@pytest.mark.parametrize("range_key, has_data, offset", SINCE_CASES)
def test_run_query_resolves_anchor_like_old_page(db, range_key, has_data, offset,
                                                 null_last_sample):
    task = _seed_task(db, samples=5 if has_data else 0)
    if null_last_sample:
        _null_last_sample_at(db, task.task_id)

    snapshot = run_history_query(db, _request(task.task_id, range_key))

    expected = None if offset is None else BASE + timedelta(seconds=offset)
    assert snapshot.request.since == expected
    expected_anchor = BASE + timedelta(seconds=4) if has_data else None
    assert snapshot.task_last_at == expected_anchor
    assert snapshot.request.anchor == expected_anchor
    assert snapshot.error is None and snapshot.ok


def test_anchor_reads_last_sample_at_without_max_query(db, monkeypatch):
    task = _seed_task(db, samples=5)
    monkeypatch.setattr(db.analytics, 'last_point_timestamp',
                        lambda tid: pytest.fail("有 last_sample_at 时不应查 MAX"))
    snapshot = run_history_query(db, _request(task.task_id, '1h'))
    assert snapshot.task_last_at == BASE + timedelta(seconds=4)


def test_given_anchor_is_used_as_is(db, monkeypatch):
    """调用方缓存的锚点直接使用（切范围/切指标不重查锚点），即使库里已有更新的采样"""
    task = _seed_task(db, samples=5)
    monkeypatch.setattr(db.analytics, 'last_point_timestamp',
                        lambda tid: pytest.fail("给定锚点时不应查 MAX"))
    stale = BASE + timedelta(seconds=2)
    request = _request(task.task_id, '10m', anchor=stale)

    snapshot = run_history_query(db, request)

    assert snapshot.request == request
    assert snapshot.request.since == stale - timedelta(minutes=10)
    assert snapshot.task_last_at == stale


# ---------- 查询编排 ----------

def _record(db, monkeypatch):
    calls = _record_db_queries(db, monkeypatch)
    real_get = db.tasks.get

    def spy(*a, **k):
        calls.append(('task',))
        return real_get(*a, **k)
    monkeypatch.setattr(db.tasks, 'get', spy)
    return calls


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, _texts",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_query_results_match_safety_net_numbers(db, monkeypatch, metric, range_key,
                                                since_minutes, rows, count, buckets,
                                                markers, _texts):
    """与安全网钉死的数值一致：同一 since、同一查询顺序、同样的行数/统计/分桶/打点"""
    task = _seed_query_dataset(db)
    calls = _record(db, monkeypatch)

    snapshot = run_history_query(db, build_request(
        task.task_id, metric, range_key, table_limit=NET_LIMIT, max_buckets=NET_BUCKETS))

    since = None if since_minutes is None else NET_BASE + timedelta(minutes=since_minutes)
    assert calls == [('task',), *_expected_calls(metric, since)]
    assert len(snapshot.points) == rows
    assert snapshot.stats['count'] == snapshot.total_count == count
    assert len(snapshot.buckets) == buckets
    assert len(snapshot.markers) == markers
    assert snapshot.task_last_at == NET_BASE + timedelta(minutes=120)
    assert snapshot.stats_since == since and snapshot.baseline is None
    assert snapshot.gaps == [] and snapshot.value_class == VALUE_CLASS_LEVEL


def test_empty_range_queries_markers_and_table_only(db, monkeypatch):
    """线程数只采到 60 分钟，锚点在 120 分钟：选 10 分钟时范围内无数据，只查打点与明细"""
    task = _seed_query_dataset(db)
    calls = _record(db, monkeypatch)

    snapshot = run_history_query(db, build_request(
        task.task_id, MetricType.NUM_THREADS, '10m', table_limit=NET_LIMIT))

    since = NET_BASE + timedelta(minutes=110)
    assert calls == [('task',), ('markers', None, None), ('table', since, NET_LIMIT)]
    assert snapshot.points == [] and snapshot.stats is None
    assert snapshot.buckets == [] and len(snapshot.markers) == 2
    assert snapshot.judgement is None and snapshot.total_count == 0
    assert snapshot.ok


def test_no_metric_skips_stats(db):
    task = _seed_task(db, samples=3)
    snapshot = run_history_query(db, build_request(task.task_id, None, 'all'))
    assert len(snapshot.points) == 3 and snapshot.stats is None
    assert snapshot.total_count == 3 and snapshot.judgement is None


def test_missing_task_stops_before_data_queries(db, monkeypatch):
    calls = _record(db, monkeypatch)
    request = _request("no-such-task", '1h', seq=3)
    snapshot = run_history_query(db, request)
    assert calls == [('task',)]
    assert snapshot.task_missing is True and not snapshot.ok
    assert snapshot.request == request and snapshot.error is None


# ---------- 基线 ----------

def _seed_series(db, values, *, interval=60.0, metric=MEM, start=BASE, skip=(),
                 markers=()):
    """按 interval 秒一点写入 values（跳过下标在 skip 里的点）；markers 为 (下标, 标签, kind)。"""
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=1, process_name="s.exe", metric_types=[metric],
        interval=interval, start_time=start, end_time=None, status="stopped")
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i * interval), float(v), metric)
        for i, v in enumerate(values) if i not in skip])
    for index, label, kind in markers:
        db.markers.add(task.task_id, start + timedelta(seconds=index * interval), label, kind)
    return task


def test_baseline_limits_stats_but_not_display(db):
    """存在基线：统计起点 = 基线时刻，图表与明细仍按范围显示；基线值 = 其后 5 点中位数"""
    values = [1000 + 50 * i for i in range(10)] + [2000 + i for i in range(40)]
    values[12] = 99999   # 基线后第 3 个样本是尖峰：中位数不受影响
    task = _seed_series(db, values, markers=[(10, "基线", 'baseline')])

    snapshot = run_history_query(db, _request(task.task_id, 'all'))

    baseline_at = BASE + timedelta(minutes=10)
    assert snapshot.request.since is None
    assert snapshot.stats_since == baseline_at
    assert snapshot.total_count == 50 and snapshot.stats['count'] == 40
    assert snapshot.stats['first'] == 2000 and snapshot.stats['first_ts'] == baseline_at
    assert snapshot.baseline.marker.label == "基线"
    assert snapshot.baseline.value == 2003.0 and snapshot.baseline.sample_count == 5
    assert len(snapshot.points) == 50


def test_baseline_range_starts_at_baseline(db):
    task = _seed_series(db, list(range(50)), markers=[(10, "基线", 'baseline')])
    snapshot = run_history_query(db, _request(task.task_id, BASELINE_RANGE_KEY))
    baseline_at = BASE + timedelta(minutes=10)
    assert snapshot.request.since == snapshot.stats_since == baseline_at
    assert snapshot.total_count == 40 and len(snapshot.points) == 40


def test_baseline_range_without_baseline_is_unfiltered(db):
    task = _seed_series(db, list(range(20)))
    snapshot = run_history_query(db, _request(task.task_id, BASELINE_RANGE_KEY))
    assert snapshot.request.since is None and snapshot.baseline is None
    assert snapshot.total_count == 20


def test_short_range_keeps_baseline_as_reference(db):
    """"10 分钟"范围在基线之后：统计从范围起点算，基线值仍给出（样本数按基线之后计）"""
    task = _seed_series(db, list(range(60)), markers=[(10, "基线", 'baseline')])
    snapshot = run_history_query(db, _request(task.task_id, '10m'))
    assert snapshot.request.since == BASE + timedelta(minutes=49)
    assert snapshot.stats_since == snapshot.request.since
    assert snapshot.baseline.value == 12.0 and snapshot.baseline.sample_count == 5


def test_baseline_sample_count_when_few_points_after_baseline(db, monkeypatch):
    task = _seed_series(db, list(range(12)), markers=[(9, "基线", 'baseline')])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.baseline.value == 10.0 and snapshot.baseline.sample_count == 3


def test_baseline_only_off_uses_display_start(db):
    task = _seed_series(db, list(range(50)), markers=[(10, "基线", 'baseline')])
    snapshot = run_history_query(
        db, build_request(task.task_id, MEM, 'all', baseline_only=False))
    assert snapshot.stats_since is None and snapshot.stats['count'] == 50
    assert snapshot.baseline.value == 12.0


def test_latest_baseline_marker_wins(db):
    task = _seed_series(db, list(range(50)), markers=[
        (5, "旧基线", 'baseline'), (20, "新基线", 'baseline')])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.baseline.marker.label == "新基线"
    assert snapshot.stats_since == BASE + timedelta(minutes=20)


def test_baseline_after_all_data_gives_no_stats(db):
    task = _seed_series(db, list(range(10)), markers=[(30, "基线", 'baseline')])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.stats is None and snapshot.judgement is None
    assert snapshot.baseline.value is None and snapshot.baseline.sample_count == 0
    assert snapshot.total_count == 10


# ---------- 判定、增速与每轮 ----------

def test_rising_series_is_judged_with_rate_per_hour(db):
    """两小时干净线性上涨：持续上涨，增速按 /小时，± 为置信区间半宽"""
    task = _seed_series(db, [1024.0 * (100 + i) for i in range(121)])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.judgement.verdict == Verdict.RISING
    assert snapshot.fit_unit == '/小时' and snapshot.rate.unit_suffix == '/小时'
    assert snapshot.rate.value == pytest.approx(1024.0 * 60)
    assert snapshot.rate.ci == pytest.approx(0.0, abs=1e-6)


def test_short_span_is_insufficient_and_per_minute(db):
    task = _seed_series(db, [100 + i for i in range(11)], interval=1.0)
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.judgement.verdict == Verdict.INSUFFICIENT
    assert snapshot.rate is None and snapshot.fit_unit == '/分'
    assert snapshot.interval_seconds == 1.0


def test_rounds_use_baseline_and_user_markers(db):
    task = _seed_series(db, [float(i) for i in range(60)], markers=[
        (10, "基线", 'baseline'), (20, "第1轮", 'user'), (40, "第2轮", 'user'),
        (30, "暂停", 'pause')])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert [(r.label, r.delta) for r in snapshot.rounds] == [("第1轮", 10.0), ("第2轮", 20.0)]


def test_single_user_marker_gives_no_rounds(db):
    task = _seed_series(db, [float(i) for i in range(60)], markers=[
        (10, "基线", 'baseline'), (20, "第1轮", 'user')])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.rounds == []


def test_user_markers_before_stats_start_are_ignored(db):
    task = _seed_series(db, [float(i) for i in range(60)], markers=[
        (5, "第1轮", 'user'), (8, "第2轮", 'user'), (10, "基线", 'baseline'),
        (30, "第3轮", 'user')])
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.rounds == []


# ---------- 空洞、累计量速率、峰值抬升 ----------

def test_gaps_are_reported_for_display_range(db):
    task = _seed_series(db, [float(i) for i in range(40)], skip=range(10, 20))
    snapshot = run_history_query(db, _request(task.task_id, 'all'))
    assert snapshot.gaps == [(BASE + timedelta(minutes=9), BASE + timedelta(minutes=20))]


def test_cumulative_metric_gets_rate_series_without_judgement(db):
    """累计字节：每秒速率序列（跨空洞的一段不算），平均/峰值速率，不做判定与增速"""
    values = [100.0 * i + (600.0 if i >= 25 else 0.0) for i in range(30)]   # 第 25 分钟突增
    task = _seed_series(db, values, metric=MetricType.IO_READ_BYTES, skip=range(10, 15))
    snapshot = run_history_query(
        db, _request(task.task_id, 'all', metric=MetricType.IO_READ_BYTES))

    assert snapshot.value_class == VALUE_CLASS_RATE
    assert snapshot.judgement is None and snapshot.rate is None
    times = [p.timestamp for p in snapshot.rate_series]
    assert BASE + timedelta(minutes=15) not in times   # 空洞后第一点没有速率
    assert len(snapshot.rate_series) == len(snapshot.buckets) - 2
    assert snapshot.rate_stats.peak_per_second == pytest.approx(700.0 / 60)
    assert snapshot.rate_stats.average_per_second == pytest.approx(3500.0 / (29 * 60))


def test_peak_metric_counts_rises(db):
    values = [100.0] * 10 + [150.0] * 10 + [150.0] * 5 + [180.0] * 5
    task = _seed_series(db, values, metric=MetricType.MEMORY_PEAK_WSET)
    snapshot = run_history_query(
        db, _request(task.task_id, 'all', metric=MetricType.MEMORY_PEAK_WSET))
    assert snapshot.value_class == VALUE_CLASS_PEAK
    assert snapshot.judgement is None and snapshot.rate is None
    assert snapshot.peak_rises == PeakRises(count=2, total=80.0)


# ---------- 失败归类 ----------

def _raise_storage(*a, **k):
    raise StorageError("读取数据点失败: database disk image is malformed")


@pytest.mark.parametrize("repo_attr, method", [
    ("tasks", "get"),
    ("analytics", "last_point_timestamp"),
    ("points", "fetch"),
    ("analytics", "aggregates"),
    ("analytics", "value_near"),
    ("analytics", "gaps"),
    ("analytics", "legacy_buckets"),
    ("markers", "list"),
])
def test_storage_error_becomes_error_field(db, monkeypatch, repo_attr, method):
    task = _seed_task(db, samples=5, markers=[(1, "m")])
    db.markers.add(task.task_id, BASE, "基线", 'baseline')   # 让基线取值也会执行
    _null_last_sample_at(db, task.task_id)   # 让 MAX 回退查询也会执行
    monkeypatch.setattr(getattr(db, repo_attr), method, _raise_storage)
    request = _request(task.task_id, '1h', seq=9)

    snapshot = run_history_query(db, request)

    assert snapshot.storage_error is True
    assert "malformed" in snapshot.error
    assert snapshot.request.request_seq == 9
    assert snapshot.points == [] and not snapshot.ok


def test_programming_error_propagates(db, monkeypatch):
    """非存储故障不吞掉：由后台线程外壳记录堆栈并打包成失败快照"""
    task = _seed_task(db, samples=3)
    monkeypatch.setattr(db.points, 'fetch', lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        run_history_query(db, _request(task.task_id))


def test_until_is_rejected(db):
    task = _seed_task(db, samples=3)
    request = HistoryRequest(
        task_id=task.task_id, metric=MEM, range_key='all', since=None,
        until=BASE, table_limit=10, max_buckets=10, anchor=None, request_seq=1)
    with pytest.raises(ValueError):
        run_history_query(db, request)


def test_snapshot_defaults():
    snapshot = HistorySnapshot(request=_request("t"))
    assert snapshot.points == [] and snapshot.buckets == [] and snapshot.markers == []
    assert snapshot.stats is None and snapshot.task_last_at is None
    assert snapshot.error is None and not snapshot.storage_error and snapshot.ok
    assert snapshot.baseline is None and snapshot.judgement is None
    assert snapshot.gaps == [] and snapshot.rate_series == [] and snapshot.rounds == []
    assert snapshot.total_count == 0 and snapshot.value_class == VALUE_CLASS_LEVEL
    assert snapshot.request.baseline_only is True
