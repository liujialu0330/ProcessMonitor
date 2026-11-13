"""场景参考比较：按完成轮次的增量分布判断回归，供界面与命令行共用。"""
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

from core.history_query import TaskAnalysis, build_request, run_history_query
from core.workers.base import BackgroundTask
from utils.metrics import VALUE_CLASS_LEVEL, value_class

MIN_ROUNDS = 3
# 双侧 95% Student-t 临界值；大样本仍取对应较小自由度的值，保持保守。
_T95 = ((1, 12.706), (2, 4.303), (3, 3.182), (4, 2.776), (5, 2.571),
        (6, 2.447), (7, 2.365), (8, 2.306), (9, 2.262), (10, 2.228),
        (15, 2.131), (20, 2.086), (30, 2.042), (60, 2.000), (120, 1.980))


@dataclass(frozen=True)
class RoundEstimate:
    count: int
    mean: float
    low: float
    high: float


@dataclass(frozen=True)
class ReferenceRow:
    metric: str
    status: str
    reason: str = ''
    current: Optional[RoundEstimate] = None
    reference: Optional[RoundEstimate] = None

    @property
    def delta(self) -> Optional[float]:
        if self.current is None or self.reference is None:
            return None
        return self.current.mean - self.reference.mean


@dataclass(frozen=True)
class ReferenceComparison:
    task_id: str
    reference_id: Optional[str]
    scenario: str
    rows: Tuple[ReferenceRow, ...] = ()
    reason: str = ''

    @property
    def status(self) -> str:
        if any(row.status == 'regression' for row in self.rows):
            return 'regression'
        if any(row.status == 'comparable' for row in self.rows):
            return 'comparable'
        return 'incomparable'

    def to_dict(self) -> dict:
        return {**asdict(self), 'status': self.status}


def estimate_rounds(snapshot) -> Tuple[Optional[RoundEstimate], str]:
    if snapshot is None:
        return None, '未采集该指标'
    if getattr(snapshot, 'error', None):
        return None, '指标查询失败'
    if getattr(snapshot, 'baseline', None) is None:
        return None, '未设置基线'
    if not getattr(snapshot, 'baseline_comparable', True):
        return None, '基线与统计不在同一启动段'
    rounds = list(getattr(snapshot, 'rounds', ()) or ())
    if any(r.delta is None or not math.isfinite(r.delta) for r in rounds):
        return None, '轮次存在缺失样本'
    values = [float(r.delta) for r in rounds]
    if len(values) < MIN_ROUNDS:
        return None, f'至少需要 {MIN_ROUNDS} 个完整轮次'
    mean = statistics.mean(values)
    df = len(values) - 1
    critical = next((value for degrees, value in reversed(_T95) if degrees <= df), 12.706)
    half = critical * statistics.stdev(values) / math.sqrt(len(values))
    return RoundEstimate(len(values), mean, mean - half, mean + half), ''


def compare_analyses(current: TaskAnalysis, reference: TaskAnalysis, *,
                     scenario: str = '') -> ReferenceComparison:
    rows = []
    for metric, snapshot in current.snapshots.items():
        if value_class(metric) != VALUE_CLASS_LEVEL:
            rows.append(ReferenceRow(metric, 'incomparable', '累计量和峰值不比较每轮增长'))
            continue
        cur, problem = estimate_rounds(snapshot)
        ref, ref_problem = estimate_rounds(reference.snapshots.get(metric))
        if problem or ref_problem:
            reason = ('当前任务：' + problem) if problem else ('参考任务：' + ref_problem)
            rows.append(ReferenceRow(metric, 'incomparable', reason, cur, ref))
            continue
        status = 'regression' if cur.low > ref.high else 'comparable'
        reason = ('当前每轮均增的 95% 置信区间高于参考且不重叠'
                  if status == 'regression' else '未发现每轮增长显著高于参考')
        rows.append(ReferenceRow(metric, status, reason, cur, ref))
    return ReferenceComparison(current.task_id, reference.task_id, scenario, tuple(rows))


def load_reference_comparison(db, task_id: str, analysis=None, *, cancelled=None):
    """在工作线程调用；双方均用完整任务的有效基线后轮次，避免刷选范围改变回归口径。"""
    task = db.tasks.get(task_id)
    if task is None:
        return ReferenceComparison(task_id, None, '', reason='任务已不存在')
    if not task.scenario:
        return ReferenceComparison(task_id, None, '', reason='请先填写测试场景')
    reference = db.tasks.reference_for(task.scenario)
    if reference is None:
        return ReferenceComparison(task_id, None, task.scenario, reason='该场景还没有参考任务')
    if reference.task_id == task_id:
        return ReferenceComparison(task_id, reference.task_id, task.scenario,
                                   reason='当前任务就是该场景的参考')
    if task.target_kind != reference.target_kind:
        return ReferenceComparison(task_id, reference.task_id, task.scenario,
                                   reason='当前任务与参考任务的目标类型不同')

    def load(record):
        snapshots = {}
        for metric in record.metric_types:
            if cancelled is not None and cancelled():
                raise ValueError('已取消参考比较')
            snapshots[metric] = run_history_query(
                db, build_request(record.task_id, metric, 'all', analysis=True))
        return TaskAnalysis(record.task_id, 'all', snapshots)

    current = (analysis if analysis is not None and analysis.task_id == task_id
               and analysis.range_key == 'all' else load(task))
    return compare_analyses(current, load(reference), scenario=task.scenario)


class ReferenceWorker(BackgroundTask):
    def __init__(self, db, task_id: str, analysis=None, parent=None):
        super().__init__(parent)
        self._db, self._task_id, self._analysis = db, task_id, analysis

    def execute(self, token):
        return load_reference_comparison(self._db, self._task_id, self._analysis,
                                         cancelled=lambda: token.is_cancelled)
