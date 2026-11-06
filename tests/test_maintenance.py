"""
Maintenance：过期任务清理（三表、只删已停止且过期的）、vacuum 失败抛 StorageError、
size_bytes 统计主库与 WAL 边车文件、常驻连接下 vacuum/清理后 -wal 被截断
"""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from data.errors import StorageError
from data.maintenance import Maintenance
from data.models import DataPoint, MonitorTask
from data.repos.marker_repo import MarkerRepo
from data.repos.point_repo import PointQuery, PointRepo
from data.repos.task_repo import TaskRepo
from tests.helpers import ConnectionFactoryStub, make_broken_factories, make_v4_db


@pytest.fixture
def factory(tmp_path):
    return ConnectionFactoryStub(make_v4_db(str(tmp_path / 'repo.db')))


def _seed(factory, task_id, *, status, start, end=None, points=3):
    TaskRepo(factory).save(MonitorTask(
        task_id=task_id, pid=1, process_name='a.exe', metric_types=['m'], interval=1.0,
        start_time=start, end_time=end, status=status))
    PointRepo(factory).append([DataPoint(task_id, start + timedelta(seconds=i), 1.0, 'm')
                               for i in range(points)])
    MarkerRepo(factory).add(task_id, start, 'x')


def _count(factory, table, task_id):
    with factory.connect() as conn:
        return conn.execute(f'SELECT COUNT(*) FROM {table} WHERE task_id = ?',
                            (task_id,)).fetchone()[0]


def test_cleanup_disabled_does_not_touch_db(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        assert Maintenance(factory).cleanup_older_than(0) == 0
        assert Maintenance(factory).cleanup_older_than(-1) == 0


def test_cleanup_deletes_only_expired_stopped_tasks(factory):
    now = datetime.now()
    old = now - timedelta(days=40)
    _seed(factory, 'expired', status='stopped', start=old, end=old + timedelta(hours=1))
    _seed(factory, 'expired_no_end', status='stopped', start=old)       # 回退到 start_time
    _seed(factory, 'old_but_running', status='running', start=old)
    _seed(factory, 'recent', status='stopped', start=now - timedelta(days=40),
          end=now - timedelta(days=1))                                  # 按结束时间判断

    assert Maintenance(factory).cleanup_older_than(30) == 2

    tasks = TaskRepo(factory)
    for gone in ('expired', 'expired_no_end'):
        assert tasks.get(gone) is None
        assert _count(factory, 'data_points', gone) == 0
        assert _count(factory, 'task_markers', gone) == 0
    for kept in ('old_but_running', 'recent'):
        assert tasks.get(kept) is not None
        assert _count(factory, 'data_points', kept) == 3
        assert _count(factory, 'task_markers', kept) == 1
    assert Maintenance(factory).cleanup_older_than(30) == 0


def test_vacuum_succeeds_and_reclaims_space(factory):
    start = datetime(2026, 1, 1)
    _seed(factory, 'big', status='stopped', start=start, end=start, points=20000)
    TaskRepo(factory).delete('big')
    maintenance = Maintenance(factory)
    before = maintenance.size_bytes()
    maintenance.vacuum()
    assert maintenance.size_bytes() < before


def test_vacuum_failure_raises_storage_error(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        with pytest.raises(StorageError) as info:
            Maintenance(factory).vacuum()
        assert isinstance(info.value.__cause__, sqlite3.Error)


def test_cleanup_failure_raises_storage_error(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        with pytest.raises(StorageError):
            Maintenance(factory).cleanup_older_than(30)


def test_size_bytes_sums_main_and_sidecar_files(tmp_path):
    db_path = str(tmp_path / 'size.db')
    factory = ConnectionFactoryStub(db_path)
    maintenance = Maintenance(factory)
    assert maintenance.size_bytes() == 0      # 文件都不存在

    for suffix, size in (('', 100), ('-wal', 20), ('-shm', 3)):
        with open(db_path + suffix, 'wb') as f:
            f.write(b'\0' * size)
    assert maintenance.size_bytes() == 123

    os.remove(db_path + '-shm')
    assert maintenance.size_bytes() == 120


def test_size_bytes_other_os_errors_raise(tmp_path, monkeypatch):
    factory = ConnectionFactoryStub(str(tmp_path / 'size.db'))

    def _deny(_path):
        raise PermissionError('denied')

    monkeypatch.setattr(os.path, 'getsize', _deny)
    with pytest.raises(StorageError) as info:
        Maintenance(factory).size_bytes()
    assert isinstance(info.value.__cause__, PermissionError)


# ========== 常驻连接下的 WAL 截断（迁移评审 M1） ==========

WAL_LIMIT = 1024 * 1024


def _wal_size(db_path):
    try:
        return os.path.getsize(db_path + '-wal')
    except FileNotFoundError:
        return 0


@pytest.fixture
def live_factory(tmp_path):
    """真实连接工厂，常驻占位连接打开：-wal 不会随工作连接关闭而删除"""
    from data.db.connection import ConnectionFactory
    factory = ConnectionFactory(make_v4_db(str(tmp_path / 'live.db')), keep_alive=True)
    yield factory
    factory.close()


def _seed_bulk(factory, task_id, start, points):
    TaskRepo(factory).save(MonitorTask(
        task_id=task_id, pid=1, process_name='a.exe', metric_types=['m'], interval=1.0,
        start_time=start, end_time=start, status='stopped'))
    PointRepo(factory).append([DataPoint(task_id, start + timedelta(seconds=i), float(i), 'm')
                               for i in range(points)])


def test_vacuum_truncates_wal_with_keep_alive_connection(live_factory):
    _seed_bulk(live_factory, 'big', datetime(2026, 1, 1), 60000)
    assert live_factory.keep_alive_open
    assert _wal_size(live_factory.db_path) > WAL_LIMIT   # 前提：写入确实把 -wal 撑大了

    Maintenance(live_factory).vacuum()

    assert live_factory.keep_alive_open
    assert _wal_size(live_factory.db_path) <= WAL_LIMIT
    # 截断后库仍可正常读写
    assert len(PointRepo(live_factory).fetch(PointQuery('big', 'm', limit=1))) == 1


def test_cleanup_truncates_wal_after_bulk_delete(live_factory):
    old = datetime.now() - timedelta(days=40)
    _seed_bulk(live_factory, 'expired', old, 60000)
    Maintenance(live_factory)._checkpoint_truncate()
    assert _wal_size(live_factory.db_path) <= WAL_LIMIT

    assert Maintenance(live_factory).cleanup_older_than(30) == 1

    assert live_factory.keep_alive_open
    assert _wal_size(live_factory.db_path) <= WAL_LIMIT
