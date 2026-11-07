"""
真实旧库迁移矩阵（golden）

样本是用各已发布版本自己的 Database 写出的真实库（tests/fixtures/legacy_db/，生成方式见其
README 与 generate.py），本文件只读 manifest.json 与 *.db.gz：解压到临时目录 → 用当前
MigrationRunner 迁到 data.db.schema.SCHEMA_VERSION → 断言结构与新建库一致、计数与
manifest 的独立推算一致、启动校正与历史查询可用、迁移幂等。
目标版本跟着 SCHEMA_VERSION 走：库结构升级后这些断言自动以新版本为准。
v5 起为重建式迁移：样本计数按 series/samples 统计，旧文件原样改名为 .bak_v<n>（逐字节不变）。
"""
import gzip
import json
import os
import re
import shutil
import sqlite3

import pytest

from core.history_query import build_request, run_history_query
from core.maintenance_service import MaintenanceService
from data.database import Database
from data.db.connection import ConnectionFactory
from data.db.migrations.runner import MigrationOutcome, MigrationRunner
from data.db.schema import SCHEMA_VERSION
from tests.fixtures.legacy_db import FIXTURE_DIR, MANIFEST_PATH

with open(MANIFEST_PATH, encoding='utf-8') as _f:
    SAMPLES = json.load(_f)['samples']


def _ids(sample):
    return sample['file'].replace('.db.gz', '')


def _sample_params(xfail_files=None):
    """参数化全部样本；xfail_files: {文件名: 原因}，这些样本按 strict xfail 运行"""
    xfail_files = xfail_files or {}
    params = []
    for sample in SAMPLES:
        marks = []
        if sample['file'] in xfail_files:
            marks.append(pytest.mark.xfail(strict=True, reason=xfail_files[sample['file']]))
        params.append(pytest.param(sample, id=_ids(sample), marks=marks))
    return params


