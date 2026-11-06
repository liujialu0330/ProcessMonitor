"""
数据库 schema v3 用例：迁移（v0/v1/v2 -> v3）、新库结构一致性、事务性、
sample_count/last_sample_at 维护、upsert、标记、统计与流式读取等数据层新接口
"""
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

import data.database as database_module
from data.database import Database
from data.db.migrations.runner import MigrationOutcome
from data.db.schema import SCHEMA_VERSION
from data.models import DataPoint, Marker, MonitorTask
from tests.test_migration import (
    _create_v0_db, _get_user_version, patched_migrations, run_migration,
)
from tests.test_migration_v2 import _create_v1_db


def _make_task(**overrides) -> MonitorTask:
    defaults = dict(
        task_id=str(uuid.uuid4()),
        pid=1234,
        process_name="v3.exe",
        metric_types=["memory_rss", "cpu_percent"],
        interval=1.0,
        start_time=datetime(2026, 1, 1),
        end_time=None,
        status="running",
    )
    defaults.update(overrides)
    return MonitorTask(**defaults)


def _points(task_id, base, n, metric="memory_rss", step_s=1.0, start_value=0.0, step_v=1.0):
    return [
        DataPoint(task_id=task_id, timestamp=base + timedelta(seconds=i * step_s),
                  value=start_value + i * step_v, metric_type=metric)
        for i in range(n)
    ]


def _create_v2_db(path: str, tasks: dict, rows: list):
    """构造 v2 库：v2 新库带三个旧索引（idx_data_points_task_id / timestamp / task_metric）"""
    _create_v1_db(path, tasks, rows)
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE INDEX idx_data_points_task_id ON data_points(task_id)')
        conn.execute('CREATE INDEX idx_data_points_timestamp ON data_points(timestamp)')
        conn.execute('PRAGMA user_version = 2')
        conn.commit()
    finally:
        conn.close()


def _schema_signature(path: str):
    """表/列/索引集合的可比较签名（忽略 sqlite 内部表与自动索引）"""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        columns = {
            t: [tuple(r[1:]) for r in conn.execute(f'PRAGMA table_info({t})')]
            for t in tables
        }
        indexes = {}
        for name, tbl in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            indexes[name] = (tbl, [r[2] for r in conn.execute(f'PRAGMA index_info({name})')])
        return tables, columns, indexes
    finally:
        conn.close()


# ========== schema 一致性 ==========

def test_new_db_has_current_structure(db_path):
    Database(db_path)
    tables, columns, indexes = _schema_signature(db_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION == 4
    assert tables == ['data_points', 'task_markers', 'tasks']
    task_cols = [c[0] for c in columns['tasks']]
    assert task_cols[-4:] == ['note', 'sample_count', 'last_sample_at', 'create_time']
    assert [c[0] for c in columns['task_markers']][-1] == 'kind'
    assert indexes == {
        'idx_data_points_task_metric_ts': ('data_points', ['task_id', 'metric_type', 'timestamp']),
        'idx_task_markers_task_ts': ('task_markers', ['task_id', 'timestamp']),
    }


@pytest.mark.parametrize('source', ['v0', 'v1', 'v2'])
def test_migrated_db_matches_new_db_structure(tmp_path, source):
    """v0/v1/v2 迁移得到的库与新建库的表/列/索引集合完全一致"""
    fresh = str(tmp_path / 'fresh.db')
    Database(fresh)

    migrated = str(tmp_path / f'migrated_{source}.db')
    if source == 'v0':
        _create_v0_db(migrated)
    elif source == 'v1':
        _create_v1_db(migrated, {'t1': ['memory_rss']},
                      [('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss')])
    else:
        _create_v2_db(migrated, {'t1': ['memory_rss']},
                      [('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss')])

    Database(migrated)

    assert _get_user_version(migrated) == SCHEMA_VERSION
    assert _schema_signature(migrated) == _schema_signature(fresh)


# ========== v2 -> v3 迁移内容 ==========

def test_v2_to_v3_cleans_backfills_and_reindexes(db_path):
    rows = [
        # t1：首指标 memory_rss；两个 NULL 行应回填为 memory_rss
        ('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss'),
        ('t1', '2026-01-01T00:00:01', 5.0, 'cpu_percent'),
        ('t1', '2026-01-01T00:00:02', 2.0, None),
        ('t1', '2026-01-01T00:00:03', 3.0, None),
        # t2：无数据点
        # 孤儿数据点：任务不存在
        ('ghost', '2026-01-01T00:00:01', 9.0, 'memory_rss'),
    ]
    _create_v2_db(db_path, {'t1': ['memory_rss', 'cpu_percent'], 't2': ['cpu_percent']}, rows)

    db = Database(db_path)

    assert db.migration_failed is False
    assert db.migration_report.outcome is MigrationOutcome.MIGRATED
    assert os.path.exists(db_path + '.bak_v2')
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM data_points WHERE task_id='ghost'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE metric_type IS NULL").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE task_id='t1' AND metric_type='memory_rss'"
        ).fetchone()[0] == 3
        t1 = conn.execute(
            "SELECT note, sample_count, last_sample_at FROM tasks WHERE task_id='t1'").fetchone()
        t2 = conn.execute(
            "SELECT note, sample_count, last_sample_at FROM tasks WHERE task_id='t2'").fetchone()
    finally:
        conn.close()
    assert t1 == ('', 3, '2026-01-01T00:00:03')   # 3 个不同时间戳
    assert t2 == ('', 0, None)
    assert db.get_sample_count('t1') == 3
    assert [t.task_id for t in db.get_tasks_with_data()] == ['t1']


