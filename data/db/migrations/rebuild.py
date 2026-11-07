"""
v0–v4 → v5 重建式迁移，以及 1.4.1 降级期间写入的 data_points 导回

重建式迁移不在旧文件上改结构，而是新建 <库>.migrating，用 create_current 建 v5 结构，
ATTACH 旧库（只读、immutable）后用 SELECT 把数据搬进来；成功后旧文件改名为备份、新文件
改名为库（改名由 MigrationRunner 负责）。旧文件全程不被写入（WAL 库只做一次 checkpoint），
因此它本身就是逐字节可信的备份，磁盘峰值约为旧库 + 新库。

数据转换口径与冻结的就地迁移链 v1–v4 逐项一致（由 tests/test_rebuild_migration.py 对 golden
样本比对"直接重建"与"就地链到 v4 再重建"守护）：
- tasks：v0 的裸指标名转 JSON 数组（v1）；v0/v1 的 memory_vms / memory_pagefile 在指标列表中
  改名为 memory_private 并去重（v2，列表不变时保留原文本）；缺失的 note 取 ''、create_time 取
  NULL；sample_count / last_sample_at 搬完样本后按样本重算；
- 样本：(task_id, 指标) 建 series；v0 没有 metric_type 列，取所属任务的原指标名（v1）；
  v0/v1 同一 (task_id, timestamp) 下的三项重复内存指标只保留 id 最小的一行再改名（v2）；
  仍为 NULL 的指标回填为所属任务指标列表的首项（v3，列表为空时记为空串指标）；
  任务不存在的孤儿数据点丢弃（v3）；ISO 文本换算为 UTC 毫秒（与 data.mappers.to_ms 一致）；
  时间戳为 NULL 或无法解析的行跳过并计数（记 WARNING，写进 RebuildStats.skipped_rows）——这类
  确定性的数据错误不能让迁移整体失败，否则每次启动都会失败；
  同一序列同一毫秒有多行时保留 id 最大（后写）的一行；
- task_markers 原样搬（v3 之前没有该表；v3 没有 kind 列时取 'user'）；
- 样本搬完后按 data.repos.rollup 的规则整体生成分钟汇总与空洞（逐序列各两条 SQL：空洞用 LAG，
  汇总用段起点的累计最大值定桶键后 GROUP BY，不在 Python 里逐行处理），按序列数回调进度；
- user_version 建表时先置 0，全部数据提交并落盘后才写成 5：迁移中途崩溃留下的 .migrating
  不会被当成完整的新库（换库中断的恢复据此判断，见 runner 模块说明第 6 步）。

导回（import_legacy_points）：用户回退到 1.4.1 后，1.4.1 只认识 data_points，降级期间的样本
写在这张空表里；再用 2.x 启动时按与 v1 源库相同的规则（合并旧内存指标键、回填 NULL 指标、
丢弃孤儿）搬进 samples，清空 data_points，并重算受影响任务的采样计数与这些任务全部序列的
分钟汇总、空洞（导回的样本可能早于已有样本，增量维护不适用）。

别名与版本号用字面量写死，不引用会演进的指标常量。
"""
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.request import pathname2url

from data.db.migrations._frozen import parse_metric_types
from data.db.schema import create_current, write_version
from data.mappers import to_ms
from data.repos import rollup
from data.repos._samples import recount_task

logger = logging.getLogger(__name__)

VERSION = 5
NAME = '紧凑样本表（重建式迁移）'

# 进度回调：(阶段名, 已完成行数, 总行数)
ProgressCallback = Callable[[str, int, int], None]
STAGE_SAMPLES = 'samples'
# 生成分钟汇总阶段：(STAGE_ROLLUPS, 已完成序列数, 序列总数)
STAGE_ROLLUPS = 'rollups'
# 搬样本时每批的行数（每批回调一次进度）
CHUNK_ROWS = 50_000

_LEGACY_MEMORY = {'memory_vms': 'memory_private', 'memory_pagefile': 'memory_private'}
_MEMORY_GROUP_SQL = "('memory_private', 'memory_vms', 'memory_pagefile')"
# 映射表里代表"metric_type 为 NULL"的键（JOIN 不能用 NULL 做等值匹配；
# 会拼进 SQL 字面量，不能含 NUL 或引号）
_NULL_KEY = '\x01null'
# 换算 ISO 文本的 Python 兜底函数在 SQLite 里的名字
_PY_TO_MS = 'pm_iso_to_ms'
# 日志里列出的无效时间戳样例个数
_BAD_EXAMPLES = 5
# 快路径核对时逐年抽样的最大年数（防御异常数据导致的超长循环）
_MAX_PROBE_YEARS = 200