def _unpack(sample, dest_dir) -> str:
    """把样本解压为 dest_dir/monitor.db，返回库路径"""
    db_path = os.path.join(str(dest_dir), 'monitor.db')
    with gzip.open(os.path.join(FIXTURE_DIR, sample['file']), 'rb') as src, \
            open(db_path, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    return db_path


def _migrate(db_path):
    factory = ConnectionFactory(db_path)
    try:
        return MigrationRunner(factory).migrate()
    finally:
        factory.close()


def _fresh_db(dest_dir) -> str:
    """当前版本新建的空库（迁移结果要与之结构一致）"""
    path = os.path.join(str(dest_dir), 'fresh.db')
    report = _migrate(path)
    assert report.created
    return path


def _norm_sql(sql):
    """去掉空白差异：折叠空白，并去掉括号与逗号两侧的空白"""
    if sql is None:
        return None
    sql = re.sub(r'\s+', ' ', sql.strip())
    return re.sub(r'\s*([(),])\s*', r'\1', sql)


def _shape(path) -> dict:
    """结构指纹：对象清单、user_version、每张表的列定义、每个索引的列与属性"""
    conn = sqlite3.connect(path)
    try:
        objects = sorted(conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')").fetchall())
        tables = [name for kind, name, _t in objects if kind == 'table']
        columns = {t: conn.execute(f'PRAGMA table_info("{t}")').fetchall() for t in tables}
        indexes = {}
        for t in tables:
            for _seq, name, unique, origin, partial in conn.execute(
                    f'PRAGMA index_list("{t}")').fetchall():
                cols = conn.execute(f'PRAGMA index_info("{name}")').fetchall()
                indexes[name] = (t, unique, origin, partial, cols)
        return {
            'objects': objects,
            'user_version': conn.execute('PRAGMA user_version').fetchone()[0],
            'columns': columns,
            'indexes': indexes,
        }
    finally:
        conn.close()


def _sql_texts(path) -> dict:
    conn = sqlite3.connect(path)
    try:
        return {(kind, name): _norm_sql(sql) for kind, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')")}
    finally:
        conn.close()


def _dump(path) -> dict:
    """逻辑内容：每张用户表按全部列排序后的全部行"""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {t: sorted(conn.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr)
                for t in tables}
    finally:
        conn.close()


def _read_bytes(path) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


def _rows_by_metric(conn, task_id):
    return dict(conn.execute(
        'SELECT se.metric, COUNT(*) FROM samples s JOIN series se ON se.series_id = s.series_id '
        'WHERE se.task_id = ? GROUP BY se.metric', (task_id,)).fetchall())


def _sums_by_metric(conn, task_id):
    return dict(conn.execute(
        'SELECT se.metric, SUM(s.value) FROM samples s '
        'JOIN series se ON se.series_id = s.series_id '
        'WHERE se.task_id = ? GROUP BY se.metric', (task_id,)).fetchall())


@pytest.mark.parametrize('sample', _sample_params())
def test_golden_sample_migrates_to_current(sample, tmp_path):
    """真实旧库迁到当前版本：结果、备份、结构、计数、启动校正、历史查询、幂等"""
    db_path = _unpack(sample, tmp_path)
    original = _read_bytes(db_path)
    original_dump = _dump(db_path)
    assert _shape(db_path)['user_version'] == sample['schema_version']

    report = _migrate(db_path)

    # 迁移结果与备份：备份就是原文件本身（重建式迁移只读旧库、最后改名），逐字节相同
    assert report.outcome is MigrationOutcome.MIGRATED
    assert (report.from_version, report.to_version) == (sample['schema_version'],
                                                        SCHEMA_VERSION)
    assert report.backup_path == db_path + f'.bak_v{sample["schema_version"]}'
    assert os.path.exists(report.backup_path)
    assert _dump(report.backup_path) == original_dump
    assert _read_bytes(report.backup_path) == original
    assert not os.path.exists(db_path + '.migrating')

    # 结构与新建库一致（对象、版本号、列定义、索引）
    assert _shape(db_path) == _shape(_fresh_db(tmp_path))

    # 计数与 manifest 一致（迁移后、启动校正前）
    expected_tasks = sample['tasks']
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == len(expected_tasks)
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == \
            sample['totals']['rows_after']
        assert conn.execute('SELECT COUNT(*) FROM task_markers').fetchone()[0] == \
            sample['totals']['markers_after']
        # 旧表在 v5 里保留为空表；不应有空名指标（NULL 未回填）、孤儿序列或失去序列的样本
        assert conn.execute('SELECT COUNT(*) FROM data_points').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM series WHERE metric = ''").fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM series WHERE task_id NOT IN '
            '(SELECT task_id FROM tasks)').fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM samples WHERE series_id NOT IN '
            '(SELECT series_id FROM series)').fetchone()[0] == 0
        for task_id, exp in expected_tasks.items():
            row = conn.execute(
                'SELECT process_name, metric_type, status, note, sample_count, '
                'last_sample_at, create_time FROM tasks WHERE task_id = ?',
                (task_id,)).fetchone()
            assert row is not None, task_id
            name, metric_text, status, note, sample_count, last_at, create_time = row
            assert name == exp['process_name']
            assert json.loads(metric_text) == exp['metric_types_after']
            assert status == exp['status_before']
            assert note == exp['note_after']
            assert sample_count == exp['sample_count_after']
            assert last_at == exp['last_sample_at']
            assert create_time is None
            assert _rows_by_metric(conn, task_id) == exp['rows_after_by_metric']
            sums = _sums_by_metric(conn, task_id)
            assert sums.keys() == exp['value_sum_after_by_metric'].keys()
            for metric, total in exp['value_sum_after_by_metric'].items():
                assert sums[metric] == pytest.approx(total, rel=1e-12)
            markers = conn.execute(
                'SELECT kind FROM task_markers WHERE task_id = ?', (task_id,)).fetchall()
            assert len(markers) == exp['markers_after']
            assert all(kind == 'user' for (kind,) in markers)
    finally:
        conn.close()

    db = Database(db_path)
    try:
        # 迁移幂等：门面再次打开不再迁移
        assert db.migration_report.outcome is MigrationOutcome.UP_TO_DATE

        # 启动校正：遗留 running 任务校正为 stopped，结束时间取最后一次采样时间；
        # v3 迁移已回填 sample_count，不应再有需要重算的任务
        result = MaintenanceService(db, manager=None).startup_reconcile(0)
        assert result.orphans == sample['totals']['running_tasks']
        assert result.recounted == 0
        assert result.cleaned == 0
        for task_id, exp in expected_tasks.items():
            task = db.get_task(task_id)
            assert task.status == exp['status_after_reconcile']
            assert task.end_time.isoformat() == exp['end_time_after_reconcile']

        # 有数据的任务出现在分析页任务列表，空任务不出现
        with_data = {t.task_id for t in db.get_tasks_with_data()}
        assert with_data == {task_id for task_id, exp in expected_tasks.items()
                             if exp['sample_count_after'] > 0}

        # 任务 A 的分析快照可算
        task_id, metric = sample['history_task_id'], sample['history_metric']
        snapshot = run_history_query(db, build_request(task_id, metric, 'all', analysis=True))
        assert snapshot.ok, snapshot.error
        expected_count = expected_tasks[task_id]['rows_after_by_metric'][metric]
        assert snapshot.total_count == expected_count
        assert snapshot.stats is not None and snapshot.stats['count'] == expected_count
        assert snapshot.buckets and snapshot.band
    finally:
        db.close()

    # 再跑一次迁移：UP_TO_DATE，不新建备份
    backups_before = sorted(p for p in os.listdir(tmp_path) if '.bak_' in p)
    again = _migrate(db_path)
    assert again.outcome is MigrationOutcome.UP_TO_DATE
    assert again.from_version == SCHEMA_VERSION and again.backup_path is None
    assert sorted(p for p in os.listdir(tmp_path) if '.bak_' in p) == backups_before


@pytest.mark.parametrize('sample', _sample_params())
def test_golden_schema_sql_matches_fresh_db(sample, tmp_path):
    """迁移后 sqlite_master 中表、索引、触发器的规范化 SQL 与新建库逐条一致"""
    db_path = _unpack(sample, tmp_path)
    assert _migrate(db_path).outcome is MigrationOutcome.MIGRATED
    assert _sql_texts(db_path) == _sql_texts(_fresh_db(tmp_path))


@pytest.mark.parametrize('sample', _sample_params())
def test_golden_backup_is_byte_identical(sample, tmp_path):
    """
    迁移前备份与原样本逐字节一致。

    v4 之前探测连接走 ConnectionFactory（设 journal_mode=WAL），回滚日志模式的旧库
    （v1.0.x、v1.1.0）文件头在备份前就被改掉，这里曾是 strict xfail。v5 的探测用裸连接、
    旧库以 immutable 只读方式 ATTACH、最后原文件改名为备份：回滚日志模式的库一个字节都不变；
    WAL 模式的库只做一次 checkpoint——样本解压时没有 -wal 文件，checkpoint 不改主文件，
    同样逐字节相同（带未合并 -wal 的情形见 test_rebuild_migration）。
    """
    db_path = _unpack(sample, tmp_path)
    original = _read_bytes(db_path)
    report = _migrate(db_path)
    assert report.outcome is MigrationOutcome.MIGRATED
    assert _read_bytes(report.backup_path) == original
    names = sorted(n for n in os.listdir(tmp_path) if n.startswith('monitor.db'))
    # 新库为 WAL 模式，可能留有它自己的 -wal / -shm；不应有 .migrating 或备份的边车文件
    assert [n for n in names if n not in ('monitor.db-wal', 'monitor.db-shm')] ==         ['monitor.db', f'monitor.db.bak_v{sample["schema_version"]}'], names


def test_manifest_lists_every_fixture_file():
    """manifest 与目录中的样本文件一一对应，且每份压缩样本 < 200 KB"""
    on_disk = sorted(n for n in os.listdir(FIXTURE_DIR) if n.endswith('.db.gz'))
    assert on_disk == sorted(s['file'] for s in SAMPLES)
    for name in on_disk:
        assert os.path.getsize(os.path.join(FIXTURE_DIR, name)) < 200 * 1024
    # 至少覆盖真实用户可能持有的两种版本（v0 / v1），以及截断链派生的 v2、v3
    assert {s['schema_version'] for s in SAMPLES} >= {0, 1, 2, 3}