def test_v3_migration_failure_rolls_back_ddl_and_restores(db_path):
    """v3 步骤失败：整条链回滚并从备份还原，库仍为 v2"""
    _create_v2_db(db_path, {'t1': ['memory_rss']},
                  [('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss')])

    def _boom(cursor):
        cursor.execute("ALTER TABLE tasks ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        raise RuntimeError("模拟迁移失败")

    report = run_migration(db_path, patched_migrations(3, _boom))

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert _get_user_version(db_path) == 2
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]
    finally:
        conn.close()
    assert 'note' not in cols


def test_transaction_rolls_back_ddl_and_user_version(db_path):
    """_transaction 内的 DDL 与 PRAGMA user_version 都随失败一起回滚（真正的单事务）"""
    db = Database(db_path)
    with pytest.raises(RuntimeError):
        with db._transaction() as cursor:
            cursor.execute('CREATE TABLE scratch (x INTEGER)')
            cursor.execute('PRAGMA user_version = 99')
            raise RuntimeError("boom")

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='scratch'").fetchone()[0] == 0
        assert conn.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


# ========== save_task upsert ==========

def test_save_task_upsert_keeps_sample_columns(db):
    task = _make_task(note="初始备注")
    assert db.save_task(task) is True
    db.save_data_points(_points(task.task_id, datetime(2026, 1, 1, 0, 0, 1), 3))

    task.status = 'stopped'
    task.end_time = datetime(2026, 1, 1, 1, 0, 0)
    assert db.save_task(task) is True

    loaded = db.get_task(task.task_id)
    assert loaded.status == 'stopped'
    assert loaded.end_time == task.end_time
    assert loaded.note == "初始备注"
    assert loaded.sample_count == 3
    assert loaded.last_sample_at == datetime(2026, 1, 1, 0, 0, 3)


def test_save_task_inserts_note(db):
    task = _make_task(note="备注 A")
    db.save_task(task)
    assert db.get_task(task.task_id).note == "备注 A"


# ========== sample_count / last_sample_at ==========

def test_save_data_points_updates_sample_count_and_last_sample_at(db):
    task = _make_task()
    db.save_task(task)
    t0 = datetime(2026, 1, 1, 0, 0, 0)

    # 同一时间戳两个指标只算一次采样
    db.save_data_points([
        DataPoint(task.task_id, t0, 1.0, "memory_rss"),
        DataPoint(task.task_id, t0, 2.0, "cpu_percent"),
        DataPoint(task.task_id, t0 + timedelta(seconds=1), 1.0, "memory_rss"),
        DataPoint(task.task_id, t0 + timedelta(seconds=1), 2.0, "cpu_percent"),
    ])
    assert db.get_sample_count(task.task_id) == 2
    assert db.get_task(task.task_id).last_sample_at == t0 + timedelta(seconds=1)

    # 单点保存同样更新；较早的时间戳不会拉低 last_sample_at
    assert db.save_data_point(DataPoint(task.task_id, t0 - timedelta(seconds=5), 1.0, "memory_rss"))
    loaded = db.get_task(task.task_id)
    assert loaded.sample_count == 3
    assert loaded.last_sample_at == t0 + timedelta(seconds=1)


