"""
数据库管理模块
负责所有数据的持久化存储和查询
"""
import json
import os
import shutil
import sqlite3
from datetime import datetime
from typing import List, Optional
from contextlib import contextmanager
from data.models import MonitorTask, DataPoint
import config

# 数据库 Schema 版本（v1：多指标支持，tasks.metric_type 存 JSON 数组，data_points 新增 metric_type 列）
SCHEMA_VERSION = 1


class Database:
    """数据库管理类"""

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径，默认使用config中的配置
        """
        self.db_path = db_path or config.DB_PATH
        # 迁移失败标志（还原备份并重试后仍失败时置 True，由 UI 层提示用户）
        self.migration_failed: bool = False
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
        """初始化数据库表结构（新库直接建 v1 结构并置 user_version=1，旧库走迁移）"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 判断是否为全新数据库（tasks 表尚不存在）
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            )
            is_new_db = cursor.fetchone() is None

            if is_new_db:
                self._create_schema_v1(cursor)

        # 旧库按需迁移到当前版本
        self._migrate_if_needed()

    @staticmethod
    def _create_schema_v1(cursor: sqlite3.Cursor):
        """创建 v1 版本的表结构与索引，并置 user_version=1"""
        # 创建任务表（metric_type 列存 JSON 数组文本，如 ["memory_rss","cpu_percent"]）
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

        # 创建数据点表（metric_type 允许 NULL，兼容旧数据）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS data_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                value REAL NOT NULL,
                metric_type TEXT,
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

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_data_points_task_metric
            ON data_points(task_id, metric_type)
        ''')

        # 新库直接标记为当前版本（PRAGMA 不能参数化，使用常量拼接）
        cursor.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')

    # ========== 迁移相关 ==========

    def _migrate_if_needed(self):
        """检查 user_version，低于当前版本则执行迁移（迁移前做文件级备份，失败还原后重试一次）"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('PRAGMA user_version')
            version = cursor.fetchone()[0]

        if version >= SCHEMA_VERSION:
            return

        # 迁移前文件级备份，原始数据永不删除
        backup_path = self.db_path + '.bak_v0'
        try:
            shutil.copy2(self.db_path, backup_path)
        except Exception as e:
            print(f"迁移前备份失败: {e}")

        # 首次失败从备份还原后立即重试一次（应对磁盘满/文件锁等瞬时故障）
        for attempt in (1, 2):
            try:
                # 单事务执行迁移，失败自动回滚
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    self._migrate_v0_to_v1(cursor)
                print(f"数据库迁移完成: v{version} -> v{SCHEMA_VERSION}")
                return
            except Exception as e:
                print(f"数据库迁移失败（第{attempt}次）: {e}")
                self._restore_from_backup(backup_path)

        # 两次迁移均失败：置失败标志，由主窗口提示用户重启重试（新采集数据将无法保存）
        self.migration_failed = True

    @staticmethod
    def _migrate_v0_to_v1(cursor: sqlite3.Cursor):
        """
        v0 -> v1 迁移（顺序敏感：先回填 data_points 再转换 tasks）

        1. data_points 增加 metric_type 列（幂等检查）
        2. 用任务当前的裸指标名回填数据点的 metric_type
        3. tasks.metric_type 裸字符串转换为 JSON 数组文本
        4. 建复合索引
        5. 置 user_version=1
        """
        # 步骤1: 增加列（先检查列是否已存在，保证幂等）
        cursor.execute('PRAGMA table_info(data_points)')
        columns = [row[1] for row in cursor.fetchall()]
        if 'metric_type' not in columns:
            cursor.execute('ALTER TABLE data_points ADD COLUMN metric_type TEXT')

        # 步骤2: 先回填数据点（此时 tasks.metric_type 还是裸字符串）
        cursor.execute('''
            UPDATE data_points
            SET metric_type = (
                SELECT t.metric_type FROM tasks t WHERE t.task_id = data_points.task_id
            )
            WHERE metric_type IS NULL
        ''')

        # 步骤3: 后转换任务表（Python 侧 json.dumps，不依赖 json1 扩展）
        cursor.execute("SELECT task_id, metric_type FROM tasks WHERE metric_type NOT LIKE '[%'")
        rows = cursor.fetchall()
        for row in rows:
            cursor.execute(
                'UPDATE tasks SET metric_type = ? WHERE task_id = ?',
                (json.dumps([row[1]]), row[0])
            )

        # 步骤4: 建复合索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_data_points_task_metric
            ON data_points(task_id, metric_type)
        ''')

        # 步骤5: 更新版本号（PRAGMA 不能参数化，使用常量拼接）
        cursor.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')

    def _restore_from_backup(self, backup_path: str):
        """迁移失败后从备份还原；还原也失败则将损坏库改名并新建空库"""
        try:
            shutil.copy2(backup_path, self.db_path)
            print(f"已从备份还原数据库: {backup_path}")
        except Exception as e:
            print(f"从备份还原失败: {e}")
            try:
                broken_path = self.db_path + '.broken_' + datetime.now().strftime('%Y%m%d_%H%M%S')
                os.replace(self.db_path, broken_path)
                print(f"损坏的数据库已改名保留: {broken_path}")
            except Exception as e2:
                print(f"保留损坏数据库失败: {e2}")
            # 新建空库（v1 结构）
            with self._get_connection() as conn:
                self._create_schema_v1(conn.cursor())

    # ========== 内部工具方法 ==========

    @staticmethod
    def _parse_metric_types(text: str) -> List[str]:
        """
        解析 tasks.metric_type 列内容为指标列表（读取双保险）

        Args:
            text: 列内容，可能是 JSON 数组文本或旧版裸指标名

        Returns:
            List[str]: 指标类型列表
        """
        if text and text.startswith('['):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list) and parsed:
                    return [str(m) for m in parsed]
            except (json.JSONDecodeError, TypeError):
                pass  # 解析失败回退为单值
        return [text]

    def _row_to_task(self, row: sqlite3.Row) -> MonitorTask:
        """将数据库行转换为 MonitorTask 对象"""
        return MonitorTask(
            task_id=row['task_id'],
            pid=row['pid'],
            process_name=row['process_name'],
            metric_types=self._parse_metric_types(row['metric_type']),
            interval=row['interval'],
            start_time=datetime.fromisoformat(row['start_time']),
            end_time=datetime.fromisoformat(row['end_time']) if row['end_time'] else None,
            status=row['status'],
        )

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
                    json.dumps(task.metric_types),
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
                    return self._row_to_task(row)
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

                return [self._row_to_task(row) for row in rows]
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
                    INSERT INTO data_points (task_id, timestamp, value, metric_type)
                    VALUES (?, ?, ?, ?)
                ''', (
                    data_point.task_id,
                    data_point.timestamp.isoformat(),
                    data_point.value,
                    data_point.metric_type,
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
                    INSERT INTO data_points (task_id, timestamp, value, metric_type)
                    VALUES (?, ?, ?, ?)
                ''', [
                    (dp.task_id, dp.timestamp.isoformat(), dp.value, dp.metric_type)
                    for dp in data_points
                ])
            return True
        except Exception as e:
            print(f"批量保存数据点失败: {e}")
            return False

    def get_task_data_points(self, task_id: str, metric_type: Optional[str] = None,
                             limit: Optional[int] = None) -> List[DataPoint]:
        """
        获取任务的数据点

        Args:
            task_id: 任务ID
            metric_type: 指标类型（可选），None 返回全部指标的数据点；
                         查询任务首指标时自动包含 metric_type 为 NULL 的旧数据
            limit: 限制返回数量（可选），指定时按时间倒序，否则按时间正序

        Returns:
            List[DataPoint]: 数据点列表
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                where = 'task_id = ?'
                params: list = [task_id]
                if metric_type is not None:
                    # 查询任务首指标时，NULL 数据点兜底归为首指标（兼容旧库回填遗漏）
                    cursor.execute('SELECT metric_type FROM tasks WHERE task_id = ?', (task_id,))
                    task_row = cursor.fetchone()
                    first_metric = self._parse_metric_types(task_row['metric_type'])[0] if task_row else None
                    if metric_type == first_metric:
                        where += ' AND (metric_type = ? OR metric_type IS NULL)'
                    else:
                        where += ' AND metric_type = ?'
                    params.append(metric_type)

                if limit:
                    cursor.execute(f'''
                        SELECT * FROM data_points
                        WHERE {where}
                        ORDER BY timestamp DESC
                        LIMIT ?
                    ''', (*params, limit))
                else:
                    cursor.execute(f'''
                        SELECT * FROM data_points
                        WHERE {where}
                        ORDER BY timestamp ASC
                    ''', params)

                rows = cursor.fetchall()
                data_points = []
                for row in rows:
                    data_points.append(DataPoint(
                        task_id=row['task_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        value=row['value'],
                        metric_type=row['metric_type'] or '',
                    ))
                return data_points
        except Exception as e:
            print(f"获取数据点失败: {e}")
            return []

    def get_data_point_count(self, task_id: str, metric_type: Optional[str] = None) -> int:
        """
        获取任务的数据点数量

        Args:
            task_id: 任务ID
            metric_type: 指标类型（可选），None 统计全部指标

        Returns:
            int: 数据点数量
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if metric_type is not None:
                    cursor.execute('''
                        SELECT COUNT(*) as count FROM data_points
                        WHERE task_id = ? AND metric_type = ?
                    ''', (task_id, metric_type))
                else:
                    cursor.execute(
                        'SELECT COUNT(*) as count FROM data_points WHERE task_id = ?',
                        (task_id,)
                    )
                row = cursor.fetchone()
                return row['count'] if row else 0
        except Exception as e:
            print(f"获取数据点数量失败: {e}")
            return 0

    def get_sample_count(self, task_id: str) -> int:
        """
        获取任务的采集次数（同一时间戳的多指标数据点算一次采集）

        Args:
            task_id: 任务ID

        Returns:
            int: 采集次数
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT COUNT(DISTINCT timestamp) as count FROM data_points WHERE task_id = ?',
                    (task_id,)
                )
                row = cursor.fetchone()
                return row['count'] if row else 0
        except Exception as e:
            print(f"获取采集次数失败: {e}")
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

    # 测试1: 创建并保存多指标任务
    print("测试1: 创建并保存多指标任务")
    task = MonitorTask(
        task_id=str(uuid.uuid4()),
        pid=1234,
        process_name="test.exe",
        metric_types=["memory_rss", "cpu_percent"],
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
    print(f"  指标列表: {retrieved_task.metric_types if retrieved_task else 'None'}")
    assert retrieved_task.metric_types == ["memory_rss", "cpu_percent"], "多指标存取回读不一致"

    # 测试3: 保存多指标数据点（同一采集周期共用一个时间戳）
    print("\n测试3: 保存多指标数据点")
    from datetime import timedelta
    ts1 = datetime.now()
    ts2 = ts1 + timedelta(seconds=1)  # 显式区分两个采集周期的时间戳
    data_points = [
        DataPoint(task_id=task.task_id, timestamp=ts1, value=100.5, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=ts1, value=12.3, metric_type="cpu_percent"),
        DataPoint(task_id=task.task_id, timestamp=ts2, value=101.2, metric_type="memory_rss"),
        DataPoint(task_id=task.task_id, timestamp=ts2, value=15.6, metric_type="cpu_percent"),
    ]
    result = db.save_data_points(data_points)
    print(f"  批量保存数据点: {'成功' if result else '失败'}")

    # 测试4: 按指标过滤获取数据点
    print("\n测试4: 按指标过滤获取数据点")
    all_points = db.get_task_data_points(task.task_id)
    mem_points = db.get_task_data_points(task.task_id, metric_type="memory_rss")
    cpu_points = db.get_task_data_points(task.task_id, metric_type="cpu_percent")
    print(f"  全部数据点: {len(all_points)}")
    print(f"  memory_rss 数据点: {len(mem_points)}")
    print(f"  cpu_percent 数据点: {len(cpu_points)}")
    assert len(all_points) == 4 and len(mem_points) == 2 and len(cpu_points) == 2, "按指标过滤结果不正确"
    for dp in mem_points:
        assert dp.metric_type == "memory_rss", "数据点指标类型回读不正确"

    # 测试5: 采集次数统计（同一时间戳多指标算一次）
    print("\n测试5: 采集次数统计")
    sample_count = db.get_sample_count(task.task_id)
    point_count = db.get_data_point_count(task.task_id)
    print(f"  采集次数: {sample_count}（数据点总数: {point_count}）")
    assert sample_count == 2 and point_count == 4, "采集次数统计不正确"

    # 测试6: 更新任务状态
    print("\n测试6: 更新任务状态")
    result = db.update_task_status(task.task_id, "stopped", datetime.now())
    print(f"  更新状态: {'成功' if result else '失败'}")
    updated_task = db.get_task(task.task_id)
    print(f"  新状态: {updated_task.status if updated_task else 'None'}")

    # 测试7: 获取所有任务
    print("\n测试7: 获取所有任务")
    all_tasks = db.get_all_tasks()
    print(f"  任务总数: {len(all_tasks)}")

    # 清理测试数据库
    os.remove(test_db_path)
    print("\n✅ 所有测试完成，测试数据库已清理")
