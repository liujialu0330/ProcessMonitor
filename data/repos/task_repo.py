"""
任务仓储：tasks 表的增删改查与孤儿任务校正

sample_count / last_sample_at 不归这里写：它们只由 PointRepo.append（写入时累加）与
PointRepo.recount（重算）维护，save() 刻意不覆盖，避免重复保存任务时把累计值清零。
"""
import logging
import sqlite3
from datetime import datetime
from typing import Any, List, Optional

from data.mappers import dump_metric_types, row_to_task, to_iso, to_iso_or_none
from data.models import MonitorTask
from data.repos._guard import storage_errors

logger = logging.getLogger(__name__)


def status_value(status: Any) -> str:
    """TaskStatus（str 枚举）或普通字符串 → 落库的原始字符串。"""
    return getattr(status, 'value', status)


def delete_task_rows(cursor: sqlite3.Cursor, task_id: str) -> int:
    """
    在调用方管理的事务内删除任务的 data_points / task_markers / tasks 三表行。
    外键约束未启用，级联只能手工完成；TaskRepo.delete 与 Maintenance.cleanup_older_than 共用。

    Returns:
        int: 被删除的数据点行数
    """
    cursor.execute('DELETE FROM data_points WHERE task_id = ?', (task_id,))
    point_count = cursor.rowcount
    cursor.execute('DELETE FROM task_markers WHERE task_id = ?', (task_id,))
    cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
    return point_count


class TaskRepo:
    """tasks 表仓储。所有方法遇 sqlite3.Error 抛 StorageError。"""

    def __init__(self, factory):
        self._factory = factory

    def save(self, task: MonitorTask) -> None:
        """
        UPSERT：不存在则插入，存在则只更新模型携带的列。

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
            with self._factory.connect() as conn:
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

        库仍停留在旧版本（迁移失败、tasks 缺 sample_count 列）时回退为按 data_points
        是否存在判断，而不是让历史页/导出页整体变空。
        """
        with storage_errors("读取有数据的任务"):
            with self._factory.connect() as conn:
                try:
                    rows = conn.execute(
                        'SELECT * FROM tasks WHERE sample_count > 0 '
                        'ORDER BY start_time DESC').fetchall()
                except sqlite3.OperationalError:
                    logger.warning("tasks 缺少 sample_count 列，回退到按数据点判断有数据的任务")
                    rows = conn.execute('''
                        SELECT * FROM tasks t
                        WHERE EXISTS (SELECT 1 FROM data_points d WHERE d.task_id = t.task_id)
                        ORDER BY start_time DESC
                    ''').fetchall()
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
        在一个事务内删除任务及其全部数据点与打点。

        Returns:
            bool: 任务行是否存在并已删除（不存在时也会顺手清掉同 ID 的孤儿数据点/打点）
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
