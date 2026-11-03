"""
v1 -> v2 数据库迁移用例：合并重复内存指标（memory_vms / memory_pagefile -> memory_private）

用裸 sqlite3 构造 v1 库（tasks.metric_type 为 JSON 数组、data_points 带 metric_type 列、
user_version=1），再实例化 Database 触发链式迁移。
"""
import json
import os
import sqlite3

from data.database import Database, SCHEMA_VERSION
from tests.test_migration import _create_v0_db, _get_user_version


def _create_v1_db(path: str, tasks: dict, rows: list):
    """
    构造 v1 库

    Args:
        tasks: {task_id: [指标列表]}
        rows: [(task_id, timestamp, value, metric_type)]
    """
    conn = sqlite3.connect(path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, pid INTEGER NOT NULL,
                process_name TEXT NOT NULL, metric_type TEXT NOT NULL,
                interval REAL NOT NULL, start_time TEXT NOT NULL,
                end_time TEXT, status TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE data_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, value REAL NOT NULL, metric_type TEXT,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            )
        ''')
        cursor.execute(
            'CREATE INDEX idx_data_points_task_metric ON data_points(task_id, metric_type)')
        for task_id, metrics in tasks.items():
            cursor.execute(
                "INSERT INTO tasks VALUES (?, 1111, 'old.exe', ?, 1.0, "
                "'2026-01-01T00:00:00', NULL, 'stopped')",
                (task_id, json.dumps(metrics)))
        cursor.executemany(
            'INSERT INTO data_points (task_id, timestamp, value, metric_type) '
            'VALUES (?, ?, ?, ?)', rows)
        cursor.execute('PRAGMA user_version = 1')
        conn.commit()
    finally:
        conn.close()


def _raw_points(path: str, task_id: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            'SELECT timestamp, metric_type, value FROM data_points '
            'WHERE task_id = ? ORDER BY timestamp, metric_type', (task_id,)).fetchall()
    finally:
        conn.close()


def test_v1_to_v2_merges_duplicate_memory_metrics(db_path):
    """四指标任务迁移后只剩 rss + private，每个时间戳一行 private，数值保留"""
    rows = []
    for i, ts in enumerate(['2026-01-01T00:00:01', '2026-01-01T00:00:02']):
        rows += [
            ('t1', ts, 100.0 + i, 'memory_rss'),
            ('t1', ts, 500.0 + i, 'memory_vms'),
            ('t1', ts, 500.0 + i, 'memory_private'),
            ('t1', ts, 500.0 + i, 'memory_pagefile'),
        ]
    _create_v1_db(
        db_path,
        {'t1': ['memory_rss', 'memory_vms', 'memory_private', 'memory_pagefile']},
        rows)

    db = Database(db_path)

    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert os.path.exists(db_path + '.bak_v1')
    assert db.migration_failed is False
    assert db.get_task('t1').metric_types == ['memory_rss', 'memory_private']
    assert _raw_points(db_path, 't1') == [
        ('2026-01-01T00:00:01', 'memory_private', 500.0),
        ('2026-01-01T00:00:01', 'memory_rss', 100.0),
        ('2026-01-01T00:00:02', 'memory_private', 501.0),
        ('2026-01-01T00:00:02', 'memory_rss', 101.0),
    ]


def test_v1_to_v2_renames_task_with_only_vms(db_path):
    """只含 memory_vms 的任务被改名；NULL metric_type 的旧行随后由 v3 回填为任务首指标，其他任务不动"""
    rows = [
        ('t1', '2026-01-01T00:00:01', 700.0, 'memory_vms'),
        ('t1', '2026-01-01T00:00:02', 701.0, 'memory_vms'),
        ('t1', '2026-01-01T00:00:03', 9.0, None),
        ('t2', '2026-01-01T00:00:01', 3.0, 'cpu_percent'),
    ]
    _create_v1_db(db_path, {'t1': ['memory_vms'], 't2': ['cpu_percent']}, rows)

    db = Database(db_path)

    assert db.get_task('t1').metric_types == ['memory_private']
    assert db.get_task('t2').metric_types == ['cpu_percent']
    assert _raw_points(db_path, 't1') == [
        ('2026-01-01T00:00:01', 'memory_private', 700.0),
        ('2026-01-01T00:00:02', 'memory_private', 701.0),
        ('2026-01-01T00:00:03', 'memory_private', 9.0),
    ]
    assert len(_raw_points(db_path, 't2')) == 1


def test_v0_db_upgrades_all_the_way_to_current(db_path):
    """v0 库一路升到当前版本，备份名按起始版本为 .bak_v0"""
    _create_v0_db(db_path)

    db = Database(db_path)

    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert os.path.exists(db_path + '.bak_v0')
    assert not os.path.exists(db_path + '.bak_v1')
    assert db.get_task('task-1').metric_types == ['memory_rss']
    assert len(db.get_task_data_points('task-1')) == 2


def test_new_db_is_created_at_current_version(db_path):
    """新库直接置为当前版本，不产生备份"""
    Database(db_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert not os.path.exists(db_path + '.bak_v1')


def test_v1_to_v2_failure_rolls_back_and_restores(db_path, monkeypatch):
    """v2 步骤抛异常：整条链回滚并从备份还原，库仍是 v1 且数据未变"""
    rows = [('t1', '2026-01-01T00:00:01', 1.0, 'memory_vms')]
    _create_v1_db(db_path, {'t1': ['memory_vms']}, rows)

    def _boom(cursor):
        cursor.execute("UPDATE tasks SET metric_type = 'x'")  # 先写点东西再失败
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(Database, '_migrate_v1_to_v2', staticmethod(_boom))

    db = Database(db_path)

    assert db.migration_failed is True
    assert _get_user_version(db_path) == 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT metric_type FROM tasks').fetchone()[0] == '["memory_vms"]'
    finally:
        conn.close()
