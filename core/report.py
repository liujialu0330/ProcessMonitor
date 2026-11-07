"""
分析报告导出的纯函数

一键报告由三个文件组成（同一目录、同一基名）：
- <任务>_报告.png：界面层用 QPainter 离屏渲染（标题/信息/结论/多指标小倍数图/脚注），
  这里只给出文件名与脚注文案；
- <任务>.csv：宽表明细，比普通导出多"已运行(秒)"列；相邻两次采集间隔超过
  空洞阈值（DEFAULT_GAP_FACTOR × 采集间隔）处插一行空行；文件头若干行以 "#" 开头的
  元数据（进程、备注、指标、范围、导出时间等），Excel 打开时一目了然、程序读取时可按
  "#" 跳过；
- <任务>_摘要.csv：指标 × 打点区间，每行一个指标的一个区间。

报告图片用到的结构化数据也在这里组装：metadata_items（元信息网格的 (标签, 值)）、
round_delta_rows（打点区间增量表：相邻两个打点围出的每一轮、各指标的 Δ 与与上轮差）。

这里不依赖 Qt 控件与数据库，便于直接单测；读库与写文件在 core.report_worker.ReportWorker。
"""
import os
from dataclasses import dataclass
from datetime import datetime
from typing import (TYPE_CHECKING, Dict, Iterable, Iterator, List, Mapping, Optional,
                    Sequence, Tuple)

from core.analysis.intervals import IntervalStats
from core.export import MARKER_COLUMN_NAME, _format_value, pivot_timed_rows
from core.target_info import target_info_from_json_or_none
from data.models import DataPoint, Marker, MonitorTask, QualityStats
from data.repos.analytics_repo import DEFAULT_GAP_FACTOR
from utils.metrics import get_metric_display_name, get_metric_unit

if TYPE_CHECKING:
    from core.history_query import HistorySnapshot, TaskAnalysis

REPORT_IMAGE_SUFFIX = '_报告.png'
SUMMARY_CSV_SUFFIX = '_摘要.csv'
DATA_CSV_SUFFIX = '.csv'
METADATA_PREFIX = '# '
ELAPSED_COLUMN_NAME = '已运行(秒)'
GAP_NOTE = (f"空行表示相邻两次采集间隔超过 {DEFAULT_GAP_FACTOR:g} 个采集间隔"
            "（采集中断，如暂停、休眠或进程无响应）")
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'

SUMMARY_HEADER = (
    '指标', '单位', '区间', '起点', '终点', '时长(秒)', '点数',
    '起值', '终值', '变化量', '每小时速率', '峰值', '与上轮差',
)

_INVALID_CHARS = '\\/:*?"<>|\r\n\t'

# 区间表首末补齐段的标签（与 core.history_query 一致）
FIRST_LABEL = '开始'

# 元信息网格的标签（报告图片）
ITEM_START = '开始'
ITEM_END = '结束'
ITEM_DURATION = '时长'
ITEM_INTERVAL = '采集周期'
ITEM_METRICS = '指标数'
ITEM_POINTS = '点数'
ITEM_QUALITY = '数据质量'
# 被测目标元数据里进报告元信息网格的两项（命令行可能很长、含敏感参数，只进 CSV）
REPORT_TARGET_LABELS = ('被测程序', '环境')
TEXT_RUNNING = '进行中'
# 区间增量表只统计两端都是打点（基线或用户打点）的区间
_ROUND_MARKER_KINDS = ('user', 'baseline')


def safe_filename_part(text: str) -> str:
    """文件名片段：非法字符换成 "_"，去掉首尾空格与点（Windows 不允许以点或空格结尾）。"""
    return ''.join('_' if ch in _INVALID_CHARS else ch for ch in text).strip(' .')


def process_stem(task: MonitorTask) -> str:
    """文件名里的进程名：去掉 .exe 与非法字符；为空时为 "task"。"""
    name = task.process_name or 'task'
    if name.lower().endswith('.exe'):
        name = name[:-4]
    return safe_filename_part(name) or 'task'


def task_basename(task: MonitorTask) -> str:
    """报告文件的基名：进程名（去 .exe 与非法字符）_PID_开始时间。"""
    return f"{process_stem(task)}_{task.pid}_{task.start_time.strftime('%Y%m%d_%H%M%S')}"


