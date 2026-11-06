"""
行 ↔ 对象映射与时间/指标列的编解码

仓储层只通过这里把 sqlite3.Row 转成模型对象、把 datetime 转成库里的 ISO 文本。
时间参数只接受 datetime：data_points.timestamp 等列是 TEXT，SQLite 会把 epoch float
转成文本参与字典序比较，结果恒小于任何 ISO 串，过滤会静默失效；因此在编码入口直接
拒绝非 datetime，而不是靠注释提醒调用方。
"""
import json
import sqlite3
from datetime import datetime
from typing import List, Optional, Sequence

from data.models import DataPoint, Marker, MonitorTask


def to_iso(dt: datetime) -> str:
    """datetime → 库内 ISO 文本；传入 float/int/str 等一律 TypeError。"""
    if not isinstance(dt, datetime):
        raise TypeError(f"时间参数必须是 datetime，收到 {type(dt).__name__}: {dt!r}")
    return dt.isoformat()


def to_iso_or_none(dt: Optional[datetime]) -> Optional[str]:
    """可空版本的 to_iso：None 原样返回，其余同 to_iso。"""
    return None if dt is None else to_iso(dt)


def from_iso(text: Optional[str]) -> Optional[datetime]:
    """库内 ISO 文本 → datetime；NULL 或空串返回 None。"""
    if not text:
        return None
    return datetime.fromisoformat(text)


def parse_metric_types(text: Optional[str]) -> List[str]:
    """
    解析 tasks.metric_type 列为指标列表。

    列内容通常是 JSON 数组文本；更早的版本存的是裸指标名（单指标），按单元素列表兼容。
    '[]' 解析为空列表（不能当成名为 "[]" 的指标）；NULL/空串同样视为没有指标。
    以 '[' 开头但不是合法 JSON 数组的内容按裸指标名兜底，不丢信息。
    """
    if not text:
        return []
    if text.startswith('['):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return [text]
        if isinstance(parsed, list):
            return [str(m) for m in parsed]
        return [text]
    return [text]


def dump_metric_types(metric_types: Sequence[str]) -> str:
    """指标列表 → tasks.metric_type 列的 JSON 数组文本。

    单个字符串会被 json.dumps 写成 '"memory_rss"' 这种读回来解析不了的形态，直接拒绝。
    """
    if isinstance(metric_types, str):
        raise TypeError("metric_types 必须是字符串列表，不能是单个字符串")
    return json.dumps(list(metric_types))


def _has(row: sqlite3.Row, column: str) -> bool:
    return column in row.keys()


def row_to_task(row: sqlite3.Row) -> MonitorTask:
    """tasks 表的一行 → MonitorTask。

    后加的列（v3 的 note/sample_count/last_sample_at、v4 的 create_time）缺失时取默认值，
    兼容迁移失败后仍停留在旧版本的库。
    """
    return MonitorTask(
        task_id=row['task_id'],
        pid=row['pid'],
        process_name=row['process_name'],
        metric_types=parse_metric_types(row['metric_type']),
        interval=row['interval'],
        start_time=from_iso(row['start_time']),
        end_time=from_iso(row['end_time']),
        status=row['status'],
        note=(row['note'] or '') if _has(row, 'note') else '',
        sample_count=(row['sample_count'] or 0) if _has(row, 'sample_count') else 0,
        last_sample_at=from_iso(row['last_sample_at']) if _has(row, 'last_sample_at') else None,
        create_time=row['create_time'] if _has(row, 'create_time') else None,
    )


def row_to_point(row: sqlite3.Row) -> DataPoint:
    """data_points 表的一行（至少含 task_id/timestamp/value/metric_type）→ DataPoint。"""
    return DataPoint(
        task_id=row['task_id'],
        timestamp=from_iso(row['timestamp']),
        value=row['value'],
        metric_type=row['metric_type'] or '',
    )


def row_to_marker(row: sqlite3.Row) -> Marker:
    """task_markers 表的一行 → Marker；kind 列缺失（v3 库）时视为用户打点。"""
    return Marker(
        task_id=row['task_id'],
        timestamp=from_iso(row['timestamp']),
        label=row['label'],
        id=row['id'],
        kind=(row['kind'] or 'user') if _has(row, 'kind') else 'user',
    )
