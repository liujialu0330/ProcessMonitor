"""
泄漏判定的统一口径（命令行 verdict / pmctl run 与证据包共用）

只依赖标准库与 utils.metrics、core.exit_forensics（也只依赖标准库），不碰 Qt、不查数据库；
输入是分析快照（core.history_query 的 HistorySnapshot / TaskAnalysis，或同字段的替身）。

单指标（metric_verdict）：
- 快照有趋势判定（judgement）→ 其结论（rising / stable / fluctuating / insufficient）；
- 有数据但没有判定的非水位量（累计量、峰值等，统计范围里有数据）→ not_judged；
- 没有数据，或统计范围里没有数据（如重启后的最后一段还没有样本）→ insufficient。

单段（segment_verdict）：把一个启动段内各指标的判定按 combine_verdicts 的规则合成：任一
上涨 → rising；没有任何指标得出平稳/波动的结论 → insufficient（本段数据不足）；否则 stable。

整体（overall_verdict）：
1. 被测进程在停止条件达成之前退出（stop_reason 是"被测进程退出"类，且
   stopped_before_condition 为真）→ target_exited；
2. 中途异常退出过（有"异常退出"打点之后又续接了新进程）→ target_exited：测试期间崩溃过，
   不论最后一段多干净，都不能报"平稳"；结论里写次数、最近一次的退出码与各段判定；
3. 被测进程重启续接过（分析结果带 segments）→ 逐段判定，整体取最差的一段
   （rising > insufficient > stable）：只看最后一段会把前面段里的泄漏、以及"最后一段太短
   什么也没测出来"都掩盖成"平稳"；
4. 没有分段信息时：任一指标持续上涨 → rising；没有任何指标得出平稳/波动的结论 →
   insufficient；否则 → stable。

进程组任务（tree / name）：合计序列与进程总数照常判定；每个成员的每个序列（指标@成员键）
各自判定（member_verdicts），整体判定把合计与全部成员一起看——合计或任一成员持续上涨即
rising（总量持平而某个成员在涨，同样是泄漏线索）。pid 任务没有成员，规则不变。

"停止条件达成之前"由调用方判断（见 exited_before_condition）：任务按方案的时长/轮数
已经满足停止条件之后被测进程才退出（如停止条件到点后关闭被测程序、或二者几乎同时发生），
测试本身是完整的，照常判定，退出信息另行附在结果里。没有停止条件的任务，被测进程退出
一律算"之前"。
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.exit_forensics import CRASH_MARKER_PREFIX
from utils.metrics import VALUE_CLASS_LEVEL, get_metric_display_name, split_member_key

VERDICT_STABLE = 'stable'
VERDICT_RISING = 'rising'
VERDICT_INSUFFICIENT = 'insufficient'
VERDICT_TARGET_EXITED = 'target_exited'
# 单指标才有的取值
VERDICT_FLUCTUATING = 'fluctuating'
METRIC_NOT_JUDGED = 'not_judged'
REASON_NO_DATA = 'no_data'
# 有明细点但统计范围里没有数据（重启后的最后一段还没有样本）：与"整个任务没数据"区分，
# 文案按"数据不足"而不是"无数据"
REASON_NO_STATS = 'no_stats_in_range'

MARKER_KIND_CRASH = 'crash'
MARKER_KIND_RESTART = 'restart'
# 逐段判定的"最差"次序：持续上涨 > 数据不足 > 平稳（数据不足的段没有证明它没泄漏）
_SEGMENT_RANK = {VERDICT_STABLE: 0, VERDICT_INSUFFICIENT: 1, VERDICT_RISING: 2}
SEGMENT_VERDICT_TEXTS = {VERDICT_RISING: "持续上涨", VERDICT_STABLE: "平稳",
                         VERDICT_INSUFFICIENT: "本段数据不足"}

# 单指标里算作"得出了结论"的取值
CONCLUSIVE_VERDICTS = frozenset({VERDICT_STABLE, VERDICT_FLUCTUATING, VERDICT_RISING})
# 视为"被测进程退出"的停止原因（core.contracts.StopReason 的取值）
TARGET_EXIT_REASONS = frozenset({'process_exited', 'process_not_found', 'pid_reused', 'crashed'})


def stop_reason_value(reason: Any) -> Optional[str]:
    """停止原因（StopReason 枚举或其字符串值）→ 字符串值；None 为 None。"""
    if reason is None:
        return None
    return str(getattr(reason, 'value', reason))


def is_target_exit(reason: Any) -> bool:
    """停止原因是否为"被测进程退出"类（进程退出、异常退出、启动时不存在、PID 被复用）。"""
    return stop_reason_value(reason) in TARGET_EXIT_REASONS


def _stats_missing(snapshot) -> bool:
    """快照有明细点，但统计范围里没有数据（也没有判定）。水位量（本该有判定）出现这种
    情况同样算没数据；替身快照没有 stats 字段时按"有统计"处理。"""
    if hasattr(snapshot, 'stats') and snapshot.stats is None:
        return True
    return getattr(snapshot, 'value_class', None) == VALUE_CLASS_LEVEL


def metric_verdict(snapshot) -> str:
    """单个指标的判定字符串（见模块说明）。"""
    judgement = getattr(snapshot, 'judgement', None)
    if judgement is not None:
        return str(getattr(judgement.verdict, 'value', judgement.verdict))
    if getattr(snapshot, 'points', None) and not _stats_missing(snapshot):
        return METRIC_NOT_JUDGED
    return VERDICT_INSUFFICIENT


def metric_reason(snapshot) -> Optional[str]:
    """单个指标判定的原因代码：有判定时取判定的原因，没有明细点为 no_data，有明细点但统计
    范围里没数据为 no_stats_in_range，否则 None。"""
    judgement = getattr(snapshot, 'judgement', None)
    if judgement is not None:
        return judgement.reason
    if getattr(snapshot, 'points', None):
        return REASON_NO_STATS if _stats_missing(snapshot) else None
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


def segment_verdict(verdicts: Iterable[str]) -> str:
    """一个启动段内各序列的判定 → 本段判定（rising / stable / insufficient）。"""
    return combine_verdicts(verdicts)


def worst_segment_verdict(segments: Sequence[Any]) -> str:
    """各段判定里最差的一个：rising > insufficient > stable。没有分段为 insufficient。"""
    verdicts = [str(s.verdict) for s in segments]
    if not verdicts:
        return VERDICT_INSUFFICIENT
    return max(verdicts, key=lambda v: _SEGMENT_RANK.get(v, 1))


def mid_run_crashes(markers: Iterable[Any]) -> List[Any]:
    """中途异常退出的打点：其后又出现了"重启"打点的"异常退出"打点（被测进程崩溃后被
    续接）。最后一次退出（后面没有重启）不算——它由停止原因 crashed 与停止条件一起判断。
    按时间升序。"""
    ordered = sorted(markers, key=lambda m: m.timestamp)
    restarts = [m.timestamp for m in ordered if m.kind == MARKER_KIND_RESTART]
    return [m for m in ordered if m.kind == MARKER_KIND_CRASH
            and any(t >= m.timestamp for t in restarts)]


def crash_detail(marker: Any) -> str:
    """"异常退出 0xC0000017（…）"打点标签 → "0xC0000017（…）"。"""
    label = str(getattr(marker, 'label', '') or '')
    if label.startswith(CRASH_MARKER_PREFIX):
        label = label[len(CRASH_MARKER_PREFIX):]
    return label.strip()


def segments_of(analysis) -> List[Any]:
    """分析结果里的分段结果 [SegmentAnalysis]；没有（或旧结构的替身）为空。"""
    return list(getattr(analysis, 'segments', None) or [])


def crashes_of(analysis) -> List[Any]:
    """分析结果所属任务的中途异常退出打点（见 mid_run_crashes）。"""
    return mid_run_crashes(getattr(analysis, 'markers', None) or [])


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec='milliseconds') if value is not None else None


def segments_payload(analysis) -> List[Dict[str, Any]]:
    """各段结果的可序列化形式（命令行 verdict、证据包 task.json 共用）：
    number / start / end（None 表示任务开始 / 仍在进行）/ verdict / rising_metrics
    （序列键）/ rising_names（显示名）/ samples。"""
    return [{
        'number': s.number, 'start': _iso(s.start), 'end': _iso(s.end),
        'verdict': s.verdict, 'rising_metrics': list(s.rising_metrics),
        'rising_names': [get_metric_display_name(k) for k in s.rising_metrics],
        'samples': s.sample_count,
    } for s in segments_of(analysis)]


def crashes_payload(analysis) -> List[Dict[str, Any]]:
    """中途异常退出的可序列化形式：[{timestamp, label}]（label 为打点原文）。"""
    return [{'timestamp': _iso(m.timestamp), 'label': m.label} for m in crashes_of(analysis)]


def segments_summary(analysis) -> str:
    """纯文本摘要，命令行、证据包 README 与界面共用，如
    "中途异常退出 2 次（最近一次 0xC0000017（…））；各段判定：第 1 段 持续上涨（私有字节）；
    第 2 段 本段数据不足；第 3 段 平稳"。没有重启也没有中途异常退出时为空串。"""
    segments = segments_of(analysis)
    crashes = crashes_of(analysis)
    parts: List[str] = []
    if crashes:
        parts.append(f"中途异常退出 {len(crashes)} 次（最近一次 {crash_detail(crashes[-1])}）")
    if len(segments) > 1 or (crashes and segments):
        lines = []
        for seg in segments:
            text = SEGMENT_VERDICT_TEXTS.get(seg.verdict, seg.verdict)
            if seg.verdict == VERDICT_RISING and seg.rising_metrics:
                names = "、".join(get_metric_display_name(k) for k in seg.rising_metrics)
                text = f"{text}（{names}）"
            lines.append(f"第 {seg.number} 段 {text}")
        parts.append("各段判定：" + "；".join(lines))
    return "；".join(parts)


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
    if stopped_before_condition and is_target_exit(stop_reason):
        return VERDICT_TARGET_EXITED
    if crashes_of(analysis):
        return VERDICT_TARGET_EXITED
    segments = segments_of(analysis)
    if segments:
        return worst_segment_verdict(segments)
    verdicts = [metric_verdict(s) for s in analysis.snapshots.values()]
    verdicts += [metric_verdict(s) for s in member_snapshots_of(analysis).values()]
    return combine_verdicts(verdicts)


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
    'VERDICT_FLUCTUATING', 'METRIC_NOT_JUDGED', 'REASON_NO_DATA', 'REASON_NO_STATS',
    'TARGET_EXIT_REASONS', 'SEGMENT_VERDICT_TEXTS', 'segment_verdict', 'worst_segment_verdict',
    'mid_run_crashes', 'crash_detail', 'segments_of', 'crashes_of', 'segments_payload',
    'crashes_payload', 'segments_summary',
    'stop_reason_value', 'is_target_exit', 'metric_verdict', 'metric_reason',
    'combine_verdicts', 'overall_verdict', 'member_snapshots_of', 'member_verdicts',
    'member_overall', 'stop_condition_reached', 'exited_before_condition',
]
