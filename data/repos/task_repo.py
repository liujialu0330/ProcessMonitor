"""
任务仓储：tasks 表的增删改查与孤儿任务校正

sample_count / last_sample_at 不归这里写：它们只由 PointRepo.append（写入时累加）与
PointRepo.recount（重算）维护，save() 刻意不覆盖，避免重复保存任务时把累计值清零。
v5 为后续版本预留的列（target_kind 等）模型尚未携带，save() 同样不碰它们；plan_json
（自动化方案）模型会读出，但只经 save_plan 写入，save() 也不碰它。
"""
import logging
import sqlite3
from datetime import datetime
from typing import Any, List, Optional

from data.mappers import dump_metric_types, row_to_task, to_iso, to_iso_or_none
from data.models import MonitorTask
from data.repos import rollup, series_cache
from data.repos._guard import storage_errors
from data.repos._samples import delete_series_rows

logger = logging.getLogger(__name__)


def status_value(status: Any) -> str:
    """TaskStatus（str 枚举）或普通字符串 → 落库的原始字符串。"""
    return getattr(status, 'value', status)


def delete_task_rows(cursor: sqlite3.Cursor, task_id: str) -> int:
    """
    在调用方管理的事务内删除任务在所有表里的行：series 及其 samples / rollup_1m /
    series_gaps，task_members、handle_snapshots、task_markers、data_points（1.4.1 降级期间
    写入、尚未导回的样本）与 tasks。外键约束未启用，级联只能手工完成；
    TaskRepo.delete 与 Maintenance.cleanup_older_than 共用。

    Returns:
        int: 被删除的样本行数（samples 与 data_points 合计）
    """
    conn = cursor.connection
    series = [row[0] for row in conn.execute(
        'SELECT series_id FROM series WHERE task_id = ?', (task_id,))]
    # 删除的 series_id 可能被之后新建的序列复用：先让写入端的映射缓存失效
    series_cache.invalidate()
    point_count = delete_series_rows(conn, series)
    point_count += conn.execute(
        'DELETE FROM data_points WHERE task_id = ?', (task_id,)).rowcount
    for table in ('task_members', 'handle_snapshots', 'task_markers', 'tasks'):
        conn.execute(f'DELETE FROM {table} WHERE task_id = ?', (task_id,))
    return point_count


