"""
存储 v5：

- 结构快照（表、列、主键、WITHOUT ROWID、索引）与 meta 四个键；
- data.mappers 的毫秒换算（to_ms / from_ms / ceil_ms / floor_ms）；
- PointRepo：同一毫秒去重（后写覆盖）、精确计数、多序列归并顺序、series 缓存失效；
- 只读模式；meta 的 min_reader 判定；启动校正的导回与孤儿清理；
- 与 v4 逐项一致：同一份数据经冻结就地链落在 v4 库（用冻结的 v4 仓储查询），
  再经重建式迁移落在 v5 库（用当前仓储查询；分析查询另用强制汇总路径再比一遍，覆盖迁移时
  生成的分钟汇总与空洞），各查询结果逐项相同（浮点相对误差 1e-9）。图表降采样 legacy_buckets
  2.0 起按时间等分（有意的行为变化），改为与 tests/test_analytics_rollup_path 的参照实现比较。
  数据用 golden 样本与 tests/history_dataset 的数据集。
"""
import dataclasses
import gzip
import json
import math
import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

from data.database import Database
from data.db import meta as meta_store
from data.db.connection import ConnectionFactory
from data.db.migrations.runner import MigrationOutcome, MigrationRunner
from data.db.schema import SCHEMA_VERSION
from data.errors import StorageError
from data.maintenance import Maintenance
from data.mappers import ceil_ms, floor_ms, from_ms, normalize_ms, to_ms
from data.models import DataPoint, MonitorTask
from data.repos.analytics_repo import AnalyticsRepo
from data.repos.point_repo import PointQuery, PointRepo
from data.repos.task_repo import TaskRepo
from tests import history_dataset
from tests.fixtures.frozen_v4_repos.analytics_repo import AnalyticsRepo as V4AnalyticsRepo
from tests.fixtures.frozen_v4_repos.point_repo import (
    PointQuery as V4PointQuery, PointRepo as V4PointRepo,
)
from tests.fixtures.legacy_db import FIXTURE_DIR
from tests.test_analytics_rollup_path import reference_legacy_buckets
from tests.test_migration_golden import SAMPLES, _ids

BASE = datetime(2026, 3, 1, 9, 0, 0)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _migrate(path, target=SCHEMA_VERSION):
    factory = ConnectionFactory(path, keep_alive=False)
    try:
        return MigrationRunner(factory, target_version=target).migrate()
    finally:
        factory.close()


def _task(task_id='t1', metrics=('memory_rss', 'cpu_percent'), interval=1.0):
    return MonitorTask(task_id=task_id, pid=1, process_name='a.exe',
                       metric_types=list(metrics), interval=interval, start_time=BASE,
                       end_time=None, status='running')


@pytest.fixture
def factory(tmp_path):
    path = str(tmp_path / 'v5.db')
    assert _migrate(path).created
    f = ConnectionFactory(path)
    yield f
    f.close()


def _query(factory, sql, params=()):
    with factory.connect() as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


# ========== 结构快照 ==========

_EXPECTED_COLUMNS = {
    'tasks': [
        'task_id', 'pid', 'process_name', 'metric_type', 'interval', 'start_time', 'end_time',
        'status', 'note', 'sample_count', 'last_sample_at', 'create_time',
        'target_kind', 'target_spec', 'group_id', 'member_key', 'exit_code',
        'resume_on_restart', 'restart_limit', 'plan_json', 'meta_json', 'scenario', 'build',
        'is_reference', 'source_task_id', 'origin', 'planned_cycles', 'done_cycles',
        'missed_cycles', 'lag_p95_ms', 'collect_p95_ms'],
    'series': ['series_id', 'task_id', 'metric'],
    'samples': ['series_id', 'ts_ms', 'value'],
    'rollup_1m': ['series_id', 'bucket_ms', 'count', 'first_ms', 'first_value', 'last_ms',
                  'last_value', 'min_value', 'min_ms', 'max_value', 'max_ms', 'sum_y',
                  'sum_yy', 'sum_x', 'sum_xx', 'sum_xy'],
    'series_gaps': ['series_id', 'start_ms', 'end_ms'],
    'meta': ['key', 'value'],
    'task_members': ['task_id', 'pid', 'create_time', 'name', 'first_ms', 'last_ms'],
    'handle_snapshots': ['task_id', 'ts_ms', 'type_name', 'count'],
    'data_points': ['id', 'task_id', 'timestamp', 'value', 'metric_type'],
    'task_markers': ['id', 'task_id', 'timestamp', 'label', 'created_at', 'kind'],
}
_EXPECTED_PK = {
    'samples': ['series_id', 'ts_ms'], 'rollup_1m': ['series_id', 'bucket_ms'],
    'series_gaps': ['series_id', 'start_ms'], 'task_members': ['task_id', 'pid', 'create_time'],
    'handle_snapshots': ['task_id', 'ts_ms', 'type_name'], 'series': ['series_id'],
    'meta': ['key'], 'tasks': ['task_id'],
}
_WITHOUT_ROWID = {'samples', 'rollup_1m', 'series_gaps', 'task_members', 'handle_snapshots'}


