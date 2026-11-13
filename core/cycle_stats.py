"""
采集周期统计：调度延迟、采集耗时、写库耗时的定长直方图，以及计划 / 完成 / 跳过周期数。

MonitorWorker 每个周期喂一次数，收尾时 summary() 得到 QualityStats 写入 tasks 的数据质量列。
每周期的开销只有几次浮点比较、一次 bisect 与几次加法，内存固定（三个 10 桶直方图），
运行多久都不增长。

口径：
- 调度延迟 = 实际醒来时刻 - 计划截止时刻（单调时钟）。落后超过一个周期时调度器重置截止
  时间、不补采：跳过的周期数计入 missed，延迟只记相对最近一个网格点的余量；
- 计划周期数按"活动段"累计：每段从该段第一个截止时间（首采或恢复时刻）到段结束（暂停
  或停止），段内网格点数 = floor(时长 / 周期) + 1；暂停段不算；
- 完成周期数：采集调用正常返回（含进程已退出、全部指标不可用）的周期；采集抛异常的
  周期不算完成，也不算跳过，因此 planned ≈ done + missed + 采集异常次数；
- p95 从直方图估算：取累计计数首次达到 95% 的那个桶的上界；落在溢出桶（500 ms 以上）
  时取观测到的最大值（向上取整）。估算值只会偏大、不会偏小。

不依赖 Qt；不加锁：写只在采集线程，别的线程读到的快照可能差一个周期，可以接受。
"""
import math
from bisect import bisect_left
from typing import List, Optional, Tuple

from data.models import QualityStats

# 直方图各桶的上界（毫秒）：0–1、1–2、2–5、5–10、10–20、20–50、50–100、100–200、
# 200–500，最后一个是 500 以上的溢出桶。桶含上界（恰好 1 ms 落在 0–1 桶）
BUCKET_BOUNDS_MS: Tuple[int, ...] = (1, 2, 5, 10, 20, 50, 100, 200, 500)
# 同一组上界换算成秒：喂数时直接比较秒数，省掉每次乘 1000
_BOUNDS_S: Tuple[float, ...] = tuple(b / 1000.0 for b in BUCKET_BOUNDS_MS)
P95 = 0.95
# 计算跳过周期数与段内网格点数时的容差（秒）：假时钟与相位网格的浮点误差
# 可能让"恰好 3 个周期"算成 2.9999999
_EPS_S = 1e-6


class Histogram:
    """定长对数分桶直方图（单位：秒入、毫秒出）。"""

    __slots__ = ('counts', 'total', 'max_s')

    def __init__(self) -> None:
        self.counts: List[int] = [0] * (len(BUCKET_BOUNDS_MS) + 1)
        self.total = 0
        self.max_s = 0.0

    def add(self, seconds: float) -> None:
        """记一个样本；负数（时钟抖动导致的提前醒来）按 0 计"""
        if seconds < 0.0:
            seconds = 0.0
        self.counts[bisect_left(_BOUNDS_S, seconds)] += 1
        self.total += 1
        if seconds > self.max_s:
            self.max_s = seconds

    def percentile_ms(self, q: float = P95) -> Optional[int]:
        """分位数的估算值（所在桶上界，毫秒）；没有样本为 None"""
        if self.total == 0:
            return None
        rank = max(1, math.ceil(q * self.total))
        cumulative = 0
        for index, count in enumerate(self.counts):
            cumulative += count
            if cumulative >= rank:
                if index < len(BUCKET_BOUNDS_MS):
                    return BUCKET_BOUNDS_MS[index]
                return max(BUCKET_BOUNDS_MS[-1], math.ceil(self.max_s * 1000.0))
        return None   # 计数一致时到不了这里

    def p95_ms(self) -> Optional[int]:
        return self.percentile_ms(P95)

    def snapshot(self) -> dict:
        return {'counts': list(self.counts), 'max_s': self.max_s}

    @classmethod
    def restore(cls, data) -> 'Histogram':
        if not isinstance(data, dict):
            raise ValueError('直方图格式不正确')
        counts = data.get('counts')
        maximum = data.get('max_s')
        if (not isinstance(counts, list) or len(counts) != len(BUCKET_BOUNDS_MS) + 1
                or any(type(n) is not int or n < 0 for n in counts)
                or type(maximum) not in (int, float) or not math.isfinite(maximum)
                or maximum < 0):
            raise ValueError('直方图计数或最大值不正确')
        result = cls()
        result.counts = list(counts)
        result.total = sum(counts)
        result.max_s = maximum
        return result


