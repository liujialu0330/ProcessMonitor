"""
PointRepo：append 维护 sample_count/last_sample_at（同事务、失败整体回滚）、
PointQuery 的 metric/since/until/limit、流式分批、recount/recount_all、
datetime 强校验与 StorageError 包装
"""
import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

from data.errors import StorageError
from data.mappers import to_ms
from data.models import DataPoint, MonitorTask
from data.repos.point_repo import PointQuery, PointRepo
from data.repos.task_repo import TaskRepo
from tests.helpers import ConnectionFactoryStub, make_broken_factories, make_v4_db

BASE = datetime(2026, 3, 1, 9, 0, 0)


@pytest.fixture
def factory(tmp_path):
    return ConnectionFactoryStub(make_v4_db(str(tmp_path / 'repo.db')))


@pytest.fixture
def repo(factory):
    return PointRepo(factory)


@pytest.fixture
def task(factory):
    t = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name='a.exe',
                    metric_types=['memory_rss', 'cpu_percent'], interval=1.0,
                    start_time=BASE, end_time=None, status='running')
    TaskRepo(factory).save(t)
    return t


def _points(task_id, n, start=BASE, metrics=('memory_rss',), step=1):
    return [DataPoint(task_id, start + timedelta(seconds=i * step), float(i), m)
            for i in range(n) for m in metrics]


def _counts(factory, task_id):
    task = TaskRepo(factory).get(task_id)
    return task.sample_count, task.last_sample_at


def _raw(factory, sql, params=()):
    with factory.connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


# ---------- append ----------

def test_append_counts_distinct_timestamps_once(repo, factory, task):
    repo.append(_points(task.task_id, 3, metrics=('memory_rss', 'cpu_percent')))
    assert _counts(factory, task.task_id) == (3, BASE + timedelta(seconds=2))
    assert _raw(factory, 'SELECT COUNT(*) FROM samples') == 6


def test_append_accumulates_and_never_moves_last_sample_back(repo, factory, task):
    repo.append(_points(task.task_id, 2, start=BASE + timedelta(minutes=10)))
    repo.append(_points(task.task_id, 3, start=BASE))   # 补写更早的数据
    assert _counts(factory, task.task_id) == (5, BASE + timedelta(minutes=10, seconds=1))


def test_append_multiple_tasks_in_one_batch(repo, factory, task):
    other = MonitorTask(task_id='other', pid=2, process_name='b.exe', metric_types=['m'],
                        interval=1.0, start_time=BASE, end_time=None, status='running')
    TaskRepo(factory).save(other)
    repo.append(_points(task.task_id, 2) + _points('other', 4, start=BASE + timedelta(hours=1)))
    assert _counts(factory, task.task_id) == (2, BASE + timedelta(seconds=1))
    assert _counts(factory, 'other') == (4, BASE + timedelta(hours=1, seconds=3))