def test_schema_snapshot(factory):
    with factory.connect() as conn:
        tables = {name: sql for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")}
        assert set(tables) == set(_EXPECTED_COLUMNS)
        for table, expected in _EXPECTED_COLUMNS.items():
            info = conn.execute(f'PRAGMA table_info({table})').fetchall()
            assert [r[1] for r in info] == expected, table
            if table in _EXPECTED_PK:
                pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
                assert pk == _EXPECTED_PK[table], table
            assert ('WITHOUT ROWID' in tables[table].upper()) == (table in _WITHOUT_ROWID), table
        # 新列全部有默认值或可空：1.4.1 只写 8 个旧列的 INSERT 仍然合法
        for _cid, name, _type, notnull, default, pk in conn.execute('PRAGMA table_info(tasks)'):
            if name in _EXPECTED_COLUMNS['tasks'][12:]:
                assert not notnull or default is not None, name
        # series 的 (task_id, metric) 唯一
        unique = [r for r in conn.execute('PRAGMA index_list(series)') if r[2]]
        cols = [[c[2] for c in conn.execute(f'PRAGMA index_info({r[1]})')] for r in unique]
        assert ['task_id', 'metric'] in cols
        # data_points 保持 v4 原定义与索引
        assert {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'")} \
            == {'idx_data_points_task_metric_ts', 'idx_task_markers_task_ts'}
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 5


def test_meta_has_four_keys(factory):
    rows = dict(_query(factory, 'SELECT key, value FROM meta'))
    assert set(rows) == {'schema_version', 'created_by_version', 'last_writer_version',
                         'min_reader_version'}
    assert rows['schema_version'] == '5'
    assert rows['min_reader_version'] == meta_store.MIN_READER_VERSION == '2.0.0'


def test_touch_writer_and_read_meta(tmp_path):
    path = str(tmp_path / 'm.db')
    db = Database(path)
    try:
        created = db.read_meta().created_by_version
        db.touch_writer('9.9.9')
        info = db.read_meta()
        assert info.last_writer_version == '9.9.9'
        assert info.created_by_version == created
        assert info.schema_version == 5
    finally:
        db.close()


def test_read_meta_on_legacy_db_returns_none_fields(tmp_path):
    path = str(tmp_path / 'v4.db')
    assert _migrate(path, target=4).created
    conn = sqlite3.connect(path)
    try:
        info = meta_store.read_meta(conn)
    finally:
        conn.close()
    assert info.min_reader_version is None and info.schema_version is None


@pytest.mark.parametrize('min_reader, app, blocked', [
    ('2.0.0', '1.4.1', True),
    ('2.0.0', '2.0.0', False),
    ('2.0.0', '2.0.0-beta.1', False),   # 预发布按其正式版比较
    ('2.0.0', '2.1.0', False),
    ('2.1.0', '2.0.5', True),
    (None, '1.0.0', False),
])
def test_reader_blocked(min_reader, app, blocked):
    info = meta_store.MetaInfo(schema_version=5, created_by_version='2.0.0',
                               last_writer_version='2.0.0', min_reader_version=min_reader)
    # 返回值为阻止原因（中文文案）或 None
    assert bool(meta_store.reader_blocked(info, app)) is blocked


# ========== 毫秒换算 ==========

def test_to_ms_round_trip_and_rounding():
    dt = datetime(2026, 3, 1, 9, 0, 0, 123000)
    assert from_ms(to_ms(dt)) == dt
    assert to_ms(dt.replace(microsecond=123499)) == to_ms(dt)
    assert to_ms(dt.replace(microsecond=123500)) == to_ms(dt) + 1   # 0.5 毫秒进位
    assert to_ms(dt.replace(microsecond=999500)) == to_ms(dt.replace(microsecond=0)) + 1000
    assert normalize_ms(dt.replace(microsecond=123456)) == dt
    assert ceil_ms(dt.replace(microsecond=123001)) == to_ms(dt) + 1
    assert ceil_ms(dt) == floor_ms(dt) == to_ms(dt)
    assert floor_ms(dt.replace(microsecond=123999)) == to_ms(dt)
    for bad in (dt.timestamp(), dt.isoformat(), None):
        with pytest.raises(TypeError):
            to_ms(bad)


def test_to_ms_matches_epoch_arithmetic():
    """整数实现与"按真实时刻"一致：to_ms(dt) == round_half_up(dt.timestamp() * 1000)"""
    for i in range(2000):
        dt = datetime(2024, 1, 1) + timedelta(seconds=3613 * i, microseconds=(i * 9973) % 10 ** 6)
        whole = int(dt.replace(microsecond=0).timestamp())
        if from_ms(whole * 1000) != dt.replace(microsecond=0):
            continue   # 夏令时跳过的本地时刻（不存在，采集不会产生），往返不成立
        assert to_ms(dt) == whole * 1000 + math.floor(dt.microsecond / 1000 + 0.5)
        assert from_ms(to_ms(dt)) == normalize_ms(dt)
        assert abs((normalize_ms(dt) - dt).total_seconds()) <= 0.0005


# ========== PointRepo ==========

def test_append_same_ms_keeps_last_and_counts_once(factory):
    TaskRepo(factory).save(_task())
    repo = PointRepo(factory)
    t = BASE.replace(microsecond=100)
    repo.append([DataPoint('t1', t, 1.0, 'memory_rss'),
                 DataPoint('t1', t.replace(microsecond=400), 2.0, 'memory_rss')])  # 同一毫秒
    repo.append([DataPoint('t1', BASE, 3.0, 'memory_rss'),                          # 再次覆盖
                 DataPoint('t1', BASE, 9.0, 'cpu_percent')])                         # 同刻另一指标
    points = repo.fetch(PointQuery('t1', None))
    assert [(p.timestamp, p.value, p.metric_type) for p in points] == [
        (BASE, 3.0, 'memory_rss'), (BASE, 9.0, 'cpu_percent')]
    task = TaskRepo(factory).get('t1')
    assert (task.sample_count, task.last_sample_at) == (1, BASE)
    assert repo.recount('t1') == 1


def test_append_overlapping_batch_counts_only_new_timestamps(factory):
    TaskRepo(factory).save(_task())
    repo = PointRepo(factory)
    repo.append([DataPoint('t1', BASE + timedelta(seconds=i), 1.0, 'memory_rss')
                 for i in range(5)])
    # 第 3、4 秒已有（另一指标也算已有），第 5、6 秒是新的
    repo.append([DataPoint('t1', BASE + timedelta(seconds=i), 2.0, 'cpu_percent')
                 for i in range(3, 7)])
    task = TaskRepo(factory).get('t1')
    assert task.sample_count == 7 == repo.recount('t1')
    assert task.last_sample_at == BASE + timedelta(seconds=6)


def test_series_created_in_metric_order_and_merge_order(factory):
    """序列按首次写入顺序建立；同一毫秒的多指标按 series_id（即指标列表顺序）排列"""
    TaskRepo(factory).save(_task(metrics=('memory_rss', 'cpu_percent', 'num_handles')))
    repo = PointRepo(factory)
    points = []
    for i in range(7):
        ts = BASE + timedelta(seconds=i // 2, milliseconds=i % 2)
        for m in ('memory_rss', 'cpu_percent', 'num_handles'):
            if (i + len(m)) % 3:
                points.append(DataPoint('t1', ts, float(i), m))
    repo.append(points)
    assert _query(factory, 'SELECT metric FROM series ORDER BY series_id') == \
        [('memory_rss',), ('cpu_percent',), ('num_handles',)]
    order = {'memory_rss': 0, 'cpu_percent': 1, 'num_handles': 2}
    expected = sorted(points, key=lambda p: (p.timestamp, order[p.metric_type]))
    fetched = repo.fetch(PointQuery('t1', None))
    streamed = [p for batch in repo.iter_batches('t1', batch_size=4) for p in batch]
    assert fetched == streamed == expected
    assert all(len(b) <= 4 for b in repo.iter_batches('t1', batch_size=4))
    # limit 取最近 N 条，同一毫秒的边界按 series_id 截断
    assert repo.fetch(PointQuery('t1', None, limit=4)) == expected[-4:]


def test_series_cache_invalidated_when_series_deleted(factory):
    """
    缓存写入方（采集线程的 PointRepo）缓存了 (t1, m) → 1；t1 被删除后序列号 1 被别的任务复用，
    同一个仓储实例再写 t1 时必须重新解析，不能把样本写进别人的序列。
    """
    tasks = TaskRepo(factory)
    writer = PointRepo(factory)
    other = PointRepo(factory)
    tasks.save(_task('t1', metrics=('m',)))
    writer.append([DataPoint('t1', BASE, 1.0, 'm')])
    assert _query(factory, 'SELECT series_id FROM series') == [(1,)]

    assert tasks.delete('t1')
    tasks.save(_task('t2', metrics=('m',)))
    other.append([DataPoint('t2', BASE, 2.0, 'm')])
    assert _query(factory, "SELECT series_id FROM series WHERE task_id = 't2'") == [(1,)]

    tasks.save(_task('t1', metrics=('m',)))
    writer.append([DataPoint('t1', BASE + timedelta(seconds=1), 3.0, 'm')])

    assert [p.value for p in other.fetch(PointQuery('t2', 'm'))] == [2.0]
    assert [p.value for p in writer.fetch(PointQuery('t1', 'm'))] == [3.0]


def test_series_cache_invalidated_by_orphan_purge(factory):
    tasks = TaskRepo(factory)
    writer = PointRepo(factory)
    tasks.save(_task('t1', metrics=('m',)))
    writer.append([DataPoint('t1', BASE, 1.0, 'm')])
    # 模拟 1.4.1 删除任务：只删 tasks
    with factory.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE task_id = 't1'")
    assert Maintenance(factory).purge_orphan_series() == 1
    tasks.save(_task('t2', metrics=('m',)))
    PointRepo(factory).append([DataPoint('t2', BASE, 2.0, 'm')])
    tasks.save(_task('t1', metrics=('m',)))
    writer.append([DataPoint('t1', BASE, 3.0, 'm')])
    assert [p.value for p in writer.fetch(PointQuery('t2', 'm'))] == [2.0]


def test_delete_task_removes_rows_in_every_v5_table(factory):
    tasks = TaskRepo(factory)
    tasks.save(_task('t1', metrics=('m',)))
    tasks.save(_task('keep', metrics=('m',)))
    PointRepo(factory).append([DataPoint(t, BASE, 1.0, 'm') for t in ('t1', 'keep')])
    with factory.connect() as conn:
        for task_id in ('t1', 'keep'):
            sid = conn.execute('SELECT series_id FROM series WHERE task_id = ?',
                               (task_id,)).fetchone()[0]
            conn.execute('INSERT INTO rollup_1m VALUES (?, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 0)',
                         (sid,))
            conn.execute('INSERT INTO series_gaps VALUES (?, 0, 1)', (sid,))
            conn.execute("INSERT INTO task_members VALUES (?, 1, 0.0, 'a', 0, 1)", (task_id,))
            conn.execute("INSERT INTO handle_snapshots VALUES (?, 0, 'File', 3)", (task_id,))
            conn.execute("INSERT INTO data_points (task_id, timestamp, value) "
                         "VALUES (?, '2026-01-01T00:00:00', 1.0)", (task_id,))
    assert tasks.delete('t1')
    for table in ('series_gaps', 'samples', 'series', 'task_members',
                  'handle_snapshots', 'data_points'):
        assert _query(factory, f'SELECT COUNT(*) FROM {table}') == [(1,)], table
    # keep 剩两行汇总：append 写入时维护的一行 + 上面手工插入的一行；t1 的都已删除
    assert _query(factory, 'SELECT COUNT(*) FROM rollup_1m') == [(2,)]
    assert _query(factory, 'SELECT COUNT(*) FROM rollup_1m WHERE series_id NOT IN '
                           '(SELECT series_id FROM series)') == [(0,)]


# ========== 只读模式 ==========

def test_read_only_database_reads_but_never_writes(tmp_path):
    path = str(tmp_path / 'ro.db')
    db = Database(path)
    try:
        db.save_task(_task('t1', metrics=('m',)))
        db.save_data_points([DataPoint('t1', BASE, 1.0, 'm')])
    finally:
        db.close()
    with open(path, 'rb') as f:
        before = f.read()

    ro = Database(path, read_only=True)
    try:
        assert ro.migration_report is None or not ro.migration_report.created
        assert [p.value for p in ro.get_task_data_points('t1', 'm')] == [1.0]
        assert ro.save_data_points([DataPoint('t1', BASE + timedelta(seconds=1), 2.0, 'm')]) \
            is False
        with pytest.raises(StorageError):
            with ro._factory.transaction():
                pass
        with pytest.raises(StorageError):
            ro.points.append([DataPoint('t1', BASE + timedelta(seconds=2), 3.0, 'm')])
    finally:
        ro.close()
    with open(path, 'rb') as f:
        assert f.read() == before


def test_read_only_factory_query_only(tmp_path):
    path = str(tmp_path / 'ro.db')
    assert _migrate(path).created
    f = ConnectionFactory(path, read_only=True)
    try:
        with pytest.raises(StorageError):
            with f.transaction():
                pass
        with pytest.raises(sqlite3.Error):
            with f.connect() as conn:
                conn.execute("INSERT INTO meta VALUES ('x', 'y')")
    finally:
        f.close()


# ========== 启动校正：导回与孤儿清理 ==========

def test_import_legacy_points_merges_fills_and_recounts(factory):
    tasks = TaskRepo(factory)
    tasks.save(_task('t1', metrics=('memory_rss', 'memory_private')))
    PointRepo(factory).append([DataPoint('t1', BASE, 1.0, 'memory_rss')])
    with factory.connect() as conn:
        # 1.4.1 建的任务带旧内存键
        conn.execute("INSERT INTO tasks (task_id, pid, process_name, metric_type, interval, "
                     "start_time, end_time, status) VALUES ('old', 1, 'o.exe', "
                     "'[\"memory_vms\", \"num_handles\"]', 1.0, '2026-03-01T09:00:00', NULL, "
                     "'stopped')")
        conn.executemany(
            'INSERT INTO data_points (task_id, timestamp, value, metric_type) VALUES (?,?,?,?)', [
                ('t1', BASE.isoformat(), 5.0, 'memory_rss'),               # 覆盖已有样本
                ('t1', (BASE + timedelta(seconds=1)).isoformat(), 6.0, None),  # 回填首指标
                ('old', BASE.isoformat(), 7.0, 'memory_vms'),              # 同刻三项内存：
                ('old', BASE.isoformat(), 8.0, 'memory_pagefile'),         # 只留 id 最小的
                ('old', BASE.isoformat(), 9.0, 'memory_private'),
                ('old', BASE.isoformat(), 10.0, 'num_handles'),
                ('ghost', BASE.isoformat(), 11.0, 'memory_rss'),           # 孤儿：丢弃
            ])
    assert Maintenance(factory).import_legacy_points() == 7
    assert _query(factory, 'SELECT COUNT(*) FROM data_points') == [(0,)]
    assert tasks.get('old').metric_types == ['memory_private', 'num_handles']
    repo = PointRepo(factory)
    assert [(p.value, p.metric_type) for p in repo.fetch(PointQuery('t1', None))] == [
        (5.0, 'memory_rss'), (6.0, 'memory_rss')]
    assert [(p.value, p.metric_type) for p in repo.fetch(PointQuery('old', None))] == [
        (7.0, 'memory_private'), (10.0, 'num_handles')]
    assert (tasks.get('t1').sample_count, tasks.get('old').sample_count) == (2, 1)
    assert tasks.get('old').last_sample_at == BASE
    assert _query(factory, "SELECT COUNT(*) FROM series WHERE task_id = 'ghost'") == [(0,)]
    # 再次调用：无事可做
    assert Maintenance(factory).import_legacy_points() == 0


def test_purge_orphan_series_removes_all_orphan_rows(factory):
    tasks = TaskRepo(factory)
    tasks.save(_task('keep', metrics=('m',)))
    tasks.save(_task('gone', metrics=('m', 'n')))
    PointRepo(factory).append([DataPoint(t, BASE, 1.0, m)
                               for t, m in (('keep', 'm'), ('gone', 'm'), ('gone', 'n'))])
    with factory.connect() as conn:
        sid = conn.execute("SELECT series_id FROM series WHERE task_id = 'gone' AND metric = 'm'"
                           ).fetchone()[0]
        conn.execute('INSERT INTO rollup_1m VALUES (?, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 0)',
                     (sid,))
        conn.execute('INSERT INTO series_gaps VALUES (?, 0, 1)', (sid,))
        conn.execute("INSERT INTO task_members VALUES ('gone', 1, 0.0, 'a', 0, 1)")
        conn.execute("INSERT INTO handle_snapshots VALUES ('gone', 0, 'File', 3)")
        conn.execute("INSERT INTO task_markers (task_id, timestamp, label, created_at) "
                     "VALUES ('gone', '2026-01-01T00:00:00', 'x', '2026-01-01T00:00:00')")
        conn.execute("DELETE FROM tasks WHERE task_id = 'gone'")   # 1.4.1 的删除
    assert Maintenance(factory).purge_orphan_series() == 2
    for table in ('series_gaps', 'task_members', 'handle_snapshots', 'task_markers'):
        assert _query(factory, f'SELECT COUNT(*) FROM {table}') == [(0,)], table
    # 只剩 keep 写入时维护的那一行汇总
    assert _query(factory, 'SELECT series_id FROM rollup_1m') == _query(
        factory, "SELECT series_id FROM series WHERE task_id = 'keep'")
    assert _query(factory, 'SELECT task_id FROM series') == [('keep',)]
    assert _query(factory, 'SELECT COUNT(*) FROM samples') == [(1,)]
    assert Maintenance(factory).purge_orphan_series() == 0


# ========== 分层：ts_ms 不出仓储层 ==========

def test_ts_ms_stays_inside_data_layer():
    offenders = []
    for top in ('core', 'ui', 'app'):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if name.endswith('.py'):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding='utf-8') as f:
                        if re.search(r'\bts_ms\b', f.read()):
                            offenders.append(os.path.relpath(path, ROOT))
    for name in ('main.py', 'config.py', 'app_config.py'):
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                if re.search(r'\bts_ms\b', f.read()):
                    offenders.append(name)
    assert offenders == []


# ========== 与 v4 逐项一致 ==========

def _close(a, b):
    if dataclasses.is_dataclass(a) and dataclasses.is_dataclass(b):
        return _close(dataclasses.asdict(a), dataclasses.asdict(b))
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    return a == b


def _bounds(stamps):
    """一组 (since, until) 组合：含不限、首末点、中点及其 ±半毫秒、范围外"""
    first, last = stamps[0], stamps[-1]
    mid = stamps[len(stamps) // 2]
    half = timedelta(microseconds=500)
    return [(None, None), (first, None), (None, last), (mid, None), (None, mid),
            (mid + half, None), (None, mid - half), (mid - half, mid + half),
            (first, mid), (mid, last + timedelta(seconds=1)),
            (last + timedelta(seconds=1), None), (None, first - timedelta(seconds=1))]


def _assert_same_results(v4_path, v5_path, task_id, metrics, interval):
    f4, f5 = ConnectionFactory(v4_path), ConnectionFactory(v5_path)
    try:
        a4, a5 = V4AnalyticsRepo(f4), AnalyticsRepo(f5)
        # 强制汇总路径：覆盖迁移时生成的分钟汇总与空洞
        a5r = AnalyticsRepo(f5, path='rollup')
        p4, p5 = V4PointRepo(f4), PointRepo(f5)

        def same(name, fn4, fn5):
            r4, r5 = fn4(), fn5()
            assert _close(r4, r5), (name, r4, r5)

        def same_analytics(name, call):
            """同一个分析查询在 v4 仓储、v5 自动路径、v5 强制汇总路径上的结果逐项相同"""
            r4 = call(a4)
            for label, repo in (('auto', a5), ('rollup', a5r)):
                r5 = call(repo)
                assert _close(r4, r5), (label, name, r4, r5)

        all4 = p4.fetch(V4PointQuery(task_id, None))
        all5 = p5.fetch(PointQuery(task_id, None))
        assert all4 == all5
        if not all4:
            same_analytics('point_count', lambda a: a.point_count(task_id))
            return
        stamps = sorted({p.timestamp for p in all4})
        bounds = _bounds(stamps)

        assert [p for b in p4.iter_batches(task_id, 7) for p in b] ==             [p for b in p5.iter_batches(task_id, 7) for p in b]
        for name in ('point_count', 'sample_count', 'last_point_timestamp'):
            same_analytics(name, lambda a: getattr(a, name)(task_id))
        for since, until in bounds:
            for limit in (None, 1, 7):
                for metric in (None, *metrics):
                    same(('fetch', metric, since, until, limit),
                         lambda: p4.fetch(V4PointQuery(task_id, metric, since, until, limit)),
                         lambda: p5.fetch(PointQuery(task_id, metric, since, until, limit)))
            same_analytics(('gaps', since, until),
                           lambda a: a.gaps(task_id, interval, since, until))
        for metric in metrics:
            same_analytics(('point_count', metric), lambda a: a.point_count(task_id, metric))
            for since, until in bounds:
                same_analytics(('aggregates', metric, since, until),
                               lambda a: a.aggregates(task_id, metric, since, until))
                same_analytics(('gaps', metric, since, until),
                               lambda a: a.gaps(task_id, interval, since, until, metric=metric))
                for max_buckets, iv in ((1, None), (7, interval), (200, None), (200, interval)):
                    same_analytics(
                        ('buckets', metric, since, until, max_buckets, iv),
                        lambda a: a.buckets(task_id, metric, max_buckets, since, until, iv))
            for since, _until in bounds:
                same_analytics(('legacy_stats', metric, since),
                               lambda a: a.legacy_stats(task_id, metric, since))
            for ts in (stamps[0], stamps[len(stamps) // 2],
                       stamps[len(stamps) // 2] + timedelta(microseconds=500),
                       stamps[-1] + timedelta(seconds=10), stamps[0] - timedelta(days=1)):
                for n in (1, 6):
                    for after_only in (False, True):
                        same_analytics(('value_near', metric, ts, n, after_only),
                                       lambda a: a.value_near(task_id, metric, ts, n, after_only))
        # 图表降采样：2.0 起按时间等分（1.x 的 v4 仓储按行号等分，不再可比），两条路径都等于参照实现
        for metric in (None, *metrics):
            for since, _until in bounds[:6]:
                for max_points in (1, 7, 64, 2000):
                    expected = reference_legacy_buckets(all5, task_id, metric, max_points, since)
                    for repo in (a5, a5r):
                        assert repo.legacy_buckets(task_id, metric, max_points, since) ==                             expected, ('legacy_buckets', metric, since, max_points)
    finally:
        f4.close()
        f5.close()


def _task_metrics(path):
    conn = sqlite3.connect(path)
    try:
        return [(task_id, json.loads(metrics), interval) for task_id, metrics, interval in
                conn.execute('SELECT task_id, metric_type, interval FROM tasks ORDER BY task_id')]
    finally:
        conn.close()


# 六份真实旧库样本逐项对拍，每份 4~6 秒，标 slow（CI 完整集运行）；同一套 _assert_same_results 在默认集里
# 由 test_history_dataset_queries_match_v4（合成数据集）守住，样本迁移保真由 test_migration_golden 守住
@pytest.mark.slow
@pytest.mark.parametrize('sample', [pytest.param(s, id=_ids(s)) for s in SAMPLES])
def test_golden_queries_match_v4(sample, tmp_path):
    for name in ('v4', 'v5'):
        (tmp_path / name).mkdir()
        with gzip.open(os.path.join(FIXTURE_DIR, sample['file']), 'rb') as src, \
                open(tmp_path / name / 'monitor.db', 'wb') as dst:
            shutil.copyfileobj(src, dst)
    v4_path = str(tmp_path / 'v4' / 'monitor.db')
    v5_path = str(tmp_path / 'v5' / 'monitor.db')
    if sample['schema_version'] < 4:
        assert _migrate(v4_path, target=4).outcome is MigrationOutcome.MIGRATED
    assert _migrate(v5_path).outcome is MigrationOutcome.MIGRATED

    tasks = _task_metrics(v4_path)
    assert [t for t, _m, _i in tasks] == [t for t, _m, _i in _task_metrics(v5_path)]
    for task_id, metrics, interval in tasks:
        _assert_same_results(v4_path, v5_path, task_id, metrics, interval)


class _Recorder:
    """history_dataset 的造数函数只调用这几个门面方法：记录下来，分别写进 v4 与 v5 库"""

    def __init__(self):
        self.tasks, self.batches, self.marks = [], [], []
        self.markers = self

    def save_task(self, task):
        self.tasks.append(task)
        return True

    def save_data_points(self, points):
        self.batches.append(list(points))
        return True

    def add_marker(self, task_id, ts, label):
        self.marks.append((task_id, ts, label, 'user'))
        return True

    def add(self, task_id, ts, label, kind='user'):
        self.marks.append((task_id, ts, label, kind))
        return True


def _write_v4(path, rec):
    assert _migrate(path, target=4).created
    conn = sqlite3.connect(path)
    try:
        for t in rec.tasks:
            conn.execute(
                'INSERT INTO tasks (task_id, pid, process_name, metric_type, interval, '
                'start_time, end_time, status, note) VALUES (?,?,?,?,?,?,?,?,?)',
                (t.task_id, t.pid, t.process_name, json.dumps(t.metric_types), t.interval,
                 t.start_time.isoformat(), t.end_time.isoformat() if t.end_time else None,
                 str(t.status), t.note))
        conn.executemany(
            'INSERT INTO task_markers (task_id, timestamp, label, created_at, kind) '
            'VALUES (?, ?, ?, ?, ?)',
            [(tid, ts.isoformat(), label, ts.isoformat(), kind)
             for tid, ts, label, kind in rec.marks])
        conn.commit()
    finally:
        conn.close()
    factory = ConnectionFactory(path)
    try:
        repo = V4PointRepo(factory)
        for batch in rec.batches:
            repo.append(batch)
    finally:
        factory.close()


_HISTORY_SEEDS = {
    'query_dataset': history_dataset._seed_query_dataset,
    'baseline_dataset': history_dataset._seed_baseline_dataset,
    'simple_with_markers': lambda db: history_dataset._seed_task(
        db, samples=40, markers=((3, '一'), (20, '二')),
        metrics=[history_dataset.MEM, history_dataset.THREADS]),
}


@pytest.mark.parametrize('name', list(_HISTORY_SEEDS))
def test_history_dataset_queries_match_v4(name, tmp_path):
    rec = _Recorder()
    _HISTORY_SEEDS[name](rec)
    v4_path = str(tmp_path / 'v4.db')
    _write_v4(v4_path, rec)
    v5_path = str(tmp_path / 'v5.db')
    shutil.copyfile(v4_path, v5_path)
    report = _migrate(v5_path)
    assert report.outcome is MigrationOutcome.MIGRATED and report.from_version == 4

    # 直接用当前门面写入的 v5 库，与迁移得到的 v5 库样本、序列、计数完全相同
    direct = str(tmp_path / 'direct.db')
    db = Database(direct)
    try:
        for t in rec.tasks:
            db.save_task(t)
        for batch in rec.batches:
            assert db.save_data_points(batch)
    finally:
        db.close()
    for sql in ('SELECT * FROM series ORDER BY series_id',
                'SELECT * FROM samples ORDER BY series_id, ts_ms',
                'SELECT task_id, sample_count, last_sample_at FROM tasks ORDER BY task_id'):
        conn_a, conn_b = sqlite3.connect(direct), sqlite3.connect(v5_path)
        try:
            assert conn_a.execute(sql).fetchall() == conn_b.execute(sql).fetchall(), sql
        finally:
            conn_a.close()
            conn_b.close()

    for t in rec.tasks:
        _assert_same_results(v4_path, v5_path, t.task_id, list(t.metric_types), t.interval)
