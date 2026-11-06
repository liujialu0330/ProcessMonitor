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

这里不依赖 Qt 控件与数据库，便于直接单测；读库与写文件在 core.report_worker.ReportWorker。
"""
import os
from dataclasses import dataclass
from datetime import datetime
from typing import (TYPE_CHECKING, Iterable, Iterator, List, Mapping, Optional,
                    Sequence, Tuple)

from core.analysis.intervals import IntervalStats
from core.export import MARKER_COLUMN_NAME, _format_value, pivot_timed_rows
from data.models import DataPoint, Marker, MonitorTask
from data.repos.analytics_repo import DEFAULT_GAP_FACTOR
from utils.metrics import get_metric_display_name, get_metric_unit

if TYPE_CHECKING:
    from core.history_query import HistorySnapshot

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


def task_basename(task: MonitorTask) -> str:
    """报告文件的基名：进程名（去 .exe 与非法字符）_PID_开始时间。"""
    name = task.process_name or 'task'
    if name.lower().endswith('.exe'):
        name = name[:-4]
    name = ''.join('_' if ch in _INVALID_CHARS else ch for ch in name).strip(' .') or 'task'
    return f"{name}_{task.pid}_{task.start_time.strftime('%Y%m%d_%H%M%S')}"


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


def metadata_lines(task: MonitorTask, *, exported_at: datetime, range_text: str,
                   sample_count: Optional[int] = None,
                   end_time: Optional[datetime] = None,
                   extra: Iterable[str] = ()) -> List[str]:
    """CSV 文件头的元数据行（每行以 "# " 开头，不含换行符）。"""
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
    lines.extend(extra)
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
