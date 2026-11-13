"""
行 ↔ 对象映射与时间/指标列的编解码

仓储层只通过这里把 sqlite3.Row 转成模型对象、把 datetime 转成库里的时间值：
- tasks / task_markers 的时间列是 ISO 文本（to_iso / from_iso）；
- samples.ts_ms 是 UTC 毫秒整数（to_ms / from_ms，以及查询边界用的 ceil_ms / floor_ms）。
时间参数只接受 datetime：SQLite 会把 epoch float 与文本列做跨类型比较，过滤会静默失效；
整数毫秒列收到秒级 epoch 也不会报错，只是差了 1000 倍。因此在编码入口直接拒绝非 datetime，
而不是靠注释提醒调用方。

毫秒换算一律用整数运算：整秒部分由 datetime.timestamp() 得到（本地 naive 时间按本机时区
解释，含夏令时规则），微秒部分按"四舍五入到毫秒、0.5 毫秒进位"取整。不用
round(dt.timestamp() * 1000)：浮点乘法在恰好 0.5 毫秒处的进位方向不确定，
与迁移时 SQL 侧的整数换算（data.db.migrations.rebuild）就可能差 1 毫秒。

边界取整规则：
- 时刻类边界（打点、基线这类"事件发生的时刻"）与样本同一取整规则，一律 to_ms（四舍五入）：
  比较的是"打点若也存成样本会落在哪一毫秒"。取整单调，因此不早于打点采集的样本一定算打点
  及之后（与打点同一时刻采集的样本不会因打点被向上取整而落到打点之前）；早于打点 1 毫秒以上
  的一定算之前；相差不足 1 毫秒时按取整后是否落在同一毫秒判定（同一毫秒算之后）。
  调用方用 normalize_ms 把打点时刻换成取整后的时刻再作查询边界；
- 范围类边界（查询的 since / until，闭区间）比较的是已存的毫秒值本身：since 向上取整、
  until 向下取整（ceil_ms / floor_ms），"m >= since 当且仅当 m >= ceil_ms(since)"。
"""
import json
import sqlite3
from datetime import datetime
from typing import List, Optional, Sequence

from data.models import DataPoint, Marker, MonitorTask, QualityStats


def to_iso(dt: datetime) -> str:
    """datetime → 库内 ISO 文本；传入 float/int/str 等一律 TypeError。"""
    if not isinstance(dt, datetime):
        raise TypeError(f"时间参数必须是 datetime，收到 {type(dt).__name__}: {dt!r}")
    return dt.isoformat()


def to_iso_or_none(dt: Optional[datetime]) -> Optional[str]:
    """可空版本的 to_iso：None 原样返回，其余同 to_iso。"""
    return None if dt is None else to_iso(dt)


def _check_dt(dt: datetime) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError(f"时间参数必须是 datetime，收到 {type(dt).__name__}: {dt!r}")
    return dt


def _whole_seconds(dt: datetime) -> int:
    """dt 所在整秒的 epoch 秒数（本地 naive 时间按本机时区解释）；结果是精确整数"""
    return int(dt.replace(microsecond=0).timestamp())


def to_ms(dt: datetime) -> int:
    """datetime（本地 naive）→ UTC 毫秒整数，微秒按四舍五入（0.5 毫秒进位）取整。

    与 from_ms 往返无损（毫秒精度的 datetime 原样返回）。
    """
    _check_dt(dt)
    return _whole_seconds(dt) * 1000 + (dt.microsecond + 500) // 1000


def ceil_ms(dt: datetime) -> int:
    """查询下界：不早于 dt 的最小整数毫秒。样本 m 满足 m >= dt 当且仅当 m >= ceil_ms(dt)"""
    _check_dt(dt)
    return _whole_seconds(dt) * 1000 + (dt.microsecond + 999) // 1000


def floor_ms(dt: datetime) -> int:
    """查询上界：不晚于 dt 的最大整数毫秒。样本 m 满足 m <= dt 当且仅当 m <= floor_ms(dt)"""
    _check_dt(dt)
    return _whole_seconds(dt) * 1000 + dt.microsecond // 1000


def from_ms(ms: int) -> datetime:
    """UTC 毫秒整数 → 本地 naive datetime（毫秒精度）。

    ms / 1000 的双精度误差远小于 0.5 微秒，fromtimestamp 舍入到微秒后恰好是整毫秒。
    """
    return datetime.fromtimestamp(ms / 1000)