def test_failed_save_rolls_back_sample_count(db, monkeypatch):
    """同一事务：写数据点失败时 sample_count 不会被单独更新"""
    task = _make_task()
    db.save_task(task)
    # 触发器让"插入数据点之后的 UPDATE tasks"失败：数据点已插入、计数更新时中途出错
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute("""
            CREATE TRIGGER fail_count BEFORE UPDATE OF sample_count ON tasks
            BEGIN SELECT RAISE(ABORT, '模拟写入中途失败'); END""")
        conn.commit()
    finally:
        conn.close()

    assert db.save_data_points(_points(task.task_id, datetime(2026, 1, 1), 2)) is False

    assert db.get_sample_count(task.task_id) == 0
    assert db.get_data_point_count(task.task_id) == 0


def test_save_data_points_failure_log_throttled(db, monkeypatch, caplog):
    """持续失败：首次记堆栈，之后每 100 次记一条计数"""
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    # 打在连接工厂上：走真实的 PointRepo → StorageError → 门面吞异常的链路
    monkeypatch.setattr(db._factory, 'transaction', _boom)
    point = DataPoint("t", datetime(2026, 1, 1), 1.0, "memory_rss")

    with caplog.at_level(logging.ERROR, logger=database_module.logger.name):
        for _ in range(250):
            assert db.save_data_points([point]) is False

    records = [r for r in caplog.records if r.name == database_module.logger.name]
    assert len(records) == 3   # 第 1、100、200 次
    assert records[0].exc_info is not None
    assert records[1].exc_info is None and "100" in records[1].getMessage()
    assert "200" in records[2].getMessage()


# ========== 任务查询 ==========

def test_get_tasks_with_data_filters_and_orders(db):
    old = _make_task(start_time=datetime(2026, 1, 1))
    new = _make_task(start_time=datetime(2026, 2, 1))
    empty = _make_task(start_time=datetime(2026, 3, 1))
    for t in (old, new, empty):
        db.save_task(t)
    db.save_data_points(_points(old.task_id, datetime(2026, 1, 1), 2))
    db.save_data_points(_points(new.task_id, datetime(2026, 2, 1), 1))

    tasks = db.get_tasks_with_data()

    assert [t.task_id for t in tasks] == [new.task_id, old.task_id]
    assert tasks[0].sample_count == 1 and tasks[1].sample_count == 2


# ========== 删除 ==========

def test_delete_task_removes_markers_too(db):
    task = _make_task()
    db.save_task(task)
    db.save_data_points(_points(task.task_id, datetime(2026, 1, 1), 2))
    db.add_marker(task.task_id, datetime(2026, 1, 1), "m")

    assert db.delete_task(task.task_id) is True

    assert db.get_task(task.task_id) is None
    assert db.get_data_point_count(task.task_id) == 0
    assert db.get_markers(task.task_id) == []


def test_cleanup_old_tasks_removes_markers(db):
    task = _make_task(start_time=datetime(2020, 1, 1), end_time=datetime(2020, 1, 2),
                      status='stopped')
    db.save_task(task)
    db.save_data_points(_points(task.task_id, datetime(2020, 1, 1), 2))
    db.add_marker(task.task_id, datetime(2020, 1, 1), "m")

    assert db.cleanup_old_tasks(30) == 1

    assert db.get_markers(task.task_id) == []
    assert db.get_data_point_count(task.task_id) == 0


# ========== 孤儿校正 ==========

def test_reconcile_orphan_uses_last_sample_at(db):
    sampled = _make_task(status='running')
    unsampled = _make_task(status='running')
    db.save_task(sampled)
    db.save_task(unsampled)
    db.save_data_points(_points(sampled.task_id, datetime(2026, 1, 1, 8, 0, 0), 3))

    before = datetime.now()
    assert db.reconcile_orphan_tasks() == 2

    a = db.get_task(sampled.task_id)
    b = db.get_task(unsampled.task_id)
    assert a.status == 'stopped' and a.end_time == datetime(2026, 1, 1, 8, 0, 2)
    assert b.status == 'stopped' and b.end_time >= before   # 无采样回退为当前时间


# ========== 标记 ==========

def test_markers_roundtrip_ascending_and_since(db):
    task = _make_task()
    db.save_task(task)
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    assert db.add_marker(task.task_id, t0 + timedelta(minutes=2), "后") is True
    assert db.add_marker(task.task_id, t0, "先") is True
    assert db.add_marker("other", t0, "别的任务") is True

    markers = db.get_markers(task.task_id)

    assert [m.label for m in markers] == ["先", "后"]
    assert all(isinstance(m, Marker) and m.id is not None for m in markers)
    assert markers[0].timestamp == t0 and markers[0].task_id == task.task_id

    since = (t0 + timedelta(minutes=1)).isoformat()
    assert [m.label for m in db.get_markers(task.task_id, since_iso=since)] == ["后"]
    # 新接口：markers.list 收 datetime
    assert [m.label for m in db.markers.list(task.task_id, since=t0 + timedelta(minutes=1))]         == ["后"]


