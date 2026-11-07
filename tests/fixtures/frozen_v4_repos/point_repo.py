# 冻结副本：v4（提交 7c7464b）的 data/repos/point_repo.py，供 tests/test_storage_v5.py 做 v4/v5 结果对照。
# 不随当前代码演进修改。
"""
数据点仓储：data_points 表的写入、范围查询、流式读取，以及采样计数的维护

tasks.sample_count / last_sample_at 是由数据点派生的冗余列，只允许两个入口改它们：
- append：写数据点的同一事务内累加（同一时间戳的多指标只算一次采样）；
- recount / recount_all：按数据点重算（修复旧版本写入、手工改库等造成的偏差）。
别处不得自行拼 SQL 维护这两列，否则口径会再次分叉。
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Set

from data.mappers import row_to_point, to_iso
from data.models import DataPoint
from data.repos._guard import storage_errors

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PointQuery:
    """
    数据点范围查询条件。

    - metric: None 表示该任务的全部指标；
    - since / until: 闭区间 [since, until]，None 表示该侧不限；只接受 datetime；
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

    def where(self) -> tuple:
        """生成 WHERE 子句与参数（时间在这里统一转 ISO）。"""
        clauses = ['task_id = ?']
        params: list = [self.task_id]
        if self.metric is not None:
            clauses.append('metric_type = ?')
            params.append(self.metric)
        if self.since is not None:
            clauses.append('timestamp >= ?')
            params.append(to_iso(self.since))
        if self.until is not None:
            clauses.append('timestamp <= ?')
            params.append(to_iso(self.until))
        return ' AND '.join(clauses), params


def _recount_in(cursor: sqlite3.Cursor, task_id: str) -> int:
    """在调用方的事务内按数据点重算一个任务的 sample_count / last_sample_at，返回新计数。"""
    cursor.execute('''
        SELECT COUNT(DISTINCT timestamp) AS cnt, MAX(timestamp) AS latest
        FROM data_points WHERE task_id = ?
    ''', (task_id,))
    row = cursor.fetchone()
    count = row[0] or 0
    cursor.execute(
        'UPDATE tasks SET sample_count = ?, last_sample_at = ? WHERE task_id = ?',
        (count, row[1], task_id))
    return count


class PointRepo:
    """data_points 表仓储。所有方法遇 sqlite3.Error 抛 StorageError。"""

    def __init__(self, factory):
        self._factory = factory

    def append(self, points: List[DataPoint]) -> None:
        """
        批量写入数据点，并在同一事务内更新所属任务的 sample_count / last_sample_at：
        sample_count 增加本批中该任务不同时间戳的个数，last_sample_at 取本批最大时间戳
        与既有值中较大者。任一步失败整体回滚，计数不会与数据点脱节。
        """
        if not points:
            return
        # 先在事务外完成全部编码：时间类型不对时直接 TypeError，不开事务
        rows = [(dp.task_id, to_iso(dp.timestamp), dp.value, dp.metric_type)
                for dp in points]
        stamps: Dict[str, Set[str]] = {}
        for task_id, ts, _value, _metric in rows:
            stamps.setdefault(task_id, set()).add(ts)

        with storage_errors(f"写入数据点（{len(rows)} 条）"):
            with self._factory.transaction() as cursor:
                cursor.executemany('''
                    INSERT INTO data_points (task_id, timestamp, value, metric_type)
                    VALUES (?, ?, ?, ?)
                ''', rows)
                for task_id, timestamps in stamps.items():
                    latest = max(timestamps)
                    cursor.execute('''
                        UPDATE tasks
                        SET sample_count = sample_count + ?,
                            last_sample_at = CASE
                                WHEN last_sample_at IS NULL OR last_sample_at < ? THEN ?
                                ELSE last_sample_at END
                        WHERE task_id = ?
                    ''', (len(timestamps), latest, latest, task_id))

    def fetch(self, query: PointQuery) -> List[DataPoint]:
        """按 PointQuery 取数据点，始终按时间升序返回。"""
        where, params = query.where()
        if query.limit:
            # 子查询先倒序取最近 N 条，外层再升序，调用方无需自行 reversed()
            sql = f'''
                SELECT * FROM (
                    SELECT task_id, timestamp, value, metric_type, id FROM data_points
                    WHERE {where}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC, id ASC
            '''
            params = [*params, query.limit]
        else:
            sql = f'''
                SELECT task_id, timestamp, value, metric_type FROM data_points
                WHERE {where}
                ORDER BY timestamp ASC, id ASC
            '''
        with storage_errors(f"读取数据点 {query.task_id} "):
            with self._factory.connect() as conn:
                return [row_to_point(row) for row in conn.execute(sql, params).fetchall()]

    def iter_batches(self, task_id: str,
                     batch_size: int = 5000) -> Iterator[List[DataPoint]]:
        """
        按时间升序分批流式产出任务的全部数据点（游标 fetchmany），避免一次性载入内存。
        每批非空；无数据时不产出任何批。提前关闭生成器时连接随之关闭。
        """
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError(f"batch_size 必须是正整数，收到 {batch_size!r}")
        # 参数校验放在生成器外，调用时立即报错，而不是等到第一次 next()
        return self._iter_batches(task_id, batch_size)

    def _iter_batches(self, task_id: str, batch_size: int) -> Iterator[List[DataPoint]]:
        with storage_errors(f"流式读取数据点 {task_id} "):
            with self._factory.connect() as conn:
                cursor = conn.execute('''
                    SELECT task_id, timestamp, value, metric_type FROM data_points
                    WHERE task_id = ?
                    ORDER BY timestamp ASC, id ASC
                ''', (task_id,))
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        return
                    yield [row_to_point(row) for row in rows]

    def recount(self, task_id: str) -> int:
        """
        按数据点重算一个任务的 sample_count（不同时间戳个数）与 last_sample_at（最大时间戳）。
        无数据点时置为 0 / NULL。

        Returns:
            int: 重算后的 sample_count
        """
        with storage_errors(f"重算采样计数 {task_id} "):
            with self._factory.transaction() as cursor:
                return _recount_in(cursor, task_id)

    def recount_all(self) -> int:
        """
        启动校正：只重算 sample_count=0 却存在数据点的任务（用户回退旧版本运行后再升级
        回来，旧版只写数据点不维护计数，这些任务会在按 sample_count 过滤的页面里消失）。
        存在性用 EXISTS 走 (task_id, metric_type, timestamp) 索引前缀，不扫全表；
        正常任务与无数据任务不受影响。必须在任何新任务启动前调用一次。

        Returns:
            int: 被校正的任务条数
        """
        with storage_errors("校正采样计数"):
            with self._factory.transaction() as cursor:
                cursor.execute('''
                    SELECT task_id FROM tasks
                    WHERE sample_count = 0
                      AND EXISTS (SELECT 1 FROM data_points d WHERE d.task_id = tasks.task_id)
                ''')
                task_ids = [row[0] for row in cursor.fetchall()]
                for task_id in task_ids:
                    _recount_in(cursor, task_id)
        if task_ids:
            logger.warning("采样计数校正: %d 条任务的 sample_count 由数据点重算", len(task_ids))
        return len(task_ids)