def sqlite_uri(path: str, **params: str) -> str:
    """本地路径 → SQLite URI 文件名（含中文、空格、#、% 的路径按百分号编码）"""
    uri = 'file:' + pathname2url(os.path.abspath(path))
    if params:
        uri += '?' + '&'.join(f'{k}={v}' for k, v in params.items())
    return uri


def merge_metric_list(metrics: Sequence[str]) -> List[str]:
    """v2 口径：旧内存指标键改名为 memory_private，保持原顺序去重"""
    merged: List[str] = []
    for metric in metrics:
        metric = _LEGACY_MEMORY.get(metric, metric)
        if metric not in merged:
            merged.append(metric)
    return merged


def convert_metric_text(text: str, from_version: int) -> str:
    """把旧库 tasks.metric_type 的文本转换为 v5 口径（就地链 v1、v2 两步的合成）"""
    if from_version < 1 and not (text or '').startswith('['):
        text = json.dumps([text])
    if from_version < 2:
        old = parse_metric_types(text)
        new = merge_metric_list(old)
        if new != old:
            text = json.dumps(new)
    return text


# ---------------------------------------------------------------- 时间换算


def _fast_ms_sql(col: str, offset_ms: int) -> str:
    """
    标准 ISO 文本（'YYYY-MM-DDTHH:MM:SS' 或其后带 '.ffffff'）的纯 SQL 换算：
    整秒部分用 julianday 按 UTC 解释（整秒的儒略日双精度误差约 5e-5 秒，ROUND 后精确），
    微秒部分按四舍五入到毫秒（0.5 毫秒进位，与 to_ms 相同），再减去本地时区偏移。
    """
    return (
        f"(CAST(ROUND((julianday(substr({col}, 1, 19)) - 2440587.5) * 86400.0) AS INTEGER)"
        f" * 1000 + CASE WHEN length({col}) = 26"
        f" THEN (CAST(substr({col}, 21, 6) AS INTEGER) + 500) / 1000 ELSE 0 END"
        f" - {int(offset_ms)})")


class MsConverter:
    """
    在给定连接上提供 "ISO 文本 → UTC 毫秒" 的 SQL 表达式。

    本机时区没有夏令时（time.daylight == 0）且用抽样时间核对过固定偏移时走纯 SQL 快路径；
    否则逐行调用注册的 Python 函数（与 to_ms 同一实现）。快路径里不是标准形态的文本
    （长度不是 19/26、小数点或数字位不对）、或 julianday 解析失败的，同样逐行交给 Python。
    核对用的抽样（probe_texts）由调用方给出：首行、末行与每个自然年各一行（见 probe_texts
    函数），这里再加上当前时间；任一处偏移不一致即走 Python 路径。

    NULL 或无法解析的文本换算为 NULL（不抛异常），由 skipped 计数；写 samples 时这些行因
    ts_ms 的 NOT NULL 约束被 OR IGNORE 跳过。
    """

    def __init__(self, conn: sqlite3.Connection, probe_texts: Iterable[Optional[str]]):
        conn.create_function(_PY_TO_MS, 1, self._py_to_ms, deterministic=True)
        self.offset_ms: Optional[int] = None
        self.fast = False
        self.skipped = 0
        self.bad_examples: List[object] = []
        if time.daylight != 0:
            return
        texts = [t for t in probe_texts if t]
        texts.append(datetime.now().replace(microsecond=0).isoformat())
        try:
            offset = None
            for text in texts:
                try:
                    dt = datetime.fromisoformat(text)
                except (TypeError, ValueError):
                    continue    # 无效样本不参与核对（换算时被跳过并计数）
                if dt.tzinfo is not None:
                    continue
                as_utc = conn.execute(f'SELECT {_fast_ms_sql("?1", 0)}', (text,)).fetchone()[0]
                if as_utc is None:
                    continue
                this = as_utc - to_ms(dt)
                if offset is None:
                    offset = this
                elif this != offset:
                    logger.info("本机时区偏移不固定（%s），时间换算改用逐行 Python 路径", text)
                    return
            if offset is None:
                return
        except (ValueError, OverflowError, OSError, sqlite3.Error):
            logger.info("时间换算快路径核对失败，改用逐行 Python 路径", exc_info=True)
            return
        self.offset_ms = offset
        self.fast = True

    def _py_to_ms(self, text) -> Optional[int]:
        """Python 兜底换算；NULL 或无法解析时返回 NULL 并计数"""
        try:
            return to_ms(datetime.fromisoformat(text))
        except (TypeError, ValueError, OverflowError, OSError):
            self.skipped += 1
            if len(self.bad_examples) < _BAD_EXAMPLES:
                self.bad_examples.append(text)
            return None

    def expr(self, col: str) -> str:
        if not self.fast:
            return f'{_PY_TO_MS}({col})'
        fast = _fast_ms_sql(col, self.offset_ms)
        standard = (f"(length({col}) = 19 OR (length({col}) = 26 AND substr({col}, 20, 1) = '.'"
                    f" AND substr({col}, 21, 6) GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'))")
        return (f"(CASE WHEN {standard} THEN COALESCE({fast}, {_PY_TO_MS}({col}))"
                f" ELSE {_PY_TO_MS}({col}) END)")


