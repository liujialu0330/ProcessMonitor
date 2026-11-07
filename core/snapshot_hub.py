"""
快照中心 SnapshotHub：多个采集线程与进程列表共享同一份全系统快照。

- get(max_age_s)：缓存快照的年龄不超过 max_age_s 就直接返回，否则取新的；多个线程
  同时请求时只取一次（锁外先查一次缓存，锁内再查一次，取快照本身在锁内）；
- 相位对齐：以 hub 创建时的单调时钟为原点，采集线程把截止时间对齐到"原点 + k × 周期"
  的网格上，同周期的任务因此在同一时刻醒来，一次快照服务多个任务；
- 快照不可用（available() 为 False）或取失败时由调用方回退 psutil，hub 只如实抛出。

线程安全；不依赖 Qt 与 psutil。
"""
import math
import threading
import time
from typing import NamedTuple, Optional, Tuple

from core import system_snapshot
from core.system_snapshot import SystemSnapshot

# 同一周期内多个任务共用一份快照的最大年龄 = min(周期, 1 秒) × 该系数：
# 周期 ≥ 1 秒时 0.5 秒，既能覆盖同一网格点上各线程的唤醒抖动，又不会让两个相邻周期
# 共用同一份快照（那样 CPU 使用率的差分窗口会变成 0）
MAX_AGE_FACTOR = 0.5
MAX_AGE_CAP_S = 1.0


class HubStats(NamedTuple):
    """取样统计：samples 实际取快照次数，hits 缓存命中次数，failures 取失败次数"""
    samples: int
    hits: int
    failures: int
    avg_ms: float
    last_ms: float


class SnapshotHub:
    """
    全系统快照的共享缓存。

    Args:
        source: 快照来源（需提供 available() / unavailable_reason / snapshot()），
            None 用 core.system_snapshot 的默认快照器
        clock: 提供 monotonic() 的时钟（缓存年龄与相位原点都按它算），None 用 time
    """

    def __init__(self, source=None, *, clock=None):
        self._source = source if source is not None else system_snapshot.default_snapshotter()
        self._clock = clock if clock is not None else time
        # _lock 只在取快照时持有（同一时刻只取一次）；计数用单独的锁，命中缓存的线程
        # 不必等正在进行的取样
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        # (取得时刻（hub 时钟）, 快照)；整体替换，锁外读取是原子的
        self._cached: Optional[Tuple[float, SystemSnapshot]] = None
        self._samples = 0
        self._hits = 0
        self._failures = 0
        self._total_ms = 0.0
        self._last_ms = 0.0
        self._origin = self._clock.monotonic()

    # ---------- 可用性 ----------

    def available(self) -> bool:
        """快照是否可用（首次调用会触发快照器自检）。"""
        return self._source.available()

    @property
    def unavailable_reason(self) -> Optional[str]:
        """不可用的原因；可用时为 None。"""
        return self._source.unavailable_reason

    # ---------- 取快照 ----------

    def get(self, max_age_s: float) -> SystemSnapshot:
        """
        年龄不超过 max_age_s 的快照：缓存够新直接返回，否则取一份新的。

        Raises:
            SnapshotUnavailable / SnapshotError: 来源抛出的异常原样抛出（调用方回退 psutil）
        """
        cached = self._cached
        if cached is not None and self._clock.monotonic() - cached[0] <= max_age_s:
            self._count_hit()
            return cached[1]
        with self._lock:
            # 双检：等锁期间别的线程可能刚取过
            cached = self._cached
            if cached is not None and self._clock.monotonic() - cached[0] <= max_age_s:
                self._count_hit()
                return cached[1]
            return self._take_locked()

    def _count_hit(self) -> None:
        with self._stats_lock:
            self._hits += 1

    def refresh(self) -> SystemSnapshot:
        """无条件取一份新快照并更新缓存。"""
        with self._lock:
            return self._take_locked()

    def _take_locked(self) -> SystemSnapshot:
        begin = time.perf_counter()
        try:
            snapshot = self._source.snapshot()
        except Exception:
            with self._stats_lock:
                self._failures += 1
            raise
        elapsed_ms = (time.perf_counter() - begin) * 1000
        with self._stats_lock:
            self._samples += 1
            self._total_ms += elapsed_ms
            self._last_ms = elapsed_ms
        self._cached = (self._clock.monotonic(), snapshot)
        return snapshot

    def stats(self) -> HubStats:
        """取样次数、命中次数、失败次数、平均与最近一次取样耗时（毫秒）。"""
        with self._stats_lock:
            avg = self._total_ms / self._samples if self._samples else 0.0
            return HubStats(self._samples, self._hits, self._failures, avg, self._last_ms)

    # ---------- 相位对齐 ----------

    @property
    def phase_origin(self) -> float:
        """对齐网格的原点（hub 创建时的单调时钟读数）。"""
        return self._origin

    @staticmethod
    def max_age_for(interval: float) -> float:
        """周期为 interval 的任务取快照时允许的最大年龄。"""
        return min(float(interval), MAX_AGE_CAP_S) * MAX_AGE_FACTOR

    def align(self, t: float, period: float) -> float:
        """网格（原点 + k × period）上不早于 t 的第一个点。"""
        if period <= 0:
            return t
        k = math.ceil((t - self._origin) / period)
        return self._origin + k * period

    def next_slot(self, t: float, period: float) -> float:
        """t 之后的下一个截止时间：网格上不早于 t + period / 2 的第一个点。

        t 在网格上时恰为 t + period（每次从原点重算，不累积浮点误差）；t 不在网格上
        （恢复或落后重置后立即采的那一次）时回到网格，间隔落在 [0.5, 1.5) 个周期内，
        不会出现紧挨着的两次采样。
        """
        return self.align(t + period / 2, period)


# ---------------------------------------------------------------- 进程内共享实例


_default: Optional[SnapshotHub] = None
_default_lock = threading.Lock()


def default_hub() -> SnapshotHub:
    """进程内共享的快照中心（监控任务与进程列表共用，惰性创建）。"""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = SnapshotHub()
    return _default