def test_append_empty_is_noop_without_touching_db(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        PointRepo(factory).append([])


def test_append_rejects_float_timestamp_and_writes_nothing(repo, factory, task):
    bad = _points(task.task_id, 2)
    bad.append(DataPoint(task.task_id, BASE.timestamp(), 1.0, 'memory_rss'))
    with pytest.raises(TypeError):
        repo.append(bad)
    assert _raw(factory, 'SELECT COUNT(*) FROM data_points') == 0
    assert _counts(factory, task.task_id) == (0, None)


def test_append_failure_rolls_back_points_and_counts(repo, factory, task):
    batch = _points(task.task_id, 3)
    batch.append(DataPoint(task.task_id, BASE + timedelta(seconds=9), None, 'memory_rss'))
    with pytest.raises(StorageError) as info:
        repo.append(batch)
    assert isinstance(info.value.__cause__, sqlite3.IntegrityError)
    assert _raw(factory, 'SELECT COUNT(*) FROM data_points') == 0
    assert _counts(factory, task.task_id) == (0, None)


# ---------- fetch / PointQuery ----------

@pytest.fixture
def seeded(repo, task):
    # 0..9 秒，每秒两个指标
    repo.append(_points(task.task_id, 10, metrics=('memory_rss', 'cpu_percent')))
    return task.task_id


def test_fetch_metric_filter_and_order(repo, seeded):
    pts = repo.fetch(PointQuery(seeded, 'memory_rss'))
    assert [p.value for p in pts] == [float(i) for i in range(10)]
    assert {p.metric_type for p in pts} == {'memory_rss'}
    assert len(repo.fetch(PointQuery(seeded, None))) == 20
    assert repo.fetch(PointQuery(seeded, 'no_such_metric')) == []
    assert repo.fetch(PointQuery('no-such-task', None)) == []


def test_fetch_since_until_inclusive(repo, seeded):
    since, until = BASE + timedelta(seconds=3), BASE + timedelta(seconds=6)
    assert [p.value for p in repo.fetch(PointQuery(seeded, 'memory_rss', since=since))] == [
        3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    assert [p.value for p in repo.fetch(PointQuery(seeded, 'memory_rss', until=until))] == [
        0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert [p.value for p in repo.fetch(
        PointQuery(seeded, 'memory_rss', since=since, until=until))] == [3.0, 4.0, 5.0, 6.0]
    # 亚秒精度的边界：until 落在两点之间
    assert [p.value for p in repo.fetch(PointQuery(
        seeded, 'memory_rss', until=BASE + timedelta(seconds=1, microseconds=500000)))] == [
        0.0, 1.0]


def test_fetch_limit_is_most_recent_ascending(repo, seeded):
    assert [p.value for p in repo.fetch(PointQuery(seeded, 'memory_rss', limit=3))] == [
        7.0, 8.0, 9.0]
    assert [p.value for p in repo.fetch(PointQuery(
        seeded, 'memory_rss', until=BASE + timedelta(seconds=5), limit=2))] == [4.0, 5.0]


def test_point_query_validates_arguments():
    with pytest.raises(TypeError):
        PointQuery('t', None, since=BASE.timestamp())
    with pytest.raises(TypeError):
        PointQuery('t', None, until=int(BASE.timestamp()))
    with pytest.raises(TypeError):
        PointQuery('t', None, since=BASE.isoformat())
    for bad_limit in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            PointQuery('t', None, limit=bad_limit)


# ---------- iter_batches ----------

def test_iter_batches(repo, seeded):
    batches = list(repo.iter_batches(seeded, batch_size=6))
    assert [len(b) for b in batches] == [6, 6, 6, 2]
    flat = [p for b in batches for p in b]
    assert [p.timestamp for p in flat] == sorted(p.timestamp for p in flat)
    assert list(repo.iter_batches('no-such-task')) == []


def test_iter_batches_early_close_releases_connection(repo, factory, seeded):
    gen = repo.iter_batches(seeded, batch_size=2)
    next(gen)
    gen.close()
    # 连接已释放：写事务可以立即拿到锁
    repo.append(_points(seeded, 1, start=BASE + timedelta(hours=1)))
    assert _raw(factory, 'SELECT COUNT(*) FROM samples') == 21


def test_iter_batches_rejects_bad_batch_size_immediately(repo):
    with pytest.raises(ValueError):
        repo.iter_batches('t', batch_size=0)


# ---------- recount ----------

def test_recount_fixes_drifted_counts(repo, factory, seeded):
    with factory.connect() as conn:
        conn.execute("UPDATE tasks SET sample_count = 999, last_sample_at = '2000-01-01T00:00:00'")
    assert repo.recount(seeded) == 10
    assert _counts(factory, seeded) == (10, BASE + timedelta(seconds=9))


def test_recount_without_points_resets(repo, factory, task):
    with factory.connect() as conn:
        conn.execute("UPDATE tasks SET sample_count = 5, last_sample_at = '2026-01-01T00:00:00'")
    assert repo.recount(task.task_id) == 0
    assert _counts(factory, task.task_id) == (0, None)
    assert repo.recount('no-such-task') == 0


def test_recount_all_only_touches_zero_count_tasks_with_points(repo, factory, task):
    tasks = TaskRepo(factory)
    healthy = MonitorTask('healthy', 1, 'h', ['m'], 1.0, BASE, None, 'stopped')
    empty = MonitorTask('empty', 1, 'e', ['m'], 1.0, BASE, None, 'stopped')
    tasks.save(healthy)
    tasks.save(empty)
    repo.append(_points('healthy', 3))
    # 模拟"样本在、计数为 0"（如 1.4.1 重存任务清零了计数）：直接插样本，不维护计数
    with factory.connect() as conn:
        sids = {}
        for m in ('memory_rss', 'cpu_percent'):
            conn.execute('INSERT INTO series (task_id, metric) VALUES (?, ?)', (task.task_id, m))
            sids[m] = conn.execute('SELECT series_id FROM series WHERE task_id = ? AND metric = ?',
                                   (task.task_id, m)).fetchone()[0]
        conn.executemany(
            'INSERT INTO samples (series_id, ts_ms, value) VALUES (?, ?, ?)',
            [(sids[m], to_ms(BASE + timedelta(seconds=i)), 1.0)
             for i in range(4) for m in ('memory_rss', 'cpu_percent')])

    assert repo.recount_all() == 1
    assert _counts(factory, task.task_id) == (4, BASE + timedelta(seconds=3))
    assert _counts(factory, 'healthy') == (3, BASE + timedelta(seconds=2))
    assert _counts(factory, 'empty') == (0, None)
    assert repo.recount_all() == 0


# ---------- 错误包装 ----------

def test_all_methods_wrap_sqlite_errors(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        repo = PointRepo(factory)
        calls = [
            lambda: repo.append(_points('t', 1)),
            lambda: repo.fetch(PointQuery('t', None)),
            lambda: repo.fetch(PointQuery('t', 'm', since=BASE, until=BASE, limit=5)),
            lambda: next(repo.iter_batches('t')),
            lambda: repo.recount('t'),
            lambda: repo.recount_all(),
        ]
        for call in calls:
            with pytest.raises(StorageError) as info:
                call()
            assert isinstance(info.value.__cause__, sqlite3.Error)