# ========== 统计 ==========

def test_metric_stats_extended_fields(db):
    task = _make_task()
    db.save_task(task)
    base = datetime(2026, 1, 1, 0, 0, 0, 123456)
    # 每 30 分钟 +6 => 斜率 12 / 小时
    db.save_data_points(_points(task.task_id, base, 5, step_s=1800, start_value=100.0, step_v=6.0))

    stats = db.get_metric_stats(task.task_id, "memory_rss")

    assert stats['count'] == 5
    assert (stats['min'], stats['max'], stats['avg']) == (100.0, 124.0, 112.0)
    assert stats['first'] == 100.0 and stats['first_ts'] == base
    assert stats['last'] == 124.0 and stats['last_ts'] == base + timedelta(hours=2)
    assert stats['slope_per_hour'] == pytest.approx(12.0, rel=1e-6)


def test_metric_stats_slope_with_since_and_negative(db):
    task = _make_task()
    db.save_task(task)
    base = datetime(2026, 1, 1)
    db.save_data_points(_points(task.task_id, base, 10, step_s=3600, start_value=50.0, step_v=-2.0))

    since = (base + timedelta(hours=5)).isoformat()
    stats = db.get_metric_stats(task.task_id, "memory_rss", since_iso=since)

    assert stats['count'] == 5
    assert stats['first'] == 40.0 and stats['last'] == 32.0
    assert stats['slope_per_hour'] == pytest.approx(-2.0, rel=1e-6)


def test_metric_stats_slope_none_for_single_point_or_zero_span(db):
    task = _make_task()
    db.save_task(task)
    t0 = datetime(2026, 1, 1)
    db.save_data_points([DataPoint(task.task_id, t0, 1.0, "memory_rss")])
    one = db.get_metric_stats(task.task_id, "memory_rss")
    assert one['count'] == 1 and one['slope_per_hour'] is None

    # 同一时间戳两个值：count=2 但时间跨度为 0
    db.save_data_points([DataPoint(task.task_id, t0, 3.0, "memory_rss")])
    same = db.get_metric_stats(task.task_id, "memory_rss")
    assert same['count'] == 2 and same['slope_per_hour'] is None


def test_metric_stats_uses_composite_index(db):
    """first/last 查询应走复合索引（避免全表扫描/临时排序）"""
    conn = sqlite3.connect(db.db_path)
    try:
        plan = ' '.join(str(r) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT timestamp, value FROM data_points "
            "WHERE task_id=? AND metric_type=? ORDER BY timestamp DESC LIMIT 1",
            ('t', 'memory_rss')))
    finally:
        conn.close()
    assert 'idx_data_points_task_metric_ts' in plan
    assert 'TEMP B-TREE' not in plan


# ========== 无 NULL 兜底 ==========

def test_null_metric_type_no_longer_matches_first_metric(db):
    """v3 起不再有 OR metric_type IS NULL 兜底（迁移已回填）"""
    task = _make_task()
    db.save_task(task)
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute(
            "INSERT INTO data_points (task_id, timestamp, value, metric_type) "
            "VALUES (?, '2026-01-01T00:00:00', 1.0, NULL)", (task.task_id,))
        conn.commit()
    finally:
        conn.close()
    assert db.get_task_data_points(task.task_id, metric_type="memory_rss") == []
    assert db.get_metric_stats(task.task_id, "memory_rss") is None


# ========== 流式读取 ==========

def test_iter_data_points_batches_ascending(db):
    task = _make_task()
    db.save_task(task)
    base = datetime(2026, 1, 1)
    pts = _points(task.task_id, base, 7)
    db.save_data_points(list(reversed(pts)))   # 乱序写入，读取必须按时间升序

    batches = list(db.iter_data_points(task.task_id, batch_size=3))

    assert [len(b) for b in batches] == [3, 3, 1]
    flat = [p for b in batches for p in b]
    assert [p.timestamp for p in flat] == [p.timestamp for p in pts]


def test_iter_data_points_early_close_and_empty(db):
    task = _make_task()
    db.save_task(task)
    assert list(db.iter_data_points(task.task_id)) == []

    db.save_data_points(_points(task.task_id, datetime(2026, 1, 1), 10))
    gen = db.iter_data_points(task.task_id, batch_size=2)
    next(gen)
    gen.close()   # 提前终止不应报错，且之后库仍可正常读写
    assert db.get_data_point_count(task.task_id) == 10
    assert db.save_data_points(_points(task.task_id, datetime(2026, 2, 1), 1)) is True
