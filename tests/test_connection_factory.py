"""
ConnectionFactory 用例：connect()/transaction() 的提交、回滚与关闭语义，连接 PRAGMA、checkpoint()，
以及常驻占位连接对 -wal 文件生命周期的影响（实测：没有占位连接时，每次最后一个连接
关闭 SQLite 都会 checkpoint 并删除 -wal，周期写入等于每次重建 -wal）
"""
import os
import sqlite3
import threading

import pytest

import data.db.connection as connection_module
from data.database import Database
from data.db.connection import JOURNAL_SIZE_LIMIT, ConnectionFactory


def _make_table(factory: ConnectionFactory):
    with factory.transaction() as cursor:
        cursor.execute('CREATE TABLE IF NOT EXISTS t (v INTEGER)')


def _count(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute('SELECT COUNT(*) FROM t').fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def factory(db_path):
    f = ConnectionFactory(db_path, keep_alive=False)
    _make_table(f)
    return f


# ========== connect() ==========

def test_connect_commits_on_normal_exit(factory, db_path):
    with factory.connect() as conn:
        conn.execute('INSERT INTO t VALUES (1)')
    assert _count(db_path) == 1


def test_connect_rolls_back_and_reraises(factory, db_path):
    with pytest.raises(ValueError):
        with factory.connect() as conn:
            conn.execute('INSERT INTO t VALUES (1)')
            raise ValueError("boom")
    assert _count(db_path) == 0


def test_connect_closes_connection_and_uses_row_factory(factory):
    with factory.connect() as conn:
        row = conn.execute('SELECT 7 AS answer').fetchone()
        assert row['answer'] == 7
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_connect_sets_pragmas(factory):
    with factory.connect() as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
        assert conn.execute('PRAGMA synchronous').fetchone()[0] == 1   # NORMAL
        assert conn.execute('PRAGMA journal_size_limit').fetchone()[0] == JOURNAL_SIZE_LIMIT
    assert JOURNAL_SIZE_LIMIT == 8 * 1024 * 1024


# ========== transaction() ==========

def test_transaction_commits_on_normal_exit(factory, db_path):
    with factory.transaction() as cursor:
        cursor.execute('INSERT INTO t VALUES (1)')
        cursor.execute('CREATE TABLE extra (x INTEGER)')
    assert _count(db_path) == 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='extra'").fetchone()[0] == 1
    finally:
        conn.close()


def test_transaction_rolls_back_dml_ddl_and_user_version(factory, db_path):
    with pytest.raises(RuntimeError):
        with factory.transaction() as cursor:
            cursor.execute('INSERT INTO t VALUES (1)')
            cursor.execute('CREATE TABLE scratch (x INTEGER)')
            cursor.execute('PRAGMA user_version = 99')
            raise RuntimeError("boom")

    assert _count(db_path) == 0
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='scratch'").fetchone()[0] == 0
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 0
    finally:
        conn.close()


def test_transaction_is_immediate_and_closes(factory):
    with factory.transaction() as cursor:
        conn = cursor.connection
        assert conn.isolation_level is None
        assert conn.in_transaction is True   # BEGIN IMMEDIATE 已执行
        assert cursor.execute('SELECT 3 AS x').fetchone()['x'] == 3
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_transaction_holds_write_lock(factory, db_path):
    """BEGIN IMMEDIATE 期间其他连接拿不到写锁"""
    with factory.transaction():
        other = sqlite3.connect(db_path, timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError):
                other.execute('INSERT INTO t VALUES (1)')
        finally:
            other.close()


# ========== 常驻占位连接与 -wal ==========

def _write(factory: ConnectionFactory, n: int = 1):
    with factory.connect() as conn:
        conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(n)])


def test_without_keep_alive_wal_is_deleted_after_every_write(factory, db_path):
    """基线（问题本身）：没有其他连接时，每次写入关闭连接后 -wal 都被删除"""
    for _ in range(3):
        _write(factory)
        assert not os.path.exists(db_path + '-wal')


def test_keep_alive_keeps_wal_between_writes(db_path):
    f = ConnectionFactory(db_path)   # 默认开启
    assert f.keep_alive is True and f.keep_alive_open is False
    _make_table(f)
    assert f.keep_alive_open is True

    sizes = []
    for _ in range(3):
        _write(f)
        assert os.path.exists(db_path + '-wal')
        sizes.append(os.path.getsize(db_path + '-wal'))
    assert sizes == sorted(sizes) and sizes[-1] > 0   # 同一个 -wal 持续追加，没有重建
    assert _count(db_path) == 3

    # 释放占位连接后它就是最后一个连接：checkpoint 并删除 -wal，数据已落主库
    f.close()
    assert f.keep_alive_open is False
    assert not os.path.exists(db_path + '-wal')
    assert _count(db_path) == 3


def test_keep_alive_reopens_after_close(db_path):
    f = ConnectionFactory(db_path)
    _make_table(f)
    f.close()
    _write(f)
    assert f.keep_alive_open is True
    assert os.path.exists(db_path + '-wal')
    f.close()


def test_keep_alive_holder_is_query_only(db_path):
    f = ConnectionFactory(db_path)
    _make_table(f)
    with pytest.raises(sqlite3.OperationalError):
        f._holder.execute('INSERT INTO t VALUES (1)')
    f.close()


