"""
导出逻辑纯函数模块
从 ui/pages/export_page.py 中抽出的表头生成与数据透视逻辑，
不依赖 UI/数据库，便于单元测试与后续（批3）流式导出复用。
"""
from typing import Iterator, Iterable, List

from data.models import MonitorTask, DataPoint
from utils.metrics import get_metric_display_name, get_metric_unit


def build_csv_header(task: MonitorTask) -> List[str]:
    """
    生成 CSV 表头

    Args:
        task: 监控任务（读取 metric_types 拼出各指标列）

    Returns:
        List[str]: ['时间', '进程名称', 'PID', <指标名(单位)>...]
    """
    header = ['时间', '进程名称', 'PID']
    for metric in task.metric_types:
        metric_display = get_metric_display_name(metric)
        metric_unit = get_metric_unit(metric)
        if metric_unit:
            header.append(f"{metric_display}({metric_unit})")
        else:
            header.append(metric_display)
    return header


def pivot_rows(task: MonitorTask, data_point_iter: Iterable[DataPoint]) -> Iterator[list]:
    """
    按时间戳流式透视数据点为宽表行（生成器）

    维护当前 timestamp 分组，读到新 timestamp 才 flush 上一组产出一行，
    迭代结束 flush 最后一组。要求入参按 timestamp 升序排列（同组行相邻）。

    Args:
        task: 监控任务（读取 process_name / pid / metric_types）
        data_point_iter: 数据点可迭代对象，按 timestamp 升序

    Yields:
        list: [时间字符串, 进程名称, PID, <各指标值或空串>...]
    """
    metrics = task.metric_types
    first_metric = metrics[0] if metrics else ''

    current_timestamp = None
    current_values = {}

    def _flush():
        row = [
            current_timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            task.process_name,
            task.pid,
        ]
        for metric in metrics:
            value = current_values.get(metric)
            row.append(f"{value:.4f}" if value is not None else "")
        return row

    for dp in data_point_iter:
        # metric_type 为 NULL/空串的旧数据兜底归入首指标
        metric = dp.metric_type or first_metric

        if current_timestamp is not None and dp.timestamp != current_timestamp:
            yield _flush()
            current_values = {}

        current_timestamp = dp.timestamp
        current_values[metric] = dp.value

    if current_timestamp is not None:
        yield _flush()
