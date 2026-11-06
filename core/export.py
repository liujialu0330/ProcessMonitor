"""
导出逻辑纯函数模块
CSV 表头生成与数据透视（同一时刻多指标合并为一行、打点列），不依赖 UI/数据库，
便于单元测试与流式导出复用；分析页报告导出（core/report.py）经 pivot_timed_rows 使用。
"""
from datetime import datetime
from typing import Iterator, Iterable, List, Optional, Tuple

from data.models import MonitorTask, DataPoint, Marker
from utils.metrics import get_metric_display_name, get_metric_unit, is_integer_metric

# 打点列的表头
MARKER_COLUMN_NAME = '打点'
# 同一行有多个打点时标签之间的分隔符
MARKER_LABEL_SEPARATOR = '; '


def _format_value(metric: str, value) -> str:
    """CSV 单元格文本：计数类指标写整数（124 而不是 124.0000），其余保留 4 位小数。"""
    if value is None:
        return ""
    if is_integer_metric(metric):
        return str(int(round(value)))
    return f"{value:.4f}"


def build_csv_header(task: MonitorTask, include_markers: bool = False) -> List[str]:
    """
    生成 CSV 表头

    Args:
        task: 监控任务（读取 metric_types 拼出各指标列）
        include_markers: 是否在末尾追加"打点"列（需与 pivot_rows 的 markers 参数
            配套：传了 markers 就要传 True）

    Returns:
        List[str]: ['时间', '进程名称', 'PID', <指标名(单位)>..., ['打点']]
    """
    header = ['时间', '进程名称', 'PID']
    for metric in task.metric_types:
        metric_display = get_metric_display_name(metric)
        metric_unit = get_metric_unit(metric)
        if metric_unit:
            header.append(f"{metric_display}({metric_unit})")
        else:
            header.append(metric_display)
    if include_markers:
        header.append(MARKER_COLUMN_NAME)
    return header


def pivot_rows(task: MonitorTask, data_point_iter: Iterable[DataPoint],
               markers: Optional[Iterable[Marker]] = None) -> Iterator[list]:
    """
    按时间戳流式透视数据点为宽表行（生成器）

    维护当前 timestamp 分组，读到新 timestamp 才 flush 上一组产出一行，
    迭代结束 flush 最后一组。要求入参按 timestamp 升序排列（同组行相邻）。

    传入 markers（不为 None，可为空列表）时，每行末尾追加"打点"列：打点写在
    "时间不早于打点时刻的第一行"，同一行的多个打点用 "; " 连接；晚于最后一次
    采集的打点并入最后一行，保证不丢信息；没有打点的行该列为空串。

    Args:
        task: 监控任务（读取 process_name / pid / metric_types）
        data_point_iter: 数据点可迭代对象，按 timestamp 升序
        markers: 任务的打点（可乱序，内部按时间排序）；None 表示不输出打点列

    Yields:
        list: [时间字符串, 进程名称, PID, <各指标值或空串>..., [打点标签]]
    """
    for _timestamp, row in pivot_timed_rows(task, data_point_iter, markers):
        yield row


def pivot_timed_rows(task: MonitorTask, data_point_iter: Iterable[DataPoint],
                     markers: Optional[Iterable[Marker]] = None
                     ) -> Iterator[Tuple[datetime, list]]:
    """与 pivot_rows 相同，但每行同时给出该行的采集时刻 (timestamp, row)，
    供报告 CSV 计算"已运行(秒)"与识别空洞。"""
    metrics = task.metric_types
    first_metric = metrics[0] if metrics else ''

    with_markers = markers is not None
    pending_markers = sorted(markers, key=lambda m: m.timestamp) if with_markers else []
    marker_index = 0

    current_timestamp = None
    current_values = {}

    def _flush(is_last: bool = False):
        nonlocal marker_index
        row = [
            current_timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            task.process_name,
            task.pid,
        ]
        for metric in metrics:
            row.append(_format_value(metric, current_values.get(metric)))
        if with_markers:
            labels = []
            while (marker_index < len(pending_markers)
                   and (is_last or pending_markers[marker_index].timestamp
                        <= current_timestamp)):
                labels.append(pending_markers[marker_index].label)
                marker_index += 1
            row.append(MARKER_LABEL_SEPARATOR.join(labels))
        return current_timestamp, row

    for dp in data_point_iter:
        # metric_type 为 NULL/空串的旧数据兜底归入首指标
        metric = dp.metric_type or first_metric

        if current_timestamp is not None and dp.timestamp != current_timestamp:
            yield _flush()
            current_values = {}

        current_timestamp = dp.timestamp
        current_values[metric] = dp.value

    if current_timestamp is not None:
        yield _flush(is_last=True)
