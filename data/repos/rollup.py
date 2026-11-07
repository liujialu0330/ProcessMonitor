"""
分钟汇总（rollup_1m）与写时空洞（series_gaps）：规则、增量维护与从样本重算（只在 data 层内部使用）

规则（写入、重算、查询三处共用这里的定义）：
- 空洞：同一序列相邻两个样本的间隔 (后 - 前) / 1000.0 秒 > tasks.interval × DEFAULT_GAP_FACTOR
  （严格大于，与 AnalyticsRepo.gaps 的口径逐位相同）；任务不存在或 interval 不是正数时不记空洞。
  series_gaps 一行 = (空洞前最后一个样本, 空洞后第一个样本)。
- 段起点：序列的首个样本，以及每个空洞的 end_ms。
- 桶键：bucket_ms = max(该样本所在自然分钟的起点, 该样本所在段的段起点)。一个自然分钟里出现空洞
  时拆成两行，任何汇总行都不跨空洞；同一序列的汇总行按 bucket_ms 排序后各覆盖一段连续的样本，
  互不重叠，bucket_ms <= first_ms <= last_ms < bucket_ms 所在分钟的终点。
- 汇总行：样本数、首末样本（时间与值）、最小/最大值及其最早出现的时间（min_ms / max_ms）、
  Σy、Σy²，以及以 x = (ts_ms - bucket_ms) / 1000.0（相对桶起点的秒数，避免绝对 epoch 平方的
  灾难性抵消）计的 Σx、Σx²、Σxy。

不变式：rollup_1m 与 series_gaps 恒等于"从 samples 按上述规则整体重算"的结果（各阶和只差求和
顺序带来的舍入）。维护方式：
- 按时间顺序追加（采集的常态）：在写 samples 的同一事务里逐行判空洞、算桶键，按 (序列, 桶)
  在 Python 里先合并，再每个桶一条 UPSERT（ON CONFLICT 走主键）；新空洞直接插入；
- 同一毫秒覆盖已有样本：值变了，min/max 及其时间无法增量修正，对该分钟按样本重算；
- 乱序写入（早于该序列已有的最后一个样本、且该毫秒原本没有样本）：会改变相邻空洞与段起点，
  按受影响的区间重算空洞与汇总（记 WARNING 与计数；正常采集不会出现）；
- 导回降级数据、重建式迁移、设置页"重建汇总"：rebuild_rollups 整体重算。
"""
import logging
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 空洞判定倍数：相邻采样间隔 > 2.5 × 采集周期视为空洞（与分析查询的默认口径相同）
DEFAULT_GAP_FACTOR = 2.5
MINUTE_MS = 60_000

_COLUMNS = ('series_id, bucket_ms, count, first_ms, first_value, last_ms, last_value, '
            'min_value, min_ms, max_value, max_ms, sum_y, sum_yy, sum_x, sum_xx, sum_xy')

# 合并一行增量：SQLite 的 UPDATE SET 右侧引用的一律是旧行的值，与各子句的先后无关
UPSERT_SQL = f'''
    INSERT INTO rollup_1m ({_COLUMNS})
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(series_id, bucket_ms) DO UPDATE SET
        count = count + excluded.count,
        first_value = CASE WHEN excluded.first_ms < first_ms
                           THEN excluded.first_value ELSE first_value END,
        first_ms = MIN(first_ms, excluded.first_ms),
        last_value = CASE WHEN excluded.last_ms > last_ms
                          THEN excluded.last_value ELSE last_value END,
        last_ms = MAX(last_ms, excluded.last_ms),
        min_ms = CASE WHEN excluded.min_value < min_value THEN excluded.min_ms
                      WHEN excluded.min_value = min_value THEN MIN(min_ms, excluded.min_ms)
                      ELSE min_ms END,
        min_value = MIN(min_value, excluded.min_value),
        max_ms = CASE WHEN excluded.max_value > max_value THEN excluded.max_ms
                      WHEN excluded.max_value = max_value THEN MIN(max_ms, excluded.max_ms)
                      ELSE max_ms END,
        max_value = MAX(max_value, excluded.max_value),
        sum_y = sum_y + excluded.sum_y,
        sum_yy = sum_yy + excluded.sum_yy,
        sum_x = sum_x + excluded.sum_x,
        sum_xx = sum_xx + excluded.sum_xx,
        sum_xy = sum_xy + excluded.sum_xy
'''

