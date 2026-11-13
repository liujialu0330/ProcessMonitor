"""
数据库连接工厂

每次读写都新建连接、用完即关（各线程互不共享连接对象）。连接打开后统一设置以下 PRAGMA：
- journal_mode=WAL：写日志模式，读写并发更稳定。它是持久属性（写在库文件头里），每个工厂只在
  首次打开连接时设一次（每次都设约多花 0.3 ms）；只读工厂不设（切换本身就是一次写）
- busy_timeout=5000：其他连接持有写锁时最多等待 5 秒再抛 OperationalError，而非立即失败
- synchronous=NORMAL：WAL 模式下的推荐权衡——牺牲操作系统崩溃/掉电那一瞬间的极端持久性，
  换取相对 FULL 明显更好的写入性能；应用自身崩溃时 WAL 机制仍保证已提交事务不丢
- journal_size_limit=8 MB：-wal 被重置复用时截到该上限，避免一次大事务后长期占着大文件
  （按连接生效、不持久化，所以每个新连接都要设）

常驻占位连接（keep_alive）：SQLite 在"最后一个连接"关闭时会做一次 checkpoint 并删除
-wal/-shm 文件。每次操作都新建连接时，每次写入都等于"最后一个连接关闭"，-wal 被反复删除
重建（实测 1 秒周期写入每次都会删除；单次写入耗时约 8~14 ms，保持一个空闲连接后降到
约 1 ms）。因此工厂默认持有一个空闲连接，只为让 -wal 常驻，不参与任何读写。

-wal 常驻后的大小：SQLite 自动 checkpoint（默认超过 1000 页时，PASSIVE 模式）只把日志
合并回主库、让后续写入从头复用 -wal，并不缩小文件。日常小批量写入时 -wal 在约 1000 页
（约 4 MB）内循环复用；但单个大事务（批量删除、VACUUM 等）会把 -wal 撑到与事务同量级，
PASSIVE checkpoint 后仍停在高位，下次写入重置日志时才截到 journal_size_limit（之后就停在
该上限）。需要立即收回空间时（如清理/VACUUM 之后）调用 checkpoint(truncate=True)，把 -wal 截到 0。

只读模式（read_only=True）：库由更高版本写入、当前版本不能安全修改时使用（见 data.db.meta）。
transaction() 直接抛 StorageError；connect() 的连接设 query_only，任何写语句都会失败并被
仓储包成 StorageError。只读连接不切换日志模式（切换本身就是一次写）。
"""
import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from data.errors import StorageError

logger = logging.getLogger(__name__)

# -wal 重置复用时保留的最大字节数
JOURNAL_SIZE_LIMIT = 8 * 1024 * 1024


def _apply_pragmas(conn: sqlite3.Connection, read_only: bool = False,
                   set_wal: bool = True) -> None:
    """新连接在其他语句之前设置的 PRAGMA（必须在事务外执行：journal_mode 不能在事务内切换）"""
    if read_only:
        conn.execute('PRAGMA query_only=1')
    elif set_wal:
        conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute(f'PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}')


