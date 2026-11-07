"""
界面文案纯函数
不依赖任何 Qt 控件，集中放置分析页/监控页/报告共用的文本拼装与简单计算，
便于脱离 GUI 直接单测。
"""
import bisect
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Sequence

from core.analysis.intervals import mean_round_delta
from core.analysis.trend import (
    MIN_POINTS, Judgement, RateText, Verdict, min_span_for)
from core.contracts import StopReason, UnavailableReason
from data.db.migrations.runner import disk_shortage_message
from data.models import MonitorTask
from utils.metrics import (
    KB_UNIT_DIVISORS, VALUE_CLASS_PEAK, VALUE_CLASS_RATE, display_unit_for,
    format_metric_delta, format_metric_rate, format_metric_value,
    get_metric_description, get_metric_display_name, get_metric_unit, is_kb_metric,
    object_count_limit, value_class,
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


def format_task_option_text(
        task: MonitorTask, running: bool, now: datetime,
        note_max: int = NOTE_MAX_CHARS) -> str:
    """任务下拉项文本：进程名 · PID · 开始时间 · 时长 · 备注（运行中标"运行中"）。

    开始时间固定为"月-日 时:分"以压缩下拉宽度，带年份的完整日期由 format_task_tooltip 给出。
    """
    duration = format_duration(task_duration_seconds(task, running, now))
    parts = [
        task.process_name,
        f"PID {task.pid}",
        task.start_time.strftime('%m-%d %H:%M'),
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


BADGE_BASELINE = "基"
_CIRCLED_MAX = 20


def marker_badge(number: int) -> str:
    """用户打点的编号徽标：1–20 用 ①–⑳，更大的写 (21)。"""
    if 1 <= number <= _CIRCLED_MAX:
        return chr(0x2460 + number - 1)
    return f"({number})"


# ---------- 判定摘要表与报告共用：统计依据 ----------

STAT_EMPTY = "--"


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
    """判定摘要表的一行：各列文本（键见 SUMMARY_COLUMNS）、各列 tooltip、是否置灰。"""
    metric: str
    texts: dict
    tooltips: dict
    dimmed: bool = False

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
    texts["baseline"] = format_metric_value(metric, start, display_unit=unit)
    tips["baseline"] = ("基线打点之后 5 个样本的中位数" if use_baseline
                        else "统计范围内最早一次采集的值（尚无基线）")
    texts["current"] = format_metric_value(metric, last, display_unit=unit)
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


INTERVAL_HEADERS = ("区间", "时长", "起值", "终值", "Δ", "速率", "峰值", "与上轮差")


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


def migrated_tip_text(version: int, backup_path: str) -> str:
    """升级成功提示：只显示备份的文件名（与库同目录）"""
    return MIGRATED_TIP_TEMPLATE.format(version=version, name=os.path.basename(backup_path))


def backup_aborted_text(required_bytes: Optional[int], free_bytes: Optional[int]) -> str:
    """迁移中止提示：磁盘预检不足时写明所需与当前可用空间"""
    if required_bytes is None or free_bytes is None:
        return BACKUP_ABORTED_TEXT
    return BACKUP_ABORTED_DISK_TEMPLATE.format(
        detail=disk_shortage_message(required_bytes, free_bytes))
