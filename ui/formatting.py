"""
界面文案纯函数
不依赖任何 Qt 控件，集中放置分析页/监控页/报告共用的文本拼装与简单计算，
便于脱离 GUI 直接单测。
"""
import bisect
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Sequence

from core.analysis.intervals import mean_round_delta
from core.analysis.trend import (
    MIN_POINTS, Judgement, RateText, Verdict, min_span_for)
from core.automation.plan import plan_from_json_or_none
from core.automation.stop_conditions import AUTO_STOP_DURATION, AUTO_STOP_ROUNDS
from core.contracts import StopReason, UnavailableReason
from core.exit_forensics import (
    CRASH_MARKER_PREFIX, exception_text, is_abnormal_exit, normalize_exit_code)
from core.history_query import segment_note
from core.verdict import (
    crash_detail, REASON_NO_DATA, REASON_NO_STATS, VERDICT_INSUFFICIENT, VERDICT_RISING, VERDICT_STABLE,
    VERDICT_TARGET_EXITED, crashes_of, is_target_exit, member_overall, member_snapshots_of,
    metric_reason, metric_verdict, overall_verdict, segments_of, segments_summary,
    worst_segment_verdict,
)
from data.db.migrations.runner import disk_shortage_message
from data.models import TARGET_NAME, TARGET_PID, TARGET_TREE, MonitorTask
from utils.metrics import (
    KB_UNIT_DIVISORS, MAX_GROUP_MEMBERS, VALUE_CLASS_PEAK, VALUE_CLASS_RATE, display_unit_for,
    format_metric_delta, format_metric_rate, format_metric_value,
    get_metric_description, get_metric_display_name, get_metric_unit, is_kb_metric,
    object_count_limit, split_member_key, value_class,
)

if TYPE_CHECKING:
    from core.history_query import HistorySnapshot

# 下拉项里备注的最大显示字数，超出截断并以省略号结尾（完整内容放 tooltip）
NOTE_MAX_CHARS = 16

# 停止原因与指标不可用原因的界面文案：core 只传枚举，显示文字只在这里定义
_STOP_REASON_TEXTS = {
    StopReason.USER: "用户停止",
    StopReason.PROCESS_EXITED: "进程已终止",
    StopReason.PROCESS_NOT_FOUND: "进程不存在",
    # 与运行中发现 PID 被复用的情形口径一致：对用户而言都是原进程已经没了
    StopReason.PID_REUSED: "进程已终止",
    StopReason.INTERNAL_ERROR: "内部错误",
    StopReason.CRASHED: "被测进程异常退出",
}

_UNAVAILABLE_REASON_TEXTS = {
    UnavailableReason.ACCESS_DENIED: "权限不足",
    UnavailableReason.PROCESS_EXITING: "进程正在退出",
    UnavailableReason.NOT_IN_SESSION: "进程不在当前会话，不支持采集",
    UnavailableReason.NOT_SUPPORTED: "仅支持 Windows",
    UnavailableReason.ERROR: "采集失败",
}


def stop_reason_text(reason: StopReason) -> str:
    """任务停止原因的界面文案。"""
    return _STOP_REASON_TEXTS[reason]


def unavailable_reason_text(reason: UnavailableReason) -> str:
    """指标不可用原因的界面文案。"""
    return _UNAVAILABLE_REASON_TEXTS[reason]


def crash_exit_text(exit_code: Optional[int]) -> str:
    """被测进程异常退出的说明："异常退出：访问冲突 0xC0000005"。退出码没取到时只写
    "被测进程异常退出"；表外的异常码写"未知异常"；非异常码（不应出现在这里）写退出码本身。"""
    value = normalize_exit_code(exit_code)
    if value is None:
        return _STOP_REASON_TEXTS[StopReason.CRASHED]
    if not is_abnormal_exit(value):
        return f"{CRASH_MARKER_PREFIX}：退出码 {value}"
    return f"{CRASH_MARKER_PREFIX}：{exception_text(value)} 0x{value:08X}"


def stop_detail_text(reason: StopReason, exit_code: Optional[int] = None) -> str:
    """已结束行 / 异常结束提示里的停止说明：异常退出（CRASHED）带退出码说明，其余同
    stop_reason_text。"""
    if reason is StopReason.CRASHED:
        return crash_exit_text(exit_code)
    return stop_reason_text(reason)


def is_abnormal_stop(reason: Optional[StopReason]) -> bool:
    """停止原因是否算"异常结束"：只有用户自己停止（含退出程序）是正常结束。"""
    return reason is not None and reason is not StopReason.USER


def task_status_text(
        running: bool, *, paused: bool = False, abnormal: bool = False) -> str:
    """任务状态文案，全应用统一四个词：运行中 / 已暂停 / 已结束 / 异常结束。

    running 对应 TaskStatus.RUNNING（含暂停中）；paused 只在运行中有意义，
    abnormal 只在已结束时有意义（由 is_abnormal_stop(StopReason) 推导）。
    库里不保存停止原因，从库读出的已结束任务一律按"已结束"显示。
    """
    if running:
        return "已暂停" if paused else "运行中"
    return "异常结束" if abnormal else "已结束"


def waiting_restart_text(restart_count: int, restart_limit: int) -> str:
    """被测进程退出后等它重启并续接的状态词："等待重启（第 2/3 次）"：数字是这次等的是第几次
    续接（已续接次数 + 1）与允许的上限；上限未知（0）时不写次数。"""
    if int(restart_limit) <= 0:
        return "等待重启"
    return f"等待重启（第 {int(restart_count) + 1}/{int(restart_limit)} 次）"


RESTARTED_TITLE = "已续接"


def restarted_hint_text(restart_count: int, pid: Optional[int]) -> str:
    """续接成功的短暂提示："已续接：第 2 次启动 PID 4321"（第 N 次启动 = 已续接次数 + 1，
    与"重启"打点的名称同口径）。"""
    text = f"已续接：第 {int(restart_count) + 1} 次启动"
    return f"{text} PID {pid}" if pid else text


def format_count(number: int) -> str:
    """整数加千分位，如 12345 -> "12,345"。"""
    return f"{int(number):,}"