def normalize_ms(dt: datetime) -> datetime:
    """dt 按 to_ms 取整到毫秒后的 datetime（即写入 samples 后再读回的样子）；
    打点时刻作为样本查询边界前先经过它（见模块说明"边界取整规则"）"""
    return from_ms(to_ms(dt))


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


# tasks 表的数据质量列（v5 预留，write_p95_ms 没有列）
QUALITY_COLUMNS = ('planned_cycles', 'done_cycles', 'missed_cycles',
                   'lag_p95_ms', 'collect_p95_ms')


def row_to_quality(row: sqlite3.Row) -> Optional[QualityStats]:
    """tasks 行的数据质量列 → QualityStats；列不存在（旧库）或尚未写入（planned_cycles
    为 NULL）时为 None。"""
    if not _has(row, 'planned_cycles') or row['planned_cycles'] is None:
        return None
    return QualityStats(
        planned_cycles=int(row['planned_cycles']),
        done_cycles=int(row['done_cycles'] or 0),
        missed_cycles=int(row['missed_cycles'] or 0),
        lag_p95_ms=row['lag_p95_ms'],
        collect_p95_ms=row['collect_p95_ms'],
    )


def quality_params(quality: QualityStats) -> tuple:
    """QualityStats → 与 QUALITY_COLUMNS 顺序一致的落库参数（毫秒值取整）。"""
    def _int(value):
        return None if value is None else int(round(value))
    return (int(quality.planned_cycles), int(quality.done_cycles),
            int(quality.missed_cycles), _int(quality.lag_p95_ms),
            _int(quality.collect_p95_ms))


# meta_json 里存停止原因的键：{"stop": {"reason": "crashed", "text": "..."}}
STOP_KEY = 'stop'


def stop_reason_of(meta_json: Optional[str]) -> Optional[str]:
    """从 tasks.meta_json 取停止原因（没有记录或内容损坏返回 None）。"""
    # 绝大多数任务没有 stop 键：先做子串检查，列表读取时不必为每行都解析 JSON
    if not meta_json or f'"{STOP_KEY}"' not in meta_json:
        return None
    try:
        data = json.loads(meta_json)
    except ValueError:
        return None
    stop = data.get(STOP_KEY) if isinstance(data, dict) else None
    reason = stop.get('reason') if isinstance(stop, dict) else None
    return reason if isinstance(reason, str) and reason else None


def row_to_task(row: sqlite3.Row) -> MonitorTask:
    """tasks 表的一行 → MonitorTask。

    后加的列（v3 的 note/sample_count/last_sample_at、v4 的 create_time、v5 的 plan_json、
    meta_json、target_kind / target_spec 与数据质量列）缺失时取默认值，兼容迁移失败后仍停留在旧版本的库。
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
        plan_json=(row['plan_json'] or None) if _has(row, 'plan_json') else None,
        quality=row_to_quality(row),
        meta_json=(row['meta_json'] or None) if _has(row, 'meta_json') else None,
        target_kind=(row['target_kind'] or 'pid') if _has(row, 'target_kind') else 'pid',
        target_spec=(row['target_spec'] or None) if _has(row, 'target_spec') else None,
        exit_code=row['exit_code'] if _has(row, 'exit_code') else None,
        stop_reason=(stop_reason_of(row['meta_json']) if _has(row, 'meta_json') else None),
        resume_on_restart=bool(row['resume_on_restart']) if _has(row, 'resume_on_restart') else False,
        restart_limit=(row['restart_limit'] or 0) if _has(row, 'restart_limit') else 0,
        scenario=(row['scenario'] or '') if _has(row, 'scenario') else '',
        build=(row['build'] or '') if _has(row, 'build') else '',
        is_reference=bool(row['is_reference']) if _has(row, 'is_reference') else False,
        starred=starred_of(row['meta_json']) if _has(row, 'meta_json') else False,
        group_id=(row['group_id'] or None) if _has(row, 'group_id') else None,
    )


def starred_of(meta_json: Optional[str]) -> bool:
    """星标是任务库偏好；旧任务或损坏的附加信息视为未标星。"""
    try:
        meta = json.loads(meta_json) if meta_json else {}
        return isinstance(meta, dict) and meta.get('starred') is True
    except (ValueError, TypeError):
        return False


def row_to_point(row: sqlite3.Row) -> DataPoint:
    """旧结构 data_points 表的一行（至少含 task_id/timestamp/value/metric_type）→ DataPoint。

    v5 起样本存 samples 表，仓储直接用 from_ms 构造 DataPoint；本函数只服务旧结构的读取。
    """
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
