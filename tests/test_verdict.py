"""
判定统一口径（core/verdict.py）的表驱动用例：单指标判定、整体判定（含"被测进程在停止条件
达成之前退出"）、停止条件是否已满足；命令行 compute_verdict 与证据包都走这一份规则。
"""
from types import SimpleNamespace

import pytest

from core import verdict as v
from core.automation.plan import AutomationPlan
from core.contracts import StopReason


def snap(verdict=None, reason=None, points=(1, 2, 3)):
    judgement = SimpleNamespace(verdict=verdict, reason=reason) if verdict else None
    return SimpleNamespace(judgement=judgement, points=list(points))


def analysis(*snapshots):
    return SimpleNamespace(snapshots={f"m{i}": s for i, s in enumerate(snapshots)})


@pytest.mark.parametrize('snapshot, verdict, reason', [
    (snap('rising', 'rising'), 'rising', 'rising'),
    (snap('stable'), 'stable', None),
    (snap('fluctuating'), 'fluctuating', None),
    (snap('insufficient', 'few_points'), 'insufficient', 'few_points'),
    (snap(None), 'not_judged', None),                    # 有数据但非水位量
    (snap(None, points=()), 'insufficient', 'no_data'),  # 没有数据
])
def test_metric_verdict(snapshot, verdict, reason):
    assert v.metric_verdict(snapshot) == verdict
    assert v.metric_reason(snapshot) == reason


def test_metric_verdict_accepts_enum_values():
    from core.analysis.trend import Verdict
    assert v.metric_verdict(snap(Verdict.RISING)) == 'rising'


R, S, F, I, N = (snap('rising'), snap('stable'), snap('fluctuating'), snap('insufficient'),
                 snap(None))
EXITED = StopReason.PROCESS_EXITED


@pytest.mark.parametrize('snapshots, kwargs, expected', [
    ((S, S), {}, 'stable'),
    ((S, F), {}, 'stable'),
    ((F,), {}, 'stable'),
    ((S, R), {}, 'rising'),
    ((I, R), {}, 'rising'),
    ((I, N), {}, 'insufficient'),
    ((), {}, 'insufficient'),
    # 被测进程在停止条件达成之前退出：优先于一切
    ((S, R), {'stop_reason': EXITED, 'stopped_before_condition': True}, 'target_exited'),
    ((S,), {'stop_reason': StopReason.PROCESS_NOT_FOUND, 'stopped_before_condition': True},
     'target_exited'),
    ((S,), {'stop_reason': 'pid_reused', 'stopped_before_condition': True}, 'target_exited'),
    # 条件达成之后才退出（或调用方不知道）：照常判定
    ((S, R), {'stop_reason': EXITED, 'stopped_before_condition': False}, 'rising'),
    ((S,), {'stop_reason': EXITED}, 'stable'),
    # 用户停止 / 内部错误不是"被测进程退出"
    ((S,), {'stop_reason': StopReason.USER, 'stopped_before_condition': True}, 'stable'),
    ((I,), {'stop_reason': StopReason.INTERNAL_ERROR, 'stopped_before_condition': True},
     'insufficient'),
])
def test_overall_verdict(snapshots, kwargs, expected):
    assert v.overall_verdict(analysis(*snapshots), **kwargs) == expected
    # 由单指标字符串组合得到同一结果（证据包走这一条）
    assert v.combine_verdicts([v.metric_verdict(s) for s in snapshots], **kwargs) == expected


@pytest.mark.parametrize('plan, elapsed, rounds, reached', [
    (None, 100.0, 9, False),
    (AutomationPlan(), 100.0, 9, False),
    (AutomationPlan(stop_after_seconds=60), 59.9, 0, False),
    (AutomationPlan(stop_after_seconds=60), 60.0, 0, True),
    (AutomationPlan(stop_after_seconds=60), None, 0, False),
    (AutomationPlan(stop_after_rounds=3), 1.0, 2, False),
    (AutomationPlan(stop_after_rounds=3), 1.0, 3, True),
    (AutomationPlan(stop_after_seconds=600, stop_after_rounds=3), 10.0, 3, True),
])
def test_stop_condition_reached(plan, elapsed, rounds, reached):
    assert v.stop_condition_reached(plan, elapsed_s=elapsed, rounds=rounds) is reached


@pytest.mark.parametrize('reason, plan, elapsed, rounds, expected', [
    (EXITED, None, 5.0, 0, True),                                   # 没有停止条件：一律算"之前"
    (EXITED, AutomationPlan(stop_after_rounds=5), 5.0, 2, True),
    (EXITED, AutomationPlan(stop_after_rounds=5), 5.0, 5, False),   # 条件已满足后才退出
    (EXITED, AutomationPlan(stop_after_seconds=30), 31.0, 0, False),
    (StopReason.USER, None, 5.0, 0, False),
    (None, None, 5.0, 0, False),
])
def test_exited_before_condition(reason, plan, elapsed, rounds, expected):
    assert v.exited_before_condition(reason, plan, elapsed_s=elapsed, rounds=rounds) is expected


def test_stop_reason_helpers():
    assert v.stop_reason_value(StopReason.PID_REUSED) == 'pid_reused'
    assert v.stop_reason_value('user') == 'user' and v.stop_reason_value(None) is None
    assert {r for r in StopReason if v.is_target_exit(r)} == {
        StopReason.PROCESS_EXITED, StopReason.CRASHED, StopReason.PROCESS_NOT_FOUND,
        StopReason.PID_REUSED}


def test_compute_verdict_uses_shared_rules():
    """命令行 verdict 与证据包同一口径：compute_verdict 只是在 core.verdict 外面加指标明细。"""
    from core.ipc.commands import compute_verdict
    stats = {'count': 3, 'first': 1.0, 'last': 3.0, 'slope_per_hour': 2.0}

    def full(verdict):
        s = snap(verdict)
        s.error, s.stats, s.baseline, s.total_count, s.rounds = None, stats, None, 3, []
        return s

    data = SimpleNamespace(snapshots={'memory_rss': full('rising'), 'num_handles': full('stable')})
    result = compute_verdict(data)
    assert result['verdict'] == 'rising' and result['target_exit'] is None
    exited = compute_verdict(data, EXITED)
    assert exited['verdict'] == 'target_exited'
    assert exited['target_exit'] == {'reason': 'process_exited', 'before_stop_condition': True}
    after = compute_verdict(data, EXITED, stopped_before_condition=False)
    assert after['verdict'] == 'rising'
    assert after['target_exit'] == {'reason': 'process_exited', 'before_stop_condition': False}
    assert after['stop_reason'] == 'process_exited'


def test_module_is_stdlib_only():
    import inspect
    source = inspect.getsource(v)
    for banned in ('PyQt5', 'import data', 'from data', 'from ui', 'import ui'):
        assert banned not in source