@dataclass(frozen=True)
class ReportPaths:
    """一份报告的三个输出路径。"""
    directory: str
    basename: str

    @property
    def image(self) -> str:
        return os.path.join(self.directory, self.basename + REPORT_IMAGE_SUFFIX)

    @property
    def csv(self) -> str:
        return os.path.join(self.directory, self.basename + DATA_CSV_SUFFIX)

    @property
    def summary(self) -> str:
        return os.path.join(self.directory, self.basename + SUMMARY_CSV_SUFFIX)

    def all(self) -> Tuple[str, str, str]:
        return self.image, self.csv, self.summary


def paths_from_image_choice(image_path: str) -> ReportPaths:
    """用户在保存对话框里选的报告图片路径 → 三个文件的路径（同目录同基名）。
    文件名以 "_报告.png" 结尾时去掉它作基名，否则去掉扩展名。"""
    directory, name = os.path.split(os.path.normpath(image_path))
    if name.endswith(REPORT_IMAGE_SUFFIX):
        base = name[:-len(REPORT_IMAGE_SUFFIX)]
    else:
        base = os.path.splitext(name)[0]
    return ReportPaths(directory, base or 'task')


def gap_threshold_seconds(interval_seconds: float) -> Optional[float]:
    """空洞阈值（秒）；采集间隔未知（<= 0）时为 None（不插空行）。"""
    if not interval_seconds or interval_seconds <= 0:
        return None
    return DEFAULT_GAP_FACTOR * float(interval_seconds)


def metric_names(metrics: Sequence[str]) -> List[str]:
    return [get_metric_display_name(m) for m in metrics]


def quality_text(quality: Optional[QualityStats]) -> str:
    """
    数据质量一句话，如"采集完成率 99.8%（12,345 / 12,370 周期，调度延迟 p95 12 ms）"；
    没有质量数据（旧任务、尚未收尾、计划周期为 0）时为空串，调用方据此不追加。
    完成率四舍五入到一位小数，但有缺采时最多显示 99.9%（不会因舍入显示成满分）。
    """
    if quality is None or quality.planned_cycles <= 0:
        return ""
    percent = round((quality.completion_ratio or 0.0) * 100, 1)
    if quality.done_cycles < quality.planned_cycles:
        percent = min(percent, 99.9)
    details = f"{quality.done_cycles:,} / {quality.planned_cycles:,} 周期"
    if quality.missed_cycles:
        details += f"，跳过 {quality.missed_cycles:,}"
    if quality.lag_p95_ms is not None:
        details += f"，调度延迟 p95 {quality.lag_p95_ms} ms"
    return f"采集完成率 {percent:.1f}%（{details}）"


def target_lines(task: MonitorTask) -> List[str]:
    """被测目标元数据的摘要行（被测程序 / 命令行 / 环境，见 TargetInfo.summary_lines）；
    任务没有 meta_json（旧任务、探测前）或解析失败时为空列表。"""
    info = target_info_from_json_or_none(getattr(task, 'meta_json', None))
    return info.summary_lines() if info is not None else []


