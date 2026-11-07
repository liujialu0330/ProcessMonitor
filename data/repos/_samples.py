"""
仓储共用的 samples / series 查询辅助（只在 data 层内部使用）

时间边界在这里统一换成整数毫秒：since 向上取整、until 向下取整（见 data.mappers.ceil_ms），
不限的一侧用远超真实时间戳的哨兵，SQL 一律写成 ts_ms BETWEEN ? AND ?。
"""
import math
import random
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from data.mappers import ceil_ms, floor_ms, from_ms

# 不限时间范围时用的哨兵（远超任何真实毫秒时间戳）
MS_MIN = -(1 << 62)
MS_MAX = 1 << 62


def check_time(value: Optional[datetime], name: str) -> None:
    """时间参数只收 datetime 或 None，其余类型 TypeError"""
    if value is not None and not isinstance(value, datetime):
        raise TypeError(f'{name} 必须是 datetime 或 None，实际为 {type(value).__name__}')


def ms_range(since: Optional[datetime], until: Optional[datetime]) -> Tuple[int, int]:
    """闭区间 [since, until] → 整数毫秒 (lo, hi)；None 一侧不限"""
    check_time(since, 'since')
    check_time(until, 'until')
    lo = ceil_ms(since) if since is not None else MS_MIN
    hi = floor_ms(until) if until is not None else MS_MAX
    return lo, hi


def series_id(conn, task_id: str, metric: str) -> Optional[int]:
    row = conn.execute('SELECT series_id FROM series WHERE task_id = ? AND metric = ?',
                       (task_id, metric)).fetchone()
    return row[0] if row is not None else None


def task_series(conn, task_id: str, metric: Optional[str] = None) -> List[Tuple[int, str]]:
    """任务的序列 [(series_id, metric)]，按 series_id 升序；metric 给定时至多一条"""
    if metric is not None:
        sid = series_id(conn, task_id, metric)
        return [] if sid is None else [(sid, metric)]
    return [(row[0], row[1]) for row in conn.execute(
        'SELECT series_id, metric FROM series WHERE task_id = ? ORDER BY series_id',
        (task_id,))]


def in_clause(ids: Iterable[int]) -> Tuple[str, list]:
    """('series_id IN (?, ?)', [ids]) 片段；ids 非空由调用方保证"""
    ids = list(ids)
    return f"series_id IN ({','.join('?' * len(ids))})", ids


def recount_task(conn, task_id: str) -> int:
    """
    在调用方的事务内按样本重算一个任务的 sample_count（不同毫秒时间戳个数）与
    last_sample_at（最大时间戳的 ISO 文本）；没有样本时置为 0 / NULL。返回新计数。
    """
    ids = [sid for sid, _m in task_series(conn, task_id)]
    count, latest = 0, None
    if len(ids) == 1:
        count, latest = conn.execute(
            'SELECT COUNT(*), MAX(ts_ms) FROM samples WHERE series_id = ?', ids).fetchone()
    elif ids:
        where, params = in_clause(ids)
        count, latest = conn.execute(
            f'SELECT COUNT(DISTINCT ts_ms), MAX(ts_ms) FROM samples WHERE {where}',
            params).fetchone()
    count = count or 0
    conn.execute('UPDATE tasks SET sample_count = ?, last_sample_at = ? WHERE task_id = ?',
                 (count, from_ms(latest).isoformat() if latest is not None else None, task_id))
    return count


# ---------------------------------------------------------------- 整包取回多行结果

# 浮点经 printf('%!.17g') 转文本再 float() 能逐位还原的量级（SQLite 3.45 实测 2^±272 以内无误差，
# 这里留足余量）；范围外的值（以及首次自检不通过的 SQLite 版本）退回逐行读取
_PACK_SAFE_ABS_MAX = 1e70
_PACK_SAFE_ABS_MIN = 1e-70
_pack_supported: Optional[bool] = None


def _probe_pack(conn) -> bool:
    """本进程首次整包读取前自检一次：一组覆盖安全范围的浮点经 SQLite 文本化后能否逐位还原"""
    rng = random.Random(20260301)
    values = [0.0, 0.1 + 0.2, 1 / 3, 2.0 ** 53 + 2.0, 123456789.123456789,
              _PACK_SAFE_ABS_MAX, _PACK_SAFE_ABS_MIN]
    values += [math.ldexp(rng.random() + 0.5, rng.randint(-230, 230)) * rng.choice((1, -1))
               for _ in range(400)]
    rows = conn.execute(
        "SELECT printf('%!.17g', column1) FROM (VALUES "
        + ','.join(['(?)'] * len(values)) + ')', values).fetchall()
    return all(float(text) == value for (text,), value in zip(rows, values))


def fetch_packed(conn, sql: str, params, kinds: str) -> List[tuple]:
    """
    执行 sql，结果在 SQLite 里拼成一个字符串一次取回、在 Python 里拆成元组列表（行序不保证，
    调用方自行排序）。

    原因：sqlite3 模块每取一行都要释放再重新获取一次 GIL。分析页 5 个指标的查询线程并行时，
    逐行取上千行会在 GIL 上反复交接（护航效应），比单线程依次执行还慢数倍；整包取回只交接一次。

    sql 的结果列须依次命名为 c0..c{n-1}，kinds 逐列给类型：'i' 整数、'f' 浮点（存成整数的
    浮点也算）。任一行出现与 kinds 不符的值（NULL、文本、'i' 列里的浮点）、浮点超出安全量级、
    或本进程的 SQLite 自检不通过时，整体退回逐行读取——结果总与逐行读取逐位相同。
    """
    global _pack_supported
    if _pack_supported is None:
        _pack_supported = _probe_pack(conn)
    if not _pack_supported:
        return [tuple(row) for row in conn.execute(sql, params)]
    parts, unsafe = [], []
    for i, kind in enumerate(kinds):
        col = f'c{i}'
        if kind == 'f':
            parts.append(f"printf('%!.17g', {col})")
            unsafe.append(f"(typeof({col}) NOT IN ('real', 'integer') "
                          f'OR ABS({col}) > {_PACK_SAFE_ABS_MAX!r} '
                          f'OR ({col} != 0 AND ABS({col}) < {_PACK_SAFE_ABS_MIN!r}))')
        else:
            parts.append(col)
            unsafe.append(f"typeof({col}) != 'integer'")
    row_text = " || ',' || ".join(parts)
    text, bad, count = conn.execute(
        f"SELECT group_concat({row_text}, ','), "
        f"MAX({' OR '.join(unsafe)}), COUNT(*) FROM ({sql})", params).fetchone()
    if not count:
        return []
    flat = text.split(',') if text is not None else []
    width = len(kinds)
    if bad or len(flat) != count * width:
        return [tuple(row) for row in conn.execute(sql, params)]
    columns = [list(map(int if kind == 'i' else float, flat[i::width]))
               for i, kind in enumerate(kinds)]
    return list(zip(*columns))


def delete_series_rows(conn, ids: List[int]) -> int:
    """删除这些序列在 samples / rollup_1m / series_gaps / series 中的行，返回样本行数"""
    if not ids:
        return 0
    where, params = in_clause(ids)
    count = conn.execute(f'DELETE FROM samples WHERE {where}', params).rowcount
    conn.execute(f'DELETE FROM rollup_1m WHERE {where}', params)
    conn.execute(f'DELETE FROM series_gaps WHERE {where}', params)
    conn.execute(f'DELETE FROM series WHERE {where}', params)
    return count
