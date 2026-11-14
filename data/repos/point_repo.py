"""
数据点仓储：samples 表的写入、范围查询、流式读取，以及采样计数的维护

v5 起样本存在紧凑表里：series(series_id, task_id, metric) 给每个 (任务, 指标) 一个短键，
samples(series_id, ts_ms, value) 以 (series_id, ts_ms) 为主键（WITHOUT ROWID），ts_ms 为
UTC 毫秒整数。对外接口仍是 DataPoint / PointQuery（datetime），毫秒换算只在仓储与
data.mappers 里出现。时间按 to_ms 取整到毫秒；同一序列同一毫秒只保留一行，后写的覆盖先写的。

tasks.sample_count / last_sample_at 是由样本派生的冗余列，只允许两个入口改它们：
- append：写样本的同一事务内累加（本批中该任务此前没有的不同时间戳个数）；
- recount / recount_all：按样本重算（修复旧版本写入、手工改库等造成的偏差）。
别处不得自行拼 SQL 维护这两列，否则口径会再次分叉。

分钟汇总 rollup_1m 与写时空洞 series_gaps 同样在 append 写样本的同一事务内维护（规则与
不变式见 data.repos.rollup）；其余改动样本的路径（导回、迁移、重建汇总）整体重算。
"""
import heapq
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Set, Tuple

from data.mappers import from_ms, to_iso, to_ms
from data.models import DataPoint
from data.repos import rollup, series_cache
from data.repos._guard import storage_errors
from data.repos._samples import fetch_packed, in_clause, ms_range, recount_task, task_series
from utils.metrics import is_member_series

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PointQuery:
    """
    数据点范围查询条件。

    - metric: None 表示该任务的全部指标；
    - since / until: 闭区间 [since, until]，None 表示该侧不限；只接受 datetime。
      样本时间为整数毫秒：since 向上取整、until 向下取整，与按真实时刻比较等价；
    - limit: 指定时取范围内"最近 limit 条"，结果仍按时间升序返回；None 表示不限。
    """
    task_id: str
    metric: Optional[str]
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    limit: Optional[int] = None

    def __post_init__(self) -> None:
        # 构造时就校验，错误出现在调用方的那一行，而不是查询执行时
        for name in ('since', 'until'):
            value = getattr(self, name)
            if value is not None:
                to_iso(value)
        if self.limit is not None and (
                isinstance(self.limit, bool) or not isinstance(self.limit, int)
                or self.limit < 1):
            raise ValueError(f"limit 必须是正整数或 None，收到 {self.limit!r}")

    def ms_range(self) -> Tuple[int, int]:
        """[since, until] 换算成的整数毫秒闭区间（不限的一侧为哨兵值）"""
        return ms_range(self.since, self.until)


