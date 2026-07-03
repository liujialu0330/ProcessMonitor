"""
数据库管理模块
负责所有数据的持久化存储和查询
"""
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional
from contextlib import contextmanager
from data.models import MonitorTask, DataPoint
import config

logger = logging.getLogger(__name__)

# 数据库 Schema 版本（v1：多指标支持，tasks.metric_type 存 JSON 数组，data_points 新增 metric_type 列）
SCHEMA_VERSION = 1


class Database:
    """数据库管理类"""

    # 进程级迁移 guard：整个进程只尝试一次迁移（即便多个 Database() 实例指向同一 db_path），
    # 防止 backup_aborted/仍是旧版本 的情况下每个新实例都重新触发一次备份。
    # _migration_attempted 置位后，后续实例直接复用 _migration_state，不重新执行迁移流程。
    _migration_attempted: bool = False
    _migration_state = {
        'migration_failed': False,
        'data_reset': False,
        'backup_aborted': False,
    }

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径，默认使用config中的配置
        """
        self.db_path = db_path or config.DB_PATH
        # 迁移三态标志（互斥，含义见 _migrate_if_needed 与 MainWindow 对应提示文案）：
        # - migration_failed: 迁移两次尝试均失败，已还原旧数据，本次运行新数据无法保存
        # - data_reset: 还原备份也失败，损坏库已改名保留，应用以新建的空库运行
        # - backup_aborted: 迁移前备份失败，已中止迁移，旧库原样保留未做任何改动
        self.migration_failed: bool = False
        self.data_reset: bool = False
        self.backup_aborted: bool = False
        self._init_database()

    @contextmanager
    def _get_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使结果可以按列名访问

        # 每个新连接在其他语句之前设置的 PRAGMA（均在 autocommit 状态下执行）：
        # - journal_mode=WAL：写日志模式，读写并发更稳定；设置持久化在库文件里，重复设置幂等
        # - busy_timeout=5000：其他连接持有写锁时最多等待 5 秒再抛 OperationalError，而非立即失败
        # - synchronous=NORMAL：WAL 模式下的推荐权衡——牺牲操作系统崩溃/掉电那一瞬间的极端持久性，
        #   换取相对 FULL 明显更好的写入性能；本应用属崩溃非频发的桌面工具，应用自身崩溃时
        #   WAL 机制仍保证已提交事务不丢，可接受的权衡
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')
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
        """
        检查 user_version，低于当前版本则执行迁移。
        迁移前先对旧库做 WAL checkpoint + 文件级备份，备份失败直接中止（backup_aborted）；
        备份成功后迁移失败，从备份还原后重试一次（应对磁盘满/文件锁等瞬时故障），仍失败则
        置 migration_failed（还原成功）或 data_reset（还原也失败，_restore_from_backup 内部置位）。
        整个流程每进程只执行一次（_migration_attempted 类级 guard）。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('PRAGMA user_version')
            version = cursor.fetchone()[0]

        if version >= SCHEMA_VERSION:
            return

        if Database._migration_attempted:
            # 本进程已经尝试过迁移（多半是 backup_aborted 导致 version 始终未变），
            # 直接复用上次结果，避免每个新 Database() 实例都重新触发一次备份
            state = Database._migration_state
            self.migration_failed = state['migration_failed']
            self.data_reset = state['data_reset']
            self.backup_aborted = state['backup_aborted']
            logger.warning("本进程已尝试过数据库迁移，跳过重复迁移，复用既有状态: %s", state)
            return

        Database._migration_attempted = True

        backup_path = self.db_path + '.bak_v0'
        if not self._backup_before_migration(backup_path):
            # 备份失败：中止迁移，旧库原样保留，不做任何写入
            self.backup_aborted = True
            logger.error("迁移前备份失败，已中止迁移，旧数据库保持不变: %s", self.db_path)
            self._save_migration_state()
            return

        # 首次失败从备份还原后立即重试一次
        for attempt in (1, 2):
            try:
                # 单事务执行迁移，失败自动回滚
                with self._get_connection() as conn:
                    cursor = conn.cursor()
                    self._migrate_v0_to_v1(cursor)
                logger.info("数据库迁移完成: v%s -> v%s", version, SCHEMA_VERSION)
                self._save_migration_state()
                return
            except Exception:
                logger.error("数据库迁移失败（第%d次）", attempt, exc_info=True)
                self._restore_from_backup(backup_path)

        # 两次迁移均失败：还原成功则 migration_failed；还原也失败则 data_reset
        # 已在 _restore_from_backup 内部置位
        if not self.data_reset:
            self.migration_failed = True
            logger.error("数据库迁移两次尝试均失败，已还原旧数据，本次运行新数据无法保存")
        self._save_migration_state()

    def _save_migration_state(self):
        """把本实例的迁移结果同步到类级状态，供本进程后续实例复用"""
        Database._migration_state = {
            'migration_failed': self.migration_failed,
            'data_reset': self.data_reset,
            'backup_aborted': self.backup_aborted,
        }

    def _backup_before_migration(self, backup_path: str) -> bool:
        """
        迁移前对旧库做文件级备份：先 WAL checkpoint(TRUNCATE) 把 -wal 中的未落盘数据
        合并进主文件（并尽量清空 -wal），再 copy2 拷贝主文件，确保备份包含完整数据。

        Returns:
            bool: 备份是否成功
        """
        try:
            with self._get_connection() as conn:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            shutil.copy2(self.db_path, backup_path)
            return True
        except Exception:
            logger.error("迁移前备份失败", exc_info=True)
            return False

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
        """迁移失败后从备份还原；还原也失败则将损坏库改名并新建空库（置 data_reset）"""
        try:
            shutil.copy2(backup_path, self.db_path)
            logger.warning("已从备份还原数据库: %s", backup_path)
        except Exception:
            logger.error("从备份还原失败", exc_info=True)
            try:
                broken_path = self.db_path + '.broken_' + datetime.now().strftime('%Y%m%d_%H%M%S')
                os.replace(self.db_path, broken_path)
                logger.warning("损坏的数据库已改名保留: %s", broken_path)
            except Exception:
                logger.error("保留损坏数据库失败", exc_info=True)
            # 新建空库（v1 结构）
            with self._get_connection() as conn:
                self._create_schema_v1(conn.cursor())
            self.data_reset = True

    # ========== 孤儿任务校正 ==========

    def reconcile_orphan_tasks(self) -> int:
        """
        孤儿任务校正：上一次运行未正常退出（崩溃/被强制结束）时遗留的 status='running'
        任务，本次启动时统一校正为 stopped。

        刻意不放进 _init_database/__init__：必须由调用方（MainWindow 持有
        monitor_manager.db 之后）在任何新任务启动前显式调用一次，避免每次
        Database() 实例化都重复执行。

        Returns:
            int: 被校正的任务条数
        """
        try:
            now = datetime.now().isoformat()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE tasks SET status = 'stopped', end_time = COALESCE(end_time, ?)
                    WHERE status = 'running'
                ''', (now,))
                count = cursor.rowcount
            if count:
                logger.warning("孤儿任务校正: %d 条 running 状态任务被校正为 stopped", count)
            return count
        except Exception:
            logger.error("孤儿任务校正失败", exc_info=True)
            return 0

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
        except Exception:
            logger.error("保存任务失败: task_id=%s", task.task_id, exc_info=True)
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
        except Exception:
            logger.error("获取任务失败: task_id=%s", task_id, exc_info=True)
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
        except Exception:
            logger.error("获取所有任务失败", exc_info=True)
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
        except Exception:
            logger.error("更新任务状态失败: task_id=%s", task_id, exc_info=True)
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
        except Exception:
            logger.error("删除任务失败: task_id=%s", task_id, exc_info=True)
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
        except Exception:
            logger.error("保存数据点失败: task_id=%s", data_point.task_id, exc_info=True)
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
        except Exception:
            logger.error("批量保存数据点失败: 条数=%d", len(data_points), exc_info=True)
            return False

    def get_task_data_points(self, task_id: str, metric_type: Optional[str] = None,
                             limit: Optional[int] = None) -> List[DataPoint]:
        """
        获取任务的数据点

        Args:
            task_id: 任务ID
            metric_type: 指标类型（可选），None 返回全部指标的数据点；
                         查询任务首指标时自动包含 metric_type 为 NULL 的旧数据
            limit: 限制返回数量（可选）。指定时语义为"最近 limit 条，按时间升序返回"
                   （子查询先按时间倒序取最近 N 条，再包一层按时间升序排列输出，
                   v1.2.0 批3 变更——调用方按 limit 拿到的仍是时间升序序列，
                   无需再自行 reversed()）；不指定则返回全部数据，按时间升序

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
                        SELECT * FROM (
                            SELECT * FROM data_points
                            WHERE {where}
                            ORDER BY timestamp DESC
                            LIMIT ?
                        )
                        ORDER BY timestamp ASC
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
        except Exception:
            logger.error("获取数据点失败: task_id=%s", task_id, exc_info=True)
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
        except Exception:
            logger.error("获取数据点数量失败: task_id=%s", task_id, exc_info=True)
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
        except Exception:
            logger.error("获取采集次数失败: task_id=%s", task_id, exc_info=True)
            return 0

    def get_task_data_points_bucketed(self, task_id: str, metric_type: Optional[str] = None,
                                       max_buckets: int = 2000) -> List[DataPoint]:
        """
        按行号分桶查询任务数据点，供历史页图表降采样用（禁止使用行号取模抽稀，
        取模是等间隔跳采样，会规律性漏掉尖峰）。

        做法：按 timestamp 升序给每行编号（ROW_NUMBER），用行号与总行数换算所属桶
        （bucket = (行号 * max_buckets) // 总行数，与"先 COUNT 再整除"等价，只是
        用窗口函数一次查询内完成，避免往返两次）；每个桶内分别取 value 最小与最大
        的那一行（各自保留真实 timestamp），两者按 timestamp 合并去重、升序输出。
        因此单桶恒定返回 <=2 个点，总点数 <= 2*max_buckets，且不会平滑掉尖峰。

        Args:
            task_id: 任务ID
            metric_type: 指标类型（可选），语义同 get_task_data_points；
                         传 None 时按 task_id 下全部指标数据分桶（调用方需自行确保
                         该场景下语义合理，历史页图表始终应传具体指标）
            max_buckets: 最大分桶数，默认 2000（故最多返回 4000 个点）

        Returns:
            List[DataPoint]: 按 timestamp 升序排列的降采样数据点
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                where = 'task_id = ?'
                params: list = [task_id]
                if metric_type is not None:
                    cursor.execute('SELECT metric_type FROM tasks WHERE task_id = ?', (task_id,))
                    task_row = cursor.fetchone()
                    first_metric = self._parse_metric_types(task_row['metric_type'])[0] if task_row else None
                    if metric_type == first_metric:
                        where += ' AND (metric_type = ? OR metric_type IS NULL)'
                    else:
                        where += ' AND metric_type = ?'
                    params.append(metric_type)

                cursor.execute(f'''
                    WITH numbered AS (
                        SELECT id, timestamp, value,
                               ROW_NUMBER() OVER (ORDER BY timestamp ASC, id ASC) - 1 AS rn,
                               COUNT(*) OVER () AS total
                        FROM data_points
                        WHERE {where}
                    ),
                    bucketed AS (
                        SELECT id, timestamp, value,
                               CASE WHEN total <= ? THEN rn
                                    ELSE MIN((rn * ?) / total, ? - 1)
                               END AS bucket
                        FROM numbered
                    ),
                    mins AS (
                        SELECT bucket, timestamp, value,
                               ROW_NUMBER() OVER (
                                   PARTITION BY bucket ORDER BY value ASC, timestamp ASC
                               ) AS rk
                        FROM bucketed
                    ),
                    maxs AS (
                        SELECT bucket, timestamp, value,
                               ROW_NUMBER() OVER (
                                   PARTITION BY bucket ORDER BY value DESC, timestamp ASC
                               ) AS rk
                        FROM bucketed
                    )
                    SELECT timestamp, value FROM mins WHERE rk = 1
                    UNION
                    SELECT timestamp, value FROM maxs WHERE rk = 1
                    ORDER BY timestamp ASC
                ''', (*params, max_buckets, max_buckets, max_buckets))

                rows = cursor.fetchall()
                data_points = []
                for row in rows:
                    data_points.append(DataPoint(
                        task_id=task_id,
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        value=row['value'],
                        metric_type=metric_type or '',
                    ))
                return data_points
        except Exception:
            logger.error("获取分桶数据点失败: task_id=%s", task_id, exc_info=True)
            return []

    # ========== 数据清理 ==========

    def cleanup_old_tasks(self, retention_days: int) -> int:
        """
        启动自动清理：删除已停止且早于保留期限的历史任务及其全部数据点。

        retention_days<=0 视为禁用（默认值，config.DATA_RETENTION_DAYS=0），直接
        返回 0、不做任何查询或删除。调用方必须保证本方法在 reconcile_orphan_tasks()
        之后调用——先把上次异常退出遗留的 running 孤儿任务校正为 stopped，避免把
        "刚崩溃、其实还新鲜"的任务在同一次启动里立即当作过期任务误删。

        判断口径：WHERE status='stopped' AND COALESCE(end_time, start_time) < cutoff，
        COALESCE 用于兜底 end_time 为 NULL 的老数据（迁移遗留/异常退出未回填的场景）。

        Args:
            retention_days: 保留天数，<=0 表示禁用清理

        Returns:
            int: 被删除的任务数
        """
        if retention_days <= 0:
            return 0
        try:
            cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT task_id FROM tasks
                    WHERE status = 'stopped' AND COALESCE(end_time, start_time) < ?
                ''', (cutoff,))
                task_ids = [row['task_id'] for row in cursor.fetchall()]

                if not task_ids:
                    return 0

                for task_id in task_ids:
                    cursor.execute(
                        'SELECT COUNT(*) as cnt FROM data_points WHERE task_id = ?', (task_id,))
                    point_count = cursor.fetchone()['cnt']
                    logger.info("启动自动清理: 删除过期任务 task_id=%s 数据点=%d 条",
                                task_id, point_count)
                    cursor.execute('DELETE FROM data_points WHERE task_id = ?', (task_id,))
                    cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))

                return len(task_ids)
        except Exception:
            logger.error("启动自动清理失败", exc_info=True)
            return 0


# 单元测试
if __name__ == "__main__":
    # 完整断言已迁入 tests/test_database.py、tests/test_migration.py（tmp_path 临时库，
    # 不写项目 data\monitor.db）；此处直接复用同一批用例，避免断言双份维护。
    import sys as _sys
    import pytest as _pytest

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _exit_code = _pytest.main([
        os.path.join(_project_root, "tests", "test_database.py"),
        os.path.join(_project_root, "tests", "test_migration.py"),
        "-v",
    ])
    _sys.exit(_exit_code)
