"""
分钟汇总 rollup_1m 与写时空洞 series_gaps 的不变式（规则见 data.repos.rollup）：

- 随机写入（按序追加、同一毫秒重复与覆盖、空洞、乱序、跨分钟、多任务不同周期）后，表中内容与
  "从 samples 整体重算"的结果完全一致（计数、时间戳逐项相同，浮点相对误差 1e-9）；
- 桶键不跨空洞（一个分钟内出现空洞拆成两行）、空洞口径与 AnalyticsRepo.gaps 相同（严格大于）；
- 同一毫秒覆盖：min/max 及其时间按新值重算；乱序写入计数并记 WARNING；
- 删除任务、过期清理无残留；导回降级数据、重建式迁移、Maintenance.rebuild_rollups 后同样一致。

assert_rollups_consistent 也供加速浸泡与迁移用例复用。
"""
import logging
import math
import random
import sqlite3
from datetime import datetime, timedelta

import pytest

from data.database import Database
from data.mappers import to_ms
from data.models import DataPoint, MonitorTask
from data.repos import rollup
from data.repos.analytics_repo import AnalyticsRepo
from data.repos.point_repo import PointQuery

T0 = datetime(2026, 3, 1, 10, 0, 0)
_ROLLUP_SQL = 'SELECT * FROM rollup_1m ORDER BY series_id, bucket_ms'
_GAPS_SQL = 'SELECT * FROM series_gaps ORDER BY series_id, start_ms'


def _same_row(a, b) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, float) or isinstance(y, float):
            scale = max(abs(x), abs(y), 1.0)
            if not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9 * scale):
                return False
        elif x != y:
            return False
    return True


def rebuilt_tables(conn: sqlite3.Connection):
    """在事务里整体重算后读出两张表，再回滚（不改库）"""
    conn.execute('BEGIN')
    try:
        rollup.rebuild_rollups(conn)
        return (conn.execute(_ROLLUP_SQL).fetchall(), conn.execute(_GAPS_SQL).fetchall())
    finally:
        conn.execute('ROLLBACK')


