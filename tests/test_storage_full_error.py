"""
磁盘已满的错误分类：sqlite3 抛 SQLITE_FULL（或有明确证据的 SQLITE_IOERR 族写入错误）时，
仓储层转成 StorageError 的子类 StorageFullError；其余仍是普通 StorageError。

真实注入：tests/storage_faults.simulate_disk_full 用 PRAGMA max_page_count 让 SQLite 报出
真正的 SQLITE_FULL；IOERR 的变体手工构造异常（真实库难以触发）。
"""
import errno
import sqlite3
from datetime import datetime, timedelta

import pytest

from data.errors import StorageError, StorageFullError
from data.models import DataPoint, MonitorTask
from data.repos import _guard
from data.repos._guard import is_disk_full, storage_errors
from tests.storage_faults import make_sqlite_error, simulate_disk_full

SQLITE_FULL = 13
SQLITE_IOERR = 10
SQLITE_IOERR_WRITE = 778
SQLITE_IOERR_READ = 266
SQLITE_IOERR_SHMSIZE = 4618


def test_storage_full_error_is_a_storage_error():
    assert issubclass(StorageFullError, StorageError)


def test_sqlite_full_is_classified():
    exc = make_sqlite_error(SQLITE_FULL, "database or disk is full", 'SQLITE_FULL')
    with pytest.raises(StorageFullError) as info:
        with storage_errors("写入数据点"):
            raise exc
    assert info.value.__cause__ is exc
    assert "写入数据点失败" in str(info.value)


def test_storage_full_is_still_caught_by_except_storage_error():
    """现有的 except StorageError 照常捕获（门面旧方法据此返回 False）"""
    caught = None
    try:
        with storage_errors("保存任务"):
            raise make_sqlite_error(SQLITE_FULL, "database or disk is full")
    except StorageError as e:
        caught = e
    assert isinstance(caught, StorageFullError)


def test_other_sqlite_errors_stay_plain_storage_error():
    for exc in (sqlite3.OperationalError("database is locked"),
                make_sqlite_error(5, "database is locked"),
                make_sqlite_error(SQLITE_IOERR_READ, "disk I/O error"),
                sqlite3.DatabaseError("database disk image is malformed")):
        with pytest.raises(StorageError) as info:
            with storage_errors("读取任务"):
                raise exc
        assert not isinstance(info.value, StorageFullError), exc


def test_ioerr_needs_evidence_of_a_full_disk():
    # 没有证据：设备故障等同样报 IOERR_WRITE，不能当"磁盘满"
    assert not is_disk_full(make_sqlite_error(SQLITE_IOERR_WRITE, "disk I/O error"))
    # 消息写明
    assert is_disk_full(make_sqlite_error(SQLITE_IOERR_WRITE, "database or disk is full"))
    assert is_disk_full(make_sqlite_error(SQLITE_IOERR_SHMSIZE, "No space left on device"))
    # 原因链上的 OSError：ENOSPC 或 Windows 的 ERROR_DISK_FULL(112)
    for oserror in (OSError(errno.ENOSPC, "No space left on device"), _winerror(112),
                    _winerror(39)):
        exc = make_sqlite_error(SQLITE_IOERR_WRITE, "disk I/O error")
        exc.__cause__ = oserror
        assert is_disk_full(exc), oserror
    # 读类 IOERR 即便消息里有"磁盘满"字样也不算
    assert not is_disk_full(make_sqlite_error(SQLITE_IOERR_READ, "disk is full"))
    # 无关的 OSError
    exc = make_sqlite_error(SQLITE_IOERR_WRITE, "disk I/O error")
    exc.__cause__ = PermissionError(errno.EACCES, "denied")
    assert not is_disk_full(exc)


def _winerror(code):
    e = OSError(0, "disk full")
    e.winerror = code
    return e


def test_old_python_without_error_codes_falls_back_to_message():
    exc = sqlite3.OperationalError("database or disk is full")
    exc.sqlite_errorcode = None
    assert is_disk_full(exc)
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = None
    assert not is_disk_full(exc)


def test_error_chain_is_cycle_safe():
    a = make_sqlite_error(SQLITE_IOERR_WRITE, "disk I/O error")
    b = OSError("x")
    a.__cause__ = b
    b.__context__ = a
    assert not is_disk_full(a)


# ---------- 真实 SQLITE_FULL（max_page_count 注入）----------

def _task():
    return MonitorTask(task_id="t-full", pid=1, process_name="p.exe",
                       metric_types=["memory_private"], interval=1.0, note="",
                       start_time=datetime(2026, 11, 1, 9, 0, 0), end_time=None,
                       status="running")


def _batch(n, start=datetime(2026, 11, 1, 9, 0, 1)):
    return [DataPoint("t-full", start + timedelta(seconds=i), 1000.0 + i, "memory_private")
            for i in range(n)]


def test_real_sqlite_full_becomes_storage_full_error(db):
    """真实的 SQLITE_FULL 经仓储抛出 StorageFullError，原始异常挂在 __cause__"""
    db.tasks.save(_task())
    db.points.append(_batch(10))
    with simulate_disk_full(db):
        with pytest.raises(StorageFullError) as info:
            for k in range(200):    # 持续写，直到需要新页
                db.points.append(_batch(500, datetime(2026, 11, 2, 0, 0, 0) + timedelta(hours=k)))
    cause = info.value.__cause__
    assert isinstance(cause, sqlite3.OperationalError)
    assert cause.sqlite_errorcode == SQLITE_FULL
    assert "full" in str(cause)


def test_facade_still_degrades_on_storage_full(db, caplog):
    """门面旧方法捕获 StorageError（含子类）：返回 False，不抛"""
    db.tasks.save(_task())
    with simulate_disk_full(db):
        results = [db.save_data_points(
            _batch(500, datetime(2026, 11, 2, 0, 0, 0) + timedelta(hours=k)))
            for k in range(200)]
    assert False in results
    # 恢复后写入照常，且失败的批次没有留下半截数据
    assert db.save_data_points(_batch(3, datetime(2026, 12, 1)))


def test_failed_write_leaves_no_partial_data(db):
    db.tasks.save(_task())
    before = db.get_sample_count("t-full")
    with simulate_disk_full(db):
        with pytest.raises(StorageFullError):
            db.points.append(_batch(50000))
    assert db.get_sample_count("t-full") == before


def test_guard_module_classifies_before_raising(monkeypatch):
    """去掉分类（始终抛普通 StorageError）本测试应失败：守护 storage_errors 确实走 is_disk_full"""
    calls = []
    real = _guard.is_disk_full
    monkeypatch.setattr(_guard, 'is_disk_full', lambda e: calls.append(e) or real(e))
    with pytest.raises(StorageFullError):
        with storage_errors("写入"):
            raise make_sqlite_error(SQLITE_FULL, "database or disk is full")
    assert len(calls) == 1