GAP_INSERT_SQL = 'INSERT OR REPLACE INTO series_gaps (series_id, start_ms, end_ms) VALUES (?, ?, ?)'


def gap_threshold(interval) -> Optional[float]:
    """空洞阈值（秒）= interval × DEFAULT_GAP_FACTOR；interval 不是正数时返回 None（不记空洞）"""
    if interval is None:
        return None
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        return None
    if not interval > 0:
        return None
    return interval * DEFAULT_GAP_FACTOR


def is_gap(delta_ms: int, threshold: Optional[float]) -> bool:
    """相邻样本间隔 delta_ms 是否构成空洞（与 SQL 里 delta / 1000.0 > threshold 逐位相同）"""
    return threshold is not None and delta_ms / 1000.0 > threshold


def minute_floor(ts_ms: int) -> int:
    """所在自然分钟的起点（向下取整，负数同样成立）"""
    return ts_ms - ts_ms % MINUTE_MS


def floor_sql(col: str) -> str:
    """minute_floor 的 SQL 写法（SQLite 的 % 向零截断，先折成非负余数）"""
    return f'({col} - ((({col}) % {MINUTE_MS}) + {MINUTE_MS}) % {MINUTE_MS})'


# ---------------------------------------------------------------- 增量（按时间顺序追加）


class _Acc:
    """一个 (序列, 桶) 的增量；样本须按时间升序加入"""
    __slots__ = ('bucket', 'count', 'first_ms', 'first_v', 'last_ms', 'last_v',
                 'min_v', 'min_ms', 'max_v', 'max_ms', 'sy', 'syy', 'sx', 'sxx', 'sxy')

    def __init__(self, bucket: int, ts: int, value: float):
        x = (ts - bucket) / 1000.0
        self.bucket = bucket
        self.count = 1
        self.first_ms = self.last_ms = self.min_ms = self.max_ms = ts
        self.first_v = self.last_v = self.min_v = self.max_v = value
        self.sy = value
        self.syy = value * value
        self.sx = x
        self.sxx = x * x
        self.sxy = x * value

    def add(self, ts: int, value: float) -> None:
        x = (ts - self.bucket) / 1000.0
        self.count += 1
        self.last_ms = ts
        self.last_v = value
        if value < self.min_v:
            self.min_v, self.min_ms = value, ts
        if value > self.max_v:
            self.max_v, self.max_ms = value, ts
        self.sy += value
        self.syy += value * value
        self.sx += x
        self.sxx += x * x
        self.sxy += x * value

    def row(self, sid: int) -> tuple:
        return (sid, self.bucket, self.count, self.first_ms, self.first_v, self.last_ms,
                self.last_v, self.min_v, self.min_ms, self.max_v, self.max_ms,
                self.sy, self.syy, self.sx, self.sxx, self.sxy)


class SeriesState:
    """
    一条序列在本批写入前的状态（均可为 None：序列尚无样本）：
    - last_ms：最后一个样本的时间；
    - segment_ms：判桶用的段下界。append_in_order 只用它做 floor >= segment_ms 的比较，
      取"当前段起点"或"最后一个样本所在的汇总行的 bucket_ms"（= max(last 的分钟起点, 段起点)）
      结果相同：新样本不早于 last，二者只在段起点落在 last 那一分钟时不同，而此时两者相等。
    """
    __slots__ = ('last_ms', 'segment_ms')

    def __init__(self, last_ms: Optional[int], segment_ms: Optional[int]):
        self.last_ms = last_ms
        self.segment_ms = segment_ms