def assert_rollups_consistent(db_path: str) -> int:
    """表中的汇总与空洞 == 从 samples 整体重算的结果；返回汇总行数"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        stored = conn.execute(_ROLLUP_SQL).fetchall()
        gaps = conn.execute(_GAPS_SQL).fetchall()
        expected, expected_gaps = rebuilt_tables(conn)
    finally:
        conn.close()
    assert gaps == expected_gaps
    assert [r[:2] for r in stored] == [r[:2] for r in expected]
    for a, b in zip(stored, expected):
        assert _same_row(a, b), (a, b)
    return len(stored)


def _task(db, task_id, interval, metrics=('m1', 'm2')):
    db.save_task(MonitorTask(task_id=task_id, pid=1, process_name='a.exe',
                             metric_types=list(metrics), interval=interval, start_time=T0,
                             end_time=None, status='running'))


def _rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ========== 随机写入 ==========

def _random_batches(rng: random.Random, task_id: str, interval: float, metrics, cycles: int):
    """
    按周期生成写入批次：常态按周期前进（带毫秒抖动），夹杂空洞（远超阈值、恰在阈值、
    略超阈值）、同一毫秒重复（批内与跨批）、乱序回填（早于最后一个样本）与漏采某个指标。
    """
    t = 0.0
    history = []
    batches = []
    for _ in range(cycles):
        batch = []
        steps = rng.randint(1, 4)
        for _s in range(steps):
            r = rng.random()
            if r < 0.04:
                t += interval * rng.choice([3.0, 10.0, 75.0])        # 空洞
            elif r < 0.06:
                t += interval * 2.5 - interval                         # 恰好 2.5 倍：不是空洞
            elif r < 0.08:
                t += interval * 2.5 + 0.001 - interval                 # 略超：是空洞
            t += interval + rng.choice([0.0, 0.0, 0.001, -0.001, 0.0004])
            ts = T0 + timedelta(seconds=t)
            history.append(ts)
            for metric in metrics:
                if rng.random() < 0.92:
                    batch.append(DataPoint(task_id, ts, float(rng.randint(0, 50)), metric))
            if rng.random() < 0.05:                                    # 批内同一毫秒重复
                batch.append(DataPoint(task_id, ts, float(rng.randint(0, 50)), metrics[0]))
        r = rng.random()
        if r < 0.05 and history:                                       # 跨批覆盖已有样本
            ts = rng.choice(history)
            batch.append(DataPoint(task_id, ts, float(rng.randint(-5, 60)), rng.choice(metrics)))
        elif r < 0.09 and history:                                     # 乱序回填
            ts = history[0] - timedelta(seconds=rng.uniform(-interval * 30, interval * 200))
            batch.append(DataPoint(task_id, ts, float(rng.randint(0, 50)), rng.choice(metrics)))
        rng.shuffle(batch)
        batches.append(batch)
    return batches


@pytest.mark.parametrize('seed', [1, 2, 3, 4])
def test_random_writes_match_full_rebuild(db, seed):
    rng = random.Random(seed)
    specs = [('fast', 1.0), ('half', 0.5), ('minute', 60.0), ('flat', 0.0)]
    all_batches = []
    for task_id, interval in specs:
        _task(db, task_id, interval)
        all_batches.append(_random_batches(rng, task_id, interval or 1.0, ['m1', 'm2'], 120))
    # 多任务交错写入，偶尔把两个任务的批次合成一次写入
    for i in range(120):
        merged = []
        for batches in all_batches:
            merged.extend(batches[i])
            if rng.random() < 0.5:
                db.points.append(merged)
                merged = []
        if merged:
            db.points.append(merged)
        if i % 30 == 29:
            assert assert_rollups_consistent(db.db_path) > 0
    assert assert_rollups_consistent(db.db_path) > 0
    assert db.points.out_of_order_rows > 0            # 随机数据确实覆盖了乱序路径
    # 采样计数仍与样本一致
    for task_id, _interval in specs:
        task = db.tasks.get(task_id)
        assert task.sample_count == db.points.recount(task_id)
    # interval 不是正数的任务不记空洞
    assert _rows(db.db_path, 'SELECT COUNT(*) FROM series_gaps WHERE series_id IN '
                 "(SELECT series_id FROM series WHERE task_id = 'flat')")[0][0] == 0


def test_rollups_never_span_gaps_and_split_minutes(db):
    _task(db, 't', 1.0, metrics=('m',))
    base = T0 + timedelta(seconds=5)
    stamps = [base + timedelta(seconds=s) for s in (0, 1, 2, 20, 21, 22, 62)]
    db.points.append([DataPoint('t', ts, float(i), 'm') for i, ts in enumerate(stamps)])
    ms = [to_ms(ts) for ts in stamps]
    assert _rows(db.db_path, 'SELECT start_ms, end_ms FROM series_gaps') == [
        (ms[2], ms[3]), (ms[5], ms[6])]
    minute = rollup.minute_floor(ms[0])
    rows = _rows(db.db_path, 'SELECT bucket_ms, count, first_ms, last_ms FROM rollup_1m '
                             'ORDER BY bucket_ms')
    # 第一分钟被 20 秒处的空洞拆成两行；62 秒处的样本在下一分钟、又是新段起点
    assert rows == [(ms[0], 3, ms[0], ms[2]), (ms[3], 3, ms[3], ms[5]), (ms[6], 1, ms[6], ms[6])]
    assert ms[0] > minute and ms[6] - rollup.minute_floor(ms[6]) == 7000
    assert_rollups_consistent(db.db_path)


@pytest.mark.parametrize('drop_rollups', [False, True])
def test_append_after_mid_minute_gap_keeps_segment_bucket(db, drop_rollups):
    """
    跨批追加：上一批末尾在本分钟内出现空洞，下一批同一分钟的样本仍归入"段起点"桶。
    drop_rollups：有样本却没有汇总行（不一致库）时退回按空洞表求段起点，桶键照样正确。
    """
    _task(db, 't', 1.0, metrics=('m',))
    base = T0 + timedelta(seconds=5)
    first = [base + timedelta(seconds=s) for s in (0, 1, 20)]
    db.points.append([DataPoint('t', ts, 1.0, 'm') for ts in first])
    if drop_rollups:
        conn = sqlite3.connect(db.db_path)
        conn.execute('DELETE FROM rollup_1m')
        conn.commit()
        conn.close()
    later = base + timedelta(seconds=21)
    db.points.append([DataPoint('t', later, 2.0, 'm')])
    rows = _rows(db.db_path, 'SELECT bucket_ms, count FROM rollup_1m ORDER BY bucket_ms')
    seg = to_ms(first[2])
    assert rows[-1] == (seg, 1 if drop_rollups else 2)
    if not drop_rollups:
        assert_rollups_consistent(db.db_path)


def test_gap_threshold_is_strictly_greater_like_analytics(db):
    _task(db, 't', 1.0, metrics=('m',))
    offsets = [0, 2500, 5001, 6001, 8501]          # 间隔 2.5 秒不是空洞，2.501 秒是
    db.points.append([DataPoint('t', T0 + timedelta(milliseconds=o), 1.0, 'm') for o in offsets])
    stored = _rows(db.db_path, 'SELECT start_ms, end_ms FROM series_gaps')
    base = to_ms(T0)
    assert stored == [(base + 2500, base + 5001)]
    raw = AnalyticsRepo(db._factory, path='raw').gaps('t', 1.0)
    assert [(to_ms(a), to_ms(b)) for a, b in raw] == stored


def test_same_ms_overwrite_recomputes_min_max(db):
    _task(db, 't', 1.0, metrics=('m',))
    stamps = [T0 + timedelta(seconds=s) for s in range(5)]
    db.points.append([DataPoint('t', ts, v, 'm') for ts, v in zip(stamps, [5, 1, 9, 1, 5])])
    row = _rows(db.db_path, 'SELECT count, min_value, min_ms, max_value, max_ms, sum_y '
                            'FROM rollup_1m')[0]
    assert row == (5, 1.0, to_ms(stamps[1]), 9.0, to_ms(stamps[2]), 21.0)
    # 覆盖最小值与最大值所在的样本：最值及其时间都要换
    db.points.append([DataPoint('t', stamps[1], 7.0, 'm'), DataPoint('t', stamps[2], 0.0, 'm')])
    row = _rows(db.db_path, 'SELECT count, min_value, min_ms, max_value, max_ms, sum_y '
                            'FROM rollup_1m')[0]
    assert row == (5, 0.0, to_ms(stamps[2]), 7.0, to_ms(stamps[1]), 18.0)
    assert db.tasks.get('t').sample_count == 5
    assert db.points.out_of_order_rows == 0
    assert_rollups_consistent(db.db_path)


def test_ties_keep_earliest_min_and_max_time(db):
    _task(db, 't', 1.0, metrics=('m',))
    stamps = [T0 + timedelta(seconds=s) for s in range(6)]
    # 分两批写入，第二批的并列最值不能覆盖第一批更早的时间
    db.points.append([DataPoint('t', ts, v, 'm') for ts, v in zip(stamps[:3], [3, 1, 8])])
    db.points.append([DataPoint('t', ts, v, 'm') for ts, v in zip(stamps[3:], [1, 8, 2])])
    assert _rows(db.db_path, 'SELECT min_ms, max_ms FROM rollup_1m') == [
        (to_ms(stamps[1]), to_ms(stamps[2]))]
    assert_rollups_consistent(db.db_path)


def test_out_of_order_insert_repairs_gaps_and_warns(db, caplog):
    _task(db, 't', 1.0, metrics=('m',))
    stamps = [T0 + timedelta(seconds=s) for s in (0, 1, 2, 30, 31, 32)]
    db.points.append([DataPoint('t', ts, 1.0, 'm') for ts in stamps])
    assert len(_rows(db.db_path, 'SELECT * FROM series_gaps')) == 1
    # 把 2 → 30 秒的空洞填满：空洞消失、两段并成一段（拆开的分钟桶也随之合并）
    with caplog.at_level(logging.WARNING, logger='data.repos.point_repo'):
        db.points.append([DataPoint('t', T0 + timedelta(seconds=s), 2.0, 'm')
                          for s in range(3, 30, 2)])
    assert _rows(db.db_path, 'SELECT * FROM series_gaps') == []
    assert db.points.out_of_order_rows == 14
    assert any('乱序' in r.getMessage() for r in caplog.records)
    assert_rollups_consistent(db.db_path)
    # 早于首个样本的回填：首段起点前移
    db.points.append([DataPoint('t', T0 - timedelta(seconds=1), 3.0, 'm')])
    assert_rollups_consistent(db.db_path)
    db.points.append([DataPoint('t', T0 - timedelta(seconds=100), 3.0, 'm')])
    assert len(_rows(db.db_path, 'SELECT * FROM series_gaps')) == 1
    assert_rollups_consistent(db.db_path)


def test_delete_task_and_cleanup_leave_no_rollup_rows(db):
    _task(db, 'keep', 1.0)
    _task(db, 'gone', 1.0)
    for task_id in ('keep', 'gone'):
        db.points.append([DataPoint(task_id, T0 + timedelta(seconds=s * 7), float(s), m)
                          for s in range(40) for m in ('m1', 'm2')])
    gone = [r[0] for r in _rows(db.db_path, "SELECT series_id FROM series WHERE task_id = 'gone'")]
    assert db.tasks.delete('gone')
    for table in ('rollup_1m', 'series_gaps'):
        left = _rows(db.db_path, f'SELECT COUNT(*) FROM {table} WHERE series_id IN '
                                 f'({",".join(map(str, gone))})')[0][0]
        assert left == 0, table
    assert _rows(db.db_path, 'SELECT COUNT(*) FROM rollup_1m')[0][0] > 0
    assert_rollups_consistent(db.db_path)


def test_maintenance_rebuild_fixes_tampered_rows(db):
    _task(db, 't', 1.0)
    db.points.append([DataPoint('t', T0 + timedelta(seconds=s * 1.5), float(s % 7), m)
                      for s in range(400) for m in ('m1', 'm2')])
    good = _rows(db.db_path, _ROLLUP_SQL), _rows(db.db_path, _GAPS_SQL)
    conn = sqlite3.connect(db.db_path)
    conn.execute('UPDATE rollup_1m SET count = count + 1, sum_y = 0')
    conn.execute('INSERT INTO series_gaps VALUES (1, 0, 1)')
    conn.commit()
    conn.close()
    assert db.maintenance.rebuild_rollups() == 2
    assert (_rows(db.db_path, _ROLLUP_SQL), _rows(db.db_path, _GAPS_SQL)) == good


def test_import_legacy_points_rebuilds_rollups(db):
    _task(db, 't', 1.0, metrics=('memory_private',))
    db.points.append([DataPoint('t', T0 + timedelta(seconds=s), 1.0, 'memory_private')
                      for s in range(0, 100, 2)])
    # 1.4.1 降级期间写进 data_points 的行：时间夹在已有样本之间、也有更晚的
    conn = sqlite3.connect(db.db_path)
    conn.executemany(
        'INSERT INTO data_points (task_id, timestamp, value, metric_type) VALUES (?, ?, ?, ?)',
        [('t', (T0 + timedelta(seconds=s)).isoformat(), 5.0, 'memory_vms')
         for s in list(range(1, 50, 2)) + list(range(200, 260))])
    conn.commit()
    conn.close()
    assert db.maintenance.import_legacy_points() == 85
    assert_rollups_consistent(db.db_path)
    assert len(_rows(db.db_path, _GAPS_SQL)) == 1


def test_upsert_is_single_statement_on_primary_key(db):
    """汇总合并走主键 UPSERT：查询计划里没有对 rollup_1m 的扫描"""
    conn = sqlite3.connect(db.db_path)
    try:
        plan = conn.execute('EXPLAIN QUERY PLAN ' + rollup.UPSERT_SQL,
                            (1, 0, 1, 0, 1.0, 0, 1.0, 1.0, 0, 1.0, 0, 1.0, 1.0, 0.0, 0.0, 0.0)
                            ).fetchall()
    finally:
        conn.close()
    assert not any('SCAN' in str(row[-1]) for row in plan), plan


def test_rebuild_rollups_single_series_matches_full(db):
    _task(db, 'a', 1.0)
    _task(db, 'b', 60.0)
    for task_id, step in (('a', 1.3), ('b', 61.0)):
        db.points.append([DataPoint(task_id, T0 + timedelta(seconds=s * step + (s // 50) * 500),
                                    float(s % 11), m) for s in range(300) for m in ('m1', 'm2')])
    full = _rows(db.db_path, _ROLLUP_SQL)
    conn = sqlite3.connect(db.db_path, isolation_level=None)
    try:
        conn.execute('BEGIN')
        for sid, in conn.execute('SELECT series_id FROM series').fetchall():
            rollup.rebuild_rollups(conn, series_id=sid)
        conn.execute('COMMIT')
    finally:
        conn.close()
    assert [r[:2] for r in _rows(db.db_path, _ROLLUP_SQL)] == [r[:2] for r in full]
    assert_rollups_consistent(db.db_path)


def test_rebuild_migration_generates_consistent_rollups(tmp_path):
    """v1 旧库经重建式迁移：一次性 SQL 生成的汇总与空洞，与把同样的样本按序增量写入的结果相同"""
    from tests.test_migration_v2 import _create_v1_db
    path = str(tmp_path / 'monitor.db')
    rows = []
    for i in range(600):
        if 200 <= i < 260:
            continue                                  # 1 分钟空洞
        ts = (T0 + timedelta(seconds=i, microseconds=(i * 7919) % 1000_000)).isoformat()
        rows.append(('t1', ts, float(i % 13), 'memory_rss'))
        rows.append(('t1', ts, float(i % 5), 'memory_vms'))
    _create_v1_db(path, {'t1': ['memory_rss', 'memory_vms']}, rows)
    direct = str(tmp_path / 'direct.db')
    db = Database(path)
    try:
        assert db.analytics.point_count('t1') == 2 * 540
        task = db.tasks.get('t1')
        other = Database(direct)
        try:
            other.save_task(task)
            other.points.append(db.points.fetch(PointQuery('t1', None)))
        finally:
            other.close()
    finally:
        db.close()
    assert assert_rollups_consistent(path) > 0
    migrated, written = _rows(path, _ROLLUP_SQL), _rows(direct, _ROLLUP_SQL)
    assert [r[:2] for r in migrated] == [r[:2] for r in written]
    assert all(_same_row(a, b) for a, b in zip(migrated, written))
    assert _rows(path, _GAPS_SQL) == _rows(direct, _GAPS_SQL)
    assert len(_rows(path, _GAPS_SQL)) == 2       # 两条序列各一个空洞


def test_task_without_row_records_no_gaps(db):
    """任务行不存在（只写了样本）时阈值未知，不记空洞；重算口径相同"""
    db.points.append([DataPoint('ghost', T0 + timedelta(seconds=s * 10), 1.0, 'm')
                      for s in range(20)])
    assert _rows(db.db_path, _GAPS_SQL) == []
    assert assert_rollups_consistent(db.db_path) == 4      # 200 秒跨 4 个自然分钟
