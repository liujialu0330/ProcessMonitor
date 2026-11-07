"""
泄漏判定的统一口径（命令行 verdict / pmctl run 与证据包共用）

只依赖标准库与 utils.metrics，不碰 Qt、不查数据库；输入是分析快照（core.history_query 的
HistorySnapshot / TaskAnalysis，或同字段的替身）。

单指标（metric_verdict）：
- 快照有趋势判定（judgement）→ 其结论（rising / stable / fluctuating / insufficient）；
- 有数据但没有判定（累计量、峰值等非水位量）→ not_judged；
- 没有数据 → insufficient。

整体（overall_verdict）：
1. 被测进程在停止条件达成之前退出（stop_reason 是"被测进程退出"类，且
   stopped_before_condition 为真）→ target_exited；
2. 任一指标持续上涨 → rising；
3. 没有任何指标得出平稳/波动的结论 → insufficient；
4. 否则 → stable。

进程组任务（tree / name）：合计序列与进程总数照常判定；每个成员的每个序列（指标@成员键）
各自判定（member_verdicts），整体判定把合计与全部成员一起看——合计或任一成员持续上涨即
rising（总量持平而某个成员在涨，同样是泄漏线索）。pid 任务没有成员，规则不变。

"停止条件达成之前"由调用方判断（见 exited_before_condition）：任务按方案的时长/轮数
已经满足停止条件之后被测进程才退出（如停止条件到点后关闭被测程序、或二者几乎同时发生），
测试本身是完整的，照常判定，退出信息另行附在结果里。没有停止条件的任务，被测进程退出
一律算"之前"。
"""
from typing import Any, Dict, Iterable, Optional

from utils.metrics import split_member_key

VERDICT_STABLE = 'stable'
VERDICT_RISING = 'rising'
VERDICT_INSUFFICIENT = 'insufficient'
VERDICT_TARGET_EXITED = 'target_exited'
# 单指标才有的取值
VERDICT_FLUCTUATING = 'fluctuating'
METRIC_NOT_JUDGED = 'not_judged'
REASON_NO_DATA = 'no_data'

# 单指标里算作"得出了结论"的取值
CONCLUSIVE_VERDICTS = frozenset({VERDICT_STABLE, VERDICT_FLUCTUATING, VERDICT_RISING})
# 视为"被测进程退出"的停止原因（core.contracts.StopReason 的取值）
TARGET_EXIT_REASONS = frozenset({'process_exited', 'process_not_found', 'pid_reused'})


def stop_reason_value(reason: Any) -> Optional[str]:
    """停止原因（StopReason 枚举或其字符串值）→ 字符串值；None 为 None。"""
    if reason is None:
        return None
    return str(getattr(reason, 'value', reason))


def is_target_exit(reason: Any) -> bool:
    """停止原因是否为"被测进程退出"类（进程退出、启动时不存在、PID 被复用）。"""
    return stop_reason_value(reason) in TARGET_EXIT_REASONS


def metric_verdict(snapshot) -> str:
    """单个指标的判定字符串（见模块说明）。"""
    judgement = getattr(snapshot, 'judgement', None)
    if judgement is not None:
        return str(getattr(judgement.verdict, 'value', judgement.verdict))
    if getattr(snapshot, 'points', None):
        return METRIC_NOT_JUDGED
    return VERDICT_INSUFFICIENT


def metric_reason(snapshot) -> Optional[str]:
    """单个指标判定的原因代码：有判定时取判定的原因，无数据为 no_data，否则 None。"""
    judgement = getattr(snapshot, 'judgement', None)
    if judgement is not None:
        return judgement.reason
    if getattr(snapshot, 'points', None):
        return None
    return REASON_NO_DATA


