"""
数据库 schema v4 用例：tasks.create_time、task_markers.kind 两列的新建与迁移，
迁移链结构一致性，以及 parse_metric_types（冻结版与运行时 mappers 版）对空 JSON 数组的修正
"""
import json
import os
import sqlite3

import pytest

from data import mappers
from data.database import Database
from data.db.migrations import MIGRATIONS, v3, v4
from data.db.migrations._frozen import parse_metric_types
from data.db.migrations.runner import MigrationOutcome
from data.db.schema import SCHEMA_VERSION
from tests.test_migration import (
    _create_v0_db, _get_user_version, patched_migrations, run_migration,
)
from tests.test_migration_v2 import _create_v1_db
from tests.test_schema_v3 import _create_v2_db, _schema_signature


def _create_v3_db(path: str):
    """构造 v3 库：v2 库上直接套用 v3 迁移步骤，再放一个任务与一个打点"""
    _create_v2_db(path, {'t1': ['memory_rss']},
                  [('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss')])
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        cursor = conn.cursor()
        cursor.execute('BEGIN')
        v3.apply(cursor)
        cursor.execute(
            "INSERT INTO task_markers (task_id, timestamp, label, created_at) "
            "VALUES ('t1', '2026-01-01T00:00:01', '旧打点', '2026-01-01T00:00:01')")
        cursor.execute('PRAGMA user_version = 3')
        cursor.execute('COMMIT')
    finally:
        conn.close()


def _columns(path: str, table: str):
    """{列名: (类型, notnull, 默认值)}"""
    conn = sqlite3.connect(path)
    try:
        return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(f'PRAGMA table_info({table})')}
    finally:
        conn.close()


def test_schema_version_is_4():
    assert SCHEMA_VERSION == 4
    assert [m.version for m in MIGRATIONS] == [1, 2, 3, 4]


def test_new_db_is_v4_with_new_columns(db_path):
    Database(db_path)
    assert _get_user_version(db_path) == 4
    assert _columns(db_path, 'tasks')['create_time'] == ('REAL', 0, None)
    assert _columns(db_path, 'task_markers')['kind'] == ('TEXT', 1, "'user'")


def test_v3_to_v4_adds_columns_with_defaults(db_path):
    _create_v3_db(db_path)
    assert 'create_time' not in _columns(db_path, 'tasks')
    assert 'kind' not in _columns(db_path, 'task_markers')

    db = Database(db_path)

    report = db.migration_report
    assert report.outcome is MigrationOutcome.MIGRATED
    assert (report.from_version, report.to_version) == (3, 4)
    assert os.path.exists(db_path + '.bak_v3')
    assert _get_user_version(db_path) == 4

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT create_time FROM tasks WHERE task_id='t1'").fetchone() == (None,)
        assert conn.execute(
            "SELECT label, kind FROM task_markers").fetchall() == [('旧打点', 'user')]
        # 不指定 kind 插入的新打点同样默认为 user
        conn.execute(
            "INSERT INTO task_markers (task_id, timestamp, label, created_at) "
            "VALUES ('t1', '2026-01-01T00:00:02', '新', '2026-01-01T00:00:02')")
        assert conn.execute(
            "SELECT kind FROM task_markers WHERE label='新'").fetchone() == ('user',)
    finally:
        conn.close()

    # 旧库任务经门面仍可正常读取，打点与数据未丢
    assert db.get_task('t1').metric_types == ['memory_rss']
    assert [m.label for m in db.get_markers('t1')] == ['旧打点']
    assert db.get_data_point_count('t1') == 1


@pytest.mark.parametrize('source', ['v0', 'v1', 'v2', 'v3'])
def test_migrated_db_matches_new_db_structure(tmp_path, source):
    """v0/v1/v2/v3 迁移得到的库与新建库的表/列（含默认值）/索引完全一致"""
    fresh = str(tmp_path / 'fresh.db')
    run_migration(fresh)

    migrated = str(tmp_path / f'migrated_{source}.db')
    if source == 'v0':
        _create_v0_db(migrated)
    elif source == 'v1':
        _create_v1_db(migrated, {'t1': ['memory_rss']},
                      [('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss')])
    elif source == 'v2':
        _create_v2_db(migrated, {'t1': ['memory_rss']},
                      [('t1', '2026-01-01T00:00:01', 1.0, 'memory_rss')])
    else:
        _create_v3_db(migrated)

    assert run_migration(migrated).outcome is MigrationOutcome.MIGRATED
    assert _get_user_version(migrated) == SCHEMA_VERSION
    assert _schema_signature(migrated) == _schema_signature(fresh)


def test_v4_apply_is_idempotent(db_path):
    """v4 步骤对已有两列的库重复执行不报错"""
    run_migration(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        cursor = conn.cursor()
        v4.apply(cursor)
        v4.apply(cursor)
    finally:
        conn.close()
    assert 'create_time' in _columns(db_path, 'tasks')


def test_v4_failure_rolls_back_and_restores(db_path):
    """v4 步骤失败：整条链回滚并从备份还原，库仍为 v3 且没有新列"""
    _create_v3_db(db_path)

    def _boom(cursor):
        cursor.execute('ALTER TABLE tasks ADD COLUMN create_time REAL')
        raise RuntimeError("模拟迁移失败")

    report = run_migration(db_path, patched_migrations(4, _boom))

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert _get_user_version(db_path) == 3
    assert 'create_time' not in _columns(db_path, 'tasks')


def test_v2_task_with_empty_metric_list_migrates(db_path):
    """指标列表为 '[]' 的任务：v3 回填首指标时跳过它（以前会把 '[]' 当指标名写进数据点）"""
    _create_v2_db(db_path, {'t1': [], 't2': ['cpu_percent']}, [
        ('t1', '2026-01-01T00:00:01', 1.0, None),
        ('t2', '2026-01-01T00:00:01', 2.0, None),
    ])

    assert run_migration(db_path).outcome is MigrationOutcome.MIGRATED

    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute('SELECT task_id, metric_type FROM data_points'))
        tasks = dict(conn.execute('SELECT task_id, metric_type FROM tasks'))
    finally:
        conn.close()
    assert rows == {'t1': None, 't2': 'cpu_percent'}
    assert json.loads(tasks['t1']) == []


# ========== parse_metric_types ==========

@pytest.mark.parametrize('parse', [parse_metric_types, mappers.parse_metric_types],
                         ids=['frozen', 'runtime'])
@pytest.mark.parametrize('text, expected', [
    ('[]', []),
    ('["memory_rss", "cpu_percent"]', ['memory_rss', 'cpu_percent']),
    ('memory_rss', ['memory_rss']),
    ('[not json', ['[not json']),
    ('[1]', ['1']),
])
def test_parse_metric_types(parse, text, expected):
    assert parse(text) == expected