def merge_histograms(current: dict, prior: Optional[dict] = None) -> Optional[dict]:
    """合并分布而非合并 p95；旧格式或损坏内容返回 None，调用方保守降级。"""
    try:
        inputs = [current] if prior is None else [current, prior]
        if any(not isinstance(d, dict) or d.get('version') != 1
               or d.get('bounds_ms') != list(BUCKET_BOUNDS_MS) for d in inputs):
            return None
        result = {'version': 1, 'bounds_ms': list(BUCKET_BOUNDS_MS)}
        for name in ('lag', 'collect', 'write'):
            merged = Histogram()
            for data in inputs:
                hist = Histogram.restore(data[name])
                merged.counts = [a + b for a, b in zip(merged.counts, hist.counts)]
                merged.total += hist.total
                merged.max_s = max(merged.max_s, hist.max_s)
            result[name] = merged.snapshot()
        return result
    except (KeyError, TypeError, ValueError):
        return None


class CycleStats:
    """
    一个采集任务的周期统计。

    调用顺序（均由采集线程调用）：
        start_segment(首个截止时间)
        每周期：lag(醒来 - 截止) 或 skip(落后量)；collected(采集耗时)；wrote(写库耗时)
        暂停：end_segment(暂停时刻)；恢复：start_segment(恢复时刻)
        收尾：end_segment(停止时刻)；summary()
    """

    def __init__(self, interval: float):
        self.interval = float(interval)
        self.lag_hist = Histogram()
        self.collect_hist = Histogram()
        self.write_hist = Histogram()
        self.done_cycles = 0
        self.missed_cycles = 0
        self.resets = 0                 # 落后重置的次数（每次可能跳过多个周期）
        self._closed_planned = 0        # 已结束的活动段的计划周期数之和
        self._segment_start: Optional[float] = None

    # ---------- 活动段 ----------

    def start_segment(self, at: float) -> None:
        """开始一个活动段（首个截止时间或恢复时刻）；已有未结束的段时忽略"""
        if self._segment_start is None:
            self._segment_start = at

    def end_segment(self, at: float) -> None:
        """结束当前活动段（暂停或停止时刻）；没有进行中的段时忽略"""
        start = self._segment_start
        if start is None:
            return
        self._segment_start = None
        self._closed_planned += self._planned_in(start, at)

    def _planned_in(self, start: float, end: float) -> int:
        """[start, end] 内的网格点数；end 早于 start（首个截止时间之前就暂停/停止）为 0"""
        if end < start or self.interval <= 0:
            return 0
        return int((end - start) / self.interval + _EPS_S) + 1

    def planned_cycles(self, now: Optional[float] = None) -> int:
        """计划周期数；now 给出时把进行中的段算到 now 为止"""
        planned = self._closed_planned
        if now is not None and self._segment_start is not None:
            planned += self._planned_in(self._segment_start, now)
        return planned

    # ---------- 每周期喂数 ----------

    def lag(self, seconds: float) -> None:
        """记一次调度延迟（醒来时刻 - 截止时刻，秒）"""
        self.lag_hist.add(seconds)

    def skip(self, behind: float) -> float:
        """
        调度落后 behind 秒（> 一个周期）、截止时间被重置：累计跳过的周期数，并以相对
        最近一个网格点的余量记一次调度延迟。

        Returns:
            float: 记入直方图的延迟余量（秒）
        """
        missed = int(behind / self.interval + _EPS_S) if self.interval > 0 else 0
        residual = behind - missed * self.interval
        if residual < 0.0:
            residual = 0.0
        self.missed_cycles += missed
        self.resets += 1
        self.lag_hist.add(residual)
        return residual

    def collected(self, seconds: float) -> None:
        """记一次采集耗时，并把本周期算作完成"""
        self.collect_hist.add(seconds)
        self.done_cycles += 1

    def wrote(self, seconds: float) -> None:
        """记一次写库耗时"""
        self.write_hist.add(seconds)

    # ---------- 汇总 ----------

    def summary(self, now: Optional[float] = None) -> QualityStats:
        """当前统计的快照；now 给出时进行中的段算到 now 为止"""
        return QualityStats(
            planned_cycles=self.planned_cycles(now),
            done_cycles=self.done_cycles,
            missed_cycles=self.missed_cycles,
            lag_p95_ms=self.lag_hist.p95_ms(),
            collect_p95_ms=self.collect_hist.p95_ms(),
            write_p95_ms=self.write_hist.p95_ms(),
        )

    def histograms(self) -> dict:
        return {'version': 1, 'bounds_ms': list(BUCKET_BOUNDS_MS),
                'lag': self.lag_hist.snapshot(), 'collect': self.collect_hist.snapshot(),
                'write': self.write_hist.snapshot()}

    @property
    def has_data(self) -> bool:
        """是否有过任何活动段或周期（启动即失败的任务没有可写的质量数据）"""
        return (self._closed_planned > 0 or self._segment_start is not None
                or self.done_cycles > 0 or self.missed_cycles > 0)