def probe_texts(conn: sqlite3.Connection, points: str) -> List[Optional[str]]:
    """
    快路径核对用的时间抽样：首行、末行，以及首末之间每个自然年的第一行。

    逐年那一行按 id 二分查找"时间文本 >= 该年"的第一行（数据点按 id 大致按时间写入，每年
    约 log2(行数) 次主键查询，不扫表）；找到的行不在该年（id 与时间不单调）时不取。
    只看首末两行会漏掉中间某年时区规则的变化（如某年取消了夏令时）。
    """
    first, last, lo_id, hi_id = conn.execute(
        f'SELECT (SELECT timestamp FROM {points} ORDER BY id ASC LIMIT 1), '
        f'(SELECT timestamp FROM {points} ORDER BY id DESC LIMIT 1), '
        f'(SELECT MIN(id) FROM {points}), (SELECT MAX(id) FROM {points})').fetchone()
    texts: List[Optional[str]] = [first, last]
    years = [int(t[:4]) for t in (first, last)
             if isinstance(t, str) and len(t) >= 4 and t[:4].isdigit()]
    if len(years) < 2 or lo_id is None:
        return texts
    start, end = min(years), max(years)
    for year in range(start + 1, min(end, start + _MAX_PROBE_YEARS) + 1):
        key = f'{year:04d}'
        lo, hi = lo_id, hi_id
        found = None
        while lo <= hi:
            mid = (lo + hi) // 2
            row = conn.execute(f'SELECT id, timestamp FROM {points} WHERE id >= ? '
                               f'ORDER BY id LIMIT 1', (mid,)).fetchone()
            if row is None:
                hi = mid - 1
            elif isinstance(row[1], str) and row[1] >= key:
                found = row[1]
                hi = mid - 1
            else:
                lo = row[0] + 1
        if found is not None and found.startswith(key):
            texts.append(found)
    return texts


# ---------------------------------------------------------------- 样本搬迁


@dataclass
class PointCopyResult:
    rows_read: int = 0                      # 源表中参与搬迁的行数（含被丢弃的孤儿、重复）
    affected_tasks: Set[str] = field(default_factory=set)   # 有样本搬入的任务
    skipped_rows: int = 0                   # 时间戳为 NULL 或无法解析而跳过的行数


def _final_metric(raw: Optional[str], metrics: List[str], merge: bool,
                  fill: bool) -> str:
    metric = raw
    if merge and metric is not None:
        metric = _LEGACY_MEMORY.get(metric, metric)
    if metric is None and fill and metrics:
        metric = metrics[0]
    return metric if metric is not None else ''


