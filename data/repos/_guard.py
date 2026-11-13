"""
仓储共用的异常转换（sqlite3.Error → StorageError）与慢操作告警

只转换 sqlite3.Error；参数类型错误（TypeError/ValueError）属于调用方编程错误，原样抛出，
不能伪装成"存储故障"。

磁盘已满分类：SQLITE_FULL（"database or disk is full"），以及 SQLITE_IOERR 族里有明确证据
（原因链上的 OSError 是 ENOSPC / Windows 的 ERROR_DISK_FULL，或消息写明磁盘已满）的写入类
错误，转成 StorageError 的子类 StorageFullError；其余仍抛普通 StorageError。IOERR 本身
含义太宽（设备故障、权限、网络盘断开都会报它），没有证据时不能当"磁盘满"处理。

慢操作告警：每个仓储操作都包在 storage_errors 里，顺带量一次耗时（一次 perf_counter 差），
超过 SLOW_QUERY_WARN_MS 记一条 WARNING（操作名 + 耗时）。同一种操作（见 action_kind，
如"保存任务 <id> "归为"保存任务"）在 SLOW_QUERY_LOG_INTERVAL_S 内只记一次，库被
长时间锁住时不至于每个周期刷屏；被抑制的次数在下一条里一并报出。
"""
import logging
import re
import errno
import sqlite3
import threading
import time
from types import TracebackType
from typing import Dict, Optional, Tuple, Type

from data.errors import StorageError, StorageFullError

logger = logging.getLogger(__name__)

SLOW_QUERY_WARN_MS = 500
SLOW_QUERY_LOG_INTERVAL_S = 60.0
# 包住生成器 yield 的操作：耗时里含调用方消费数据的时间（导出逐批写文件），不是查询本身
# 慢，不计时
UNTIMED_ACTIONS = frozenset({'流式读取数据点'})

_KIND_SPLIT = re.compile(r'[\s（(]')

# SQLite 结果码：低 8 位是主码，其余是扩展码
SQLITE_FULL_CODE = 13
SQLITE_IOERR_CODE = 10
# IOERR 族里"写文件时失败"的扩展码：磁盘写满时 SQLite 在个别路径上报它们而不是 SQLITE_FULL
# （WRITE=778、FSYNC=1034、TRUNCATE=1546、SHMSIZE=4618）；只有同时有写满的证据才算磁盘已满
_IOERR_WRITE_EXTENDED = frozenset({778, 1034, 1546, 4618})
# Windows 错误码：ERROR_HANDLE_DISK_FULL / ERROR_DISK_FULL
_WINERROR_DISK_FULL = frozenset({39, 112})
_DISK_FULL_TEXTS = ('disk is full', 'no space left', 'not enough space on the disk',
                    '磁盘空间不足', '磁盘已满')

# 计时函数；测试替换它来制造"慢操作"，不必真的等待
_clock = time.perf_counter

_slow_lock = threading.Lock()
# 操作种类 → (上次记日志的时刻, 此后被抑制的次数)
_slow_last: Dict[str, Tuple[float, int]] = {}


def action_kind(action: str) -> str:
    """操作描述 → 告警节流用的种类：第一个空白或左括号之前的部分（去掉任务 ID、
    条数等变量，如"写入数据点（5 条）"归为"写入数据点"）"""
    return _KIND_SPLIT.split(action.strip(), 1)[0]


def _report_slow(action: str, elapsed_ms: float, now: float) -> None:
    kind = action_kind(action)
    if kind in UNTIMED_ACTIONS:
        return
    with _slow_lock:
        last = _slow_last.get(kind)
        if last is not None and now - last[0] < SLOW_QUERY_LOG_INTERVAL_S:
            _slow_last[kind] = (last[0], last[1] + 1)
            return
        suppressed = last[1] if last is not None else 0
        _slow_last[kind] = (now, 0)
    if suppressed:
        logger.warning("慢数据库操作: %s耗时 %.0f ms（此前 %d 秒内另有 %d 次同类慢操作未记录）",
                       action, elapsed_ms, int(SLOW_QUERY_LOG_INTERVAL_S), suppressed)
    else:
        logger.warning("慢数据库操作: %s耗时 %.0f ms", action, elapsed_ms)


def _error_chain(exc: BaseException):
    """异常本身及其 __cause__ / __context__ 链（防环、限深）"""
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _os_error_is_disk_full(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    return (exc.errno == errno.ENOSPC
            or getattr(exc, 'winerror', None) in _WINERROR_DISK_FULL)


def is_disk_full(exc: BaseException) -> bool:
    """sqlite3 异常是否表示"磁盘已满"（分类规则见模块说明）。

    Python 3.11 起 sqlite3.Error 带 sqlite_errorcode；取不到（老版本、手工构造的异常）时
    退回按消息文本判断。
    """
    code = getattr(exc, 'sqlite_errorcode', None)
    text = str(exc).lower()
    text_says_full = any(marker in text for marker in _DISK_FULL_TEXTS)
    if code is not None and (code & 0xFF) == SQLITE_FULL_CODE:
        return True
    if code is None:
        return text_says_full
    if (code & 0xFF) != SQLITE_IOERR_CODE:
        return False
    if code != SQLITE_IOERR_CODE and code not in _IOERR_WRITE_EXTENDED:
        return False   # 读、锁、删除等与写满无关的 IOERR
    return text_says_full or any(_os_error_is_disk_full(e) for e in _error_chain(exc))


def reset_slow_log() -> None:
    """清空慢操作的节流状态（测试用）"""
    with _slow_lock:
        _slow_last.clear()


class storage_errors:
    """上下文管理器：块内抛出的 sqlite3.Error 改抛为 StorageError（原异常挂在 __cause__），
    并在块耗时超过 SLOW_QUERY_WARN_MS 时记慢操作告警。

    用类而非 @contextmanager 实现，便于包在生成器的 yield 外层（GeneratorExit 不受影响）。
    """

    __slots__ = ('_action', '_begin')

    def __init__(self, action: str):
        self._action = action
        self._begin = 0.0

    def __enter__(self) -> 'storage_errors':
        self._begin = _clock()
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc: Optional[BaseException],
                 tb: Optional[TracebackType]) -> bool:
        now = _clock()
        elapsed_ms = (now - self._begin) * 1000.0
        if elapsed_ms > SLOW_QUERY_WARN_MS:
            try:
                _report_slow(self._action, elapsed_ms, now)
            except Exception:
                # 告警只是辅助信息，不能改变操作本身的结果
                pass
        if exc is not None and isinstance(exc, sqlite3.Error):
            error_type = StorageFullError if is_disk_full(exc) else StorageError
            raise error_type(f"{self._action}失败: {exc}") from exc
        return False
