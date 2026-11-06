"""
data.mappers：时间编解码（只收 datetime）、指标列编解码、行 → 模型映射，以及模型的小改动
"""
import sqlite3
from datetime import date, datetime

import pytest

from data import models
from data.mappers import (
    dump_metric_types, from_iso, parse_metric_types, row_to_marker, row_to_point,
    row_to_task, to_iso, to_iso_or_none,
)
from data.models import DataPoint, Marker, MonitorTask, TaskRecord


def _rows(create_sql: str, insert_sql: str, params: tuple) -> sqlite3.Row:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(create_sql)
    conn.execute(insert_sql, params)
    return conn.execute('SELECT * FROM t').fetchone()


# ---------- 时间 ----------

def test_to_iso_accepts_datetime_only():
    dt = datetime(2026, 1, 2, 3, 4, 5, 600000)
    assert to_iso(dt) == '2026-01-02T03:04:05.600000'
    for bad in (1767225600.0, 1767225600, '2026-01-01T00:00:00', None, True,
                date(2026, 1, 1)):
        with pytest.raises(TypeError):
            to_iso(bad)


def test_to_iso_or_none():
    assert to_iso_or_none(None) is None
    assert to_iso_or_none(datetime(2026, 1, 1)) == '2026-01-01T00:00:00'
    with pytest.raises(TypeError):
        to_iso_or_none(0.0)


def test_from_iso_roundtrip_and_empty():
    dt = datetime(2026, 5, 6, 7, 8, 9, 123)
    assert from_iso(to_iso(dt)) == dt
    assert from_iso(None) is None
    assert from_iso('') is None


# ---------- 指标列 ----------

@pytest.mark.parametrize('text, expected', [
    ('[]', []),
    ('["memory_rss", "cpu_percent"]', ['memory_rss', 'cpu_percent']),
    ('memory_rss', ['memory_rss']),          # 旧版单指标裸字符串
    ('[1, 2]', ['1', '2']),
    ('[broken', ['[broken']),                # 不是合法 JSON 时按裸指标名兜底
    ('["a"', ['["a"']),
    (None, []),
    ('', []),
])
def test_parse_metric_types(text, expected):
    assert parse_metric_types(text) == expected


def test_dump_metric_types_roundtrip_and_rejects_str():
    assert dump_metric_types(['memory_rss', 'num_handles']) == '["memory_rss", "num_handles"]'
    assert dump_metric_types(('a',)) == '["a"]'
    assert parse_metric_types(dump_metric_types(['x', 'y'])) == ['x', 'y']
    assert parse_metric_types(dump_metric_types([])) == []
    with pytest.raises(TypeError):
        dump_metric_types('memory_rss')


# ---------- 行映射 ----------

_TASK_V4 = '''CREATE TABLE t (task_id TEXT, pid INTEGER, process_name TEXT, metric_type TEXT,
    interval REAL, start_time TEXT, end_time TEXT, status TEXT, note TEXT,
    sample_count INTEGER, last_sample_at TEXT, create_time REAL)'''


def test_row_to_task_v4_full():
    row = _rows(_TASK_V4, 'INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
        'tid', 42, 'a.exe', '["memory_rss","cpu_percent"]', 2.0,
        '2026-01-01T00:00:00', '2026-01-01T01:00:00', 'stopped', '备注', 7,
        '2026-01-01T00:59:58', 1767196800.25))
    task = row_to_task(row)
    assert task == MonitorTask(
        task_id='tid', pid=42, process_name='a.exe', metric_types=['memory_rss', 'cpu_percent'],
        interval=2.0, start_time=datetime(2026, 1, 1), end_time=datetime(2026, 1, 1, 1),
        status='stopped', note='备注', sample_count=7,
        last_sample_at=datetime(2026, 1, 1, 0, 59, 58), create_time=1767196800.25)


def test_row_to_task_legacy_columns_missing_and_nulls():
    row = _rows('''CREATE TABLE t (task_id TEXT, pid INTEGER, process_name TEXT,
        metric_type TEXT, interval REAL, start_time TEXT, end_time TEXT, status TEXT)''',
                'INSERT INTO t VALUES (?,?,?,?,?,?,?,?)',
                ('tid', 1, 'b.exe', 'memory_rss', 1.0, '2026-01-01T00:00:00', None, 'running'))
    task = row_to_task(row)
    assert task.metric_types == ['memory_rss']
    assert task.end_time is None
    assert (task.note, task.sample_count, task.last_sample_at, task.create_time) == (
        '', 0, None, None)


def test_row_to_point_null_metric_becomes_empty():
    row = _rows('CREATE TABLE t (id INTEGER, task_id TEXT, timestamp TEXT, value REAL, '
                'metric_type TEXT)', 'INSERT INTO t VALUES (?,?,?,?,?)',
                (1, 'tid', '2026-01-01T00:00:01', 3.5, None))
    assert row_to_point(row) == DataPoint('tid', datetime(2026, 1, 1, 0, 0, 1), 3.5, '')


def test_row_to_marker_with_and_without_kind():
    with_kind = _rows('CREATE TABLE t (id INTEGER, task_id TEXT, timestamp TEXT, label TEXT, '
                      'created_at TEXT, kind TEXT)', 'INSERT INTO t VALUES (?,?,?,?,?,?)',
                      (9, 'tid', '2026-01-01T00:00:00', '开始', 'x', 'baseline'))
    assert row_to_marker(with_kind) == Marker('tid', datetime(2026, 1, 1), '开始', 9, 'baseline')

    v3 = _rows('CREATE TABLE t (id INTEGER, task_id TEXT, timestamp TEXT, label TEXT, '
               'created_at TEXT)', 'INSERT INTO t VALUES (?,?,?,?,?)',
               (3, 'tid', '2026-01-01T00:00:00', '旧打点', 'x'))
    assert row_to_marker(v3).kind == 'user'
    assert row_to_marker(v3).id == 3


# ---------- 模型 ----------

def test_model_changes():
    assert TaskRecord is MonitorTask
    assert Marker('t', datetime(2026, 1, 1), 'x').kind == 'user'
    assert models.MARKER_KINDS == ('user', 'baseline', 'pause', 'resume')
    task = MonitorTask('t', 1, 'a', ['m'], 1.0, None, None, 'pending')
    assert task.create_time is None
    for cls in (MonitorTask, DataPoint):
        assert not hasattr(cls, 'to_dict')
        assert not hasattr(cls, 'from_dict')
