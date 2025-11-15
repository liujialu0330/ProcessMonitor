"""
数据库管理模块
负责所有数据的持久化存储和查询
"""
import sqlite3
from datetime import datetime
from typing import List, Optional
from contextlib import contextmanager
from data.models import MonitorTask, DataPoint
import config


class Database:
    """数据库管理类"""

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径，默认使用config中的配置
        """
        self.db_path = db_path or config.DB_PATH
        self._init_database()

    @contextmanager
    def _get_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使结果可以按列名访问
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_database(self):
        """初始化数据库表结构"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 创建任务表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    process_name TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    interval REAL NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    status TEXT NOT NULL
                )
            ''')

            # 创建数据点表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS data_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    value REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                )
            ''')

            # 创建索引以提高查询性能
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_data_points_task_id
                ON data_points(task_id)
            ''')

            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_data_points_timestamp
                ON data_points(timestamp)
            ''')

    # ========== 任务相关操作 ==========

    def save_task(self, task: MonitorTask) -> bool:
        """
        保存任务到数据库

        Args:
            task: 监控任务对象

        Returns:
            bool: 保存是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO tasks
                    (task_id, pid, process_name, metric_type, interval, start_time, end_time, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    task.task_id,
                    task.pid,
                    task.process_name,
                    task.metric_type,
                    task.interval,
                    task.start_time.isoformat() if task.start_time else None,
                    task.end_time.isoformat() if task.end_time else None,
                    task.status,
                ))
            return True
        except Exception as e:
            print(f"保存任务失败: {e}")
            return False

    def get_task(self, task_id: str) -> Optional[MonitorTask]:
        """
        根据ID获取任务

        Args:
            task_id: 任务ID

        Returns:
            Optional[MonitorTask]: 任务对象，不存在返回None
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tasks WHERE task_id = ?', (task_id,))
                row = cursor.fetchone()

                if row:
                    return MonitorTask(
                        task_id=row['task_id'],
                        pid=row['pid'],
                        process_name=row['process_name'],
                        metric_type=row['metric_type'],
                        interval=row['interval'],
                        start_time=datetime.fromisoformat(row['start_time']),
                        end_time=datetime.fromisoformat(row['end_time']) if row['end_time'] else None,
                        status=row['status'],
                    )
                return None
        except Exception as e:
            print(f"获取任务失败: {e}")
            return None

    def get_all_tasks(self) -> List[MonitorTask]:
        """
        获取所有任务

        Returns:
            List[MonitorTask]: 任务列表
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tasks ORDER BY start_time DESC')
                rows = cursor.fetchall()

                tasks = []
                for row in rows:
                    tasks.append(MonitorTask(
                        task_id=row['task_id'],
                        pid=row['pid'],
                        process_name=row['process_name'],
                        metric_type=row['metric_type'],
                        interval=row['interval'],
                        start_time=datetime.fromisoformat(row['start_time']),
                        end_time=datetime.fromisoformat(row['end_time']) if row['end_time'] else None,
                        status=row['status'],
                    ))
                return tasks
        except Exception as e:
            print(f"获取所有任务失败: {e}")
            return []

    def update_task_status(self, task_id: str, status: str, end_time: Optional[datetime] = None) -> bool:
        """
        更新任务状态

        Args:
            task_id: 任务ID
            status: 新状态
            end_time: 结束时间（可选）

        Returns:
            bool: 更新是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if end_time:
                    cursor.execute('''
                        UPDATE tasks
                        SET status = ?, end_time = ?
                        WHERE task_id = ?
                    ''', (status, end_time.isoformat(), task_id))
                else:
                    cursor.execute('''
                        UPDATE tasks
                        SET status = ?
                        WHERE task_id = ?
                    ''', (status, task_id))
            return True
        except Exception as e:
            print(f"更新任务状态失败: {e}")
            return False

    def delete_task(self, task_id: str) -> bool:
        """
        删除任务及其所有数据点

        Args:
            task_id: 任务ID

        Returns:
            bool: 删除是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # 删除数据点
                cursor.execute('DELETE FROM data_points WHERE task_id = ?', (task_id,))
                # 删除任务
                cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            return True
        except Exception as e:
            print(f"删除任务失败: {e}")
            return False

    # ========== 数据点相关操作 ==========

    def save_data_point(self, data_point: DataPoint) -> bool:
        """
        保存单个数据点

        Args:
            data_point: 数据点对象

        Returns:
            bool: 保存是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO data_points (task_id, timestamp, value)
                    VALUES (?, ?, ?)
                ''', (
                    data_point.task_id,
                    data_point.timestamp.isoformat(),
                    data_point.value,
                ))
            return True
        except Exception as e:
            print(f"保存数据点失败: {e}")
            return False

    def save_data_points(self, data_points: List[DataPoint]) -> bool:
        """
        批量保存数据点

        Args:
            data_points: 数据点列表

        Returns:
            bool: 保存是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany('''
                    INSERT INTO data_points (task_id, timestamp, value)
                    VALUES (?, ?, ?)
                ''', [
                    (dp.task_id, dp.timestamp.isoformat(), dp.value)
                    for dp in data_points
                ])
            return True
        except Exception as e:
            print(f"批量保存数据点失败: {e}")
            return False

    def get_task_data_points(self, task_id: str, limit: Optional[int] = None) -> List[DataPoint]:
        """
        获取任务的所有数据点

        Args:
            task_id: 任务ID
            limit: 限制返回数量（可选）

        Returns:
            List[DataPoint]: 数据点列表
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if limit:
                    cursor.execute('''
                        SELECT * FROM data_points
                        WHERE task_id = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    ''', (task_id, limit))
                else:
                    cursor.execute('''
                        SELECT * FROM data_points
                        WHERE task_id = ?
                        ORDER BY timestamp ASC
                    ''', (task_id,))

                rows = cursor.fetchall()
                data_points = []
                for row in rows:
                    data_points.append(DataPoint(
                        task_id=row['task_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        value=row['value'],
                    ))
                return data_points
        except Exception as e:
            print(f"获取数据点失败: {e}")
            return []

    def get_data_point_count(self, task_id: str) -> int:
        """
        获取任务的数据点数量

        Args:
            task_id: 任务ID

        Returns:
            int: 数据点数量
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) as count FROM data_points WHERE task_id = ?', (task_id,))
                row = cursor.fetchone()
                return row['count'] if row else 0
        except Exception as e:
            print(f"获取数据点数量失败: {e}")
            return 0


# 单元测试
if __name__ == "__main__":
    import os
    import uuid

    # 使用临时数据库测试
    test_db_path = "test_monitor.db"

    # 清理旧的测试数据库
    if os.path.exists(test_db_path):
        os.remove(test_db_path)

    # 创建数据库实例
    db = Database(test_db_path)

    # 测试1: 创建并保存任务
    print("测试1: 创建并保存任务")
    task = MonitorTask(
        task_id=str(uuid.uuid4()),
        pid=1234,
        process_name="test.exe",
        metric_type="memory_rss",
        interval=1.0,
        start_time=datetime.now(),
        end_time=None,
        status="running",
    )
    result = db.save_task(task)
    print(f"  保存任务: {'成功' if result else '失败'}")

    # 测试2: 获取任务
    print("\n测试2: 获取任务")
    retrieved_task = db.get_task(task.task_id)
    print(f"  任务ID: {retrieved_task.task_id if retrieved_task else 'None'}")
    print(f"  进程名: {retrieved_task.process_name if retrieved_task else 'None'}")

    # 测试3: 保存数据点
    print("\n测试3: 保存数据点")
    data_points = [
        DataPoint(task_id=task.task_id, timestamp=datetime.now(), value=100.5),
        DataPoint(task_id=task.task_id, timestamp=datetime.now(), value=101.2),
        DataPoint(task_id=task.task_id, timestamp=datetime.now(), value=102.8),
    ]
    result = db.save_data_points(data_points)
    print(f"  批量保存数据点: {'成功' if result else '失败'}")

    # 测试4: 获取数据点
    print("\n测试4: 获取数据点")
    retrieved_points = db.get_task_data_points(task.task_id)
    print(f"  数据点数量: {len(retrieved_points)}")
    for i, dp in enumerate(retrieved_points):
        print(f"  数据点{i+1}: 值={dp.value}")

    # 测试5: 更新任务状态
    print("\n测试5: 更新任务状态")
    result = db.update_task_status(task.task_id, "stopped", datetime.now())
    print(f"  更新状态: {'成功' if result else '失败'}")
    updated_task = db.get_task(task.task_id)
    print(f"  新状态: {updated_task.status if updated_task else 'None'}")

    # 测试6: 获取所有任务
    print("\n测试6: 获取所有任务")
    all_tasks = db.get_all_tasks()
    print(f"  任务总数: {len(all_tasks)}")

    # 清理测试数据库
    os.remove(test_db_path)
    print("\n✅ 所有测试完成，测试数据库已清理")