def load_task_state(conn, task_id: str) -> Tuple[Optional[float], Dict[int, SeriesState]]:
    """
    一条查询取任务的空洞阈值与其全部序列的状态（每列都走主键一端）：最后一个样本取自 samples，
    段下界取该序列最后一个汇总行的 bucket_ms（见 SeriesState）。有样本却没有汇总行（汇总与
    样本不一致，正常不会出现）时退回按空洞表 / 首个样本求段起点。
    """
    rows = conn.execute('''
        SELECT se.series_id,
               (SELECT MAX(ts_ms) FROM samples s WHERE s.series_id = se.series_id),
               (SELECT r.bucket_ms FROM rollup_1m r WHERE r.series_id = se.series_id
                ORDER BY r.bucket_ms DESC LIMIT 1),
               (SELECT interval FROM tasks WHERE task_id = ?1)
        FROM series se WHERE se.task_id = ?1
    ''', (task_id,)).fetchall()
    threshold = gap_threshold(rows[0][3]) if rows else None
    states = {}
    for sid, last_ms, bucket_ms, _interval in rows:
        if last_ms is not None and bucket_ms is None:
            bucket_ms = _segment_start(conn, sid)
        states[sid] = SeriesState(last_ms, bucket_ms)
    return threshold, states


def _segment_start(conn, sid: int) -> Optional[int]:
    """序列当前段起点：最后一个空洞的 end_ms，没有空洞时取首个样本"""
    row = conn.execute('''
        SELECT COALESCE(
            (SELECT end_ms FROM series_gaps WHERE series_id = ?1 ORDER BY start_ms DESC LIMIT 1),
            (SELECT MIN(ts_ms) FROM samples WHERE series_id = ?1))
    ''', (sid,)).fetchone()
    return row[0]


def append_in_order(state: SeriesState, rows: Sequence[Tuple[int, float]],
                    threshold: Optional[float], sid: int,
                    gaps_out: List[tuple], rollups_out: List[tuple]) -> None:
    """
    把一条序列晚于其最后一个样本的新样本（按时间升序、已去重）折算成空洞行与汇总增量行，
    分别追加到 gaps_out / rollups_out（同一桶只出一行）。state 随之前移。
    """
    last, segment = state.last_ms, state.segment_ms
    acc: Optional[_Acc] = None
    for ts, value in rows:
        if last is None:
            segment = ts
        elif is_gap(ts - last, threshold):
            gaps_out.append((sid, last, ts))
            segment = ts
        floor = ts - ts % MINUTE_MS
        bucket = floor if floor >= segment else segment
        if acc is not None and acc.bucket == bucket:
            acc.add(ts, value)
        else:
            if acc is not None:
                rollups_out.append(acc.row(sid))
            acc = _Acc(bucket, ts, value)
        last = ts
    if acc is not None:
        rollups_out.append(acc.row(sid))
    state.last_ms, state.segment_ms = last, segment


# ---------------------------------------------------------------- 从样本重算


