"""
界面文案纯函数
不依赖任何 Qt 控件，集中放置历史页/导出页/监控页共用的文本拼装与简单计算，
便于脱离 GUI 直接单测。
"""
import bisect
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Sequence

from core.analysis.intervals import mean_round_delta
from core.analysis.trend import (
    MIN_POINTS, Judgement, RateText, Verdict, min_span_for, rate_unit_for_span)
from core.contracts import StopReason, UnavailableReason
from data.models import MonitorTask
from utils.metrics import (
    KB_UNIT_DIVISORS, VALUE_CLASS_PEAK, VALUE_CLASS_RATE, display_unit_for,
    format_metric_delta, format_metric_value, get_metric_unit, is_kb_metric,
    object_count_limit,
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


def task_status_text(running: bool) -> str:
    """任务运行状态文案（下拉 tooltip、导出页摘要共用）。"""
    return "运行中" if running else "已停止"


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
    """任务时长：运行中取到 now；已停止取到结束时间，缺失时退回最后采样时间。

    now 由调用方传入（本模块不取系统时间），同一次刷新的多个下拉项共用一个时刻。
    """
    if running:
        end = now
    else:
        end = task.end_time or task.last_sample_at or task.start_time
    return max(0.0, (end - task.start_time).total_seconds())


def format_task_option_text(
        task: MonitorTask, running: bool, now: datetime,
        note_max: int = NOTE_MAX_CHARS, full_date: bool = False) -> str:
    """任务下拉项文本：进程名 · PID · 开始时间 · 时长 · 备注（运行中标"运行中"）。"""
    time_format = '%Y-%m-%d %H:%M' if full_date else '%m-%d %H:%M'
    duration = format_duration(task_duration_seconds(task, running, now))
    parts = [
        task.process_name,
        f"PID {task.pid}",
        task.start_time.strftime(time_format),
        f"运行中 {duration}" if running else duration,
    ]
    note = (task.note or '').strip()
    if note:
        parts.append(truncate_text(note, note_max))
    return " · ".join(parts)


def format_task_tooltip(
        task: MonitorTask, running: bool, now: datetime) -> str:
    """任务下拉项 tooltip：完整进程名、起止时间、时长与完整备注。"""
    duration = format_duration(task_duration_seconds(task, running, now))
    lines = [
        f"{task.process_name}（PID {task.pid}）",
        f"开始：{task.start_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"状态：{task_status_text(running)}",
        f"时长：{duration}",
    ]
    note = (task.note or '').strip()
    if note:
        lines.append(f"备注：{note}")
    return "\n".join(lines)


# ---------- 趋势判定与增速文案（历史页统计条、后续的判定摘要表共用） ----------

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


BADGE_BASELINE = "基"
_CIRCLED_MAX = 20


def marker_badge(number: int) -> str:
    """用户打点的编号徽标：1–20 用 ①–⑳，更大的写 (21)。"""
    if 1 <= number <= _CIRCLED_MAX:
        return chr(0x2460 + number - 1)
    return f"({number})"


# ---------- 历史页统计条（六格） ----------

# 六个格位：标题随指标类别变化，格位不变（列数与排版只看格位）
STAT_SLOTS = ("start", "current", "delta", "rate", "low", "high")
STAT_EMPTY = "--"
_DEFAULT_TITLES = {
    "start": "开始值", "current": "当前值", "delta": "变化量",
    "rate": "每小时增长", "low": "最小", "high": "最大",
}
_DEFAULT_TOOLTIPS = {
    "start": "统计范围内最早一次采集的值；存在基线时改为基线值",
    "current": "统计范围内最新一次采集的值",
    "delta": "当前值相对开始值（或基线值）的变化量（绝对值与百分比）",
    "rate": "对统计范围内全部数据做线性回归得到的增长速度",
    "low": "统计范围内的最小值",
    "high": "统计范围内的最大值",
}


@dataclass(frozen=True)
class StatCell:
    """统计条的一格：标题、主值、值下方的小字说明、tooltip、是否置灰、判定胶囊。"""
    slot: str
    title: str
    value: str = STAT_EMPTY
    note: str = ""
    tooltip: str = ""
    dimmed: bool = False
    pill: str = ""
    pill_tooltip: str = ""


def empty_stat_cells() -> List[StatCell]:
    """无数据时的六格（默认标题，值为 "--"）。"""
    return [StatCell(slot, _DEFAULT_TITLES[slot], tooltip=_DEFAULT_TOOLTIPS[slot])
            for slot in STAT_SLOTS]


def _baseline_in_use(snapshot: "HistorySnapshot") -> bool:
    baseline = snapshot.baseline
    return (baseline is not None and baseline.value is not None
            and snapshot.request.baseline_only)


def basis_text(snapshot: "HistorySnapshot") -> str:
    """统计依据行，如 "基线 16:37:33 起 · 2小时15分 · 8,100 点"；无统计时为空串。"""
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
    return " · ".join(parts)


def _based_on(stats: dict) -> str:
    return f"基于 {format_duration(stats['span_seconds'])} / {stats['count']:,} 点"


def _level_rate_cell(snapshot: "HistorySnapshot", metric: str, basis: str) -> StatCell:
    stats = snapshot.stats
    suffix = snapshot.fit_unit or rate_unit_for_span(stats['span_seconds'])[0]
    title = "每分钟增长" if suffix == "/分" else "每小时增长"
    judgement = snapshot.judgement
    pill = verdict_text(judgement.verdict) if judgement else ""
    pill_tip = judgement_reason_text(judgement) if judgement else ""
    if snapshot.rate is not None:
        rate_line = f"增速 {format_rate_text(metric, snapshot.rate)}（± 为 95% 置信区间）"
    else:
        rate_line = insufficient_hint(snapshot.interval_seconds)

    mean = mean_round_delta(snapshot.rounds)
    if mean is not None:
        counted = [r for r in snapshot.rounds if r.delta is not None]
        lines = [f"{r.label}：{format_metric_delta(metric, r.delta)}" for r in counted]
        return StatCell(
            "rate", "每轮增长", format_metric_delta(metric, mean),
            note=f"{len(counted)} 轮均值",
            tooltip="\n".join(["各轮增量（打点后 5 个样本中位数之差）：", *lines,
                               rate_line, basis]),
            pill=pill, pill_tooltip=pill_tip)
    if snapshot.rate is None:
        return StatCell(
            "rate", title,
            insufficient_text(stats['span_seconds'], stats['count'], snapshot.interval_seconds),
            tooltip=f"{rate_line}\n{basis}", dimmed=True, pill=pill, pill_tooltip=pill_tip)
    return StatCell(
        "rate", title, format_rate_text(metric, snapshot.rate), note=_based_on(stats),
        tooltip=f"对统计范围内全部数据做线性回归的斜率，± 为 95% 置信区间\n{basis}",
        pill=pill, pill_tooltip=pill_tip)


def build_stat_cells(snapshot: Optional["HistorySnapshot"],
                     display_unit: str = "") -> List[StatCell]:
    """
    按快照生成统计条六格（格位顺序见 STAT_SLOTS）。

    - 存在基线（且统计只用基线之后的数据）时首格为"基线值"，变化量相对基线值；
    - 水位量：第四格为增速（门槛未过置灰"数据不足"）或有 ≥2 个用户打点时的"每轮增长"，
      并带判定胶囊；GDI/USER 对象数在当前值下注明配额占用；
    - 累计量：第四至六格为平均速率 / 峰值速率 / 数据跨度（不外推增速）；
    - 峰值类：第四格为"峰值抬升 N 次，共 +X"。
    数值格与图表同一显示单位（display_unit）；变化量与速率按自身量级自适应单位。
    """
    if snapshot is None or not snapshot.request.metric or not snapshot.stats:
        return empty_stat_cells()
    metric = snapshot.request.metric
    stats = snapshot.stats
    use_baseline = _baseline_in_use(snapshot)
    start = snapshot.baseline.value if use_baseline else stats['first']
    last = stats['last']
    basis = basis_text(snapshot)

    def fmt(value: float) -> str:
        return format_metric_value(metric, value, display_unit=display_unit)

    cells = {}
    if use_baseline:
        baseline = snapshot.baseline
        stamp = baseline.timestamp.strftime('%H:%M:%S')
        cells["start"] = StatCell(
            "start", "基线值", fmt(start), note=f"基线 {stamp}",
            tooltip=(f"基线打点「{baseline.marker.label}」"
                     f"{baseline.timestamp.strftime('%Y-%m-%d %H:%M:%S')} 之后 "
                     f"{baseline.sample_count} 个样本的中位数；统计、回归与判定只用基线之后的数据"))
    else:
        cells["start"] = StatCell("start", "开始值", fmt(start),
                                  tooltip="统计范围内最早一次采集的值")
    cells["current"] = StatCell(
        "current", "当前值", fmt(last), note=object_usage_text(metric, last),
        tooltip="统计范围内最新一次采集的值")

    delta = last - start
    change = format_metric_delta(metric, delta)
    if change != "±0" and start:
        change = f"{change} ({delta / abs(start) * 100:+.1f}%)"
    reference = "基线值" if use_baseline else "开始值"
    cells["delta"] = StatCell("delta", "变化量", change,
                              tooltip=f"当前值相对{reference}的变化量（绝对值与百分比）")

    low = StatCell("low", "最小", fmt(stats['min']), tooltip=_DEFAULT_TOOLTIPS["low"])
    high = StatCell("high", "最大", fmt(stats['max']), tooltip=_DEFAULT_TOOLTIPS["high"])

    if snapshot.value_class == VALUE_CLASS_RATE:
        rates = snapshot.rate_stats
        average = rates.average_per_second if rates else None
        peak = rates.peak_per_second if rates else None
        cells["rate"] = StatCell(
            "rate", "平均速率", format_per_second(metric, average), note=_based_on(stats),
            tooltip=f"累计量的平均每秒增量：（当前值 - 开始值）/ 时长\n{basis}")
        low = StatCell(
            "low", "峰值速率", format_per_second(metric, peak),
            tooltip="图表速率曲线的最大值：相邻采集点之差 / 间隔，跨空洞的一段不计；"
                    "数据量大时按图表分桶点计算")
        high = StatCell("high", "数据跨度", format_duration(stats['span_seconds']),
                        note=f"{stats['count']:,} 点", tooltip=basis)
    elif snapshot.value_class == VALUE_CLASS_PEAK:
        rises = snapshot.peak_rises
        if rises is None:
            text = STAT_EMPTY
        elif rises.count == 0:
            text = "未抬升"
        else:
            text = f"{rises.count} 次，共 {format_metric_delta(metric, rises.total)}"
        cells["rate"] = StatCell(
            "rate", "峰值抬升", text,
            tooltip="统计范围内峰值上升的次数与总幅度（按图表采样点统计，同一分桶内的"
                    f"多次抬升计为一次）；峰值只增不减，不计算增速\n{basis}")
    else:
        cells["rate"] = _level_rate_cell(snapshot, metric, basis)
    cells["low"], cells["high"] = low, high
    return [cells[slot] for slot in STAT_SLOTS]


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


_INVALID_FILENAME_CHARS = re.compile(r'[\/:*?"<>|\r\n\t]+')


def sanitize_filename(text: str) -> str:
    """去掉 Windows 文件名非法字符。"""
    return _INVALID_FILENAME_CHARS.sub('_', text or '').strip(' .') or 'task'


def default_export_filename(task: MonitorTask) -> str:
    """默认导出文件名：进程名_PID_开始时间.csv（去掉 .exe 后缀与非法字符）。"""
    name = task.process_name
    if name.lower().endswith('.exe'):
        name = name[:-4]
    stamp = task.start_time.strftime('%Y%m%d_%H%M%S')
    return f"{sanitize_filename(name)}_{task.pid}_{stamp}.csv"