def test_keep_alive_allows_checkpoint_and_vacuum(db_path):
    """空闲的占位连接不妨碍 wal_checkpoint(TRUNCATE) 与 VACUUM"""
    f = ConnectionFactory(db_path)
    _make_table(f)
    _write(f, 100)
    with f.connect() as conn:
        busy, _, _ = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert busy == 0
        conn.execute('VACUUM')
    assert _count(db_path) == 100
    f.close()


def test_keep_alive_open_failure_degrades_gracefully(db_path, monkeypatch):
    """占位连接打开失败只记日志并关闭该功能，本次读写照常完成"""
    f = ConnectionFactory(db_path, keep_alive=False)
    _make_table(f)
    f.keep_alive = True
    real_connect = sqlite3.connect

    def _connect(path, *args, **kwargs):
        if kwargs.get('check_same_thread') is False:
            raise sqlite3.OperationalError("模拟打不开")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr('data.db.connection.sqlite3.connect', _connect)
    _write(f)
    assert f.keep_alive is False and f.keep_alive_open is False
    assert _count(db_path) == 1


def test_keep_alive_opened_in_worker_thread_can_close_in_main(db_path):
    """占位连接可能在后台线程首次打开，在主线程关闭"""
    f = ConnectionFactory(db_path)
    errors = []

    def _worker():
        try:
            _make_table(f)
            _write(f)
        except Exception as e:   # pragma: no cover - 失败时由断言报告
            errors.append(e)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert errors == [] and f.keep_alive_open is True
    f.close()
    assert not os.path.exists(db_path + '-wal')


def test_database_opens_keep_alive_only_after_migration(db_path):
    """Database 构造（含迁移）不打开占位连接，首次读写时才打开"""
    db = Database(db_path)
    assert db._factory.keep_alive_open is False
    db.get_all_tasks()
    assert db._factory.keep_alive_open is True
    db.close()
    assert db._factory.keep_alive_open is False


# ========== checkpoint() 与 -wal 大小上限 ==========

def _wal_size(path: str) -> int:
    return os.path.getsize(path + '-wal') if os.path.exists(path + '-wal') else 0


def _write_big(factory: ConnectionFactory, megabytes: int = 12):
    """单个事务写入约 megabytes MB，把 -wal 撑过 journal_size_limit"""
    with factory.connect() as conn:
        conn.executemany('INSERT INTO t VALUES (?)',
                         [(b'x' * 1000,) for _ in range(megabytes * 1000)])


def test_checkpoint_truncate_shrinks_wal_after_large_write(db_path):
    """大事务把 -wal 撑大后，checkpoint(truncate=True) 成功并把 -wal 截到上限以内（实际为 0）"""
    f = ConnectionFactory(db_path)
    _make_table(f)
    _write_big(f)
    assert _wal_size(db_path) > JOURNAL_SIZE_LIMIT

    assert f.checkpoint(truncate=True) is True
    assert _wal_size(db_path) <= JOURNAL_SIZE_LIMIT
    assert _wal_size(db_path) == 0
    assert _count(db_path) == 12000
    f.close()


def test_passive_checkpoint_keeps_wal_until_next_write_caps_it(db_path):
    """PASSIVE checkpoint 只合并不缩小；下次写入重置日志时 -wal 才截到 journal_size_limit"""
    f = ConnectionFactory(db_path)
    _make_table(f)
    _write_big(f)
    assert f.checkpoint(truncate=False) is True
    assert _wal_size(db_path) > JOURNAL_SIZE_LIMIT

    _write(f)
    assert 0 < _wal_size(db_path) <= JOURNAL_SIZE_LIMIT
    f.close()


def test_checkpoint_returns_false_when_busy(db_path, monkeypatch):
    """
    另一个连接持有旧快照时 TRUNCATE checkpoint 无法完成：SQLite 不抛错而是返回 busy=1，
    checkpoint() 应返回 False。缩短 busy_timeout 避免用例等满 5 秒。
    """
    real_apply = connection_module._apply_pragmas

    def _short_timeout(conn, *args, **kwargs):
        real_apply(conn, *args, **kwargs)
        conn.execute('PRAGMA busy_timeout=50')

    monkeypatch.setattr(connection_module, '_apply_pragmas', _short_timeout)
    f = ConnectionFactory(db_path)
    _make_table(f)
    _write(f)

    reader = sqlite3.connect(db_path, isolation_level=None)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT COUNT(*) FROM t').fetchone()   # 持有读快照
        _write(f)   # 快照之后的新提交，checkpoint 不能越过读者合并它们
        with f.connect() as conn:
            busy, _, _ = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert busy == 1   # 前提：SQLite 确实以返回值而非异常报告阻塞
        assert f.checkpoint(truncate=True) is False
        reader.execute('COMMIT')
    finally:
        reader.close()

    assert f.checkpoint(truncate=True) is True
    f.close()


def test_checkpoint_returns_false_on_sqlite_error(db_path):
    """库文件损坏等 SQLite 错误只记日志并返回 False，不抛出"""
    with open(db_path, 'wb') as fp:
        fp.write(b'not a sqlite database' * 100)
    assert ConnectionFactory(db_path, keep_alive=False).checkpoint() is False
