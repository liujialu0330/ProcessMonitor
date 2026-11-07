"""
存储故障注入：让真实的 SQLite 报出 SQLITE_FULL（"database or disk is full"）

做法：给连接工厂的每个新连接补一条 PRAGMA max_page_count = <当前页数 + 余量>。库增长超过
上限时 SQLite 返回真正的 SQLITE_FULL（sqlite_errorcode = 13），与磁盘写满走同一条错误路径，
不需要真的写满磁盘，也不改任何产品代码。max_page_count 按连接生效、不持久化，所以要对
工厂的 _prepare 打补丁，每个新连接都设一次。

用法（C-2b 的"写满时保留缓冲并暂停"测试可直接复用）：

    with simulate_disk_full(db):                 # 此后任何需要新页的写入都抛 StorageFullError
        with pytest.raises(StorageFullError):
            db.points.append(big_batch)
    # 退出 with 后恢复正常，库里没有半截数据
"""
import sqlite3
from contextlib import contextmanager


@contextmanager
def simulate_disk_full(db, extra_pages: int = 0):
    """Database 的所有新连接都带上页数上限（当前页数 + extra_pages）。"""
    factory = db._factory
    with sqlite3.connect(db.db_path) as probe:
        pages = probe.execute('PRAGMA page_count').fetchone()[0]
    probe.close()
    limit = pages + extra_pages
    original = factory._prepare

    def prepare(conn):
        original(conn)
        conn.execute(f'PRAGMA max_page_count={limit}')

    factory._prepare = prepare
    try:
        yield limit
    finally:
        factory._prepare = original


def make_sqlite_error(code: int, message: str, name: str = '') -> sqlite3.OperationalError:
    """手工构造带 sqlite_errorcode 的 sqlite3 异常（真实库难以触发的 IOERR 变体用）。
    Python 3.11 的 sqlite3 异常实例允许设置这两个属性。"""
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = code
    exc.sqlite_errorname = name
    return exc
