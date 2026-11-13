"""任务对比的纯计算：按完整操作轮、相对时间或绝对时间对齐。"""
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Tuple

from core.analysis.intervals import mean_round_delta
from core.verdict import metric_verdict

ALIGN_ROUNDS = 'rounds'
ALIGN_RELATIVE = 'relative'
ALIGN_ABSOLUTE = 'absolute'
ALIGNMENTS = (ALIGN_ROUNDS, ALIGN_RELATIVE, ALIGN_ABSOLUTE)
MAX_COMPARE_TASKS = 4


@dataclass(frozen=True)
class CompareEntry:
    task: object
    snapshots: Dict[str, object] = field(default_factory=dict)
    message: str = ''


@dataclass(frozen=True)
class ComparisonCurve:
    task_id: str
    name: str
    xs: Tuple[float, ...]
    ys: Tuple[float, ...]
    gaps: tuple = ()
    message: str = ''


@dataclass(frozen=True)
class MatrixCell:
    metric: str
    mean_delta: Optional[float]
    verdict: str
    rounds: int
    message: str = ''


def task_label(task) -> str:
    """同名任务也能辨认：进程、版本、开始时间和短 ID。"""
    name = getattr(task, 'process_name', '') or '任务'
    build = getattr(task, 'build', '')
    started = getattr(task, 'start_time', None)
    parts = [name, build, started.strftime('%m-%d %H:%M') if started else '',
             str(getattr(task, 'task_id', ''))[:8]]
    return ' · '.join(p for p in parts if p)


def origin_of(snapshot) -> Optional[datetime]:
    baseline = getattr(snapshot, 'baseline', None)
    if baseline is not None and getattr(snapshot, 'baseline_in_use', False):
        return baseline.timestamp
    for series in (snapshot.band, snapshot.buckets, snapshot.points):
        if series:
            return getattr(series[0], 'start', None) or series[0].timestamp
    return None


def center_points(snapshot):
    if snapshot.band:
        return [(b.start + (b.end - b.start) / 2, b.avg) for b in snapshot.band]
    return [(p.timestamp, p.value) for p in (snapshot.buckets or snapshot.points)]


def curve_for(entry: CompareEntry, metric: str, alignment: str) -> ComparisonCurve:
    if alignment not in ALIGNMENTS:
        raise ValueError('未知的对齐方式')
    task_id = entry.task.task_id
    name = task_label(entry.task)
    snapshot = entry.snapshots.get(metric)
    if snapshot is None or not snapshot.ok:
        return ComparisonCurve(task_id, name, (), (), message=(getattr(snapshot, 'error', None) or entry.message or '未采集该指标'))
    if alignment == ALIGN_ROUNDS:
        rounds = snapshot.rounds
        if not rounds:
            return ComparisonCurve(task_id, name, (), (), message='没有完整轮次，请改用相对时间')
        intervals = {(row.start, row.end): row for row in snapshot.intervals}
        xs, ys, cumulative = [0.0], [0.0], 0.0
        complete = True
        for index, row in enumerate(rounds, 1):
            xs.append(float(index))
            # 缺失一轮后不能把后续增量当作自基线累计变化。
            interval = intervals.get((row.start, row.end))
            delta = interval.delta if interval is not None else None
            complete = complete and delta is not None and math.isfinite(delta)
            cumulative += delta if complete else 0.0
            ys.append(cumulative if complete else math.nan)
        message = '' if complete else '部分轮次缺少采样，累计变化留空'
        return ComparisonCurve(task_id, name, tuple(xs), tuple(ys), message=message)
    points = center_points(snapshot)
    origin = origin_of(snapshot)
    baseline = snapshot.baseline.value if snapshot.baseline_in_use else None
    reference = baseline if baseline is not None else (points[0][1] if points else None)
    if origin is None or reference is None:
        return ComparisonCurve(task_id, name, (), (), message='所选范围没有数据')
    shift = origin.timestamp() if alignment == ALIGN_RELATIVE else 0.0
    xs = tuple(t.timestamp() - shift for t, _ in points)
    ys = tuple(value - reference for _, value in points)
    gaps = tuple((a.timestamp() - shift, b.timestamp() - shift) for a, b in snapshot.gaps)
    return ComparisonCurve(task_id, name, xs, ys, gaps)


def matrix_cell(entry: CompareEntry, metric: str) -> MatrixCell:
    snapshot = entry.snapshots.get(metric)
    if snapshot is None or not snapshot.ok:
        return MatrixCell(metric, None, 'insufficient', 0, getattr(snapshot, 'error', None) or entry.message or '未采集该指标')
    rounds = [r for r in snapshot.rounds if r.delta is not None]
    return MatrixCell(metric, mean_round_delta(rounds), metric_verdict(snapshot), len(rounds))