def copy_points(conn: sqlite3.Connection, src: str, *, has_metric_column: bool,
                merge: bool, fill: bool, task_metrics: Dict[str, List[str]],
                task_raw_metric: Optional[Dict[str, str]] = None,
                on_progress: Optional[ProgressCallback] = None,
                chunk_rows: int = CHUNK_ROWS) -> PointCopyResult:
    """
    把 <src>.data_points 搬进 main.samples（调用方已开启事务）。

    Args:
        src: 源库别名（重建时为 ATTACH 的 'src'，导回时为 'main'）
        has_metric_column: 源表是否有 metric_type 列（v0 没有）
        merge: 是否按 v2 口径去重并合并旧内存指标键
        fill: 是否按 v3 口径回填 NULL 指标
        task_metrics: 目标库中每个任务（已转换后）的指标列表；不在其中的任务视为孤儿
        task_raw_metric: v0 源库每个任务的原始裸指标名（has_metric_column=False 时必填）
    """
    result = PointCopyResult()
    points = f'{src}.data_points'

    # 1. (任务, 原始指标键) → series_id 映射表
    if has_metric_column:
        pairs = conn.execute(f'SELECT DISTINCT task_id, metric_type FROM {points}').fetchall()
    else:
        raw_map = task_raw_metric or {}
        pairs = [(row[0], raw_map.get(row[0]))
                 for row in conn.execute(f'SELECT DISTINCT task_id FROM {points}')]
    mapping: List[Tuple[str, Optional[str], str]] = []
    for task_id, raw in pairs:
        if task_id not in task_metrics:
            continue    # 孤儿：内连接自然丢弃
        mapping.append((task_id, raw, _final_metric(raw, task_metrics[task_id], merge, fill)))
    series_ids: Dict[Tuple[str, str], int] = {}

    def _series_order(key: Tuple[str, str]) -> Tuple[str, int, str]:
        # 同一任务按其指标列表顺序建序列：series_id 的先后与 v4 里采集写入的先后一致，
        # 同一毫秒多指标的排列（按 series_id）也就与 v4 按 id 的排列一致
        task_id, metric = key
        metrics = task_metrics[task_id]
        return (task_id, metrics.index(metric) if metric in metrics else len(metrics), metric)

    for task_id, metric in sorted({(t, m) for t, _r, m in mapping}, key=_series_order):
        conn.execute('INSERT OR IGNORE INTO main.series (task_id, metric) VALUES (?, ?)',
                     (task_id, metric))
        series_ids[(task_id, metric)] = conn.execute(
            'SELECT series_id FROM main.series WHERE task_id = ? AND metric = ?',
            (task_id, metric)).fetchone()[0]
    conn.execute('DROP TABLE IF EXISTS temp.pm_map')
    conn.execute('''
        CREATE TEMP TABLE pm_map (
            task_id TEXT NOT NULL, raw TEXT NOT NULL, series_id INTEGER NOT NULL,
            PRIMARY KEY(task_id, raw)
        ) WITHOUT ROWID
    ''')
    conn.executemany('INSERT INTO temp.pm_map VALUES (?, ?, ?)', [
        (task_id, _NULL_KEY if raw is None else raw, series_ids[(task_id, metric)])
        for task_id, raw, metric in mapping])
    result.affected_tasks = {task_id for task_id, _r, _m in mapping}

    if has_metric_column:
        join = (f'{points} d CROSS JOIN temp.pm_map m '
                f"ON m.task_id = d.task_id AND m.raw = COALESCE(d.metric_type, '{_NULL_KEY}')")
        raw_expr = 'd.metric_type'
    else:
        join = f'{points} d CROSS JOIN temp.pm_map m ON m.task_id = d.task_id'
        raw_expr = f"NULLIF(m.raw, '{_NULL_KEY}')"

    # 2. v2 去重：同一 (task_id, timestamp) 的三项内存指标只保留 id 最小的一行
    keep = ''
    if merge:
        conn.execute('DROP TABLE IF EXISTS temp.pm_keep')
        conn.execute('CREATE TEMP TABLE pm_keep (id INTEGER PRIMARY KEY)')
        if has_metric_column:
            conn.execute(f'''
                INSERT INTO temp.pm_keep
                SELECT MIN(id) FROM {points}
                WHERE metric_type IN {_MEMORY_GROUP_SQL}
                GROUP BY task_id, timestamp
            ''')
        else:
            conn.execute(f'''
                INSERT INTO temp.pm_keep
                SELECT MIN(d.id) FROM {join}
                WHERE m.raw IN {_MEMORY_GROUP_SQL}
                GROUP BY d.task_id, d.timestamp
            ''')
        keep = (f' AND ({raw_expr} IS NULL OR {raw_expr} NOT IN {_MEMORY_GROUP_SQL}'
                f' OR d.id IN (SELECT id FROM temp.pm_keep))')

    # 3. 按 id 分批搬迁：批内与批间都按 id 升序写入，ON CONFLICT 覆盖使同一毫秒保留后写的行；
    # OR IGNORE 只作用于 UPSERT 之外的约束：换算为 NULL 的行违反 ts_ms 的 NOT NULL，被跳过
    converter = MsConverter(conn, probe_texts(conn, points))
    total = conn.execute(f'SELECT COUNT(*) FROM {points}').fetchone()[0]
    insert_sql = f'''
        INSERT OR IGNORE INTO main.samples (series_id, ts_ms, value)
        SELECT m.series_id, {converter.expr('d.timestamp')}, d.value
        FROM {join}
        WHERE d.id > ? AND d.id <= ?{keep}
        ORDER BY d.id
        ON CONFLICT (series_id, ts_ms) DO UPDATE SET value = excluded.value
    '''
    boundary_sql = f'SELECT id FROM {points} WHERE id > ? ORDER BY id LIMIT 1 OFFSET ?'
    last_id = -(1 << 62)
    done = 0
    while done < total:
        row = conn.execute(boundary_sql, (last_id, chunk_rows - 1)).fetchone()
        high = row[0] if row is not None else (1 << 62)
        conn.execute(insert_sql, (last_id, high))
        last_id = high
        done = min(total, done + chunk_rows) if row is not None else total
        if on_progress is not None:
            on_progress(STAGE_SAMPLES, done, total)
    result.rows_read = total
    result.skipped_rows = converter.skipped
    logger.info("样本搬迁完成: %d 行（时间换算%s）", total,
                '纯 SQL 快路径' if converter.fast else '逐行 Python 路径')
    if converter.skipped:
        logger.warning("跳过时间戳为 NULL 或无法解析的数据点 %d 行，样例: %r",
                       converter.skipped, converter.bad_examples)
    conn.execute('DROP TABLE IF EXISTS temp.pm_map')
    conn.execute('DROP TABLE IF EXISTS temp.pm_keep')
    return result