class PointRepo:
    """samples 表仓储。所有方法遇 sqlite3.Error 抛 StorageError。"""

    def __init__(self, factory):
        self._factory = factory
        self._series = series_cache.SeriesCache()
        # 本实例累计的乱序写入样本数（早于所属序列最后一个样本的新样本；正常采集为 0）
        self.out_of_order_rows = 0

    # ========== 写入 ==========

    def _resolve_series(self, cursor, keys: Dict[Tuple[str, str], None]
                        ) -> Tuple[Dict[Tuple[str, str], int], int, Dict[Tuple[str, str], int]]:
        """
        在写事务内（已持有写锁）把 (task_id, 指标) 解析为 series_id，缺的新建。
        keys 按在本批中首次出现的顺序排列，新序列也按这个顺序建立（采集按任务的指标列表
        顺序写入，series_id 的先后因此与指标列表一致，同一毫秒多指标按 series_id 排列）。
        缓存在拿到写锁之后才读：删除 series 的事务提交前已递增缓存代号，这里看到的
        要么是新代号（缓存已清空），要么删除尚未发生。

        Returns:
            (全部映射, 读缓存时的代号, 本次从库里查到的映射)——后者在提交后写回缓存
        """
        gen, cached = self._series.snapshot()
        resolved: Dict[Tuple[str, str], int] = {}
        looked_up: Dict[Tuple[str, str], int] = {}
        for key in keys:
            sid = cached.get(key)
            if sid is None:
                cursor.execute('INSERT OR IGNORE INTO series (task_id, metric) VALUES (?, ?)',
                               key)
                sid = cursor.execute(
                    'SELECT series_id FROM series WHERE task_id = ? AND metric = ?',
                    key).fetchone()[0]
                looked_up[key] = sid
            resolved[key] = sid
        return resolved, gen, looked_up

    def append(self, points: List[DataPoint]) -> None:
        """
        批量写入样本，并在同一事务内更新所属任务的 sample_count / last_sample_at：
        sample_count 增加本批中该任务此前没有样本的不同时间戳（毫秒）个数，
        last_sample_at 取本批最大时间戳与既有值中较大者。任一步失败整体回滚，
        计数不会与样本脱节。同一序列同一毫秒已有样本时覆盖其值。
        同一事务内维护分钟汇总与空洞（见 _maintain_rollups）。
        """
        if not points:
            return
        # 先在事务外完成全部编码：时间类型不对时直接 TypeError，不开事务
        ms_of: Dict[datetime, int] = {}
        encoded: List[Tuple[str, str, int, float]] = []
        for dp in points:
            ms = ms_of.get(dp.timestamp) if isinstance(dp.timestamp, datetime) else None
            if ms is None:
                ms = to_ms(dp.timestamp)
                ms_of[dp.timestamp] = ms
            encoded.append((dp.task_id, dp.metric_type or '', ms, dp.value))
        stamps: Dict[str, Set[int]] = {}
        for task_id, _metric, ms, _value in encoded:
            stamps.setdefault(task_id, set()).add(ms)
        keys = dict.fromkeys((task_id, metric) for task_id, metric, _ms, _v in encoded)

        with storage_errors(f"写入数据点（{len(encoded)} 条）"):
            with self._factory.transaction() as cursor:
                conn = cursor.connection
                ids, gen, looked_up = self._resolve_series(cursor, keys)
                # 同一 (序列, 毫秒) 只留本批最后写的值（与 INSERT OR REPLACE 的结果相同），
                # 按 (series_id, ts_ms) 排序后写入：页内更紧凑，汇总按时间顺序累加
                latest: Dict[Tuple[int, int], float] = {}
                for t, m, ms, v in encoded:
                    latest[(ids[(t, m)], ms)] = v
                rows = sorted(latest.items())
                states: Dict[int, rollup.SeriesState] = {}
                thresholds: Dict[int, Optional[float]] = {}
                new_counts: Dict[str, int] = {}
                for task_id, task_stamps in stamps.items():
                    threshold, task_states = rollup.load_task_state(conn, task_id)
                    for sid, state in task_states.items():
                        states[sid] = state
                        thresholds[sid] = threshold
                    task_last = max((s.last_ms for s in task_states.values()
                                     if s.last_ms is not None), default=None)
                    # 常态：本批时间全部晚于该任务已有的最后一个样本，不必再查重叠
                    new_counts[task_id] = len(task_stamps) - (
                        0 if task_last is None or min(task_stamps) > task_last
                        else self._existing_count(cursor, task_id, task_stamps))
                late = self._classify_late(conn, rows, states)
                cursor.executemany(
                    'INSERT OR REPLACE INTO samples (series_id, ts_ms, value) VALUES (?, ?, ?)',
                    [(sid, ms, v) for (sid, ms), v in rows])
                self._maintain_rollups(conn, rows, states, thresholds, late)
                for task_id, task_stamps in stamps.items():
                    latest = from_ms(max(task_stamps)).isoformat()
                    cursor.execute('''
                        UPDATE tasks
                        SET sample_count = sample_count + ?,
                            last_sample_at = CASE
                                WHEN last_sample_at IS NULL OR last_sample_at < ? THEN ?
                                ELSE last_sample_at END
                        WHERE task_id = ?
                    ''', (new_counts[task_id], latest, latest, task_id))
        self._series.put_all(gen, looked_up)

    @staticmethod
    def _classify_late(conn, rows, states: Dict[int, "rollup.SeriesState"]
                       ) -> Dict[int, Tuple[Set[int], Set[int]]]:
        """
        找出不晚于所属序列最后一个样本的行（须在写 samples 之前调用）：
        {series_id: (覆盖已有样本的毫秒集合, 乱序新插入的毫秒集合)}。常态下为空字典。
        """
        late: Dict[int, Tuple[Set[int], Set[int]]] = {}
        for (sid, ms), _v in rows:
            last = states[sid].last_ms
            if last is None or ms > last:
                continue
            overwrite, inserted = late.setdefault(sid, (set(), set()))
            exists = ms == last or conn.execute(
                'SELECT 1 FROM samples WHERE series_id = ? AND ts_ms = ?', (sid, ms)).fetchone()
            (overwrite if exists else inserted).add(ms)
        return late

    def _maintain_rollups(self, conn, rows, states: Dict[int, "rollup.SeriesState"],
                          thresholds: Dict[int, Optional[float]],
                          late: Dict[int, Tuple[Set[int], Set[int]]]) -> None:
        """
        在写样本的同一事务内维护 rollup_1m / series_gaps（规则见 data.repos.rollup）：
        晚于序列最后一个样本的行逐行判空洞、按 (序列, 桶) 合并后每桶一条 UPSERT；
        覆盖已有样本的分钟、乱序插入影响的区间按样本重算。须在 samples 写入之后调用。
        """
        gaps: List[tuple] = []
        upserts: List[tuple] = []
        tail: List[Tuple[int, float]] = []
        current = None
        for (sid, ms), value in rows:
            if sid != current:
                if tail:
                    rollup.append_in_order(states[current], tail, thresholds[current],
                                           current, gaps, upserts)
                    tail = []
                current = sid
                last = states[sid].last_ms
            if last is None or ms > last:
                tail.append((ms, value))
        if tail:
            rollup.append_in_order(states[current], tail, thresholds[current],
                                   current, gaps, upserts)
        if gaps:
            conn.executemany(rollup.GAP_INSERT_SQL, gaps)
        if upserts:
            conn.executemany(rollup.UPSERT_SQL, upserts)
        if not late:
            return
        inserted_total = 0
        for sid, (overwrite, inserted) in late.items():
            windows = [(rollup.minute_floor(ms), rollup.minute_floor(ms) + rollup.MINUTE_MS)
                       for ms in overwrite]
            if inserted:
                inserted_total += len(inserted)
                windows.append(rollup.repair_gaps(conn, sid, thresholds[sid],
                                                  min(inserted), max(inserted)))
            for lo, hi in rollup.merge_windows(windows):
                rollup.recompute_window(conn, sid, lo, hi)
        if inserted_total:
            self.out_of_order_rows += inserted_total
            logger.warning("乱序写入 %d 个样本（早于所属序列已有的最后一个样本），"
                           "已按受影响区间重算汇总与空洞；累计 %d 个",
                           inserted_total, self.out_of_order_rows)

    @staticmethod
    def _existing_count(cursor, task_id: str, task_stamps: Set[int]) -> int:
        """本批时间戳里该任务已有样本（任一指标）的个数；按时间顺序追加时范围内为空，极快"""
        rows = cursor.execute('''
            SELECT DISTINCT ts_ms FROM samples
            WHERE series_id IN (SELECT series_id FROM series WHERE task_id = ?)
              AND ts_ms BETWEEN ? AND ?
        ''', (task_id, min(task_stamps), max(task_stamps))).fetchall()
        if not rows:
            return 0
        return len(task_stamps.intersection(row[0] for row in rows))

    # ========== 读取 ==========

    def fetch(self, query: PointQuery) -> List[DataPoint]:
        """按 PointQuery 取数据点，始终按时间升序返回（同一时刻按序列建立顺序）。"""
        lo, hi = query.ms_range()
        with storage_errors(f"读取数据点 {query.task_id} "):
            with self._factory.connect() as conn:
                series = task_series(conn, query.task_id, query.metric)
                if not series:
                    return []
                metric_of = dict(series)
                where, params = in_clause(metric_of)
                params = [*params, lo, hi]
                if query.limit:
                    # 先倒序取最近 N 条，再在 Python 里排回升序，调用方无需自行 reversed()；
                    # 行数有上限，整包取回（见 fetch_packed：多线程并行查询时避免逐行交接 GIL）
                    rows = fetch_packed(conn, f'''
                        SELECT ts_ms AS c0, series_id AS c1, value AS c2 FROM samples
                        WHERE {where} AND ts_ms BETWEEN ? AND ?
                        ORDER BY ts_ms DESC, series_id DESC
                        LIMIT ?
                    ''', [*params, query.limit], 'iif')
                    rows = [(sid, ms, value) for ms, sid, value in sorted(rows)]
                else:
                    rows = conn.execute(f'''
                        SELECT series_id, ts_ms, value FROM samples
                        WHERE {where} AND ts_ms BETWEEN ? AND ?
                        ORDER BY ts_ms ASC, series_id ASC
                    ''', params).fetchall()
        task_id = query.task_id
        return [DataPoint(task_id, from_ms(ms), value, metric_of[sid])
                for sid, ms, value in rows]

    def iter_batches(self, task_id: str, batch_size: int = 5000, *,
                     include_members: bool = True,
                     since: Optional[datetime] = None,
                     until: Optional[datetime] = None) -> Iterator[List[DataPoint]]:
        """
        按时间升序分批流式产出任务的全部数据点（每个序列一个有序游标，按 (时间, 序列号)
        归并），避免一次性载入内存。同一时刻的多个指标相邻产出、按序列建立顺序排列。
        每批非空；无数据时不产出任何批。提前关闭生成器时连接随之关闭。

        include_members 为 False 时不读进程组的成员序列（指标键带 "@"），只产出合计
        序列：明细导出只要合计，成员序列的行数是合计的几十倍，不必读出来再丢掉。
        since / until 为闭区间边界；报告导出据此锁定界面已分析的快照，避免读进之后写入的数据。
        """
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError(f"batch_size 必须是正整数，收到 {batch_size!r}")
        # 参数校验放在生成器外，调用时立即报错，而不是等到第一次 next()
        lo, hi = ms_range(since, until)
        return self._iter_batches(task_id, batch_size, include_members, lo, hi)

    def _iter_batches(self, task_id: str, batch_size: int,
                      include_members: bool, lo: int, hi: int) -> Iterator[List[DataPoint]]:
        with storage_errors(f"流式读取数据点 {task_id} "):
            with self._factory.connect() as conn:
                series = task_series(conn, task_id)
                if not include_members:
                    series = [(sid, metric) for sid, metric in series
                              if not is_member_series(metric)]
                metric_of = dict(series)
                cursors = []
                for sid, _metric in series:
                    # 归并要比较元组，游标不能产出 sqlite3.Row
                    cursor = conn.cursor()
                    cursor.row_factory = None
                    cursor.execute('SELECT ts_ms, series_id, value FROM samples '
                                   'WHERE series_id = ? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms',
                                   (sid, lo, hi))
                    cursors.append(cursor)
                batch: List[DataPoint] = []
                for ms, sid, value in heapq.merge(*cursors):
                    batch.append(DataPoint(task_id, from_ms(ms), value, metric_of[sid]))
                    if len(batch) >= batch_size:
                        yield batch
                        batch = []
                if batch:
                    yield batch

    # ========== 计数重算 ==========

    def recount(self, task_id: str) -> int:
        """
        按样本重算一个任务的 sample_count（不同时间戳个数）与 last_sample_at（最大时间戳）。
        无样本时置为 0 / NULL。

        Returns:
            int: 重算后的 sample_count
        """
        with storage_errors(f"重算采样计数 {task_id} "):
            with self._factory.transaction() as cursor:
                return recount_task(cursor.connection, task_id)

    def recount_all(self) -> int:
        """
        启动校正：只重算 sample_count=0 却存在样本的任务（例如回退 1.4.1 后它对已有任务
        INSERT OR REPLACE 把计数清零；或手工改库）。存在性按任务的序列逐个走主键前缀，
        不扫全表；正常任务与无数据任务不受影响。必须在任何新任务启动前调用一次。

        Returns:
            int: 被校正的任务条数
        """
        with storage_errors("校正采样计数"):
            with self._factory.transaction() as cursor:
                cursor.execute('''
                    SELECT task_id FROM tasks
                    WHERE sample_count = 0
                      AND EXISTS (
                          SELECT 1 FROM series se
                          WHERE se.task_id = tasks.task_id
                            AND EXISTS (SELECT 1 FROM samples s
                                        WHERE s.series_id = se.series_id))
                ''')
                task_ids = [row[0] for row in cursor.fetchall()]
                for task_id in task_ids:
                    recount_task(cursor.connection, task_id)
        if task_ids:
            logger.warning("采样计数校正: %d 条任务的 sample_count 由样本重算", len(task_ids))
        return len(task_ids)
