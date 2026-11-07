"""
(task_id, 指标) → series_id 的进程内缓存

采集线程每个周期都写同一批序列，缓存省掉每次写入前的序列查找。series 行被删除后
（删除任务、过期清理、孤儿清理）其 series_id 可能被新序列复用，旧缓存会把样本写进别的
序列，所以所有删除 series 的路径都必须调用 invalidate()：它递增全局代号，各缓存在下次
使用时发现代号变化就整体清空。invalidate() 在删除事务内调用，写入方在拿到写锁之后才读
缓存，因此不会用到删除事务提交前取得的旧映射。
"""
import threading
from typing import Dict, Optional, Tuple

_lock = threading.Lock()
_generation = 0


def invalidate() -> None:
    """有 series 行被删除：所有缓存在下次使用时清空"""
    global _generation
    with _lock:
        _generation += 1


def generation() -> int:
    with _lock:
        return _generation


class SeriesCache:
    """线程安全的序列映射缓存；只缓存已提交的映射（由调用方在事务提交后 put）"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: Dict[Tuple[str, str], int] = {}
        self._generation = generation()

    def _sync(self, current: int) -> None:
        if current != self._generation:
            self._map.clear()
            self._generation = current

    def snapshot(self) -> Tuple[int, Dict[Tuple[str, str], int]]:
        """(当前代号, 当前有效映射的副本)"""
        current = generation()
        with self._lock:
            self._sync(current)
            return current, dict(self._map)

    def put_all(self, gen: int, mapping: Dict[Tuple[str, str], int]) -> None:
        """写入 gen 代号下取得的映射；期间有过 invalidate 则丢弃"""
        if not mapping:
            return
        current = generation()
        if current != gen:
            return
        with self._lock:
            self._sync(current)
            self._map.update(mapping)

    def get(self, key: Tuple[str, str]) -> Optional[int]:
        return self.snapshot()[1].get(key)
