"""
Database 门面：仓储公开属性、旧方法对仓储的委托与参数换算、仓储抛 StorageError 时
旧方法的返回值与日志（吞异常语义），以及两个例外——vacuum / iter_data_points 失败上抛。
"""
import logging
from datetime import datetime, timedelta

import pytest

import data.database as database_module
from data.database import Database
from data.errors import StorageError
from data.maintenance import Maintenance
from data.models import DataPoint, MonitorTask
from data.repos.analytics_repo import AnalyticsRepo
from data.repos.marker_repo import MarkerRepo
from data.repos.point_repo import PointQuery, PointRepo
from data.repos.task_repo import TaskRepo

T0 = datetime(2026, 5, 1, 9, 0, 0)
TASK = MonitorTask(task_id='t', pid=1, process_name='a.exe', metric_types=['m'],
                   interval=1.0, start_time=T0, end_time=None, status='stopped')
POINT = DataPoint('t', T0, 1.0, 'm')


def _boom(*args, **kwargs):
    raise StorageError("模拟存储故障: database is locked")


# (旧方法名, 调用参数, 仓储属性, 仓储方法, 失败时的返回值)
SWALLOWING = [
    ('reconcile_orphan_tasks', (), 'tasks', 'reconcile_orphans', 0),
    ('reconcile_sample_counts', (), 'points', 'recount_all', 0),
    ('save_task', (TASK,), 'tasks', 'save', False),
    ('get_task', ('t',), 'tasks', 'get', None),
    ('get_all_tasks', (), 'tasks', 'list_all', []),
    ('get_tasks_with_data', (), 'tasks', 'list_with_data', []),
    ('update_task_status', ('t', 'stopped', T0), 'tasks', 'update_status', False),
    ('delete_task', ('t',), 'tasks', 'delete', False),
    ('save_data_point', (POINT,), 'points', 'append', False),
    ('save_data_points', ([POINT],), 'points', 'append', False),
    ('get_task_data_points', ('t', 'm', 10, T0.isoformat()), 'points', 'fetch', []),
    ('get_data_point_count', ('t',), 'analytics', 'point_count', 0),
    ('get_sample_count', ('t',), 'analytics', 'sample_count', 0),
    ('get_task_data_points_bucketed', ('t', 'm', 10, T0.isoformat()),
     'analytics', 'legacy_buckets', []),
    ('get_metric_stats', ('t', 'm', T0.isoformat()), 'analytics', 'legacy_stats', None),
    ('get_last_point_timestamp', ('t',), 'analytics', 'last_point_timestamp', None),
    ('add_marker', ('t', T0, 'x'), 'markers', 'add', False),
    ('get_markers', ('t', T0.isoformat()), 'markers', 'list', []),
    ('cleanup_old_tasks', (30,), 'maintenance', 'cleanup_older_than', 0),
    ('get_db_size_bytes', (), 'maintenance', 'size_bytes', 0),
]


def test_repositories_are_public_attributes_sharing_one_factory(db):
    assert isinstance(db.tasks, TaskRepo)
    assert isinstance(db.points, PointRepo)
    assert isinstance(db.markers, MarkerRepo)
    assert isinstance(db.analytics, AnalyticsRepo)
    assert isinstance(db.maintenance, Maintenance)
    for repo in (db.tasks, db.points, db.markers, db.analytics, db.maintenance):
        assert repo._factory is db._factory


def test_internal_implementations_removed():
    for name in ('_row_to_task', '_parse_metric_types', '_insert_data_points',
                 '_delete_task_rows'):
        assert not hasattr(Database, name), name


@pytest.mark.parametrize('method, args, repo_attr, repo_method, fallback', SWALLOWING,
                         ids=[row[0] for row in SWALLOWING])
