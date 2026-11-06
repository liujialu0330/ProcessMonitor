"""
界面文案纯函数
不依赖任何 Qt 控件，集中放置历史页/导出页/监控页共用的文本拼装与简单计算，
便于脱离 GUI 直接单测。
"""
import bisect
import re
from datetime import datetime
from typing import Optional, Sequence

from core.contracts import StopReason, UnavailableReason
from data.models import MonitorTask
from utils.metrics import (
    format_metric_delta, format_metric_rate, format_metric_value
)

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


def build_stat_texts(
        metric_type: str, stats: Optional[dict], display_unit: str = None) -> dict:
    """历史页统计条文本：起点、当前、变化量（绝对值 + 百分比）、增速、最小、最大。

    stats 即 Database.get_metric_stats 的返回值（覆盖所选时间范围内全部数据）；
    为 None 时全部返回 "--"。起点/当前/最小/最大使用图表固定的显示单位，变化量
    与增速按其自身量级自适应单位（小幅增长不会被大单位吞成 0）。
    """
    keys = ("起点", "当前", "变化", "增速", "最小", "最大")
    if not stats or not metric_type:
        return {key: "--" for key in keys}

    first = stats['first']
    last = stats['last']
    delta = last - first
    change = format_metric_delta(metric_type, delta)
    if change != "±0" and first:
        change = f"{change} ({delta / abs(first) * 100:+.1f}%)"

    return {
        "起点": format_metric_value(metric_type, first, display_unit=display_unit),
        "当前": format_metric_value(metric_type, last, display_unit=display_unit),
        "变化": change,
        "增速": format_metric_rate(metric_type, stats.get('slope_per_hour')),
        "最小": format_metric_value(metric_type, stats['min'], display_unit=display_unit),
        "最大": format_metric_value(metric_type, stats['max'], display_unit=display_unit),
    }


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