class ConnectionFactory:
    """按库路径创建连接；可选持有一个常驻占位连接（见模块说明）"""

    def __init__(self, db_path: str, keep_alive: bool = True, read_only: bool = False):
        """
        Args:
            db_path: 数据库文件路径
            keep_alive: 是否在首次使用时打开常驻占位连接。迁移、备份、还原、改名库文件
                这类需要库文件没有任何打开句柄的场景必须用 False（Windows 上被打开的
                文件无法改名）。
            read_only: 只读模式（见模块说明）
        """
        self.db_path = db_path
        self.keep_alive = keep_alive
        self.read_only = read_only
        self._holder: Optional[sqlite3.Connection] = None
        self._holder_lock = threading.Lock()
        # 本工厂是否已把库切到 WAL（持久属性，设一次即可；并发首次打开时重复设置无害）
        self._wal_ready = False
        self._write_options = threading.local()

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """新连接的 PRAGMA：WAL 只在本工厂首次打开可写连接时设置"""
        set_wal = not self.read_only and not self._wal_ready
        _apply_pragmas(conn, self.read_only, set_wal=set_wal)
        timeout_ms = getattr(self._write_options, 'timeout_ms', None)
        if timeout_ms is not None:
            conn.execute(f'PRAGMA busy_timeout={timeout_ms}')
        if set_wal:
            self._wal_ready = True

    @contextmanager
    def write_timeout(self, timeout_ms: int):
        """只限制当前线程、当前作用域新建连接的锁等待，不影响采集线程。"""
        previous = getattr(self._write_options, 'timeout_ms', None)
        self._write_options.timeout_ms = max(0, int(timeout_ms))
        try:
            yield
        finally:
            self._write_options.timeout_ms = previous

    # ========== 连接上下文 ==========

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """
        普通连接上下文：row_factory=Row，正常退出 commit，异常 rollback 后原样抛出，
        最后总是关闭连接。事务由 sqlite3 模块的默认隐式事务管理。
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使结果可以按列名访问
        try:
            self._prepare(conn)
            self._ensure_keep_alive()
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """
        显式事务上下文：isolation_level=None 关闭 sqlite3 模块的隐式事务管理，
        用 BEGIN IMMEDIATE ... COMMIT 包住整段逻辑（失败 ROLLBACK）。
        与默认模式不同，这样 DDL（CREATE/ALTER/DROP）与 PRAGMA user_version
        也真正处于同一个事务内，迁移链任一步失败都能整体回滚。

        Raises:
            StorageError: 只读模式（不打开连接）
        """
        if self.read_only:
            raise StorageError(f"数据库为只读，不能写入: {self.db_path}")
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            self._prepare(conn)
            self._ensure_keep_alive()
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn.cursor()
                conn.execute('COMMIT')
            except BaseException:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    logger.error("事务回滚失败", exc_info=True)
                raise
        finally:
            conn.close()

    # ========== checkpoint ==========

    def wal_checkpoint(self, mode: str, busy_timeout_ms: Optional[int] = None) -> Optional[tuple]:
        """
        执行一次 PRAGMA wal_checkpoint(<mode>) 并返回结果行 (busy, -wal 总页数, 已合并页数)；
        非 WAL 库为 (0, -1, -1)。被其他连接挡住时 SQLite 不抛错而是 busy=1。
        sqlite3 异常原样抛出，由调用方决定怎么处理。

        Args:
            mode: PASSIVE（不等待，能合并多少算多少）/ FULL / RESTART / TRUNCATE（合并完把
                -wal 截到 0 字节，需要等其他连接的读写让路）
            busy_timeout_ms: 本次连接的忙等待上限；None 沿用连接默认的 5 秒。TRUNCATE 等待读者
                时会挡住新的写入，周期性的后台 checkpoint 应传较小的值，别让采集线程跟着等
        """
        with self.connect() as conn:
            if busy_timeout_ms is not None:
                conn.execute(f'PRAGMA busy_timeout={int(busy_timeout_ms)}')
            row = conn.execute(f'PRAGMA wal_checkpoint({mode})').fetchone()
        return tuple(row) if row is not None else None

    def checkpoint(self, truncate: bool = True) -> bool:
        """
        手动 WAL checkpoint，把 -wal 中已提交的内容合并回主库文件。

        Args:
            truncate: True 用 TRUNCATE 模式（合并完把 -wal 截到 0 字节；需要等其他连接
                的读写让路，最多等 busy_timeout）；False 用 PASSIVE 模式（不等待，能合并多少算多少）

        Returns:
            bool: 是否完整合并。被其他连接挡住时 SQLite 不抛错而是在结果行里返回 busy=1，
            这种情况与 SQLite 报错一样返回 False（只记日志，不抛出）
        """
        mode = 'TRUNCATE' if truncate else 'PASSIVE'
        try:
            row = self.wal_checkpoint(mode)
        except sqlite3.Error:
            logger.warning("WAL checkpoint(%s) 失败: %s", mode, self.db_path, exc_info=True)
            return False
        # 结果行为 (busy, -wal 总页数, 已合并页数)；非 WAL 库为 (0, -1, -1)
        if row is None or row[0] != 0:
            logger.warning("WAL checkpoint(%s) 被其他连接阻挡未完成: %s, 结果=%s",
                           mode, self.db_path, row)
            return False
        return True

    # ========== 常驻占位连接 ==========

    @property
    def keep_alive_open(self) -> bool:
        """常驻占位连接当前是否已打开"""
        return self._holder is not None

    def _ensure_keep_alive(self) -> None:
        """
        按需打开常驻占位连接。必须在工作连接已切到 WAL 之后调用：占位连接要读一次库，
        才会映射 -shm（WAL 索引），之后其他连接关闭时才不会被当成"最后一个连接"。
        打开失败只记日志，不影响本次读写（退化为没有占位连接的行为）。
        """
        if not self.keep_alive or self._holder is not None:
            return
        with self._holder_lock:
            if self._holder is not None:
                return
            holder = None
            try:
                # 可能在任意线程首次打开、在另一线程关闭，故关闭同线程检查；
                # 它从不执行业务语句，不存在跨线程并发使用
                holder = sqlite3.connect(self.db_path, check_same_thread=False)
                # 刻意用读写连接加 query_only，而不是 mode=ro：只读连接关闭时不会
                # checkpoint/删除 -wal，退出后会残留日志文件；读写连接作为最后一个
                # 连接关闭时会正常收尾
                holder.execute('PRAGMA query_only=1')
                holder.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchall()
            except sqlite3.Error:
                logger.warning("打开数据库常驻连接失败，继续以无常驻连接方式运行",
                               exc_info=True)
                if holder is not None:
                    holder.close()
                self.keep_alive = False
                return
            self._holder = holder

    def close(self) -> None:
        """
        释放常驻占位连接（若最后一个连接是它，SQLite 会 checkpoint 并删除 -wal/-shm）。
        工厂之后仍可使用；keep_alive 为 True 时下次读写会重新打开占位连接。
        """
        with self._holder_lock:
            holder, self._holder = self._holder, None
        if holder is not None:
            try:
                holder.close()
            except sqlite3.Error:
                logger.warning("关闭数据库常驻连接失败", exc_info=True)