def _keyed_aggregate_sql(keyed: str) -> str:
    """
    keyed 为产出 (series_id, ts_ms, value, bucket) 的子查询；按 (series_id, bucket) 分组写入
    rollup_1m。首末值、最值的最早时间在分组之后按主键回查（每桶几次定位，不再排序）。
    """
    return f'''
        INSERT INTO rollup_1m ({_COLUMNS})
        SELECT g.series_id, g.bucket, g.cnt,
               g.f, (SELECT value FROM samples WHERE series_id = g.series_id AND ts_ms = g.f),
               g.l, (SELECT value FROM samples WHERE series_id = g.series_id AND ts_ms = g.l),
               g.mn, (SELECT MIN(ts_ms) FROM samples WHERE series_id = g.series_id
                      AND ts_ms BETWEEN g.f AND g.l AND value = g.mn),
               g.mx, (SELECT MIN(ts_ms) FROM samples WHERE series_id = g.series_id
                      AND ts_ms BETWEEN g.f AND g.l AND value = g.mx),
               g.sy, g.syy, g.sx, g.sxx, g.sxy
        FROM (
            SELECT series_id, bucket, COUNT(*) AS cnt, MIN(ts_ms) AS f, MAX(ts_ms) AS l,
                   MIN(value) AS mn, MAX(value) AS mx,
                   SUM(value) AS sy, SUM(value * value) AS syy,
                   SUM(x) AS sx, SUM(x * x) AS sxx, SUM(x * value) AS sxy
            FROM (SELECT series_id, ts_ms, value, bucket,
                         (ts_ms - bucket) / 1000.0 AS x FROM ({keyed}))
            GROUP BY series_id, bucket
        ) g
    '''


def _series_threshold(conn, sid: int) -> Optional[float]:
    row = conn.execute('SELECT t.interval FROM series se JOIN tasks t ON t.task_id = se.task_id '
                       'WHERE se.series_id = ?', (sid,)).fetchone()
    return gap_threshold(row[0]) if row is not None else None


def _rebuild_one(conn, sid: int, threshold: Optional[float]) -> None:
    """整体重算一条序列（调用方已删除其旧的汇总与空洞行）：空洞用 LAG，桶键用段起点的累计最大值"""
    if threshold is not None:
        conn.execute('''
            INSERT INTO series_gaps (series_id, start_ms, end_ms)
            SELECT ?1, prev, ts_ms FROM (
                SELECT ts_ms, LAG(ts_ms) OVER (ORDER BY ts_ms) AS prev
                FROM samples WHERE series_id = ?1
            )
            WHERE prev IS NOT NULL AND (ts_ms - prev) / 1000.0 > ?2
        ''', (sid, threshold))
    # 段起点 = 至今最后一个"首样本或空洞后首样本"的时间
    start_flag = ('prev IS NULL' if threshold is None
                  else 'prev IS NULL OR (ts_ms - prev) / 1000.0 > ?2')
    keyed = f'''
        SELECT ?1 AS series_id, ts_ms, value, MAX({floor_sql('ts_ms')}, seg) AS bucket FROM (
            SELECT ts_ms, value,
                   MAX(CASE WHEN {start_flag} THEN ts_ms END)
                       OVER (ORDER BY ts_ms ROWS UNBOUNDED PRECEDING) AS seg
            FROM (
                SELECT ts_ms, value, LAG(ts_ms) OVER (ORDER BY ts_ms) AS prev
                FROM samples WHERE series_id = ?1
            )
        )
    '''
    params = (sid,) if threshold is None else (sid, threshold)
    conn.execute(_keyed_aggregate_sql(keyed), params)


def rebuild_rollups(conn, series_id: Optional[int] = None,
                    series_ids: Optional[Iterable[int]] = None) -> int:
    """
    从 samples 整体重算 rollup_1m 与 series_gaps（调用方已开启事务）。空洞阈值取所属任务的
    interval；任务不存在（孤儿序列）或 interval 不是正数时不记空洞。

    Args:
        series_id: 只重算这一条序列
        series_ids: 只重算这些序列（与 series_id 二选一；都不给时重算全部）

    Returns:
        int: 重算的序列数
    """
    if series_id is not None:
        ids = [series_id]
    elif series_ids is not None:
        ids = sorted(set(series_ids))
    else:
        ids = None
    if ids is None:
        conn.execute('DELETE FROM rollup_1m')
        conn.execute('DELETE FROM series_gaps')
        rows = conn.execute('''
            SELECT se.series_id, t.interval FROM series se
            LEFT JOIN tasks t ON t.task_id = se.task_id ORDER BY se.series_id
        ''').fetchall()
        targets = [(sid, gap_threshold(interval)) for sid, interval in rows]
    else:
        targets = []
        for sid in ids:
            conn.execute('DELETE FROM rollup_1m WHERE series_id = ?', (sid,))
            conn.execute('DELETE FROM series_gaps WHERE series_id = ?', (sid,))
            targets.append((sid, _series_threshold(conn, sid)))
    for sid, threshold in targets:
        _rebuild_one(conn, sid, threshold)
    return len(targets)