def combine_verdicts(verdicts: Iterable[str], *, stop_reason: Any = None,
                     stopped_before_condition: bool = False) -> str:
    """若干单指标判定 → 整体判定（规则见模块说明）。"""
    if stopped_before_condition and is_target_exit(stop_reason):
        return VERDICT_TARGET_EXITED
    verdicts = list(verdicts)
    if VERDICT_RISING in verdicts:
        return VERDICT_RISING
    if not any(v in (VERDICT_STABLE, VERDICT_FLUCTUATING) for v in verdicts):
        return VERDICT_INSUFFICIENT
    return VERDICT_STABLE


def member_snapshots_of(analysis) -> Dict[str, Any]:
    """分析结果里的成员序列快照 {指标@成员键: 快照}；pid 任务（或旧结构的替身）为空。"""
    return getattr(analysis, 'member_snapshots', None) or {}


def member_verdicts(analysis) -> Dict[str, Dict[str, str]]:
    """每个成员各指标的判定：{成员键: {指标键: 判定}}（成员与指标保持快照里的顺序）。
    无 Qt 依赖，命令行、证据包与界面共用。"""
    result: Dict[str, Dict[str, str]] = {}
    for series_key, snapshot in member_snapshots_of(analysis).items():
        metric, member = split_member_key(series_key)
        if member is None:
            continue
        result.setdefault(member, {})[metric] = metric_verdict(snapshot)
    return result


def member_overall(analysis) -> Dict[str, str]:
    """每个成员自己的整体判定：{成员键: rising / stable / insufficient}（规则同 combine_verdicts，
    不含"被测进程退出"，那是任务级的）。"""
    return {member: combine_verdicts(metrics.values())
            for member, metrics in member_verdicts(analysis).items()}


def overall_verdict(analysis, *, stop_reason: Any = None,
                    stopped_before_condition: bool = False) -> str:
    """
    任务的整体判定。

    Args:
        analysis: 有 snapshots（{指标: 快照}）属性的分析结果（TaskAnalysis）
        stop_reason: 任务的停止原因（StopReason 或其值）；None 表示未知/未停止
        stopped_before_condition: 被测进程是否在停止条件达成之前退出（调用方用
            exited_before_condition 判断）。只有它为真且 stop_reason 是退出类时才判
            target_exited；缺省为假，即调用方不知道时按普通判定处理
    """
    verdicts = [metric_verdict(s) for s in analysis.snapshots.values()]
    verdicts += [metric_verdict(s) for s in member_snapshots_of(analysis).values()]
    return combine_verdicts(verdicts, stop_reason=stop_reason,
                            stopped_before_condition=stopped_before_condition)


def stop_condition_reached(plan, *, elapsed_s: Optional[float], rounds: int) -> bool:
    """
    方案的停止条件在任务结束时是否已经满足（与 core.automation.stop_conditions 同口径：
    时长按墙钟、暂停也算；轮数为 kind='user' 的打点数）。没有方案或方案没有停止条件为假。
    """
    if plan is None:
        return False
    seconds = getattr(plan, 'stop_after_seconds', None)
    if seconds is not None and elapsed_s is not None and elapsed_s >= seconds:
        return True
    limit = getattr(plan, 'stop_after_rounds', None)
    return limit is not None and rounds >= limit


def exited_before_condition(stop_reason: Any, plan, *, elapsed_s: Optional[float],
                            rounds: int) -> bool:
    """被测进程退出（stop_reason 为退出类）且此时停止条件尚未满足。"""
    return is_target_exit(stop_reason) and not stop_condition_reached(
        plan, elapsed_s=elapsed_s, rounds=rounds)


__all__ = [
    'VERDICT_STABLE', 'VERDICT_RISING', 'VERDICT_INSUFFICIENT', 'VERDICT_TARGET_EXITED',
    'VERDICT_FLUCTUATING', 'METRIC_NOT_JUDGED', 'REASON_NO_DATA', 'TARGET_EXIT_REASONS',
    'stop_reason_value', 'is_target_exit', 'metric_verdict', 'metric_reason',
    'combine_verdicts', 'overall_verdict', 'member_snapshots_of', 'member_verdicts',
    'member_overall', 'stop_condition_reached', 'exited_before_condition',
]