def duration_text(seconds: float) -> str:
    """易读时长：45秒 / 12分钟 / 2小时15分 / 1天3小时（与界面层 format_duration 同一写法；
    core 不依赖 ui，这里各留一份，tests/test_report.py 守护两者一致）。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        return f"{total // 60}分钟"
    if total < 86400:
        return f"{total // 3600}小时{total % 3600 // 60}分"
    return f"{total // 86400}天{total % 86400 // 3600}小时"


def analysis_span(analysis: "TaskAnalysis") -> Optional[Tuple[datetime, datetime]]:
    """分析结果覆盖的时间范围（各指标显示范围的最早起点、最晚终点）；没有数据时为 None。"""
    starts, ends = [], []
    for snapshot in analysis.snapshots.values():
        if snapshot.band:
            starts.append(snapshot.band[0].start)
            ends.append(snapshot.band[-1].end)
        elif snapshot.buckets:
            starts.append(snapshot.buckets[0].timestamp)
            ends.append(snapshot.buckets[-1].timestamp)
    if not starts:
        return None
    return min(starts), max(ends)


def _split_summary_line(line: str) -> Tuple[str, str]:
    label, _sep, value = line.partition(': ')
    return label, value


def metadata_items(task: MonitorTask, analysis: "TaskAnalysis") -> List[Tuple[str, str]]:
    """
    报告图片元信息网格的 (标签, 值)，顺序即网格的行优先顺序：
    开始 / 结束 / 时长（分析范围内数据的跨度）/ 采集周期 / 指标数 / 点数（各指标中最多的
    一个）/ 数据质量 / 被测程序 / 环境。数据质量与被测目标取不到时省略该项（与 metadata_lines
    相同：旧任务、尚未收尾、探测前）。
    """
    span = analysis_span(analysis)
    names = metric_names(task.metric_types)
    points = max((s.total_count for s in analysis.snapshots.values()), default=0)
    items = [
        (ITEM_START, task.start_time.strftime(TIME_FORMAT)),
        (ITEM_END, task.end_time.strftime(TIME_FORMAT) if task.end_time else TEXT_RUNNING),
        (ITEM_DURATION, duration_text((span[1] - span[0]).total_seconds() if span else 0.0)),
        (ITEM_INTERVAL, f"{task.interval:g} 秒"),
        (ITEM_METRICS, f"{len(names)} 项：{'、'.join(names)}"),
        (ITEM_POINTS, f"{points:,}"),
    ]
    quality = quality_text(getattr(task, 'quality', None))
    if quality:
        items.append((ITEM_QUALITY, quality))
    for line in target_lines(task):
        label, value = _split_summary_line(line)
        if label in REPORT_TARGET_LABELS and value:
            items.append((label, value))
    return items


def metadata_lines(task: MonitorTask, *, exported_at: datetime, range_text: str,
                   sample_count: Optional[int] = None,
                   end_time: Optional[datetime] = None,
                   extra: Iterable[str] = ()) -> List[str]:
    """CSV 文件头的元数据行（每行以 "# " 开头，不含换行符）。
    被测目标元数据（若有）跟在数据质量与 extra 之后、导出时间之前。"""
    note = (task.note or '').strip()
    lines = [
        f"进程: {task.process_name} (PID {task.pid})",
        f"备注: {note or '无'}",
        f"指标: {'、'.join(metric_names(task.metric_types))}",
        f"采集间隔: {task.interval:g} 秒",
        f"开始: {task.start_time.strftime(TIME_FORMAT)}",
        f"结束: {end_time.strftime(TIME_FORMAT) if end_time else '进行中'}",
        f"范围: {range_text}",
    ]
    if sample_count is not None:
        lines.append(f"采集次数: {sample_count:,}")
    quality = quality_text(getattr(task, 'quality', None))
    if quality:
        lines.append(f"数据质量: {quality}")
    lines.extend(extra)
    lines.extend(target_lines(task))
    lines.append(f"导出时间: {exported_at.strftime(TIME_FORMAT)}")
    lines.append(f"说明: {GAP_NOTE}")
    return [METADATA_PREFIX + line for line in lines]


def build_report_csv_header(task: MonitorTask) -> List[str]:
    """报告明细 CSV 表头：时间、已运行(秒)、进程名称、PID、各指标、打点。"""
    header = ['时间', ELAPSED_COLUMN_NAME, '进程名称', 'PID']
    for metric in task.metric_types:
        name, unit = get_metric_display_name(metric), get_metric_unit(metric)
        header.append(f"{name}({unit})" if unit else name)
    header.append(MARKER_COLUMN_NAME)
    return header


def format_elapsed(seconds: float) -> str:
    """已运行秒数：整秒写整数，否则保留 3 位小数。"""
    if abs(seconds - round(seconds)) < 1e-9:
        return str(int(round(seconds)))
    return f"{seconds:.3f}"


def report_rows(task: MonitorTask, data_points: Iterable[DataPoint],
                markers: Optional[Iterable[Marker]],
                gap_seconds: Optional[float]) -> Iterator[list]:
    """
    报告明细 CSV 的数据行（生成器，入参须按时间升序）。

    每行 = [时间, 已运行(秒), 进程名称, PID, 各指标..., 打点]，已运行秒数相对任务开始
    时间；相邻两行间隔大于 gap_seconds 时先产出一个空列表（写成空行）。
    """
    previous: Optional[datetime] = None
    for timestamp, row in pivot_timed_rows(task, data_points, markers):
        if (gap_seconds is not None and previous is not None
                and (timestamp - previous).total_seconds() > gap_seconds):
            yield []
        previous = timestamp
        elapsed = (timestamp - task.start_time).total_seconds()
        yield [row[0], format_elapsed(elapsed), *row[1:]]


def interval_spans(intervals: Sequence[IntervalStats],
                   markers: Sequence[Marker] = ()) -> List[Tuple[str, str]]:
    """每个区间的 (左边界标签, 右边界标签)。区间只记右边界标签：左边界取上一段的
    标签；首段左边界是与其起点同时刻的打点（若有），否则为"开始"。"""
    spans: List[Tuple[str, str]] = []
    for i, item in enumerate(intervals):
        if i > 0:
            left = intervals[i - 1].label
        else:
            left = next((m.label for m in markers
                         if m.timestamp == item.start and m.kind in ('user', 'baseline')),
                        FIRST_LABEL)
        spans.append((left, item.label))
    return spans


def _number(metric: str, value: Optional[float]) -> str:
    return _format_value(metric, value) if value is not None else ''


def _rate_number(value: Optional[float]) -> str:
    return f"{value:.4f}" if value is not None else ''


def summary_rows(snapshots: Mapping[str, "HistorySnapshot"]) -> List[list]:
    """摘要 CSV 数据行：每个指标的每个打点区间一行（列见 SUMMARY_HEADER）；
    没有区间（无数据）的指标不出行。"""
    rows: List[list] = []
    for metric, snapshot in snapshots.items():
        if not metric:
            continue
        name, unit = get_metric_display_name(metric), get_metric_unit(metric)
        intervals = snapshot.intervals
        for item, (left, right) in zip(intervals, interval_spans(intervals, snapshot.markers)):
            rows.append([
                name, unit, f"{left} → {right}",
                item.start.strftime(TIME_FORMAT), item.end.strftime(TIME_FORMAT),
                format_elapsed(item.duration_seconds), item.sample_count,
                _number(metric, item.start_value), _number(metric, item.end_value),
                _number(metric, item.delta), _rate_number(item.rate_per_hour),
                _number(metric, item.peak), _number(metric, item.delta_vs_previous),
            ])
    return rows


def downsample_footnote(raw_points: int) -> str:
    """报告图片脚注。"""
    return f"每桶 min/max 降采样，{raw_points:,} 原始点"


# ---------------------------------------------------------------- 打点区间增量表


@dataclass(frozen=True)
class RoundDeltaRow:
    """
    区间增量表的一行：相邻两个打点（基线或用户打点）围出的一轮。

    deltas：指标 → 本轮 Δ（终值 - 起值，与打点区间表同一口径：左闭右开、最后一段闭区间，
    起止值取区间内第一个 / 最后一个样本）；该指标本轮没有样本时为 None。
    vs_previous：指标 → 本轮 Δ 减上一轮 Δ；第一轮（没有上一轮）或任一轮缺数据时为 None。
    指标顺序同 TaskAnalysis.snapshots。
    """
    start_label: str
    end_label: str
    start: datetime
    end: datetime
    duration_seconds: float
    deltas: Dict[str, Optional[float]]
    vs_previous: Dict[str, Optional[float]]

    @property
    def label(self) -> str:
        return f"{self.start_label} → {self.end_label}"


def round_delta_rows(analysis: "TaskAnalysis") -> List[RoundDeltaRow]:
    """
    打点区间增量表的数据：各指标快照的打点区间（HistorySnapshot.intervals）里两端都是打点的
    那些（不含"开始 → 首个打点"的预热段与"最后打点 → 结束"的末段），按 (起点, 终点) 对齐成行，
    按起点升序。"与上轮差"相对表内上一行：第一轮为 None（区间表里它对比的是预热段，不是一轮）。
    """
    columns: List[str] = []
    bounds: Dict[Tuple[datetime, datetime], Tuple[str, str, float]] = {}
    values: Dict[Tuple[datetime, datetime], Dict[str, Optional[float]]] = {}
    for metric, snapshot in analysis.snapshots.items():
        if not metric or not snapshot.intervals:
            continue
        columns.append(metric)
        marks = {m.timestamp for m in snapshot.markers if m.kind in _ROUND_MARKER_KINDS}
        spans = interval_spans(snapshot.intervals, snapshot.markers)
        for item, (left, right) in zip(snapshot.intervals, spans):
            if item.start not in marks or item.end not in marks:
                continue
            key = (item.start, item.end)
            bounds.setdefault(key, (left, right, item.duration_seconds))
            values.setdefault(key, {})[metric] = item.delta

    rows: List[RoundDeltaRow] = []
    previous: Dict[str, Optional[float]] = {}
    for key in sorted(bounds):
        left, right, duration = bounds[key]
        deltas = {metric: values[key].get(metric) for metric in columns}
        vs_previous = {
            metric: (delta - previous[metric]
                     if delta is not None and previous.get(metric) is not None else None)
            for metric, delta in deltas.items()}
        rows.append(RoundDeltaRow(left, right, key[0], key[1], duration, deltas, vs_previous))
        previous = deltas
    return rows