def format_duration(seconds: float) -> str:
    """把秒数格式化为易读时长：45秒 / 12分钟 / 2小时15分 / 1天3小时。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        return f"{total // 60}分钟"
    if total < 86400:
        return f"{total // 3600}小时{total % 3600 // 60}分"
    return f"{total // 86400}天{total % 86400 // 3600}小时"


def truncate_text(text: str, max_chars: int) -> str:
    """超过 max_chars 个字符时截断并加省略号。"""
    text = text or ''
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + '…'


def task_duration_seconds(
        task: MonitorTask, running: bool, now: datetime) -> float:
    """任务时长：运行中取到 now；已结束取到结束时间，缺失时退回最后采样时间。

    now 由调用方传入（本模块不取系统时间），同一次刷新的多个下拉项共用一个时刻。
    """
    if running:
        end = now
    else:
        end = task.end_time or task.last_sample_at or task.start_time
    return max(0.0, (end - task.start_time).total_seconds())


# ---------- 监控目标类型（单个进程 / 进程树 / 同名进程） ----------

TARGET_KIND_TEXTS = {TARGET_PID: "单个进程", TARGET_TREE: "进程树", TARGET_NAME: "同名进程"}


def target_identity_text(kind: Optional[str], pid: int, name: str) -> str:
    """任务卡片身份行的目标标签：单个进程 "PID 1234"，进程树 "进程树 · 根 PID 1234"，
    同名进程 "同名进程"（进程名已在身份行最前，不再重复）。"""
    if kind == TARGET_TREE:
        return f"进程树 · 根 PID {pid}"
    if kind == TARGET_NAME:
        return TARGET_KIND_TEXTS[TARGET_NAME]
    return f"PID {pid}"


def task_pid_part(task) -> str:
    """任务下拉项里"PID 1234"的位置：进程树写根 PID，同名进程没有 PID（写"同名进程"）。"""
    kind = getattr(task, 'target_kind', TARGET_PID)
    if kind == TARGET_TREE:
        return f"进程树 · 根 PID {task.pid}"
    if kind == TARGET_NAME:
        return TARGET_KIND_TEXTS[TARGET_NAME]
    return f"PID {task.pid}"


def task_title_text(task) -> str:
    """"chrome.exe（PID 1234）"一类的完整任务称呼（tooltip、确认框用）。"""
    kind = getattr(task, 'target_kind', TARGET_PID)
    if kind == TARGET_NAME:
        return f"{task.process_name}（{TARGET_KIND_TEXTS[TARGET_NAME]}）"
    return f"{task.process_name}（{task_pid_part(task)}）"


def format_task_option_text(
        task: MonitorTask, running: bool, now: datetime,
        note_max: int = NOTE_MAX_CHARS) -> str:
    """任务下拉项文本：进程名 · PID · 开始时间 · 时长 · 备注（运行中标"运行中"）。

    开始时间固定为"月-日 时:分"以压缩下拉宽度，带年份的完整日期由 format_task_tooltip 给出。
    """
    duration = format_duration(task_duration_seconds(task, running, now))
    parts = [
        task.process_name,
        task_pid_part(task),
        task.start_time.strftime('%m-%d %H:%M'),
        f"运行中 {duration}" if running else duration,
    ]
    note = (task.note or '').strip()
    if note:
        parts.append(truncate_text(note, note_max))
    return " · ".join(parts)


def format_task_tooltip(
        task: MonitorTask, running: bool, now: datetime) -> str:
    """任务下拉项 tooltip：完整进程名、起止时间、时长、完整备注与自动化方案简述。"""
    duration = format_duration(task_duration_seconds(task, running, now))
    lines = [
        task_title_text(task),
        f"开始：{task.start_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"状态：{task_status_text(running)}",
        f"时长：{duration}",
    ]
    note = (task.note or '').strip()
    if note:
        lines.append(f"备注：{note}")
    summary = plan_summary(plan_from_json_or_none(getattr(task, "plan_json", None)))
    if summary:
        lines.append(f"自动化：{summary}")
    return "\n".join(lines)


# ---------- 趋势判定与增速文案（判定摘要表、打点区间表与报告共用） ----------

_VERDICT_TEXTS = {
    Verdict.RISING: "持续上涨",
    Verdict.STABLE: "平稳",
    Verdict.FLUCTUATING: "波动",
    Verdict.INSUFFICIENT: "数据不足",
}

_REASON_TEXTS = {
    'few_points': "样本点数不足",
    'short_span': "采集时长不足",
    'degenerate': "数据无法拟合直线",
    'rising': "斜率显著为正且拟合良好",
    'constant': "数值恒定",
    'slope_not_significant': "斜率的置信区间包含 0",
    'slope_below_threshold': "变化幅度相对起伏很小",
    'low_r2': "起伏大、线性拟合度低",
    'falling': "显著下降",
    'rise_below_threshold': "上涨幅度相对起伏太小",
}


def verdict_text(verdict: Verdict) -> str:
    """趋势结论的界面文案：持续上涨 / 平稳 / 波动 / 数据不足。"""
    return _VERDICT_TEXTS[Verdict(verdict)]


def judgement_reason_text(judgement: Judgement) -> str:
    """判定依据的一句话说明（tooltip 用）；能拟合时附 R²。"""
    text = _REASON_TEXTS.get(judgement.reason, judgement.reason)
    if judgement.fit is not None:
        text += f"（R² {judgement.fit.r2:.2f}）"
    return text


def _plain_number(magnitude: float) -> str:
    """非负数的紧凑文本：≥100 取整、≥10 一位小数、否则两位，去掉无意义的尾随零。"""
    decimals = 0 if magnitude >= 100 else (1 if magnitude >= 10 else 2)
    text = f"{magnitude:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _is_zero_text(number: str) -> bool:
    return float(number.replace(",", "")) == 0.0


def _rate_unit(metric_type: str, reference: float):
    """速率文本的单位与除数：KB 类按参考值自适应 KB/MB/GB/TB，其余用定义单位。"""
    if is_kb_metric(metric_type):
        unit = display_unit_for(metric_type, reference)
        return unit, KB_UNIT_DIVISORS[unit]
    return get_metric_unit(metric_type), 1.0


def _with_unit(number: str, unit: str, suffix: str) -> str:
    if unit == "%":
        return f"{number}%{suffix}"
    if unit:
        return f"{number} {unit}{suffix}"
    return f"{number}{suffix}"


def format_rate_text(metric_type: str, rate: RateText) -> str:
    """增速文本，如 "+7.9 ±0.3 MB/分"；四舍五入为 0 时数值写 "±0"；
    置信区间无法估计（只有两个点）时不写 ±。"""
    ci = rate.ci if rate.ci is not None and math.isfinite(rate.ci) else None
    unit, divisor = _rate_unit(metric_type, max(abs(rate.value), ci or 0.0))
    number = _plain_number(abs(rate.value) / divisor)
    head = "±0" if _is_zero_text(number) else ("+" if rate.value > 0 else "-") + number
    if ci is not None:
        head += f" ±{_plain_number(ci / divisor)}"
    return _with_unit(head, unit, rate.unit_suffix)


def format_per_second(metric_type: str, value: Optional[float]) -> str:
    """每秒速率文本，如 "1.2 MB/秒"；None 或非有限数返回 "--"。"""
    if value is None or not math.isfinite(value):
        return "--"
    unit, divisor = _rate_unit(metric_type, abs(value))
    number = _plain_number(abs(value) / divisor)
    if value < 0 and not _is_zero_text(number):
        number = "-" + number
    return _with_unit(number, unit, "/秒")


def insufficient_text(span_seconds: float, count: int, interval_seconds: float) -> str:
    """增速门槛未过时的文案：时长不够写已采时长，时长够但点数不够写点数。"""
    if span_seconds < min_span_for(interval_seconds):
        return f"数据不足（已采 {format_duration(span_seconds)}）"
    return f"数据不足（仅 {count:,} 点）"


def insufficient_hint(interval_seconds: float) -> str:
    """增速门槛说明（tooltip）。"""
    return (f"至少采集 {format_duration(min_span_for(interval_seconds))}"
            f"且不少于 {MIN_POINTS} 个点后才计算增速")


def object_usage_text(metric_type: Optional[str], value: Optional[float]) -> str:
    """GDI/USER 对象数的配额占用，如 "已用 37.5%（上限 10000）"；其余指标返回空串。"""
    limit = object_count_limit(metric_type)
    if limit is None or value is None:
        return ""
    return f"已用 {value / limit * 100:.1f}%（上限 {limit}）"


BADGE_BASELINE = "基线"
_CIRCLED_MAX = 20


def marker_badge(number: int) -> str:
    """用户打点的编号徽标：1–20 用 ①–⑳，更大的写 (21)。"""
    if 1 <= number <= _CIRCLED_MAX:
        return chr(0x2460 + number - 1)
    return f"({number})"


# ---------- 判定摘要表与报告共用：统计依据 ----------

STAT_EMPTY = "--"


def _baseline_in_use(snapshot: "HistorySnapshot") -> bool:
    """基线值是否作为摘要表 / 基线依据的起点：基线不在统计所在的启动段时不是（见
    HistorySnapshot.baseline_in_use）。"""
    return snapshot.baseline_in_use


def basis_text(snapshot: "HistorySnapshot") -> str:
    """统计依据行，如 "基线 16:37:33 起 · 2小时15分 · 8,100 点"；无统计时为空串。被测进程
    重启并续接过的任务末尾追加"仅统计第 N 次启动后的数据"（与报告、证据包同一句）。"""
    stats = snapshot.stats
    if not stats:
        return ""
    parts = []
    baseline = snapshot.baseline
    if (_baseline_in_use(snapshot) and snapshot.stats_since is not None
            and snapshot.stats_since == baseline.timestamp):
        parts.append(f"基线 {baseline.timestamp.strftime('%H:%M:%S')} 起")
    parts.append(format_duration(stats['span_seconds']))
    parts.append(f"{stats['count']:,} 点")
    note = segment_note(snapshot)
    if note:
        parts.append(note)
    return " · ".join(parts)


def marker_context(
        marker_times: Sequence[float], marker_labels: Sequence[str],
        x: float) -> Optional[str]:
    """悬停位置所处的打点区间说明：返回 x 之前（含）最近一个打点的标签。

    Args:
        marker_times: 打点时间戳（epoch 秒，升序）
        marker_labels: 与 marker_times 等长的标签
        x: 悬停位置的时间戳（epoch 秒）

    Returns:
        形如 "打点「标记 2」后 3分钟" 的文本；x 早于全部打点时返回 None。
    """
    index = bisect.bisect_right(list(marker_times), x) - 1
    if index < 0:
        return None
    elapsed = format_duration(x - marker_times[index])
    return f"打点「{marker_labels[index]}」后 {elapsed}"


# ---------- 分析页：判定摘要表、打点区间表、相对时间 ----------

SUMMARY_COLUMNS = ("metric", "baseline", "current", "delta", "per_round", "rate",
                   "tail", "verdict", "usage")
SUMMARY_HEADERS = {
    "metric": "指标", "baseline": "基线", "current": "当前", "delta": "较基线",
    "per_round": "每轮均增", "rate": "增速 · R²", "tail": "末段趋势",
    "verdict": "判定", "usage": "上限占比",
}


@dataclass(frozen=True)
class SummaryRow:
    """判定摘要表的一行：各列文本（键见 SUMMARY_COLUMNS）、各列 tooltip、是否置灰；
    note 非空时判定胶囊后加 ⓘ，note 即其 tooltip（样本不足的原因）。"""
    metric: str
    texts: dict
    tooltips: dict
    dimmed: bool = False
    note: str = ""

    def text(self, column: str) -> str:
        return self.texts.get(column, STAT_EMPTY)


def _arrow(delta: Optional[float], text: str) -> str:
    """增长方向只用箭头表达（不着色）。"""
    if delta is None or text in ("±0", STAT_EMPTY):
        return "→"
    return "↑" if delta > 0 else "↓"


def percent_change(value: float, reference: Optional[float]) -> Optional[float]:
    """相对参考值的百分比变化；参考值缺失或为 0 时为 None。"""
    if reference is None or reference == 0:
        return None
    return (value - reference) / abs(reference) * 100.0


def build_summary_row(metric: str, snapshot: Optional["HistorySnapshot"]) -> SummaryRow:
    """
    判定摘要表一行：基线（无基线时为开始值）/ 当前 / 较基线（Δ 与 %）/ 每轮均增 /
    增速 ±CI · R²（累计量为平均速率、峰值类为抬升次数）/ 末段趋势（最后一个打点之后）/
    判定 / 上限占比（GDI/USER）。样本不足（无统计或判定为数据不足）时整行置灰。
    """
    name = get_metric_display_name(metric)
    texts = {column: STAT_EMPTY for column in SUMMARY_COLUMNS}
    texts["metric"], texts["usage"] = name, ""
    tips = {"metric": get_metric_description(metric)}
    if snapshot is None or not snapshot.stats:
        return SummaryRow(metric, texts, tips, dimmed=True)

    stats = snapshot.stats
    use_baseline = _baseline_in_use(snapshot)
    start = snapshot.baseline.value if use_baseline else stats['first']
    last = stats['last']
    unit = display_unit_for(metric, max(abs(start), abs(last))) if is_kb_metric(metric) else None
    texts["current"] = format_metric_value(metric, last, display_unit=unit)
    if snapshot.baseline_foreign:
        # 基线属于另一个启动段（另一个进程）：它既不能当起点，也不能拿来算增量
        texts["baseline"] = texts["delta"] = BASELINE_DASH
        tips["baseline"] = tips["delta"] = snapshot.baseline_note
    else:
        texts["baseline"] = format_metric_value(metric, start, display_unit=unit)
        tips["baseline"] = ("基线打点之后 5 个样本的中位数" if use_baseline
                            else "统计范围内最早一次采集的值（尚无基线）")
        delta = last - start
        change = format_metric_delta(metric, delta)
        pct = percent_change(last, start)
        texts["delta"] = change if change == "±0" or pct is None else f"{change} ({pct:+.1f}%)"

    mean = mean_round_delta(snapshot.rounds)
    if mean is not None:
        counted = [r for r in snapshot.rounds if r.delta is not None]
        texts["per_round"] = format_metric_delta(metric, mean)
        tips["per_round"] = "\n".join(
            [f"{len(counted)} 轮均值："] + [f"{r.label}：{format_metric_delta(metric, r.delta)}"
                                         for r in counted])

    dimmed = False
    judgement = snapshot.judgement
    if snapshot.value_class == VALUE_CLASS_RATE:
        rates = snapshot.rate_stats
        texts["rate"] = "平均 " + format_per_second(metric, rates.average_per_second if rates else None)
        tips["rate"] = "累计量不外推增速，显示平均每秒增量"
        texts["verdict"] = "累计量"
    elif snapshot.value_class == VALUE_CLASS_PEAK:
        rises = snapshot.peak_rises
        texts["rate"] = ("未抬升" if rises is not None and rises.count == 0 else
                         f"抬升 {rises.count} 次" if rises is not None else STAT_EMPTY)
        texts["verdict"] = "峰值"
    elif snapshot.rate is not None and judgement is not None and judgement.fit is not None:
        texts["rate"] = f"{format_rate_text(metric, snapshot.rate)} · R² {judgement.fit.r2:.2f}"
        tips["rate"] = "对统计范围内全部数据做线性回归的斜率，± 为 95% 置信区间"
    else:
        texts["rate"] = insufficient_text(
            stats['span_seconds'], stats['count'], snapshot.interval_seconds)
        tips["rate"] = insufficient_hint(snapshot.interval_seconds)
        dimmed = True
    if judgement is not None:
        texts["verdict"] = verdict_text(judgement.verdict)
        tips["verdict"] = judgement_reason_text(judgement)
        dimmed = dimmed or judgement.verdict == Verdict.INSUFFICIENT

    if snapshot.intervals:
        tail = snapshot.intervals[-1]
        if tail.delta is not None:
            tail_text = format_metric_delta(metric, tail.delta)
            texts["tail"] = f"{_arrow(tail.delta, tail_text)} {tail_text}"
            tips["tail"] = (f"最后一段（{format_duration(tail.duration_seconds)}）的变化量；"
                            "有打点时为最后一个打点之后")
    # 表格列窄：单元格只放占比与上限，完整说明放 tooltip
    limit = object_count_limit(metric)
    if limit is not None and last is not None:
        texts["usage"] = f"{last / limit * 100:.1f}% / {limit}"
        tips["usage"] = object_usage_text(metric, last) + "；超过上限后进程创建该类对象会失败"
    return SummaryRow(metric, texts, tips, dimmed=dimmed)


# ---------- 分析页摘要表的显示规则（报告与安全网仍用 build_summary_row 的原文） ----------

# 平稳行的"增速""每轮均增"写这个（平稳就不必再读数），原文放 tooltip
STABLE_DASH = "—"
# 基线与统计不在同一启动段时，"基线"与"较基线"写这个（悬停说明"基线在第 N 段，不可比"）
BASELINE_DASH = STABLE_DASH
STABLE_TIP_PREFIX = "判定平稳，不列出该值；原值："
# 样本不足的行在判定胶囊后加的标记
INFO_MARK = "ⓘ"
NO_DATA_NOTE = "所选范围内没有该指标的数据"
# 有明细点但统计范围里没有数据（重启后的新一段还没有样本）：不是"没有数据"
NO_STATS_NOTE = "所选范围内没有可统计的数据"
METRIC_REASON_TEXTS = {REASON_NO_DATA: NO_DATA_NOTE, REASON_NO_STATS: NO_STATS_NOTE}


def metric_reason_text(snapshot: Optional["HistorySnapshot"]) -> str:
    """没有统计的指标的原因文案（core.verdict.metric_reason 的 no_data / no_stats_in_range）；
    没有快照按没有数据。"""
    reason = metric_reason(snapshot) if snapshot is not None else REASON_NO_DATA
    return METRIC_REASON_TEXTS.get(reason, NO_DATA_NOTE)


def usage_percent_text(value: float, limit: float) -> str:
    """上限占比只写百分比，如 "62%"；不足 10% 时保留至多两位小数（"3%"、"0.45%"）。"""
    percent = value / limit * 100
    return f"{percent:.0f}%" if percent >= 10 else f"{_plain_number(percent)}%"


def build_table_row(metric: str, snapshot: Optional["HistorySnapshot"], *,
                    usage_note: Optional[str] = None) -> SummaryRow:
    """分析页判定摘要表的一行：在 build_summary_row 的基础上——平稳行的增速、每轮均增
    写 "—"；上限占比只写百分比（原文 "62.0% / 10000" 进 tooltip）；样本不足的行带
    note（判定胶囊后的 ⓘ，tooltip 写原因）。

    usage_note 非 None 表示该行的上限占比不适用（进程组合计、有多个实例的成员：GDI/USER
    对象数的 10000 上限是单个进程的口径）：有上限的指标写 "—"，usage_note 作为 tooltip。"""
    row = build_summary_row(metric, snapshot)
    texts, tips = dict(row.texts), dict(row.tooltips)
    if texts["verdict"] == verdict_text(Verdict.STABLE):
        for column in ("rate", "per_round"):
            original = texts[column]
            if original != STAT_EMPTY:
                detail = tips.get(column)
                tips[column] = STABLE_TIP_PREFIX + original + (f"\n{detail}" if detail else "")
            texts[column] = STABLE_DASH
    stats = snapshot.stats if snapshot is not None else None
    limit = object_count_limit(metric)
    if usage_note is not None and limit is not None:
        texts["usage"], tips["usage"] = STABLE_DASH, usage_note
    elif texts["usage"] and limit is not None and stats:
        tips["usage"] = f"{texts['usage']}\n{tips.get('usage', '')}".rstrip()
        texts["usage"] = usage_percent_text(stats['last'], limit)
    note = ""
    if row.dimmed:
        if not stats:
            note = metric_reason_text(snapshot)
        else:
            interval = snapshot.interval_seconds
            note = (insufficient_text(stats['span_seconds'], stats['count'], interval)
                    + "；" + insufficient_hint(interval))
    return SummaryRow(metric, texts, tips, dimmed=row.dimmed, note=note)


# ---------- 分析页结论条 ----------

@dataclass(frozen=True)
class VerdictSummary:
    """结论条内容：verdict 为左侧胶囊的判定文案（没有可判定的指标时为空串），
    sentence 为主句，tooltip 列出各判定下的全部指标。"""
    verdict: str
    sentence: str
    tooltip: str = ""


# 主句里最多点名几个指标，其余写"等 N 项"
BANNER_MAX_NAMES = 3
BANNER_RISING = "{count} / {total} 项持续上涨：{items}"
BANNER_ALSO_FLUCTUATING = "；另 {count} 项波动"
BANNER_FLUCTUATING = "{count} / {total} 项波动：{items}"
BANNER_ALL_STABLE = "{count} 项指标均平稳"
BANNER_STABLE_PARTIAL = "{stable} 项平稳，{insufficient} 项数据不足"
BANNER_SHORT_SPAN = "数据不足：已采 {span} / 需 ≥ {need}"
BANNER_FEW_POINTS = "数据不足：仅 {count} 点 / 需 ≥ {need} 点"
BANNER_NO_DATA = "数据不足：所选范围内没有数据"
BANNER_NO_STATS = "数据不足：所选范围内没有可统计的数据"
BANNER_UNJUDGED = "所选指标均为累计量或峰值类，不做趋势判定"
BANNER_MORE = "等 {count} 项"
AUTO_REFRESH_STATUS = "自动刷新 · {ago}"


def rate_brief(metric: str, rate: RateText) -> str:
    """结论条里的增速：不带置信区间，如 "+35 MB/小时"。"""
    return format_rate_text(metric, RateText(rate.value, None, rate.unit_suffix,
                                             rate.unit_seconds))


def _name_list(parts: Sequence[str]) -> str:
    if len(parts) <= BANNER_MAX_NAMES:
        return "、".join(parts)
    rest = len(parts) - BANNER_MAX_NAMES
    return "、".join(parts[:BANNER_MAX_NAMES]) + BANNER_MORE.format(count=rest)


def verdict_summary(items: Sequence) -> VerdictSummary:
    """结论条文案。items 为按严重程度排好序的 (指标, 快照或 None)。

    规则（自上而下取第一条成立的）：有持续上涨 → "2 / 5 项持续上涨：私有字节 +35 MB/小时、
    句柄数 +120 个/小时"（另有波动时追加"；另 N 项波动"）；有波动 → "N / 总数 项波动：…"；
    全部平稳 → "5 项指标均平稳"；平稳与数据不足并存 → "3 项平稳，2 项数据不足"；
    全部数据不足 → "数据不足：已采 8分钟 / 需 ≥ 10分钟"（时长够而点数不够时写点数）。
    累计量与峰值类不做趋势判定，只计入总数。"""
    groups = {verdict: [] for verdict in Verdict}
    total = len(items)
    for metric, snapshot in items:
        if snapshot is None or not snapshot.stats:
            groups[Verdict.INSUFFICIENT].append((metric, snapshot))
        elif snapshot.judgement is not None:
            groups[Verdict(snapshot.judgement.verdict)].append((metric, snapshot))
    rising, fluctuating = groups[Verdict.RISING], groups[Verdict.FLUCTUATING]
    stable, insufficient = groups[Verdict.STABLE], groups[Verdict.INSUFFICIENT]
    tooltip = "\n".join(
        f"{verdict_text(verdict)}：{'、'.join(get_metric_display_name(m) for m, _ in members)}"
        for verdict, members in groups.items() if members)

    def described(members) -> str:
        parts = []
        for metric, snapshot in members:
            name = get_metric_display_name(metric)
            parts.append(f"{name} {rate_brief(metric, snapshot.rate)}"
                         if snapshot.rate is not None else name)
        return _name_list(parts)

    if rising:
        sentence = BANNER_RISING.format(count=len(rising), total=total, items=described(rising))
        if fluctuating:
            sentence += BANNER_ALSO_FLUCTUATING.format(count=len(fluctuating))
        return VerdictSummary(verdict_text(Verdict.RISING), sentence, tooltip)
    if fluctuating:
        return VerdictSummary(verdict_text(Verdict.FLUCTUATING), BANNER_FLUCTUATING.format(
            count=len(fluctuating), total=total,
            items=_name_list([get_metric_display_name(m) for m, _ in fluctuating])), tooltip)
    if stable and not insufficient:
        return VerdictSummary(verdict_text(Verdict.STABLE),
                              BANNER_ALL_STABLE.format(count=len(stable)), tooltip)
    if stable:
        return VerdictSummary(verdict_text(Verdict.STABLE), BANNER_STABLE_PARTIAL.format(
            stable=len(stable), insufficient=len(insufficient)), tooltip)
    if insufficient:
        return VerdictSummary(verdict_text(Verdict.INSUFFICIENT),
                              _insufficient_sentence(insufficient), tooltip)
    return VerdictSummary("", BANNER_UNJUDGED if total else "", tooltip)


def _insufficient_sentence(members) -> str:
    """全部数据不足时的主句：取统计跨度最长的一个指标说明还差什么。"""
    with_stats = [s for _m, s in members if s is not None and s.stats]
    if not with_stats:
        # 有明细点（快照在）但统计范围里没有数据，与整个范围都没有数据区分
        return BANNER_NO_STATS if any(s is not None for _m, s in members) else BANNER_NO_DATA
    snapshot = max(with_stats, key=lambda s: s.stats['span_seconds'])
    span, need = snapshot.stats['span_seconds'], min_span_for(snapshot.interval_seconds)
    if span < need:
        return BANNER_SHORT_SPAN.format(span=format_duration(span), need=format_duration(need))
    return BANNER_FEW_POINTS.format(count=f"{snapshot.stats['count']:,}", need=MIN_POINTS)


# ---------- 整体判定：重启续接的分段与中途崩溃 ----------

BANNER_CRASHES = "被测进程中途异常退出 {count} 次（最近一次 {detail}）"
BANNER_EARLY_EXIT = "被测进程在停止条件达成之前退出，测试未完成"
BANNER_LAST_SEGMENT = "最后一段：{sentence}"
BANNER_CRASH_LINE = "{time} 中途异常退出：{detail}"
BANNER_LAST_SEGMENT_TOOLTIP = "最后一段各指标："
SEGMENT_TOTAL = "共 {count} 段，"
SEGMENT_ALL_STABLE = "各段均平稳"
SEGMENT_MAX_NUMBERS = 3
# core.verdict.segments_summary 的两段文字的开头（悬停里逐段列出时去掉它们）
CRASH_SUMMARY_PREFIX = "中途异常退出"
SEGMENT_SUMMARY_PREFIX = "各段判定："
SEGMENT_VERDICT_SHORT = "数据不足"
PILL_CRASHED = "异常退出"
PILL_EARLY_EXIT = "提前退出"

_CRASH_CODE_RE = re.compile(r"0[xX]([0-9A-Fa-f]{1,8})")
# 结论胶囊的严重程度（整体判定的次序：持续上涨 > 数据不足 > 平稳；波动与平稳同属一类，
# 但比平稳醒目）。空串（没有可判定的指标）最低
_PILL_RANK = {verdict_text(Verdict.RISING): 4, verdict_text(Verdict.INSUFFICIENT): 3,
              verdict_text(Verdict.FLUCTUATING): 2, verdict_text(Verdict.STABLE): 1, "": 0}
_SEGMENT_PILLS = {VERDICT_RISING: verdict_text(Verdict.RISING),
                  VERDICT_INSUFFICIENT: verdict_text(Verdict.INSUFFICIENT),
                  VERDICT_STABLE: verdict_text(Verdict.STABLE)}


def crash_label_text(marker) -> str:
    """"异常退出 0xC0000005（访问冲突）"打点 → "访问冲突 0xC0000005"（与卡片、已结束行同一写法）；
    认不出异常码时退回打点标签里的说明。"""
    label = str(getattr(marker, 'label', '') or '')
    found = _CRASH_CODE_RE.search(label)
    if found:
        code = int(found.group(1), 16)
        text = exception_text(code)
        if text:
            return f"{text} 0x{code:08X}"
    return crash_detail(marker)


def segment_digest(segments: Sequence) -> str:
    """多段摘要，如 "共 3 段，第 2 段持续上涨"；有数据不足的段同写，都没有时"各段均平稳"。
    同类的段超过 SEGMENT_MAX_NUMBERS 个时只写个数。"""
    def phrase(verdict: str, what: str) -> Optional[str]:
        numbers = [s.number for s in segments if s.verdict == verdict]
        if not numbers:
            return None
        if len(numbers) > SEGMENT_MAX_NUMBERS:
            return f"{len(numbers)} 段{what}"
        return f"第 {'、'.join(str(n) for n in numbers)} 段{what}"

    parts = [p for p in (phrase(VERDICT_RISING, verdict_text(Verdict.RISING)),
                         phrase(VERDICT_INSUFFICIENT, SEGMENT_VERDICT_SHORT))
             if p is not None]
    return SEGMENT_TOTAL.format(count=len(segments)) + ("，".join(parts) or SEGMENT_ALL_STABLE)



def task_overall_verdict(task, analysis) -> str:
    """任务的整体判定，与 pmctl verdict、证据包同一口径（core.verdict.overall_verdict）：
    逐启动段取最差、中途崩溃过即 target_exited；任务记录里保存的停止原因是"被测进程退出"类、
    且当时停止条件还没达成时同样是 target_exited。"""
    stop_reason = getattr(task, 'stop_reason', None)
    before = False
    if task is not None and is_target_exit(stop_reason):
        from core.evidence import target_exited_before_condition
        try:
            before = target_exited_before_condition(task, stop_reason, analysis.markers)
        except AttributeError:
            before = False
    return overall_verdict(analysis, stop_reason=stop_reason, stopped_before_condition=before)


def overall_summary(base: VerdictSummary, analysis, overall: str) -> VerdictSummary:
    """在结论条（base，只反映当前显示的那一段）上叠加整体判定的内容，与 core.verdict 一致：

    - 中途异常退出过：主句以"被测进程中途异常退出 N 次（最近一次 访问冲突 0xC0000005）"开头，
      胶囊取最严重的"异常退出"；
    - 多个启动段：附"共 3 段，第 2 段持续上涨"，胶囊取各段与当前段里最严重的一个；
      悬停逐段列出结论（core.verdict.segments_summary）；
    - 被测进程在停止条件达成之前退出（没有崩溃）：主句写"测试未完成"，胶囊不再写"平稳"，
      改为"提前退出"（持续上涨仍保持原样，泄漏线索不能被盖掉）。
    都不是时原样返回 base。"""
    segments, crashes = segments_of(analysis), crashes_of(analysis)
    multi = len(segments) > 1
    early = overall == VERDICT_TARGET_EXITED and not crashes
    if not (crashes or multi or early):
        return base

    parts: List[str] = []
    if crashes:
        parts.append(BANNER_CRASHES.format(count=len(crashes),
                                           detail=crash_label_text(crashes[-1])))
    elif early:
        parts.append(BANNER_EARLY_EXIT)
    if multi:
        parts.append(segment_digest(segments))
    if base.sentence:
        parts.append(BANNER_LAST_SEGMENT.format(sentence=base.sentence) if multi
                     else base.sentence)

    verdict = base.verdict
    if segments:
        worst = _SEGMENT_PILLS[worst_segment_verdict(segments)]
        if verdict and _PILL_RANK[worst] > _PILL_RANK.get(verdict, 0):
            verdict = worst
    if crashes:
        verdict = PILL_CRASHED
    elif early and verdict != verdict_text(Verdict.RISING):
        verdict = PILL_EARLY_EXIT

    lines: List[str] = []
    if early:
        lines.append(BANNER_EARLY_EXIT)
    lines += [BANNER_CRASH_LINE.format(time=m.timestamp.strftime("%m-%d %H:%M:%S"),
                                       detail=crash_label_text(m)) for m in crashes]
    summary = segments_summary(analysis)
    if multi and summary:
        lines += [part.removeprefix(SEGMENT_SUMMARY_PREFIX) for part in summary.split("；")
                  if not part.startswith(CRASH_SUMMARY_PREFIX)]
    if base.tooltip:
        lines.append(BANNER_LAST_SEGMENT_TOOLTIP if multi else "")
        lines += base.tooltip.split("\n")
    return VerdictSummary(verdict, "；".join(parts), "\n".join(line for line in lines if line))


# ---------- 进程组任务：成员摘要、成员结论与组结论条 ----------

GROUP_USAGE_NOTE = "进程组合计不适用单进程上限"
MEMBER_USAGE_NOTE = "该成员有 {count} 个实例，合计不适用单进程上限"
MEMBERS_TRUNCATED_TEXT = f"成员种类超过 {MAX_GROUP_MEMBERS}，超出部分只计入合计"
MEMBERS_EMPTY_TEXT = "尚无成员"
MEMBERS_EMPTY_TOOLTIP = "第一次采集后显示各成员；同名进程任务在没有匹配进程时也暂无成员"
MEMBER_TOTAL_OPTION = "合计（默认）"
MEMBER_TOTAL_TOOLTIP = "全部成员的合计（报告与导出用的就是合计）"
MEMBER_OPTION_TOOLTIP = "{name}：出现过 {count} 个实例（含已退出的）；切换后整页只看该成员"
MEMBER_OPTION_TOOLTIP_PLAIN = "{name}：切换后整页只看该成员"
MEMBER_ROWS_TITLE = "成员结论"
MEMBER_ROWS_HINT = "点击一行，整页切到该成员"
GROUP_BANNER_MEMBERS_ALSO = "；成员 {names} 也在持续上涨"
GROUP_BANNER_MEMBERS_ONLY = "合计未见持续上涨，但 {count} 个成员持续上涨：{items}"
MEMBER_BANNER_PREFIX = "成员 {name}："


NAME_GROUP_TOOLTIP = "监控全部同名进程；进程退出、新启动的同名进程都会自动计入"


def members_summary_text(members: Sequence, kind: Optional[str] = None) -> str:
    """任务卡片的成员摘要，如 "成员 3 种 · 共 7 个进程"；还没有成员时为"尚无成员"。
    members 为带 instance_count 的成员快照（MonitorManager.group_members）；"共 N 个进程"
    是各成员当前实例数之和。同名进程任务（kind 为 name）永远只有一种成员，不写"成员 1 种"，
    只写"共 N 个进程"。"""
    total = sum(int(m.instance_count) for m in members)
    if kind == TARGET_NAME:
        return f"共 {total} 个进程"
    if not members:
        return MEMBERS_EMPTY_TEXT
    return f"成员 {len(members)} 种 · 共 {total} 个进程"


def members_tooltip(members: Sequence, kind: Optional[str] = None) -> str:
    """成员摘要的悬停说明：每行一个成员"显示名 × 当前实例数"，实例数多的在前、同数按名称；
    当前没有实例的成员注明。同名进程任务只有一种成员，改写说明口径。"""
    if kind == TARGET_NAME:
        return NAME_GROUP_TOOLTIP
    if not members:
        return MEMBERS_EMPTY_TOOLTIP
    ordered = sorted(members, key=lambda m: (-int(m.instance_count), m.name.casefold()))
    lines = []
    for m in ordered:
        suffix = "（当前没有实例）" if int(m.instance_count) == 0 else ""
        lines.append(f"{m.name} × {int(m.instance_count)}{suffix}")
    return "\n".join(lines)


def member_option_text(name: str, instances: Optional[int]) -> str:
    """成员下拉项文本："chrome.exe（37）"，括号里是出现过的实例数；未知时只写名称。"""
    return f"{name}（{instances}）" if instances is not None else name


def member_option_tooltip(name: str, instances: Optional[int]) -> str:
    """成员下拉项的悬停说明；实例数未知时不写实例数。"""
    if instances is None:
        return MEMBER_OPTION_TOOLTIP_PLAIN.format(name=name)
    return MEMBER_OPTION_TOOLTIP.format(name=name, count=instances)


@dataclass(frozen=True)
class MemberVerdictRow:
    """成员结论的一行：判定码（rising / stable / insufficient）与中文、持续上涨的指标说明
    （没有时为 "—"）。"""
    member_key: str
    name: str
    instances: Optional[int]
    verdict: str
    verdict_text: str
    rising_text: str
    rising_metrics: tuple = ()


_MEMBER_VERDICT_TEXTS = {
    VERDICT_RISING: verdict_text(Verdict.RISING),
    VERDICT_STABLE: verdict_text(Verdict.STABLE),
    VERDICT_INSUFFICIENT: verdict_text(Verdict.INSUFFICIENT),
}


def _rising_parts(analysis, member: str) -> List[str]:
    """某成员持续上涨的指标，如 ["私有字节 +35 MB/小时", ...]（按成员快照里的指标顺序）。"""
    parts = []
    for series_key, snapshot in member_snapshots_of(analysis).items():
        metric, key = split_member_key(series_key)
        if key != member or metric_verdict(snapshot) != VERDICT_RISING:
            continue
        name = get_metric_display_name(metric)
        parts.append(f"{name} {rate_brief(metric, snapshot.rate)}"
                     if snapshot.rate is not None else name)
    return parts


def build_member_rows(analysis, names: Optional[dict] = None,
                      instances: Optional[dict] = None) -> List[MemberVerdictRow]:
    """每个成员一行的结论（成员判定见 core.verdict.member_overall）：持续上涨的在前，其余
    按显示名排序。names / instances 为 {成员键: 显示名 / 出现过的实例数}，缺省用成员键。"""
    names, instances = names or {}, instances or {}
    rows = []
    for member, verdict in member_overall(analysis).items():
        parts = _rising_parts(analysis, member)
        rows.append(MemberVerdictRow(
            member_key=member, name=names.get(member, member),
            instances=instances.get(member), verdict=verdict,
            verdict_text=_MEMBER_VERDICT_TEXTS.get(verdict, verdict),
            rising_text=_name_list(parts) if parts else STABLE_DASH,
            rising_metrics=tuple(parts)))
    rows.sort(key=lambda r: (r.verdict != VERDICT_RISING, r.name.casefold()))
    return rows


def rising_member_rows(rows: Sequence[MemberVerdictRow]) -> List[MemberVerdictRow]:
    return [r for r in rows if r.verdict == VERDICT_RISING]


def member_rows_header(rows: Sequence[MemberVerdictRow]) -> str:
    """成员结论折叠区的标题行，如 "成员结论 · 5 个成员，2 个持续上涨"。"""
    rising = len(rising_member_rows(rows))
    tail = f"，{rising} 个持续上涨" if rising else "，没有成员持续上涨"
    return f"{MEMBER_ROWS_TITLE} · {len(rows)} 个成员{tail}"


def group_verdict_summary(total: VerdictSummary, rows: Sequence[MemberVerdictRow]
                          ) -> VerdictSummary:
    """进程组合计视图的结论条：合计或任一成员持续上涨即算上涨，文案点名上涨的成员。
    合计已在涨时在主句后追加"；成员 a、b 也在持续上涨"；合计没涨而成员在涨时改写成
    "合计未见持续上涨，但 N 个成员持续上涨：renderer.exe（私有字节 +35 MB/小时）"。"""
    rising = rising_member_rows(rows)
    if not rising:
        return total
    tooltip = "\n".join(
        t for t in (total.tooltip,
                    "持续上涨的成员：" + "；".join(
                        f"{r.name}（{'、'.join(r.rising_metrics)}）" for r in rising)) if t)
    rising_text = verdict_text(Verdict.RISING)
    if total.verdict == rising_text:
        names = _name_list([r.name for r in rising])
        return VerdictSummary(
            rising_text, total.sentence + GROUP_BANNER_MEMBERS_ALSO.format(names=names), tooltip)
    items = _name_list([f"{r.name}（{'、'.join(r.rising_metrics[:2])}）" for r in rising])
    sentence = GROUP_BANNER_MEMBERS_ONLY.format(count=len(rising), items=items)
    return VerdictSummary(rising_text, sentence, tooltip)


def member_view_summary(name: str, summary: VerdictSummary) -> VerdictSummary:
    """成员视图的结论条：在该成员自己的结论主句前点名成员。"""
    if not summary.sentence:
        return summary
    return VerdictSummary(
        summary.verdict, MEMBER_BANNER_PREFIX.format(name=name) + summary.sentence,
        summary.tooltip)


def refresh_status_text(seconds_ago: float) -> str:
    """运行中任务的自动刷新状态，如 "自动刷新 · 15 秒前"。"""
    seconds = max(0, int(seconds_ago))
    if seconds < 1:
        ago = "刚刚"
    elif seconds < 60:
        ago = f"{seconds} 秒前"
    else:
        ago = f"{seconds // 60} 分钟前"
    return AUTO_REFRESH_STATUS.format(ago=ago)


INTERVAL_HEADERS =("区间", "时长", "起值", "终值", "Δ", "速率", "峰值", "与上轮差")


def interval_row_texts(metric: str, item, span: Sequence[str],
                       display_unit: Optional[str] = None) -> List[str]:
    """打点区间表一行文本（列顺序见 INTERVAL_HEADERS）；span 为 (左标签, 右标签)。"""
    def value(v: Optional[float]) -> str:
        return format_metric_value(metric, v, display_unit=display_unit) if v is not None else STAT_EMPTY

    if item.rate_per_hour is None:
        rate = STAT_EMPTY
    elif value_class(metric) == VALUE_CLASS_RATE:
        rate = format_per_second(metric, item.rate_per_hour / 3600.0)
    else:
        rate = format_metric_rate(metric, item.rate_per_hour)
    return [
        f"{span[0]} → {span[1]}",
        format_duration(item.duration_seconds),
        value(item.start_value), value(item.end_value),
        format_metric_delta(metric, item.delta),
        rate,
        value(item.peak),
        format_metric_delta(metric, item.delta_vs_previous),
    ]


def format_relative(seconds: float) -> str:
    """相对时间：T+mm:ss；满 1 小时为 T+h:mm:ss；负数为 T-…。"""
    sign = "-" if seconds < 0 else "+"
    total = int(round(abs(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"T{sign}{hours}:{minutes:02d}:{secs:02d}"
    return f"T{sign}{minutes:02d}:{secs:02d}"


# ========== 数据库升级与只读模式 ==========

READ_ONLY_TITLE = "只读模式"
# 只读横幅：{version} 为写入本库的更高版本号
READ_ONLY_BANNER_TEMPLATE = (
    "数据库由更高版本（{version}）创建，本版本以只读方式打开：可以查看与导出，不能开始监控")
READ_ONLY_UNKNOWN_VERSION = "更新的版本"
# 开始监控按钮的 tooltip 与被拒时的就近提示
READ_ONLY_START_BLOCKED = "数据库为只读（由更高版本创建），不能开始监控；请升级到最新版本"
READ_ONLY_MARKER_BLOCKED = "数据库为只读，不能打点"

# 升级失败、库仍是旧结构时的只读横幅：{reason} 为失败原因，{advice} 为处理建议
SCHEMA_STALE_BANNER_TEMPLATE = (
    "数据库尚未升级（上次升级失败：{reason}），本次以只读方式打开：不能开始监控与打点，"
    "历史数据要等升级完成后才能查看与分析。{advice}")
SCHEMA_STALE_UNKNOWN_REASON = "原因见日志"
SCHEMA_STALE_ADVICE_DISK = "请释放磁盘空间后重启。"
SCHEMA_STALE_ADVICE = "请关闭可能占用数据库的程序后重启重试，详情见日志。"
SCHEMA_STALE_START_BLOCKED = "数据库尚未升级，不能开始监控；请按窗口顶部的提示处理后重启"

MIGRATED_TIP_TITLE = "数据库已升级"
# 升级成功提示：{version} 为新结构版本，{name} 为旧文件备份的文件名
MIGRATED_TIP_TEMPLATE = "数据库已升级到 v{version}，旧文件已保留为 {name}（可用于回退到 1.4.1）"
MIGRATED_TIP_DURATION_MS = 6000

BACKUP_ABORTED_TEXT = "未能升级数据库，历史数据完好保存在磁盘，请释放磁盘空间后重启。"
# 磁盘预检不足：{detail} 为"需要约 N MB 可用空间（当前 M MB），数据库未改动"
BACKUP_ABORTED_DISK_TEMPLATE = "磁盘空间不足，未能升级数据库：{detail}。请释放磁盘空间后重启。"


def read_only_banner_text(version: Optional[str]) -> str:
    """只读横幅文案；不知道写入版本时用"更新的版本"代替"""
    return READ_ONLY_BANNER_TEMPLATE.format(version=version or READ_ONLY_UNKNOWN_VERSION)


MIGRATION_FAILED_TEXT = "数据库升级失败，本次运行新数据无法保存；旧数据已保留，请重启重试。"
# 带失败原因：{reason} 为 MigrationReport.reason
MIGRATION_FAILED_REASON_TEMPLATE = (
    "数据库升级失败（{reason}），本次运行新数据无法保存；旧数据已保留，请重启重试。")


def migration_failed_text(reason: Optional[str]) -> str:
    """迁移失败提示；报告带原因时写明原因"""
    if not reason:
        return MIGRATION_FAILED_TEXT
    return MIGRATION_FAILED_REASON_TEMPLATE.format(reason=reason)


def schema_stale_banner_text(reason: Optional[str], disk_shortage: bool = False) -> str:
    """升级失败、以只读方式打开旧结构库时的横幅文案"""
    return SCHEMA_STALE_BANNER_TEMPLATE.format(
        reason=reason or SCHEMA_STALE_UNKNOWN_REASON,
        advice=SCHEMA_STALE_ADVICE_DISK if disk_shortage else SCHEMA_STALE_ADVICE)


def migrated_tip_text(version: int, backup_path: str) -> str:
    """升级成功提示：只显示备份的文件名（与库同目录）"""
    return MIGRATED_TIP_TEMPLATE.format(version=version, name=os.path.basename(backup_path))


def backup_aborted_text(required_bytes: Optional[int], free_bytes: Optional[int]) -> str:
    """迁移中止提示：磁盘预检不足时写明所需与当前可用空间"""
    if required_bytes is None or free_bytes is None:
        return BACKUP_ABORTED_TEXT
    return BACKUP_ABORTED_DISK_TEMPLATE.format(
        detail=disk_shortage_message(required_bytes, free_bytes))


# ---------- 自动化方案（自动打点与停止条件） ----------

AUTO_STOP_TITLE = "已按方案自动停止"
_AUTO_STOP_TEMPLATES = {
    AUTO_STOP_DURATION: "已运行满 {span}",
    AUTO_STOP_ROUNDS: "已完成 {rounds} 轮",
}
AUTO_EXPORT_DONE_TITLE = "已自动导出"
AUTO_EXPORT_FAILED_TITLE = "自动导出失败"
AUTO_EXPORT_BUSY = "正在清理数据库，本次未能自动导出；请稍后在分析页手动导出"
AUTO_EXPORT_NO_TASK = "找不到该任务的数据，未能自动导出"
AUTO_EXPORT_NO_DATABASE = "未连接数据库，未能自动导出"
# 自动导出的 CSV 元数据"范围"一行
AUTO_EXPORT_RANGE_TEXT = "全部（任务结束后自动导出）"
PLAN_INVALID_TITLE = "自动化设置有误"
STAGE_SNAPSHOT_SUMMARY_TEMPLATE = "每 {hours} 小时写阶段快照"
RESTART_SUMMARY_TEMPLATE = "退出后续接（最多 {limit} 次）"
STAGE_SNAPSHOT_DONE_TITLE = "已写阶段快照"
STAGE_SNAPSHOT_FAILED_TITLE = "阶段快照写入失败"
# 失败只提示一次：说明采集不受影响、之后不再提示
STAGE_SNAPSHOT_FAILED_TEMPLATE = "{name}：{message}。采集照常进行，之后的快照失败不再提示（见日志）"


def plan_span_text(seconds: int) -> str:
    """方案里的时长：整小时写"N 小时"，整分钟写"N 分钟"，否则"N 秒"。"""
    seconds = int(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def plan_summary(plan) -> str:
    """自动化方案的一行简述（卡片悬停提示、任务下拉提示），如
    "自动打点 每 10 分钟 · 日志触发（app.log） · 2 小时后停止 · 停止后自动导出"。
    没有方案或方案为空时返回空串。"""
    if plan is None:
        return ""
    parts: List[str] = []
    if plan.timed_marker_seconds is not None:
        parts.append(f"自动打点 每 {plan_span_text(plan.timed_marker_seconds)}")
    if plan.log_path and plan.log_pattern:
        parts.append(f"日志触发（{os.path.basename(plan.log_path) or plan.log_path}）")
    if plan.stop_after_seconds is not None:
        parts.append(f"{plan_span_text(plan.stop_after_seconds)}后停止")
    if plan.stop_after_rounds is not None:
        parts.append(f"满 {plan.stop_after_rounds} 轮停止")
    if plan.export_on_stop:
        parts.append("停止后自动导出")
    hours = getattr(plan, 'stage_snapshot_hours', None)
    if hours is not None and getattr(plan, 'stage_snapshot_dir', None):
        parts.append(stage_snapshot_summary(hours))
    if getattr(plan, 'resume_on_restart', False):
        parts.append(RESTART_SUMMARY_TEMPLATE.format(limit=plan.restart_limit))
    return " · ".join(parts)


def stage_snapshot_summary(hours: float) -> str:
    """阶段快照的简述，如"每 6 小时写阶段快照"（小数小时保留原样，如 0.5）。"""
    return STAGE_SNAPSHOT_SUMMARY_TEMPLATE.format(hours=f"{float(hours):g}")


def stage_snapshot_failed_text(name: str, message: str) -> str:
    """阶段快照失败提示的正文（每个任务只提示一次）。"""
    return STAGE_SNAPSHOT_FAILED_TEMPLATE.format(name=name, message=message.rstrip('。'))


def auto_stop_text(reason: str, plan=None) -> str:
    """自动停止原因（core.automation.stop_conditions.AUTO_STOP_*）的说明。"""
    template = _AUTO_STOP_TEMPLATES.get(reason)
    if template is None:
        raise ValueError(f"未知的自动停止原因：{reason!r}")
    seconds = getattr(plan, 'stop_after_seconds', None)
    rounds = getattr(plan, 'stop_after_rounds', None)
    if reason == AUTO_STOP_DURATION and seconds is None:
        return "已达到设定的运行时长"
    if reason == AUTO_STOP_ROUNDS and rounds is None:
        return "已达到设定的轮数"
    return template.format(span=plan_span_text(seconds or 0), rounds=rounds)
