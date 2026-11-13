"""
存储健康的底层操作（data/db/maintenance.py）：-wal 兜底 checkpoint 与 PRAGMA quick_check。
全部用 tmp_path 临时库，真实 SQLite，不碰项目 data 目录。
"""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from data.database import Database
from data.db import maintenance as health
from data.db.connection import ConnectionFactory
from data.models import DataPoint, MonitorTask


def _fill(db: Database, rows: int = 3000) -> None:
    task = MonitorTask(task_id="t1", pid=1, process_name="p.exe",
                       metric_types=["memory_private"], interval=1.0, note="",
                       start_time=datetime(2026, 11, 1, 9, 0, 0), end_time=None,
                       status="running")
    db.tasks.save(task)
    start = datetime(2026, 11, 1, 9, 0, 1)
    for k in range(rows // 500):
        db.points.append([DataPoint("t1", start + timedelta(seconds=k * 500 + i),
                                    1000.0 + i, "memory_private") for i in range(500)])


def _fill_more(db: Database) -> None:
    start = datetime(2026, 11, 2, 9, 0, 1)
    db.points.append([DataPoint("t1", start + timedelta(seconds=i), 5.0 + i, "memory_private")
                      for i in range(500)])


# ---------- wal_size ----------

def test_wal_size_missing_file_is_zero(tmp_path):
    assert health.wal_size(str(tmp_path / "nope.db")) == 0


def test_wal_size_reports_file_size(db):
    _fill(db)
    assert health.wal_size(db.db_path) == os.path.getsize(db.db_path + "-wal") > 0
    assert db.wal_size_bytes() == health.wal_size(db.db_path)


# ---------- 兜底 checkpoint ----------

def test_checkpoint_wal_truncates_when_unblocked(db):
    _fill(db)
    assert health.wal_size(db.db_path) > 0
    outcome = db.checkpoint_wal()
    assert outcome.complete and not outcome.fell_back and outcome.mode == 'TRUNCATE'
    assert outcome.error is None
    assert outcome.size_before > 0
    assert outcome.size_after == 0 == health.wal_size(db.db_path)
    assert outcome.elapsed_s >= 0
    assert db.get_sample_count("t1") > 0     # 数据都在主库里


def test_checkpoint_wal_falls_back_to_passive_when_blocked(db):
    """有读者压着时 TRUNCATE 合并不完，退到 PASSIVE，不抛异常"""
    _fill(db)
    reader = sqlite3.connect(db.db_path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM tasks").fetchall()   # 占住读快照
        _fill_more(db)                                            # 读者之后的新写入
        outcome = health.checkpoint_wal(db._factory, busy_timeout_ms=50)
    finally:
        reader.rollback()
        reader.close()
    assert outcome.fell_back and outcome.mode == 'PASSIVE' and not outcome.complete
    assert outcome.error is None


def test_checkpoint_wal_size_function_is_injectable(db):
    """前后大小经注入的 size_of 取得（测试与日志口径一致）"""
    sizes = iter([111, 222])
    outcome = health.checkpoint_wal(db._factory, size_of=lambda path: next(sizes))
    assert (outcome.size_before, outcome.size_after) == (111, 222)


def test_checkpoint_wal_reports_error_instead_of_raising(tmp_path):
    class Broken:
        db_path = str(tmp_path / "x.db")

        def wal_checkpoint(self, mode, busy_timeout_ms=None):
            raise sqlite3.OperationalError("disk I/O error")

    outcome = health.checkpoint_wal(Broken())
    assert outcome.fell_back and outcome.error == "disk I/O error" and not outcome.complete


def test_checkpoint_busy_timeout_is_applied_before_the_checkpoint(tmp_path):
    """wal_checkpoint 用调用方给的忙等待上限，而不是连接默认的 5 秒（别让采集线程跟着等）"""
    seen = []
    factory = ConnectionFactory(str(tmp_path / "t.db"))
    real_connect = factory.connect

    @contextmanager
    def spy():
        with real_connect() as conn:
            class Wrap:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *args):
                    seen.append(sql)
                    return conn.execute(sql, *args)
            yield Wrap()

    factory.connect = spy
    factory.wal_checkpoint('PASSIVE', 123)
    assert 'PRAGMA busy_timeout=123' in seen
    assert seen.index('PRAGMA busy_timeout=123') < seen.index('PRAGMA wal_checkpoint(PASSIVE)')
    factory.close()


def test_legacy_checkpoint_method_still_works(db):
    """ConnectionFactory.checkpoint 的旧语义不变（清理与压缩沿用）"""
    _fill(db)
    assert db._factory.checkpoint(truncate=True) is True
    assert health.wal_size(db.db_path) == 0


# ---------- quick_check ----------

def test_quick_check_ok_on_healthy_db(db):
    _fill(db)
    result = health.QuickCheck(db.db_path).run()
    assert result.ok and result.messages == ('ok',) and result.summary == "ok"
    assert not result.interrupted and result.error is None
    assert result.elapsed_s >= 0


def test_quick_check_does_not_modify_the_db_and_writers_proceed(db):
    _fill(db)
    before = os.path.getsize(db.db_path)
    assert db.new_quick_check().run().ok
    _fill_more(db)         # 检查之后写入毫无障碍
    assert os.path.getsize(db.db_path) >= before


def _corrupt(path: str) -> None:
    size = os.path.getsize(path)
    with open(path, 'r+b') as f:
        f.seek(size // 2 // 4096 * 4096)
        f.write(b'\xde\xad\xbe\xef' * 1024)


def test_quick_check_reports_corruption(tmp_path):
    path = str(tmp_path / "bad.db")
    db = Database(path)
    _fill(db, rows=20000)
    db.checkpoint_wal()
    db.close()
    _corrupt(path)
    result = health.QuickCheck(path).run()
    assert not result.ok and result.error is None
    assert result.messages and result.messages != ('ok',)
    assert result.summary and result.summary != "ok"


def test_quick_check_not_a_database_counts_as_corruption(tmp_path):
    path = str(tmp_path / "junk.db")
    with open(path, 'wb') as f:
        f.write(os.urandom(8192))
    result = health.QuickCheck(path).run()
    assert not result.ok and result.error is None and result.messages


def test_quick_check_open_failure_is_not_corruption(tmp_path):
    """打不开库是"没能检查"（error），不是"库损坏"：不能因此给用户发完整性警告"""
    result = health.QuickCheck(str(tmp_path / "missing" / "no.db")).run()
    assert not result.ok and result.error and not result.messages
    assert "检查未能完成" in result.summary


def test_quick_check_interrupt_before_run(db):
    check = health.QuickCheck(db.db_path)
    check.interrupt()
    result = check.run()
    assert result.interrupted and not result.ok


def test_quick_check_interrupt_during_run_returns_promptly(db):
    """长检查中途 interrupt()：很快返回 interrupted（退出路径不会被它卡住）"""
    _fill(db, rows=3000)
    with sqlite3.connect(db.db_path) as conn:    # 造一张大表，让检查有足够的工作量
        conn.execute("CREATE TABLE bulk (a INTEGER, b TEXT)")
        conn.execute("CREATE INDEX bulk_a ON bulk(a)")
        conn.executemany("INSERT INTO bulk VALUES (?, ?)",
                         ((i, 'x' * 100) for i in range(300000)))
    check = health.QuickCheck(db.db_path)
    out = {}

    def runner():
        out['result'] = check.run()
        out['done'] = time.perf_counter()

    thread = threading.Thread(target=runner)
    thread.start()
    time.sleep(0.01)
    asked = time.perf_counter()
    check.interrupt()
    thread.join(5)
    assert not thread.is_alive()
    result = out['result']
    assert result.interrupted or result.ok       # 机器太快、检查先跑完也可
    if result.interrupted:
        assert out['done'] - asked < 1.0


def test_quick_check_uses_readonly_connection(db):
    _fill(db)
    uri = health.readonly_uri(db.db_path)
    assert uri.startswith("file:///") and uri.endswith("?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_fail (a)")
    finally:
        conn.close()


def test_quick_check_opens_the_database_read_only(db, monkeypatch):
    """QuickCheck 第一个打开的连接就是 mode=ro 的 URI 连接（不写库、不切日志模式）"""
    _fill(db)
    opened = []
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append((str(target), kwargs.get('uri', False)))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(health.sqlite3, 'connect', spy)
    assert health.QuickCheck(db.db_path).run().ok
    assert len(opened) == 1
    target, uri = opened[0]
    assert uri is True and target.startswith("file:") and target.endswith("?mode=ro")


def test_quick_check_falls_back_to_query_only_connection(db, monkeypatch):
    """mode=ro 打不开时（-shm 还不存在等）退到 query_only 的普通连接，仍然只读"""
    _fill(db)
    real = sqlite3.connect
    calls = []

    def spy(target, *args, **kwargs):
        calls.append(kwargs.get('uri', False))
        if kwargs.get('uri'):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *args, **kwargs)

    monkeypatch.setattr(health.sqlite3, 'connect', spy)
    result = health.QuickCheck(db.db_path).run()
    assert result.ok and calls == [True, False]


def test_quick_check_handles_non_ascii_and_spaces_in_path(tmp_path):
    folder = tmp_path / "进程 监控 #1"
    folder.mkdir()
    path = str(folder / "monitor.db")
    db = Database(path)
    _fill(db)
    assert health.QuickCheck(path).run().ok
    db.close()
