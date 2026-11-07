"""
仓储共用的异常转换（sqlite3.Error → StorageError）与慢操作告警

只转换 sqlite3.Error；参数类型错误（TypeError/ValueError）属于调用方编程错误，原样抛出，
不能伪装成"存储故障"。

慢操作告警：每个仓储操作都包在 storage_errors 里，顺带量一次耗时（一次 perf_counter 差），
超过 SLOW_QUERY_WARN_MS 记一条 WARNING（操作名 + 耗时）。同一种操作（见 action_kind，
如"保存任务 <id> "归为"保存任务"）在 SLOW_QUERY_LOG_INTERVAL_S 内只记一次，库被
长时间锁住时不至于每个周期刷屏；被抑制的次数在下一条里一并报出。
"""
import logging
import re
import sqlite3
import threading
import time
from types import TracebackType
from typing import Dict, Optional, Tuple, Type

from data.errors import StorageError

logger = logging.getLogger(__name__)

SLOW_QUERY_WARN_MS = 500
SLOW_QUERY_LOG_INTERVAL_S = 60.0
# 包住生成器 yield 的操作：耗时里含调用方消费数据的时间（导出逐批写文件），不是查询本身
# 慢，不计时
UNTIMED_ACTIONS = frozenset({'流式读取数据点'})

_KIND_SPLIT = re.compile(r'[\s（(]')

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
            raise StorageError(f"{self._action}失败: {exc}") from exc
        return False
