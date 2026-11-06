"""
时间序列的显示辅助：空洞切断、累计量的每秒速率、峰值抬升次数

空洞以 (空洞前最后一点, 空洞后第一点) 表示（与 AnalyticsRepo.gaps 一致）。相邻两个绘制点
之间"跨过"某个空洞，当且仅当该空洞的两端都落在这两点之间（含端点）——绘制点都是真实
数据点（降采样只挑点不造点），所以这一判断不受降采样影响。时间可以是 datetime，
也可以是 epoch 秒（同一次调用里与空洞的类型一致即可）。
"""
from bisect import bisect_left
from typing import List, Sequence, Tuple, TypeVar

T = TypeVar('T')
Gap = Tuple[T, T]


def crosses_gap(left: T, right: T, gap_starts: Sequence[T], gaps: Sequence[Gap]) -> bool:
    """left→right 这一段是否跨过空洞；gap_starts 为 gaps 各项起点（升序，供二分）。"""
    index = bisect_left(gap_starts, left)
    return index < len(gaps) and gaps[index][1] <= right


def connect_mask(times: Sequence[T], gaps: Sequence[Gap]) -> List[bool]:
    """
    与 times 等长的连线掩码：第 i 项为 True 表示第 i 点与第 i+1 点相连（pyqtgraph 的
    connect 数组语义），最后一项恒为 True（无后继点，取值无影响）。times 须升序。
    """
    ordered = sorted(gaps, key=lambda g: g[0])
    starts = [g[0] for g in ordered]
    mask = [True] * len(times)
    if not ordered:
        return mask
    for i in range(len(times) - 1):
        if crosses_gap(times[i], times[i + 1], starts, ordered):
            mask[i] = False
    return mask


def per_second_rates(points: Sequence[Tuple[T, float]],
                     gaps: Sequence[Gap] = ()) -> List[Tuple[T, float]]:
    """
    累计量的每秒速率：相邻两点的差 / 间隔秒数，记在后一点的时刻上。
    跨空洞的那一段不算（空洞里发生了多少无从得知，摊平会把尖峰抹掉），
    时间相同的两点也跳过；points 须升序。时间为 datetime 时按 total_seconds 计间隔。
    """
    ordered = sorted(gaps, key=lambda g: g[0])
    starts = [g[0] for g in ordered]
    result: List[Tuple[T, float]] = []
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        dt = t1 - t0
        seconds = dt.total_seconds() if hasattr(dt, 'total_seconds') else float(dt)
        if seconds <= 0:
            continue
        if ordered and crosses_gap(t0, t1, starts, ordered):
            continue
        result.append((t1, (v1 - v0) / seconds))
    return result


def count_rises(values: Sequence[float]) -> int:
    """相邻值上升（严格大于前一值）的次数：峰值类指标的"峰值抬升次数"。"""
    return sum(1 for a, b in zip(values, values[1:]) if b > a)
