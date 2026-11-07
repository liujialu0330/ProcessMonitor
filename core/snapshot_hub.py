"""
快照中心 SnapshotHub：多个采集线程与进程列表共享同一份全系统快照。

- get(max_age_s, not_before=None)：两种"够新"的判据，多个线程同时请求时只取一次（锁外先
  查一次缓存，锁内再查一次，取快照本身在锁内）：
  - 采集线程传 not_before（本周期的截止时间减一个小容差）：缓存快照的取得时刻不早于它
    才命中。按截止时间而不按年龄判，是因为数据点的时间戳是本周期写库时刻，而速率曲线
    用时间戳作分母——周期不同的任务（1 s 与 1.5 s）、列表刷新、恢复后的非网格采样都会在
    任意相位往缓存里放快照，按年龄判会让下一个周期拿到上一个时刻的快照；
  - 没有截止时间的调用方（进程列表、按名等待）传 max_age_s：缓存年龄不超过它就命中；
- 缓存时刻记"开始调用系统接口"的时刻：内核在调用期间采集数据，这个时刻是数据时刻的
  下界，"不早于截止时间"的判断因此偏保守，不会把调用前已经过时的数据当成本周期的；
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

# 没有截止时间的调用方（进程列表、按名等待）允许的最大年龄 = min(周期, 1 秒) × 该系数。
# 它只管这类调用方之间互相复用；采集线程按截止时间判（见 not_before_for），不受它约束
MAX_AGE_FACTOR = 0.5
MAX_AGE_CAP_S = 1.0

# 采集线程判断缓存是否够新的容差上限：快照取得时刻不早于"截止时间 - 容差"即算本周期的。
# 容差必须能覆盖同一网格点上各线程的唤醒抖动与取样耗时（十几毫秒），又要明显小于
# 半个周期，否则恢复后的非网格采样会被下一周期命中
NOT_BEFORE_TOLERANCE_S = 0.05
NOT_BEFORE_TOLERANCE_FRACTION = 0.25   # 短周期时取周期的 1/4


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
        # (开始取快照的时刻（hub 时钟）, 快照)；整体替换，锁外读取是原子的
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

    def get(self, max_age_s: float, *, not_before: Optional[float] = None) -> SystemSnapshot:
        """
        够新的快照：缓存满足条件直接返回，否则取一份新的。

        Args:
            max_age_s: 缓存年龄（取得时刻到现在，hub 时钟）不超过它即命中；
                给了 not_before 时不再按年龄判
            not_before: 缓存的取得时刻（hub 时钟）不早于它才命中；采集线程传
                not_before_for(本周期截止时间, 周期)

        Raises:
            SnapshotUnavailable / SnapshotError: 来源抛出的异常原样抛出（调用方回退 psutil）
        """
        cached = self._cached
        if self._is_fresh(cached, max_age_s, not_before):
            self._count_hit()
            return cached[1]
        with self._lock:
            # 双检：等锁期间别的线程可能刚取过
            cached = self._cached
            if self._is_fresh(cached, max_age_s, not_before):
                self._count_hit()
                return cached[1]
            return self._take_locked()

    def _is_fresh(self, cached, max_age_s: float, not_before: Optional[float]) -> bool:
        if cached is None:
            return False
        if not_before is not None:
            return cached[0] >= not_before
        return self._clock.monotonic() - cached[0] <= max_age_s

    def _count_hit(self) -> None:
        with self._stats_lock:
            self._hits += 1

    def refresh(self) -> SystemSnapshot:
        """无条件取一份新快照并更新缓存。"""
        with self._lock:
            return self._take_locked()

    def _take_locked(self) -> SystemSnapshot:
        taken = self._clock.monotonic()
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
        self._cached = (taken, snapshot)
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
        """周期为 interval 的任务在没有截止时间可用时取快照允许的最大年龄。"""
        return min(float(interval), MAX_AGE_CAP_S) * MAX_AGE_FACTOR

    @staticmethod
    def not_before_for(deadline: float, interval: float) -> float:
        """周期为 interval、截止时间为 deadline 的采样可接受的最早快照时刻。"""
        tolerance = min(NOT_BEFORE_TOLERANCE_S, float(interval) * NOT_BEFORE_TOLERANCE_FRACTION)
        return deadline - tolerance

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
