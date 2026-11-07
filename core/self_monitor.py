"""
自身监控 SelfMonitor：本进程（进程监控助手自己）的资源占用，供诊断信息与 24 小时浸泡用。

调用方（界面线程的定时器或独立线程）每 SAMPLE_INTERVAL_S 秒调用一次 tick()，采本进程的
私有字节 / 句柄 / 线程 / GDI / USER，存入定长环形缓冲（默认 1440 项 = 24 小时，内存固定）。
verdict() 对最近 N 小时的私有字节做与分析页同一套的趋势判定（core.analysis.trend.judge），
回答"监控工具自己有没有在泄漏"。

不依赖 Qt；tick() 与读取方法可在不同线程调用（环形缓冲的读写经锁）。
"""
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Deque, Dict, List, Mapping, Optional

from core.analysis.trend import FitInput, Judgement, judge
from utils.metrics import MetricType

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 60.0
DEFAULT_CAPACITY = 1440          # 24 小时 × 每分钟一个
DEFAULT_WINDOW_HOURS = 6.0       # verdict() 默认看最近 6 小时

# 采集的指标（与被监控进程同一口径，私有字节单位 KB）
SELF_METRICS = (MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES, MetricType.NUM_THREADS,
                MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS)

# 采样函数：返回 {指标: 值}，取不到的指标可缺省；抛异常视为本次采样失败
Sampler = Callable[[], Mapping[str, float]]


@dataclass(frozen=True)
class SelfSample:
    """一次自身采样。取不到的指标为 None"""
    timestamp: datetime          # 墙钟时刻（显示用）
    mono: float                  # 单调时钟读数（算窗口与拟合用）
    private_kb: Optional[float]
    handles: Optional[float]
    threads: Optional[float]
    gdi: Optional[float]
    user: Optional[float]


class _SystemClock:
    """默认时钟：只用到 monotonic() 与 now()"""

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def now() -> datetime:
        return datetime.now()


def default_sampler(pid: Optional[int] = None) -> Sampler:
    """用 ProcessCollector 采本进程（psutil 路径，不依赖快照中心）"""
    from core.process_collector import ProcessCollector
    collector = ProcessCollector(pid if pid is not None else os.getpid())

    def sample() -> Dict[str, float]:
        values = collector.collect_metrics(list(SELF_METRICS))
        if values is None:
            raise RuntimeError("读取本进程资源失败")
        return values

    return sample


class SelfMonitor:
    """
    Args:
        clock: 提供 monotonic() 与 now() 的时钟，None 用系统时钟
        sampler: 采样函数（见 Sampler），None 用 default_sampler()（首次 tick 时创建）
        capacity: 环形缓冲长度
    """

    def __init__(self, clock=None, sampler: Optional[Sampler] = None,
                 capacity: int = DEFAULT_CAPACITY):
        self._clock = clock if clock is not None else _SystemClock()
        self._sampler = sampler
        self._samples: Deque[SelfSample] = deque(maxlen=max(1, int(capacity)))
        self._lock = threading.Lock()
        self.failures = 0
        self._failure_logged = False

    @property
    def capacity(self) -> int:
        return self._samples.maxlen or 0

    def tick(self) -> Optional[SelfSample]:
        """采一次并存入缓冲；失败返回 None（只在首次失败记日志）"""
        try:
            if self._sampler is None:
                self._sampler = default_sampler()
            values = self._sampler()
        except Exception:
            self.failures += 1
            if not self._failure_logged:
                self._failure_logged = True
                logger.warning("自身监控采样失败（之后不再重复记录）", exc_info=True)
            return None
        sample = SelfSample(
            timestamp=self._clock.now(),
            mono=self._clock.monotonic(),
            private_kb=values.get(MetricType.MEMORY_PRIVATE),
            handles=values.get(MetricType.NUM_HANDLES),
            threads=values.get(MetricType.NUM_THREADS),
            gdi=values.get(MetricType.NUM_GDI_OBJECTS),
            user=values.get(MetricType.NUM_USER_OBJECTS),
        )
        with self._lock:
            self._samples.append(sample)
        return sample

    def snapshot(self) -> List[SelfSample]:
        """缓冲内全部样本（按时间先后）"""
        with self._lock:
            return list(self._samples)

    def latest(self) -> Optional[SelfSample]:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def verdict(self, hours: float = DEFAULT_WINDOW_HOURS,
                interval_seconds: float = SAMPLE_INTERVAL_S) -> Judgement:
        """
        最近 hours 小时私有字节的趋势判定（core.analysis.trend.judge，门槛与分析页相同：
        至少 30 个点、跨度至少 max(10 分钟, 30 × 周期)）。数据不够时为"数据不足"。
        """
        samples = self.snapshot()
        if samples:
            since = samples[-1].mono - hours * 3600.0
            samples = [s for s in samples if s.mono >= since]
        points = [(s.mono, s.private_kb) for s in samples if s.private_kb is not None]
        return judge(fit_input(points), interval_seconds)


def fit_input(points: List[tuple]) -> FitInput:
    """[(单调秒, 值)] → 拟合输入（x 相对第一个点）；空列表给一个 count=0 的输入"""
    if not points:
        return FitInput(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    x0 = points[0][0]
    sum_x = sum_y = sum_xx = sum_xy = sum_yy = 0.0
    values = []
    for t, y in points:
        x = t - x0
        sum_x += x
        sum_y += y
        sum_xx += x * x
        sum_xy += x * y
        sum_yy += y * y
        values.append(y)
    return FitInput(
        count=len(points), sum_x=sum_x, sum_y=sum_y, sum_xx=sum_xx, sum_xy=sum_xy,
        sum_yy=sum_yy, span_seconds=points[-1][0] - x0,
        first_value=values[0], last_value=values[-1],
        min_value=min(values), max_value=max(values))