class TaskRepo:
    """tasks 表仓储。所有方法遇 sqlite3.Error 抛 StorageError。"""

    def __init__(self, factory):
        self._factory = factory

    def save(self, task: MonitorTask) -> None:
        """
        UPSERT：不存在则插入，存在则只更新模型携带的列（一个事务）。
        采集周期（interval）变化且任务已有样本时，在同一事务内按新周期重算其汇总与空洞。

        不能用 INSERT OR REPLACE：那是"删旧行再插新行"，会把 sample_count /
        last_sample_at 重置为默认值。create_time 用 COALESCE 保留已有值，
        防止从别处重建的任务对象（create_time=None）把它清掉——进程创建时间对同一
        任务不会变化。
        """
        params = (
            task.task_id,
            task.pid,
            task.process_name,
            dump_metric_types(task.metric_types),
            task.interval,
            to_iso_or_none(task.start_time),
            to_iso_or_none(task.end_time),
            status_value(task.status),
            task.note or '',
            task.create_time,
        )
        with storage_errors(f"保存任务 {task.task_id} "):
            with self._factory.transaction() as cursor:
                conn = cursor.connection
                old = conn.execute('SELECT interval FROM tasks WHERE task_id = ?',
                                   (task.task_id,)).fetchone()
                conn.execute('''
                    INSERT INTO tasks
                    (task_id, pid, process_name, metric_type, interval, start_time,
                     end_time, status, note, create_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        pid = excluded.pid,
                        process_name = excluded.process_name,
                        metric_type = excluded.metric_type,
                        interval = excluded.interval,
                        start_time = excluded.start_time,
                        end_time = excluded.end_time,
                        status = excluded.status,
                        note = excluded.note,
                        create_time = COALESCE(excluded.create_time, tasks.create_time)
                ''', params)
                if old is not None and float(old[0]) != float(task.interval):
                    self._rebuild_rollups(conn, task.task_id)

    @staticmethod
    def _rebuild_rollups(conn, task_id: str) -> None:
        """
        采集周期改了：空洞阈值与汇总的段起点都随之变化，已有样本的序列按新周期整体重算
        汇总与空洞（在 save 的同一事务内，汇总与 tasks.interval 始终一致）。
        """
        ids = [row[0] for row in conn.execute('''
            SELECT series_id FROM series se WHERE task_id = ?
              AND EXISTS (SELECT 1 FROM samples s WHERE s.series_id = se.series_id)
        ''', (task_id,))]
        if ids:
            rollup.rebuild_rollups(conn, series_ids=ids)
            logger.info("任务采集周期变化，已重算 %d 条序列的分钟汇总与空洞: task_id=%s",
                        len(ids), task_id)

    def save_plan(self, task_id: str, plan_json: Optional[str]) -> bool:
        """
        写入（或以 None 清除）任务的自动化方案 tasks.plan_json。只更新已有任务行：
        任务行由采集线程启动时写入，调用方在它之前调用时返回 False，应稍后重试。

        Returns:
            bool: 是否命中了任务行
        """
        with storage_errors(f"保存任务方案 {task_id} "):
            with self._factory.connect() as conn:
                return conn.execute('UPDATE tasks SET plan_json = ? WHERE task_id = ?',
                                    (plan_json or None, task_id)).rowcount > 0

    def load_plan(self, task_id: str) -> Optional[str]:
        """任务的自动化方案 JSON 文本；任务不存在或没有方案返回 None。"""
        with storage_errors(f"读取任务方案 {task_id} "):
            with self._factory.connect() as conn:
                row = conn.execute('SELECT plan_json FROM tasks WHERE task_id = ?',
                                   (task_id,)).fetchone()
                return (row[0] or None) if row else None

    def get(self, task_id: str) -> Optional[MonitorTask]:
        """按 ID 取任务；不存在返回 None。"""
        with storage_errors(f"读取任务 {task_id} "):
            with self._factory.connect() as conn:
                row = conn.execute(
                    'SELECT * FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
                return row_to_task(row) if row else None

    def list_all(self) -> List[MonitorTask]:
        """全部任务，按开始时间倒序。"""
        with storage_errors("读取任务列表"):
            with self._factory.connect() as conn:
                rows = conn.execute(
                    'SELECT * FROM tasks ORDER BY start_time DESC').fetchall()
                return [row_to_task(row) for row in rows]

    def list_with_data(self) -> List[MonitorTask]:
        """
        至少采样过一次的任务（sample_count > 0），按开始时间倒序，单条 SQL。

        库仍停留在旧版本（迁移失败、tasks 缺 sample_count 列）时回退为按是否存在样本判断
        （有 samples 表按样本，否则按旧 data_points），而不是让分析页整体变空。
        """
        with storage_errors("读取有数据的任务"):
            with self._factory.connect() as conn:
                try:
                    rows = conn.execute(
                        'SELECT * FROM tasks WHERE sample_count > 0 '
                        'ORDER BY start_time DESC').fetchall()
                except sqlite3.OperationalError:
                    logger.warning("tasks 缺少 sample_count 列，回退到按样本判断有数据的任务")
                    has_samples = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'samples'"
                    ).fetchone() is not None
                    exists = ('''EXISTS (SELECT 1 FROM series se JOIN samples s
                                         ON s.series_id = se.series_id
                                         WHERE se.task_id = t.task_id)'''
                              if has_samples else
                              'EXISTS (SELECT 1 FROM data_points d WHERE d.task_id = t.task_id)')
                    rows = conn.execute(
                        f'SELECT * FROM tasks t WHERE {exists} ORDER BY start_time DESC'
                    ).fetchall()
                return [row_to_task(row) for row in rows]

    def update_status(self, task_id: str, status: str,
                      end_time: Optional[datetime] = None) -> bool:
        """
        更新任务状态；end_time 为 None 时只改状态、不动原结束时间。

        Returns:
            bool: 是否命中了任务行
        """
        params: tuple
        if end_time is not None:
            sql = 'UPDATE tasks SET status = ?, end_time = ? WHERE task_id = ?'
            params = (status_value(status), to_iso(end_time), task_id)
        else:
            sql = 'UPDATE tasks SET status = ? WHERE task_id = ?'
            params = (status_value(status), task_id)
        with storage_errors(f"更新任务状态 {task_id} "):
            with self._factory.connect() as conn:
                return conn.execute(sql, params).rowcount > 0

    def delete(self, task_id: str) -> bool:
        """
        在一个事务内删除任务及其全部样本、打点与其他附属行（见 delete_task_rows）。

        Returns:
            bool: 任务行是否存在并已删除（不存在时也会顺手清掉同 ID 的孤儿样本/打点）
        """
        with storage_errors(f"删除任务 {task_id} "):
            with self._factory.transaction() as cursor:
                cursor.execute('SELECT 1 FROM tasks WHERE task_id = ?', (task_id,))
                existed = cursor.fetchone() is not None
                delete_task_rows(cursor, task_id)
                return existed

    def reconcile_orphans(self) -> int:
        """
        把上次运行未正常退出而遗留的 status='running' 任务校正为 stopped；
        结束时间取最后一次采样时间（为空再用原结束时间、最后用当前时间），
        而不是"下次启动的时刻"。必须在任何新任务启动前由调用方显式调用一次。

        Returns:
            int: 被校正的任务条数
        """
        now = to_iso(datetime.now())
        with storage_errors("孤儿任务校正"):
            with self._factory.connect() as conn:
                count = conn.execute('''
                    UPDATE tasks
                    SET status = 'stopped',
                        end_time = COALESCE(last_sample_at, end_time, ?)
                    WHERE status = 'running'
                ''', (now,)).rowcount
        if count:
            logger.warning("孤儿任务校正: %d 条 running 状态任务被校正为 stopped", count)
        return count
