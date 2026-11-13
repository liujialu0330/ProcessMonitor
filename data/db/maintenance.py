"""
存储健康检查的底层操作：-wal 过大时的兜底 checkpoint，与后台 PRAGMA quick_check

只管"怎么做"：什么时候做、与数据库维护怎么互斥、结果怎么提示，由 core.storage_guard 与
core.maintenance_service 决定。这里的函数都在调用线程里同步执行，失败以结果对象或
sqlite3 异常的形式交给调用方，不记 ERROR 之外的堆栈，也不直接碰界面。

兜底 checkpoint：SQLite 自带的自动 checkpoint（超过 1000 页时的 PASSIVE）只让后续写入复用
-wal、并不缩小文件；长时间有读者（导出、分析页查询）压着时它甚至合并不过去，-wal 会越长越大。
-wal 超过阈值时先试 TRUNCATE（合并并截成 0 字节，最多等 busy_timeout_ms），被读者挡住或
超时再退到 PASSIVE（不等待，能合并多少算多少）。TRUNCATE 等读者时会挡住新的写入，所以
忙等待上限给得很小（默认 1 秒），宁可退到 PASSIVE，也不让采集线程跟着等。

quick_check：只用只读连接（mode=ro，打不开时退到 query_only 连接），不写库、不挡写入。
检查期间读者快照一直占着，-wal 在此期间不能被 TRUNCATE，所以调用方不要和兜底 checkpoint
并行。可从其他线程 interrupt()：置标志并 sqlite3 级中断，退出路径不会被它卡住。
"""
import logging
import os
import pathlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from data.db.connection import ConnectionFactory

logger = logging.getLogger(__name__)

WAL_SUFFIX = '-wal'
# TRUNCATE 等读者让路的忙等待上限（毫秒）
CHECKPOINT_BUSY_TIMEOUT_MS = 1000
# quick_check 最多收集多少条问题描述（SQLite 默认 100，界面只需要第一条与一个数量级）
QUICK_CHECK_MAX_MESSAGES = 20
# 这两个主码意味着库本身有问题（SQLITE_CORRUPT / SQLITE_NOTADB）：quick_check 在页结构坏到
# 连查都进行不下去时会直接抛它们而不是返回描述行，要当成“检查未通过”而不是“没能检查”
_CORRUPTION_CODES = frozenset({11, 26})
# quick_check 的进度回调间隔（VM 指令数）：让取消标志在长查询里也能被及时看到
_PROGRESS_STEP = 1000


def wal_size(db_path: str) -> int:
    """-wal 文件的字节数；文件不存在（连接都已关闭、被 checkpoint 删除）按 0 计"""
    try:
        return os.path.getsize(db_path + WAL_SUFFIX)
    except FileNotFoundError:
        return 0


# ========== 兜底 checkpoint ==========

@dataclass(frozen=True)
class CheckpointOutcome:
    """一次兜底 checkpoint 的结果"""
    mode: str            # 最终完成（或最后一次尝试）的模式：TRUNCATE / PASSIVE
    complete: bool       # TRUNCATE 完整合并并截断
    fell_back: bool      # TRUNCATE 被挡住、退到了 PASSIVE
    size_before: int     # -wal 的字节数（前）
    size_after: int      # -wal 的字节数（后）
    elapsed_s: float
    error: Optional[str] = None   # 两种模式都抛了 sqlite3 异常时的描述


def checkpoint_wal(factory: ConnectionFactory, *,
                   busy_timeout_ms: int = CHECKPOINT_BUSY_TIMEOUT_MS,
                   size_of: Callable[[str], int] = wal_size,
                   clock: Callable[[], float] = time.perf_counter) -> CheckpointOutcome:
    """-wal 兜底 checkpoint：先 TRUNCATE，被挡住（结果 busy=1 或忙等待超时）再 PASSIVE。

    不抛 sqlite3 异常：失败写进结果的 error，调用方记日志即可（下一轮定时检查会再试）。
    """
    size_before = size_of(factory.db_path)
    begin = clock()
    mode = 'TRUNCATE'
    complete = False
    fell_back = False
    error: Optional[str] = None
    try:
        row = factory.wal_checkpoint('TRUNCATE', busy_timeout_ms)
        complete = row is not None and row[0] == 0
    except sqlite3.Error as e:
        error = str(e)
    if not complete:
        fell_back = True
        mode = 'PASSIVE'
        try:
            factory.wal_checkpoint('PASSIVE', busy_timeout_ms)
            error = None
        except sqlite3.Error as e:
            error = str(e)
    return CheckpointOutcome(
        mode=mode, complete=complete, fell_back=fell_back, size_before=size_before,
        size_after=size_of(factory.db_path), elapsed_s=clock() - begin, error=error)


