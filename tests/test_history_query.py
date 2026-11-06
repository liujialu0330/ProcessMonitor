"""
历史查询（core.history_query）：请求组装、锚点与起点口径、查询编排、失败归类。

起点/锚点的期望值取自旧页面 HistoryPage._compute_since 的实际输出（锚点为任务最后
一次采样 BASE+4s；last_sample_at 为空时回退 MAX(timestamp) 结果相同；任务无数据点
或选"全部"/未知范围时不过滤），写死在 SINCE_CASES 里逐一对照。
"""
from datetime import timedelta

import pytest

from core.history_query import (
    CHART_MAX_BUCKETS, DEFAULT_RANGE_KEY, TABLE_POINT_LIMIT, TIME_RANGE_SECONDS,
    HistoryRequest, HistorySnapshot, build_request, compute_since, run_history_query,
)
from data.errors import StorageError
from tests.test_history_page_flow import BASE, _seed_task
from tests.test_safety_net_history import (
    BASE as NET_BASE, MAX_BUCKETS as NET_BUCKETS, QUERY_CASES,
    TABLE_LIMIT as NET_LIMIT, _seed_query_dataset,
)
from utils.metrics import MetricType

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
    assert (TABLE_POINT_LIMIT, CHART_MAX_BUCKETS) == (2000, 2000)


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
    calls = []

    def wrap(repo, name, label, extract):
        real = getattr(repo, name)

        def spy(*a, **k):
            calls.append((label, *extract(a, k)))
            return real(*a, **k)
        monkeypatch.setattr(repo, name, spy)

    wrap(db.tasks, 'get', 'task', lambda a, k: ())
    wrap(db.points, 'fetch', 'table', lambda a, k: (a[0].since, a[0].limit))
    wrap(db.analytics, 'legacy_stats', 'stats', lambda a, k: (k.get('since'),))
    wrap(db.analytics, 'legacy_buckets', 'buckets', lambda a, k: (k.get('since'), a[2]))
    wrap(db.markers, 'list', 'markers', lambda a, k: (k.get('since'),))
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
    assert calls == [('task',), ('table', since, NET_LIMIT), ('stats', since),
                     ('buckets', since, NET_BUCKETS), ('markers', since)]
    assert len(snapshot.points) == rows
    assert snapshot.stats['count'] == count
    assert len(snapshot.buckets) == buckets
    assert len(snapshot.markers) == markers
    assert snapshot.task_last_at == NET_BASE + timedelta(minutes=120)


def test_empty_range_only_queries_table(db, monkeypatch):
    """线程数只采到 60 分钟，锚点在 120 分钟：选 10 分钟时范围内无数据，只查明细"""
    task = _seed_query_dataset(db)
    calls = _record(db, monkeypatch)

    snapshot = run_history_query(db, build_request(
        task.task_id, MetricType.NUM_THREADS, '10m', table_limit=NET_LIMIT))

    since = NET_BASE + timedelta(minutes=110)
    assert calls == [('task',), ('table', since, NET_LIMIT)]
    assert snapshot.points == [] and snapshot.stats is None
    assert snapshot.buckets == [] and snapshot.markers == []
    assert snapshot.ok


def test_no_metric_skips_stats(db):
    task = _seed_task(db, samples=3)
    snapshot = run_history_query(db, build_request(task.task_id, None, 'all'))
    assert len(snapshot.points) == 3 and snapshot.stats is None


def test_missing_task_stops_before_data_queries(db, monkeypatch):
    calls = _record(db, monkeypatch)
    request = _request("no-such-task", '1h', seq=3)
    snapshot = run_history_query(db, request)
    assert calls == [('task',)]
    assert snapshot.task_missing is True and not snapshot.ok
    assert snapshot.request == request and snapshot.error is None


# ---------- 失败归类 ----------

def _raise_storage(*a, **k):
    raise StorageError("读取数据点失败: database disk image is malformed")


@pytest.mark.parametrize("repo_attr, method", [
    ("tasks", "get"),
    ("analytics", "last_point_timestamp"),
    ("points", "fetch"),
    ("analytics", "legacy_stats"),
    ("analytics", "legacy_buckets"),
    ("markers", "list"),
])
def test_storage_error_becomes_error_field(db, monkeypatch, repo_attr, method):
    task = _seed_task(db, samples=5, markers=[(1, "m")])
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