def test_storage_error_is_swallowed_logged_and_returns_old_value(
        db, monkeypatch, caplog, method, args, repo_attr, repo_method, fallback):
    monkeypatch.setattr(getattr(db, repo_attr), repo_method, _boom)
    with caplog.at_level(logging.ERROR, logger=database_module.logger.name):
        result = getattr(db, method)(*args)

    assert result == fallback and type(result) is type(fallback)
    records = [r for r in caplog.records if r.name == database_module.logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert isinstance(records[0].exc_info[1], StorageError)   # 带堆栈


def test_non_storage_errors_are_not_swallowed(db, monkeypatch):
    """编程错误（TypeError 等）不属于存储故障，门面不吞"""
    def type_error(*a, **k):
        raise TypeError("时间参数必须是 datetime")

    monkeypatch.setattr(db.tasks, 'get', type_error)
    with pytest.raises(TypeError):
        db.get_task('t')


def test_vacuum_raises_storage_error(db, monkeypatch):
    monkeypatch.setattr(db.maintenance, 'vacuum', _boom)
    with pytest.raises(StorageError):
        db.vacuum()


def test_iter_data_points_raises_storage_error(db, monkeypatch):
    monkeypatch.setattr(db._factory, 'connect', _sqlite_boom)
    with pytest.raises(StorageError):
        list(db.iter_data_points('t'))


def _sqlite_boom(*a, **k):
    import sqlite3
    raise sqlite3.OperationalError("disk I/O error")


def test_real_sqlite_failure_flows_through_repo_to_facade(db, monkeypatch, caplog):
    """不打桩仓储：连接层抛 sqlite3.Error → 仓储包成 StorageError → 门面吞掉返回旧值"""
    monkeypatch.setattr(db._factory, 'connect', _sqlite_boom)
    with caplog.at_level(logging.ERROR, logger=database_module.logger.name):
        assert db.get_task('t') is None
        assert db.get_markers('t') == []
        assert db.get_metric_stats('t', 'm') is None
    assert len(caplog.records) >= 3


# ---------- 委托与参数换算 ----------

def _spy(monkeypatch, repo, name, result):
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return result
    monkeypatch.setattr(repo, name, spy)
    return calls


def test_get_task_data_points_converts_since_and_limit(db, monkeypatch):
    calls = _spy(monkeypatch, db.points, 'fetch', ['ok'])
    since = T0 + timedelta(minutes=5)

    assert db.get_task_data_points('t', 'm', limit=50, since_iso=since.isoformat()) == ['ok']
    assert db.get_task_data_points('t', limit=0) == ['ok']
    assert db.get_task_data_points('t', 'm') == ['ok']

    queries = [args[0] for args, _ in calls]
    assert queries == [
        PointQuery('t', 'm', since=since, limit=50),
        PointQuery('t', None, since=None, limit=None),   # limit=0 → 不限（同旧实现 `if limit:`）
        PointQuery('t', 'm'),
    ]


def test_legacy_queries_receive_datetime(db, monkeypatch):
    stats = _spy(monkeypatch, db.analytics, 'legacy_stats', {'count': 1})
    buckets = _spy(monkeypatch, db.analytics, 'legacy_buckets', [])
    markers = _spy(monkeypatch, db.markers, 'list', [])
    since = T0 + timedelta(seconds=1, microseconds=500)

    db.get_metric_stats('t', 'm', since_iso=since.isoformat())
    db.get_task_data_points_bucketed('t', metric_type='m', max_buckets=7,
                                     since_iso=since.isoformat())
    db.get_markers('t', since_iso=since.isoformat())

    assert stats == [(('t', 'm'), {'since': since})]
    # 旧参数名 max_buckets 对应仓储的 max_points（位置参数）
    assert buckets == [(('t', 'm', 7), {'since': since})]
    assert markers == [(('t',), {'since': since})]


def test_bool_methods_keep_true_semantics_on_success(db):
    """旧语义：执行成功即 True（与是否命中行无关）"""
    assert db.save_task(TASK) is True
    assert db.update_task_status('missing', 'stopped') is True
    assert db.delete_task('missing') is True
    assert db.add_marker('t', T0, '打点') is True
    assert [m.label for m in db.get_markers('t')] == ['打点']
    assert db.save_data_points([POINT]) is True
    assert db.save_data_point(DataPoint('t', T0 + timedelta(seconds=1), 2.0, 'm')) is True
    assert db.get_sample_count('t') == 2


def test_save_failure_counter_resets_after_success(db, monkeypatch, caplog):
    """连续失败计数在门面：成功一次后复位，下一轮失败重新记完整堆栈"""
    db.save_task(TASK)
    real = db.points.append
    monkeypatch.setattr(db.points, 'append', _boom)
    for _ in range(3):
        assert db.save_data_points([POINT]) is False
    assert db._save_failures == 3

    monkeypatch.setattr(db.points, 'append', real)
    assert db.save_data_points([POINT]) is True
    assert db._save_failures == 0

    monkeypatch.setattr(db.points, 'append', _boom)
    with caplog.at_level(logging.ERROR, logger=database_module.logger.name):
        assert db.save_data_points([POINT]) is False
    assert caplog.records[-1].exc_info is not None


# ========== 迁移报告映射：升级成功时的备份位置；只读时的更高版本号 ==========

def _report(outcome, backup_path=None):
    from data.db.migrations.runner import MigrationReport
    return MigrationReport(outcome, 1, 5 if outcome.value == 'migrated' else 1,
                           backup_path=backup_path)


def test_migrated_report_exposes_backup_path(tmp_path):
    from data.db.migrations.runner import MigrationOutcome
    backup = str(tmp_path / 'monitor.db.bak_v1')
    db = Database(str(tmp_path / 'monitor.db'), migrate=False,
                  migration_report=_report(MigrationOutcome.MIGRATED, backup))
    assert (db.migrated_from, db.backup_path) == (1, backup)
    assert (db.migration_failed, db.data_reset, db.backup_aborted) == (False, False, False)


def test_failed_report_has_no_backup_hint(tmp_path):
    """失败结果即使带了 backup_path 也不给"已升级"提示用的字段"""
    from data.db.migrations.runner import MigrationOutcome
    for outcome in MigrationOutcome:
        if outcome is MigrationOutcome.MIGRATED:
            continue
        db = Database(str(tmp_path / 'monitor.db'), migrate=False,
                      migration_report=_report(outcome, str(tmp_path / 'x.bak')))
        assert (db.migrated_from, db.backup_path) == (None, None), outcome


def test_newer_version_only_kept_when_read_only(tmp_path):
    path = str(tmp_path / 'monitor.db')
    Database(path).close()
    assert Database(path, migrate=False, newer_version='9.1.0').newer_version is None
    ro = Database(path, read_only=True, newer_version='9.1.0')
    try:
        assert ro.read_only and ro.newer_version == '9.1.0'
    finally:
        ro.close()