def recompute_window(conn, sid: int, lo_ms: int, hi_ms: int) -> None:
    """
    按样本重算一条序列在整分钟对齐的 [lo_ms, hi_ms) 内的汇总行（series_gaps 须已正确）。
    窗口内样本的桶键都落在窗口内、窗口内的桶也只含窗口内的样本，因此先删后插即可。
    段起点按主键回查：最后一个起点早于该样本的空洞的 end_ms，没有则为序列首样本。
    """
    conn.execute('DELETE FROM rollup_1m WHERE series_id = ? AND bucket_ms >= ? AND bucket_ms < ?',
                 (sid, lo_ms, hi_ms))
    keyed = f'''
        SELECT ?1 AS series_id, s.ts_ms, s.value,
               MAX({floor_sql('s.ts_ms')}, COALESCE(
                   (SELECT g.end_ms FROM series_gaps g
                    WHERE g.series_id = ?1 AND g.start_ms < s.ts_ms
                    ORDER BY g.start_ms DESC LIMIT 1),
                   (SELECT MIN(ts_ms) FROM samples WHERE series_id = ?1))) AS bucket
        FROM samples s WHERE s.series_id = ?1 AND s.ts_ms >= ?2 AND s.ts_ms < ?3
    '''
    conn.execute(_keyed_aggregate_sql(keyed), (sid, lo_ms, hi_ms))


def repair_gaps(conn, sid: int, threshold: Optional[float], lo_ms: int, hi_ms: int
                ) -> Tuple[int, int]:
    """
    乱序插入了时间在 [lo_ms, hi_ms] 内的新样本后，按样本重判这条序列受影响的空洞：
    p = lo_ms 之前最后一个样本、n = hi_ms 之后第一个样本，只有 [p, n] 内相邻样本的空洞可能变化。
    空洞与段起点的变化都在 [p, n] 内，而段起点只影响它所在那一分钟的桶键，所以返回的
    "p 所在分钟起点到 n 所在分钟终点"之外的汇总行不受影响，调用方对该窗口 recompute_window。

    Returns:
        需要重算的汇总窗口 [起, 止)（整分钟）
    """
    p = conn.execute('SELECT MAX(ts_ms) FROM samples WHERE series_id = ? AND ts_ms < ?',
                     (sid, lo_ms)).fetchone()[0]
    n = conn.execute('SELECT MIN(ts_ms) FROM samples WHERE series_id = ? AND ts_ms > ?',
                     (sid, hi_ms)).fetchone()[0]
    start = p if p is not None else lo_ms
    end = n if n is not None else hi_ms
    conn.execute('DELETE FROM series_gaps WHERE series_id = ? AND start_ms >= ? AND start_ms < ?',
                 (sid, start, end))
    if threshold is not None:
        conn.execute('''
            INSERT INTO series_gaps (series_id, start_ms, end_ms)
            SELECT ?1, prev, ts_ms FROM (
                SELECT ts_ms, LAG(ts_ms) OVER (ORDER BY ts_ms) AS prev
                FROM samples WHERE series_id = ?1 AND ts_ms BETWEEN ?2 AND ?3
            )
            WHERE prev IS NOT NULL AND (ts_ms - prev) / 1000.0 > ?4
        ''', (sid, start, end, threshold))
    return minute_floor(start), minute_floor(end) + MINUTE_MS


def merge_windows(windows: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """合并重叠或相接的 [起, 止) 窗口"""
    merged: List[List[int]] = []
    for lo, hi in sorted(windows):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]
