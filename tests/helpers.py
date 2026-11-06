"""测试辅助：轮询等待，替代固定 time.sleep 的时序断言"""
import time


def poll_until(condition, timeout: float = 10.0, interval: float = 0.01) -> bool:
    """轮询等待 condition() 为真，最长 timeout 秒；超时返回最后一次判定结果"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


def wait_stable(getter, quiet: float = 0.4, timeout: float = 10.0) -> bool:
    """等待 getter() 的值连续 quiet 秒不变（用于确认"已不再产生新数据"）"""
    deadline = time.monotonic() + timeout
    last = getter()
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.02)
        current = getter()
        if current != last:
            last = current
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= quiet:
            return True
    return False


def stop_and_join(manager, timeout_ms: int = 5000) -> bool:
    """停止 manager 的全部任务并带超时等待线程结束（stop_all_tasks 本身不再等待）。
    用例收尾与需要"任务已全部收尾落库"的断言前调用。"""
    manager.stop_all_tasks()
    return manager.join_all(timeout_ms=timeout_ms)


def process_events_until(condition, timeout: float = 5.0) -> bool:
    """一边处理 Qt 事件一边等 condition() 为真（队列信号与 deleteLater 需要事件循环）"""
    from PyQt5.QtCore import QCoreApplication, QEvent

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if condition():
            return True
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return bool(condition())


def record_stop_reasons(task) -> list:
    """直连记录 task_stopped 的停止原因（StopReason），在发射线程内同步追加，
    适用于 MonitorTask 与 MonitorManager"""
    from PyQt5.QtCore import Qt

    reasons = []
    task.task_stopped.connect(
        lambda _tid, reason: reasons.append(reason), Qt.DirectConnection)
    return reasons


class ConnectionFactoryStub:
    """
    与 data.db.connection.ConnectionFactory 同接口的测试桩（db_path / connect() / transaction()），
    两个上下文管理器照搬 Database._get_connection / Database._transaction，
    供仓储单元测试在连接工厂就绪前独立驱动仓储。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _open(self, **kwargs):
        import sqlite3
        conn = sqlite3.connect(self.db_path, **kwargs)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _pragmas(conn):
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = self._open()
            try:
                self._pragmas(conn)
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return _cm()

    def transaction(self):
        import sqlite3
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            conn = self._open(isolation_level=None)
            try:
                self._pragmas(conn)
                conn.execute('BEGIN IMMEDIATE')
                try:
                    yield conn.cursor()
                    conn.execute('COMMIT')
                except BaseException:
                    try:
                        conn.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                conn.close()
        return _cm()


def make_v4_db(db_path: str) -> str:
    """
    建一个 v4 结构的临时库并返回路径。
    现在先用 Database(path) 建 v3，再补 v4 的两列；连接工厂/迁移的 v4 就绪后改为直接用 Database。
    """
    import sqlite3
    from data.database import Database

    Database(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
        if 'create_time' not in columns:
            conn.execute('ALTER TABLE tasks ADD COLUMN create_time REAL')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(task_markers)')}
        if 'kind' not in columns:
            conn.execute("ALTER TABLE task_markers ADD COLUMN kind TEXT NOT NULL DEFAULT 'user'")
        conn.commit()
    finally:
        conn.close()
    return db_path


def make_broken_factories(tmp_dir: str) -> list:
    """
    两个必然触发 sqlite3.Error 的连接工厂桩：库所在目录不存在（打不开）、库文件已损坏
    （不是 SQLite 文件），用于验证仓储把 sqlite3.Error 包成 StorageError。
    """
    import os

    corrupt = os.path.join(tmp_dir, 'corrupt.db')
    with open(corrupt, 'wb') as f:
        f.write(b'this is not a sqlite database file' * 64)
    return [
        ConnectionFactoryStub(os.path.join(tmp_dir, 'no_such_dir', 'x.db')),
        ConnectionFactoryStub(corrupt),
    ]