def recount_tasks(conn: sqlite3.Connection, task_ids: Optional[Iterable[str]] = None) -> None:
    """
    按 samples 重算任务的 sample_count（不同毫秒时间戳个数）与 last_sample_at（最大时间戳
    的 ISO 文本）；没有样本的任务置为 0 / NULL。task_ids 为 None 时重算全部任务。
    """
    if task_ids is None:
        task_ids = [row[0] for row in conn.execute('SELECT task_id FROM main.tasks')]
    for task_id in task_ids:
        recount_task(conn, task_id)


# ---------------------------------------------------------------- 重建


@dataclass(frozen=True)
class RebuildStats:
    tasks: int
    rows_read: int
    samples: int
    markers: int
    skipped_rows: int = 0   # 时间戳为 NULL 或无法解析而跳过的数据点行数


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> List[str]:
    return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info({table})')]


def _src_has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM src.sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _copy_tasks(conn: sqlite3.Connection, from_version: int) -> Dict[str, List[str]]:
    """搬 tasks 行并转换指标列表；返回 {task_id: 转换后的指标列表}"""
    columns = set(_columns(conn, 'src', 'tasks'))
    select = ['task_id', 'pid', 'process_name', 'metric_type', 'interval', 'start_time',
              'end_time', 'status',
              'note' if 'note' in columns else "''",
              'create_time' if 'create_time' in columns else 'NULL']
    rows = conn.execute(f'SELECT {", ".join(select)} FROM src.tasks ORDER BY rowid').fetchall()
    metrics: Dict[str, List[str]] = {}
    converted = []
    for row in rows:
        text = convert_metric_text(row[3], from_version)
        metrics[row[0]] = parse_metric_types(text)
        converted.append((*row[:3], text, *row[4:8], row[8] if row[8] is not None else '',
                          row[9]))
    conn.executemany('''
        INSERT INTO main.tasks (task_id, pid, process_name, metric_type, interval,
                                start_time, end_time, status, note, create_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', converted)
    return metrics


def _copy_markers(conn: sqlite3.Connection) -> int:
    if not _src_has_table(conn, 'task_markers'):
        return 0
    columns = set(_columns(conn, 'src', 'task_markers'))
    kind = 'kind' if 'kind' in columns else "'user'"
    return conn.execute(f'''
        INSERT INTO main.task_markers (id, task_id, timestamp, label, created_at, kind)
        SELECT id, task_id, timestamp, label, created_at, {kind} FROM src.task_markers
        ORDER BY id
    ''').rowcount


def rebuild_into(new_path: str, old_path: str, from_version: int, *,
                 app_version: Optional[str] = None,
                 on_progress: Optional[ProgressCallback] = None) -> RebuildStats:
    """
    在 new_path（必须不存在）建 v5 库并从 old_path（v0–v4，只读打开）搬入全部数据。

    旧库以 mode=ro&immutable=1 打开：不加锁、不建 -wal/-shm，旧文件不会被触碰。调用方须保证
    旧库已自包含（WAL 库已 checkpoint(TRUNCATE)）且迁移期间没有其他连接写它。
    user_version 建表时置 0，数据提交并 fsync 之后才写成 VERSION（完成标记），再切到 WAL
    并 fsync；失败时由调用方删除 new_path。

    on_progress：搬样本时每 CHUNK_ROWS 行一次 (STAGE_SAMPLES, 行, 总行)，生成汇总时每条
    序列一次 (STAGE_ROLLUPS, 序列, 总序列)。
    """
    if os.path.exists(new_path):
        raise FileExistsError(new_path)
    conn = sqlite3.connect(sqlite_uri(new_path, mode='rwc'), uri=True, isolation_level=None)
    try:
        # 新文件是临时产物，失败即整个删除，不需要日志与同步；成功后再统一 fsync
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA cache_size=-65536')
        conn.execute("ATTACH DATABASE ? AS src",
                     (sqlite_uri(old_path, mode='ro', immutable='1'),))
        conn.execute('BEGIN')
        create_current(conn.cursor(), app_version)
        write_version(conn.cursor(), 0)
        task_metrics = _copy_tasks(conn, from_version)
        raw_metric = None
        has_metric_column = 'metric_type' in _columns(conn, 'src', 'data_points')
        if not has_metric_column:
            raw_metric = dict(conn.execute('SELECT task_id, metric_type FROM src.tasks'))
        copied = copy_points(conn, 'src', has_metric_column=has_metric_column,
                             merge=from_version < 2, fill=from_version < 3,
                             task_metrics=task_metrics, task_raw_metric=raw_metric,
                             on_progress=on_progress)
        recount_tasks(conn)
        rollup_progress = None
        if on_progress is not None:
            def rollup_progress(done: int, total: int) -> None:
                on_progress(STAGE_ROLLUPS, done, total)
        rollup.rebuild_rollups(conn, on_progress=rollup_progress)
        markers = _copy_markers(conn)
        samples = conn.execute('SELECT COUNT(*) FROM main.samples').fetchone()[0]
        conn.execute('COMMIT')
        conn.execute('DETACH DATABASE src')
        _fsync_path(new_path)
        write_version(conn.cursor(), VERSION)
        conn.execute('PRAGMA journal_mode=WAL')
    finally:
        conn.close()
    _fsync_path(new_path)
    return RebuildStats(tasks=len(task_metrics), rows_read=copied.rows_read,
                        samples=samples, markers=markers, skipped_rows=copied.skipped_rows)


def _fsync_path(path: str) -> None:
    with open(path, 'rb+') as f:
        os.fsync(f.fileno())


# ---------------------------------------------------------------- 导回


def import_legacy_points(conn: sqlite3.Connection,
                         on_progress: Optional[ProgressCallback] = None
                         ) -> Tuple[int, Set[str], int]:
    """
    把 main.data_points（1.4.1 降级期间写入）导回 samples 并清空 data_points（调用方已开启事务）。

    同时把任务指标列表里的旧内存指标键改名（1.4.1 建的任务可能带 memory_vms 等），
    并重算受影响任务的采样计数。on_progress 同 rebuild_into（搬样本、生成汇总两个阶段）。

    Returns:
        (data_points 原有行数, 有样本导入的任务集合, 时间戳无效而跳过的行数)
    """
    total = conn.execute('SELECT COUNT(*) FROM main.data_points').fetchone()[0]
    if total == 0:
        return 0, set(), 0
    metrics: Dict[str, List[str]] = {}
    for task_id, text in conn.execute('SELECT task_id, metric_type FROM main.tasks').fetchall():
        old = parse_metric_types(text)
        new = merge_metric_list(old)
        if new != old:
            conn.execute('UPDATE main.tasks SET metric_type = ? WHERE task_id = ?',
                         (json.dumps(new), task_id))
        metrics[task_id] = new
    copied = copy_points(conn, 'main', has_metric_column=True, merge=True, fill=True,
                         task_metrics=metrics, on_progress=on_progress)
    conn.execute('DELETE FROM main.data_points')
    affected = sorted(copied.affected_tasks)
    recount_tasks(conn, affected)
    series_ids = [row[0] for task_id in affected for row in conn.execute(
        'SELECT series_id FROM main.series WHERE task_id = ?', (task_id,))]
    if series_ids:
        rollup_progress = None
        if on_progress is not None:
            def rollup_progress(done: int, count: int) -> None:
                on_progress(STAGE_ROLLUPS, done, count)
        rollup.rebuild_rollups(conn, series_ids=series_ids, on_progress=rollup_progress)
    return total, copied.affected_tasks, copied.skipped_rows
