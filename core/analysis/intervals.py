"""
打点区间统计
按打点（含基线、起点、终点）把一段时间序列切成相邻区间，逐段给出时长、起止值、增量、
速率、峰值与"与上一区间增量之差"，用来看每轮操作的增量是否收敛。
"""
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

Point = Tuple[datetime, float]

BOUNDARY_KINDS = ('start', 'baseline', 'user', 'end')


@dataclass(frozen=True)
class Boundary:
    """一个区间边界：用户打点、基线，或任务的起点/终点。kind 取 BOUNDARY_KINDS 之一。"""
    timestamp: datetime
    label: str
    kind: str


@dataclass(frozen=True)
class IntervalStats:
    """
    一个区间的统计。label 取区间右边界的标签：打点通常在一轮操作做完后点下，
    所以 [基线, 第 1 轮] 这段就是"第 1 轮"，首段 [开始, 基线] 是预热段、标签为"基线"。
    """
    index: int
    label: str
    start: datetime
    end: datetime
    duration_seconds: float
    start_value: Optional[float]
    end_value: Optional[float]
    delta: Optional[float]
    rate_per_hour: Optional[float]
    peak: Optional[float]
    sample_count: int
    delta_vs_previous: Optional[float]   # 本区间 delta - 上一区间 delta，用于判断每轮增量是否收敛


def _sorted_points(points: Sequence[Point]) -> List[Point]:
    # sorted 是稳定排序：同一时间戳的点保持传入顺序
    return sorted(points, key=lambda p: p[0])


def split_intervals(points: Sequence[Point], boundaries: Sequence[Boundary], *,
                    first_label: str = '开始', last_label: str = '结束') -> List[IntervalStats]:
    """
    按相邻边界切区间并统计。points 可以无序，内部按时间升序（稳定）排序。

    边界补齐：没有 kind='start' 的边界且首点早于所有边界时，在首点处补一个起点（first_label）；
    没有 kind='end' 的边界且末点晚于所有边界时，在末点处补一个终点（last_label）。
    因此落在首点之前的打点自己充当起点、落在末点之后的打点自己充当终点，不会多出空的头尾段；
    显式给了 start/end 时，落在它们之外的点不参与统计。边界少于 2 个时返回空列表。

    归属：区间为左闭右开 [start, end)，最后一个区间为闭区间 [start, end]；
    打点时刻恰好有样本时，该样本归入打点之后的区间。

    数值：start_value / end_value 取区间内第一个 / 最后一个点，delta = end_value - start_value，
    peak 为区间内最大值；rate_per_hour = delta / duration × 3600（duration 为 0 时为 None）。
    空区间的数值字段均为 None、sample_count = 0；相邻任一区间为空时 delta_vs_previous 为 None。
    重复时间戳的打点会产生时长为 0 的区间，保留它以免打点与区间的对应关系错位。
    """
    pts = _sorted_points(points)
    edges = sorted(boundaries, key=lambda b: b.timestamp)
    if pts:
        has_start = any(b.kind == 'start' for b in edges)
        has_end = any(b.kind == 'end' for b in edges)
        if not has_start and (not edges or pts[0][0] < edges[0].timestamp):
            edges.insert(0, Boundary(pts[0][0], first_label, 'start'))
        if not has_end and (not edges or pts[-1][0] > edges[-1].timestamp):
            edges.append(Boundary(pts[-1][0], last_label, 'end'))
    if len(edges) < 2:
        return []

    times = [p[0] for p in pts]
    result: List[IntervalStats] = []
    prev_delta: Optional[float] = None
    last_index = len(edges) - 2
    for i in range(len(edges) - 1):
        left, right = edges[i], edges[i + 1]
        lo = bisect_left(times, left.timestamp)
        hi = bisect_right(times, right.timestamp) if i == last_index else bisect_left(times, right.timestamp)
        values = [v for _, v in pts[lo:hi]]
        duration = (right.timestamp - left.timestamp).total_seconds()

        if values:
            start_value, end_value = values[0], values[-1]
            delta: Optional[float] = end_value - start_value
            peak: Optional[float] = max(values)
            rate = delta / duration * 3600.0 if duration > 0 else None
        else:
            start_value = end_value = delta = peak = rate = None

        if delta is not None and prev_delta is not None:
            delta_vs_previous: Optional[float] = delta - prev_delta
        else:
            delta_vs_previous = None

        result.append(IntervalStats(
            index=i,
            label=right.label,
            start=left.timestamp,
            end=right.timestamp,
            duration_seconds=duration,
            start_value=start_value,
            end_value=end_value,
            delta=delta,
            rate_per_hour=rate,
            peak=peak,
            sample_count=len(values),
            delta_vs_previous=delta_vs_previous,
        ))
        prev_delta = delta
    return result


def value_near(points: Sequence[Point], ts: datetime, n: int = 5, *,
               after_only: bool = False) -> Optional[float]:
    """
    取时间上最接近 ts 的 n 个点的中位数，用作基线等参考值，避免单个尖峰样本带偏。
    n 为偶数时取中间两值的平均；距离相同时较早的点优先；点数不足 n 时用全部点。
    after_only=True 时只取 ts 及之后的点（产品规则"基线值取其后 5 个样本中位数"）。
    没有可用点时返回 None；n < 1 抛 ValueError。
    """
    if n < 1:
        raise ValueError(f'n 必须 >= 1，实际为 {n}')
    candidates = _sorted_points(points)
    if after_only:
        candidates = [p for p in candidates if p[0] >= ts]
    if not candidates:
        return None
    nearest = sorted(candidates, key=lambda p: abs((p[0] - ts).total_seconds()))[:n]
    return float(statistics.median(v for _, v in nearest))


def baseline_delta(points: Sequence[Point], baseline_ts: datetime, n: int = 5, *,
                   after_only: bool = False) -> Optional[float]:
    """当前值（时间最晚的点）减去基线值（value_near 取得）；没有点时返回 None"""
    pts = _sorted_points(points)
    if not pts:
        return None
    base = value_near(pts, baseline_ts, n, after_only=after_only)
    if base is None:
        return None
    return pts[-1][1] - base