# ========== quick_check ==========

@dataclass(frozen=True)
class QuickCheckResult:
    """一次 quick_check 的结果"""
    ok: bool                      # 结果恰为一行 "ok"
    messages: Tuple[str, ...]     # SQLite 返回的各行（ok 时为 ("ok",)）
    elapsed_s: float
    interrupted: bool = False     # 被 interrupt() 中途打断（此时 ok 与 messages 没有意义）
    error: Optional[str] = None   # 没能完成检查（打不开库、读失败等）：不等于库损坏

    @property
    def summary(self) -> str:
        """给人看的一句话：第一条问题 + 总条数"""
        if self.ok:
            return "ok"
        if self.error:
            return f"检查未能完成：{self.error}"
        head = self.messages[0] if self.messages else "未知"
        extra = f"（共 {len(self.messages)} 条，最多记录 {QUICK_CHECK_MAX_MESSAGES} 条）" \
            if len(self.messages) > 1 else ""
        return f"{head}{extra}"


def readonly_uri(db_path: str) -> str:
    """只读连接用的 URI：路径中的中文、空格、# 等由 pathlib 做百分号编码"""
    return pathlib.Path(os.path.abspath(db_path)).as_uri() + '?mode=ro'


class QuickCheck:
    """后台 PRAGMA quick_check：run() 阻塞执行，interrupt() 可在任意线程调用。

    一个对象只用一次。interrupt() 在 run() 开始前调用，run() 直接返回 interrupted；
    在检查中途调用，进度回调与 sqlite3_interrupt 双保险中止当前语句。
    """

    def __init__(self, db_path: str, *, max_messages: int = QUICK_CHECK_MAX_MESSAGES,
                 busy_timeout_ms: int = 5000,
                 clock: Callable[[], float] = time.perf_counter):
        self.db_path = db_path
        self.max_messages = max_messages
        self.busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._interrupted = threading.Event()
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def interrupt(self) -> None:
        """请求中止（幂等，任意线程）。不阻塞：中止在 SQLite 下一个检查点生效"""
        self._interrupted.set()
        with self._lock:
            conn = self._conn
            if conn is not None:
                try:
                    conn.interrupt()
                except sqlite3.Error:
                    pass   # 连接正在关闭，检查本来就要结束了

    @property
    def interrupted(self) -> bool:
        return self._interrupted.is_set()

    def _open(self) -> sqlite3.Connection:
        """只读连接；mode=ro 在 -shm 还不存在时打不开 WAL 库，退到 query_only 的普通连接
        （同样不写库：不切日志模式、不 checkpoint）"""
        try:
            conn = sqlite3.connect(readonly_uri(self.db_path), uri=True,
                                   check_same_thread=False)
            conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchall()
            return conn
        except sqlite3.OperationalError:
            logger.info("quick_check 以 mode=ro 打开失败，改用 query_only 连接", exc_info=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute('PRAGMA query_only=1')
            return conn

    def run(self) -> QuickCheckResult:
        begin = self._clock()
        if self._interrupted.is_set():
            return QuickCheckResult(False, (), 0.0, interrupted=True)
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._open()
            conn.execute(f'PRAGMA busy_timeout={int(self.busy_timeout_ms)}')
            conn.set_progress_handler(
                lambda: 1 if self._interrupted.is_set() else 0, _PROGRESS_STEP)
            with self._lock:
                self._conn = conn
            if self._interrupted.is_set():
                return QuickCheckResult(False, (), self._clock() - begin, interrupted=True)
            rows = conn.execute(f'PRAGMA quick_check({int(self.max_messages)})').fetchall()
        except sqlite3.Error as e:
            elapsed = self._clock() - begin
            if self._interrupted.is_set():
                return QuickCheckResult(False, (), elapsed, interrupted=True)
            code = getattr(e, 'sqlite_errorcode', None)
            if code is not None and (code & 0xFF) in _CORRUPTION_CODES:
                logger.error("quick_check 遇到数据库损坏: %s", e)
                return QuickCheckResult(False, (str(e),), elapsed)
            logger.warning("quick_check 未能完成: %s", e, exc_info=True)
            return QuickCheckResult(False, (), elapsed, error=str(e))
        finally:
            with self._lock:
                self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    logger.warning("关闭 quick_check 连接失败", exc_info=True)
        messages: List[str] = [str(r[0]) for r in rows]
        return QuickCheckResult(messages == ['ok'], tuple(messages), self._clock() - begin)
