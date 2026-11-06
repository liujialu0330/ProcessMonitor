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
from typing import Dict, Iterator, List, Optional
from contextlib import contextmanager
from data.models import MonitorTask, DataPoint, Marker
import config

logger = logging.getLogger(__name__)

# 数据库 Schema 版本
# v1：多指标支持，tasks.metric_type 存 JSON 数组，data_points 新增 metric_type 列
# v2：合并重复内存指标（memory_vms / memory_pagefile 并入 memory_private，表结构不变）
# v3：tasks 增加 note / sample_count / last_sample_at；新增 task_markers 表；
#     清理孤儿数据点并回填 NULL 的 metric_type；索引收敛为 (task_id, metric_type, timestamp)
SCHEMA_VERSION = 3

# 批量写数据点持续失败时的日志节流间隔（首次记堆栈，之后每隔该次数记一条）
SAVE_FAILURE_LOG_INTERVAL = 100


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
        # - migration_failed: 迁移失败且未切到新库，本次运行新数据无法保存（通常已还原旧数据；
        #   还原与改名损坏库都失败时则是旧库原样留在原路径，下次启动会重新迁移）
        # - data_reset: 还原备份也失败，损坏库已改名保留，应用以新建的空库运行
        # - backup_aborted: 迁移前备份失败，已中止迁移，旧库原样保留未做任何改动
        self.migration_failed: bool = False
        self.data_reset: bool = False
        self.backup_aborted: bool = False
        self._save_failures = 0   # save_data_points 连续失败次数（日志节流用）
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

    @contextmanager
    def _transaction(self):
        """
        显式事务上下文：isolation_level=None 关闭 sqlite3 模块的隐式事务管理，
        用 BEGIN IMMEDIATE ... COMMIT 包住整段逻辑（失败 ROLLBACK）。
        与默认模式不同，这样 DDL（CREATE/ALTER/DROP）与 PRAGMA user_version
        也真正处于同一个事务内，迁移链任一步失败都能整体回滚。
        """
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            # 这些 PRAGMA 必须在 BEGIN 之前执行（journal_mode 不能在事务内切换）
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn.cursor()
                conn.execute('COMMIT')
            except BaseException:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    logger.error("事务回滚失败", exc_info=True)
                raise
        finally:
            conn.close()

    def _init_database(self):
        """初始化数据库表结构（新库直接建当前结构并置 user_version=SCHEMA_VERSION，旧库走迁移）"""
        with self._transaction() as cursor:
            # 判断是否为全新数据库（tasks 表尚不存在）
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            )
            if cursor.fetchone() is None:
                self._create_schema_current(cursor)

        # 旧库按需迁移到当前版本
        self._migrate_if_needed()

    @staticmethod
    def _create_schema_current(cursor: sqlite3.Cursor):
        """创建当前版本（v3）的表结构与索引，并置 user_version=SCHEMA_VERSION"""
        # tasks.metric_type 列存 JSON 数组文本，如 ["memory_rss","cpu_percent"]；
        # 后三列与 _migrate_v2_to_v3 的 ALTER TABLE ADD COLUMN 顺序保持一致
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                process_name TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                interval REAL NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                sample_count INTEGER NOT NULL DEFAULT 0,
                last_sample_at TEXT
            )
        ''')

        # metric_type 列保持可空：SQLite 无法原地改列约束，迁移库与新库须结构一致
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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_markers (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')

        # data_points 唯一的索引：覆盖按任务/指标/时间范围的过滤、排序与首尾点查询
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_data_points_task_metric_ts
            ON data_points(task_id, metric_type, timestamp)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_markers_task_ts
            ON task_markers(task_id, timestamp)
        ''')

        # PRAGMA 不能参数化，使用常量拼接
        cursor.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')

    # ========== 迁移相关 ==========

    def _migrate_if_needed(self):
        """
        检查 user_version，低于当前版本则按版本链式执行迁移（v0→v1→v2→v3，同一显式事务内）。
        迁移前先对旧库做 WAL checkpoint + 文件级备份，备份失败直接中止（backup_aborted）；
        备份成功后迁移失败，从备份还原后重试一次（应对磁盘满/文件锁等瞬时故障），仍失败则
        置 migration_failed（还原成功）；还原失败时不再重试，由 _restore_from_backup 置
        data_reset（损坏库已改名、原路径新建空库）或 migration_failed（改名也失败、旧库原样保留）。
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

        backup_path = self.db_path + f'.bak_v{version}'
        if not self._backup_before_migration(backup_path):
            # 备份失败：中止迁移，旧库原样保留，不做任何写入
            self.backup_aborted = True
            logger.error("迁移前备份失败，已中止迁移，旧数据库保持不变: %s", self.db_path)
            self._save_migration_state()
            return

        # 首次失败从备份还原后立即重试一次
        for attempt in (1, 2):
            try:
                # 单事务执行迁移（含 DDL），整条链任一步失败整体回滚
                with self._transaction() as cursor:
                    if version < 1:
                        self._migrate_v0_to_v1(cursor)
                    if version < 2:
                        self._migrate_v1_to_v2(cursor)
                    if version < 3:
                        self._migrate_v2_to_v3(cursor)
                logger.info("数据库迁移完成: v%s -> v%s", version, SCHEMA_VERSION)
                self._save_migration_state()
                return
            except Exception:
                logger.error("数据库迁移失败（第%d次）", attempt, exc_info=True)
                if not self._restore_from_backup(backup_path):
                    # 没能还原出旧库（已重置为空库，或旧库原样留着）：原路径已不是
                    # 迁移起点的那个旧库，不能再拿旧版本号重跑迁移链
                    break

        # 两次迁移均失败：还原成功则 migration_failed；还原失败时的标志
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
        5. 置 user_version=1（迁移代码不引用会演进的 SCHEMA_VERSION）
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
        cursor.execute('PRAGMA user_version = 1')

    @classmethod
    def _migrate_v1_to_v2(cls, cursor: sqlite3.Cursor):
        """
        v1 -> v2 迁移：合并重复内存指标（Windows 上 vms/pagefile/private 数值完全相同）

        1. tasks：memory_vms / memory_pagefile 映射为 memory_private，保持原顺序去重
        2. data_points：同一 (task_id, timestamp) 下三者只保留一行，再统一改名为 memory_private
        3. 置 user_version=2

        别名映射用字面量写死，迁移代码不依赖会演进的常量。metric_type 为 NULL 的旧行不动。
        """
        legacy = {'memory_vms': 'memory_private', 'memory_pagefile': 'memory_private'}

        # 步骤1: 任务指标列表改名并去重（保持原顺序）
        cursor.execute('SELECT task_id, metric_type FROM tasks')
        for task_id, text in cursor.fetchall():
            old_list = cls._parse_metric_types(text)
            new_list: List[str] = []
            for metric in old_list:
                metric = legacy.get(metric, metric)
                if metric not in new_list:
                    new_list.append(metric)
            if new_list != old_list:
                cursor.execute(
                    'UPDATE tasks SET metric_type = ? WHERE task_id = ?',
                    (json.dumps(new_list), task_id)
                )

        # 步骤2: 数据点去重（同一采集周期在 psutil oneshot 内取值，三者数值相同，
        # 保留哪行都正确），再统一改名
        cursor.execute('''
            DELETE FROM data_points
            WHERE metric_type IN ('memory_private', 'memory_vms', 'memory_pagefile')
              AND id NOT IN (
                SELECT MIN(id) FROM data_points
                WHERE metric_type IN ('memory_private', 'memory_vms', 'memory_pagefile')
                GROUP BY task_id, timestamp
              )
        ''')
        cursor.execute('''
            UPDATE data_points SET metric_type = 'memory_private'
            WHERE metric_type IN ('memory_vms', 'memory_pagefile')
        ''')

        # 步骤3: 更新版本号
        cursor.execute('PRAGMA user_version = 2')

    @classmethod
    def _migrate_v2_to_v3(cls, cursor: sqlite3.Cursor):
        """
        v2 -> v3 迁移：

        1. tasks 增加 note / sample_count / last_sample_at（幂等：先查列是否存在）
        2. 新建 task_markers 表与 (task_id, timestamp) 索引
        3. 删除孤儿数据点（task_id 不在 tasks 中）
        4. 仍为 NULL 的 data_points.metric_type 回填为所属任务的首指标
        5. 索引收敛为 (task_id, metric_type, timestamp)，删除三个旧索引
        6. 回填 sample_count = COUNT(DISTINCT timestamp)、last_sample_at = MAX(timestamp)
        7. 置 user_version=3
        """
        # 步骤1
        cursor.execute('PRAGMA table_info(tasks)')
        task_columns = {row[1] for row in cursor.fetchall()}
        if 'note' not in task_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if 'sample_count' not in task_columns:
            cursor.execute(
                'ALTER TABLE tasks ADD COLUMN sample_count INTEGER NOT NULL DEFAULT 0')
        if 'last_sample_at' not in task_columns:
            cursor.execute('ALTER TABLE tasks ADD COLUMN last_sample_at TEXT')

        # 步骤2
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_markers (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_markers_task_ts
            ON task_markers(task_id, timestamp)
        ''')

        # 步骤3
        cursor.execute(
            'DELETE FROM data_points WHERE task_id NOT IN (SELECT task_id FROM tasks)')

        # 步骤4：按任务回填（首指标取自 tasks.metric_type，兼容 JSON 数组与裸指标名）
        cursor.execute('SELECT task_id, metric_type FROM tasks')
        for task_id, text in cursor.fetchall():
            first_metric = cls._parse_metric_types(text)[0]
            cursor.execute(
                'UPDATE data_points SET metric_type = ? '
                'WHERE task_id = ? AND metric_type IS NULL',
                (first_metric, task_id))

        # 步骤5：先建新索引再删旧索引
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_data_points_task_metric_ts
            ON data_points(task_id, metric_type, timestamp)
        ''')
        for old_index in ('idx_data_points_timestamp', 'idx_data_points_task_id',
                          'idx_data_points_task_metric'):
            cursor.execute(f'DROP INDEX IF EXISTS {old_index}')

        # 步骤6：一次扫描按任务聚合后逐任务回写
        cursor.execute('''
            SELECT task_id, COUNT(DISTINCT timestamp), MAX(timestamp)
            FROM data_points GROUP BY task_id
        ''')
        for task_id, sample_count, last_sample_at in cursor.fetchall():
            cursor.execute(
                'UPDATE tasks SET sample_count = ?, last_sample_at = ? WHERE task_id = ?',
                (sample_count, last_sample_at, task_id))

        # 步骤7
        cursor.execute('PRAGMA user_version = 3')

    def _restore_from_backup(self, backup_path: str) -> bool:
        """
        迁移失败后从备份还原。

        还原失败时把损坏库连同 -wal/-shm 改名保留，再在原路径新建空库（置 data_reset）。
        改名也失败时绝不在原路径建表或写 user_version：否则旧表结构会被标成当前版本，
        以后永不再迁移、新数据永久写不进去；此时原样留下旧库（置 migration_failed），
        下次启动版本号仍是旧值，会重新尝试迁移。

        Returns:
            bool: True 表示已还原为迁移前的旧库，可以重试迁移；False 表示未还原，
                  调用方不得再重试
        """
        try:
            shutil.copy2(backup_path, self.db_path)
            logger.warning("已从备份还原数据库: %s", backup_path)
            return True
        except Exception:
            logger.error("从备份还原失败", exc_info=True)

        if not self._move_aside_broken_db():
            self.migration_failed = True
            logger.error("损坏的数据库无法改名，保持原样不新建空库，本次运行新数据无法保存: %s",
                         self.db_path)
            return False

        # 原路径已腾空，新建空库（当前结构）
        with self._transaction() as cursor:
            self._create_schema_current(cursor)
        self.data_reset = True
        return False

    def _move_aside_broken_db(self) -> bool:
        """
        把损坏库连同 -wal/-shm 改名为 <库>.broken_<时间戳>[-wal|-shm] 保留。

        伴随文件必须一起挪走：残留的旧 -wal 会被 SQLite 当成新建空库的日志回放。
        先挪伴随文件、最后挪主库；主库挪不动时把已挪走的伴随文件放回，保持旧库原样。

        Returns:
            bool: 主库是否已挪走（原路径已腾空）
        """
        broken_path = self.db_path + '.broken_' + datetime.now().strftime('%Y%m%d_%H%M%S')
        moved: List[str] = []
        try:
            for suffix in ('-wal', '-shm', ''):
                src = self.db_path + suffix
                if suffix and not os.path.exists(src):
                    continue
                os.replace(src, broken_path + suffix)
                moved.append(suffix)
        except Exception:
            logger.error("保留损坏数据库失败", exc_info=True)
            for suffix in moved:
                try:
                    os.replace(broken_path + suffix, self.db_path + suffix)
                except Exception:
                    logger.error("放回数据库伴随文件失败: %s", broken_path + suffix,
                                 exc_info=True)
            return False
        logger.warning("损坏的数据库已改名保留: %s", broken_path)
        return True

    # ========== 孤儿任务校正 ==========

    def reconcile_orphan_tasks(self) -> int:
        """
        孤儿任务校正：上一次运行未正常退出（崩溃/被强制结束）时遗留的 status='running'
        任务，本次启动时统一校正为 stopped；结束时间取该任务最后一次采样时间
        （last_sample_at，为空再用当前时间），而不是"下次启动的时刻"。

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
                    UPDATE tasks
                    SET status = 'stopped',
                        end_time = COALESCE(last_sample_at, end_time, ?)
                    WHERE status = 'running'
                ''', (now,))
                count = cursor.rowcount
            if count:
                logger.warning("孤儿任务校正: %d 条 running 状态任务被校正为 stopped", count)
            return count
        except Exception:
            logger.error("孤儿任务校正失败", exc_info=True)
            return 0

    def reconcile_sample_counts(self) -> int:
        """
        校正 sample_count：sample_count / last_sample_at 只由新版本写入路径维护。
        用户回退到旧版本运行后再升级回来，旧版写入的任务数据点存在但 sample_count=0，
        会在历史页/导出页（按 sample_count > 0 过滤）里永久消失。

        对 sample_count=0 且存在数据点的任务重算 sample_count（不同时间戳个数）与
        last_sample_at（最大时间戳）。存在性判断用 EXISTS，走
        (task_id, metric_type, timestamp) 索引前缀，不扫全表；只处理少数异常任务，
        正常任务（sample_count>0）与无数据任务不受影响。

        必须在任何新任务启动前（与 reconcile_orphan_tasks 一起）由调用方显式调用一次。

        Returns:
            int: 被校正的任务条数
        """
        try:
            fixed = 0
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT task_id FROM tasks
                    WHERE sample_count = 0
                      AND EXISTS (SELECT 1 FROM data_points d WHERE d.task_id = tasks.task_id)
                ''')
                task_ids = [row['task_id'] for row in cursor.fetchall()]
                for task_id in task_ids:
                    cursor.execute('''
                        SELECT COUNT(DISTINCT timestamp) AS cnt, MAX(timestamp) AS latest
                        FROM data_points WHERE task_id = ?
                    ''', (task_id,))
                    row = cursor.fetchone()
                    if not row or not row['cnt']:
                        continue
                    cursor.execute(
                        'UPDATE tasks SET sample_count = ?, last_sample_at = ? '
                        'WHERE task_id = ?',
                        (row['cnt'], row['latest'], task_id))
                    fixed += 1
            if fixed:
                logger.warning("采样计数校正: %d 条任务的 sample_count 由数据点重算", fixed)
            return fixed
        except Exception:
            logger.error("采样计数校正失败", exc_info=True)
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
        """将数据库行转换为 MonitorTask 对象（v3 新增列缺失时按默认值处理，
        兼容迁移失败后仍停留在旧版本的库）"""
        columns = row.keys()
        last_sample_at = row['last_sample_at'] if 'last_sample_at' in columns else None
        return MonitorTask(
            task_id=row['task_id'],
            pid=row['pid'],
            process_name=row['process_name'],
            metric_types=self._parse_metric_types(row['metric_type']),
            interval=row['interval'],
            start_time=datetime.fromisoformat(row['start_time']),
            end_time=datetime.fromisoformat(row['end_time']) if row['end_time'] else None,
            status=row['status'],
            note=row['note'] if 'note' in columns else '',
            sample_count=row['sample_count'] if 'sample_count' in columns else 0,
            last_sample_at=datetime.fromisoformat(last_sample_at) if last_sample_at else None,
        )

    # ========== 任务相关操作 ==========

    def save_task(self, task: MonitorTask) -> bool:
        """
        保存任务到数据库（upsert：不存在则插入，存在则更新模型携带的列）。

        sample_count / last_sample_at 由 save_data_points 在写数据点的同一事务内
        维护，这里刻意不覆盖，避免重复保存任务时把累计值清零。

        Args:
            task: 监控任务对象

        Returns:
            bool: 保存是否成功
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO tasks
                    (task_id, pid, process_name, metric_type, interval, start_time,
                     end_time, status, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        pid = excluded.pid,
                        process_name = excluded.process_name,
                        metric_type = excluded.metric_type,
                        interval = excluded.interval,
                        start_time = excluded.start_time,
                        end_time = excluded.end_time,
                        status = excluded.status,
                        note = excluded.note
                ''', (
                    task.task_id,
                    task.pid,
                    task.process_name,
                    json.dumps(task.metric_types),
                    task.interval,
                    task.start_time.isoformat() if task.start_time else None,
                    task.end_time.isoformat() if task.end_time else None,
                    task.status,
                    task.note or '',
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

    def get_tasks_with_data(self) -> List[MonitorTask]:
        """
        获取至少有一次采样的任务（sample_count > 0），按开始时间倒序。
        单条 SQL，不逐任务统计数据点。

        库仍停留在旧版本（迁移失败、tasks 缺 sample_count 列）时回退为按 data_points
        判断"有数据"的单条 EXISTS 查询，而不是返回空列表。
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        'SELECT * FROM tasks WHERE sample_count > 0 ORDER BY start_time DESC')
                except sqlite3.OperationalError:
                    logger.warning("tasks 缺少 sample_count 列，回退到按数据点判断有数据的任务")
                    cursor.execute('''
                        SELECT * FROM tasks t
                        WHERE EXISTS (SELECT 1 FROM data_points d WHERE d.task_id = t.task_id)
                        ORDER BY start_time DESC
                    ''')
                return [self._row_to_task(row) for row in cursor.fetchall()]
        except Exception:
            logger.error("获取有数据的任务失败", exc_info=True)
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
        删除任务及其所有数据点、标记

        Args:
            task_id: 任务ID

        Returns:
            bool: 删除是否成功
        """
        try:
            with self._get_connection() as conn:
                self._delete_task_rows(conn.cursor(), task_id)
            return True
        except Exception:
            logger.error("删除任务失败: task_id=%s", task_id, exc_info=True)
            return False

    @staticmethod
    def _delete_task_rows(cursor: sqlite3.Cursor, task_id: str) -> int:
        """
        在给定游标（调用方管理事务）上删除任务的 data_points / task_markers / tasks。
        外键约束未启用，级联删除只能手工完成；delete_task 与 cleanup_old_tasks 共用。

        Returns:
            int: 被删除的数据点行数
        """
        cursor.execute('DELETE FROM data_points WHERE task_id = ?', (task_id,))
        point_count = cursor.rowcount
        cursor.execute('DELETE FROM task_markers WHERE task_id = ?', (task_id,))
        cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
        return point_count

    # ========== 数据点相关操作 ==========

    @staticmethod
    def _insert_data_points(cursor: sqlite3.Cursor, data_points: List[DataPoint]):
        """
        在调用方的事务内写入数据点，并同步更新所属任务的 sample_count / last_sample_at：
        sample_count 增加本批中该任务不同时间戳的个数（同一周期多指标只算一次采样），
        last_sample_at 取本批最大时间戳与既有值中较大者。
        """
        cursor.executemany('''
            INSERT INTO data_points (task_id, timestamp, value, metric_type)
            VALUES (?, ?, ?, ?)
        ''', [
            (dp.task_id, dp.timestamp.isoformat(), dp.value, dp.metric_type)
            for dp in data_points
        ])

        stamps: Dict[str, set] = {}
        for dp in data_points:
            stamps.setdefault(dp.task_id, set()).add(dp.timestamp.isoformat())
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
                self._insert_data_points(conn.cursor(), [data_point])
            return True
        except Exception:
            logger.error("保存数据点失败: task_id=%s", data_point.task_id, exc_info=True)
            return False

    def save_data_points(self, data_points: List[DataPoint]) -> bool:
        """
        批量保存数据点（同一事务内更新任务的 sample_count / last_sample_at）

        Args:
            data_points: 数据点列表

        Returns:
            bool: 保存是否成功
        """
        try:
            with self._get_connection() as conn:
                self._insert_data_points(conn.cursor(), data_points)
            self._save_failures = 0
            return True
        except Exception:
            # 日志节流：持续失败（如磁盘满）时每个采集周期都会走到这里，
            # 首次记完整堆栈，之后每 SAVE_FAILURE_LOG_INTERVAL 次记一条累计次数
            self._save_failures += 1
            if self._save_failures == 1:
                logger.error("批量保存数据点失败: 条数=%d", len(data_points), exc_info=True)
            elif self._save_failures % SAVE_FAILURE_LOG_INTERVAL == 0:
                logger.error("批量保存数据点持续失败，累计连续失败 %d 次", self._save_failures)
            return False

    def get_task_data_points(self, task_id: str, metric_type: Optional[str] = None,
                             limit: Optional[int] = None,
                             since_iso: Optional[str] = None) -> List[DataPoint]:
        """
        获取任务的数据点

        Args:
            task_id: 任务ID
            metric_type: 指标类型（可选），None 返回全部指标的数据点
            limit: 限制返回数量（可选）。指定时语义为"最近 limit 条，按时间升序返回"
                   （子查询先按时间倒序取最近 N 条，再包一层按时间升序排列输出，
                   调用方拿到的始终是时间升序序列，无需再自行 reversed()）；
                   不指定则返回全部数据，按时间升序。
                   与 since_iso 组合时语义 = "范围内最近 limit 条，升序"
            since_iso: ISO 格式字符串（可选，datetime.isoformat() 产出的形态），
                       只返回 timestamp >= since_iso 的点；None 表示不做时间过滤。
                       data_points.timestamp 在库中是 TEXT，SQLite 类型亲和性下若传入
                       epoch float 会被转成 TEXT 参与字典序比较、恒小于任意 ISO 串，
                       导致过滤静默失效——此参数只接受 ISO 字符串，
                       绘图用的 epoch float（dp.timestamp.timestamp()）严禁传入这里

        Returns:
            List[DataPoint]: 数据点列表
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                where = 'task_id = ?'
                params: list = [task_id]
                if metric_type is not None:
                    where += ' AND metric_type = ?'
                    params.append(metric_type)

                if since_iso is not None:
                    where += ' AND timestamp >= ?'
                    params.append(since_iso)

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
        获取任务的采集次数（同一时间戳的多指标数据点算一次采集），
        读取 tasks.sample_count（由 save_data_points 在写入时累加，不再扫描数据点）；
        库仍停留在旧版本（tasks 缺 sample_count 列）时回退为按不同时间戳计数。

        Args:
            task_id: 任务ID

        Returns:
            int: 采集次数
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        'SELECT sample_count FROM tasks WHERE task_id = ?', (task_id,))
                    row = cursor.fetchone()
                    return row['sample_count'] if row else 0
                except sqlite3.OperationalError:
                    logger.warning("tasks 缺少 sample_count 列，回退到按数据点统计采集次数")
                    cursor.execute(
                        'SELECT COUNT(DISTINCT timestamp) AS count FROM data_points '
                        'WHERE task_id = ?', (task_id,))
                    row = cursor.fetchone()
                    return row['count'] if row else 0
        except Exception:
            logger.error("获取采集次数失败: task_id=%s", task_id, exc_info=True)
            return 0

    def get_task_data_points_bucketed(self, task_id: str, metric_type: Optional[str] = None,
                                       max_buckets: int = 2000,
                                       since_iso: Optional[str] = None) -> List[DataPoint]:
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
            since_iso: ISO 格式字符串（可选），语义同 get_task_data_points——只对
                       timestamp >= since_iso 的数据分桶（总行数/桶归属均按过滤后的
                       子集重新计算）；None 表示不做时间过滤。同样只接受 ISO 字符串，
                       严禁传入 epoch float

        Returns:
            List[DataPoint]: 按 timestamp 升序排列的降采样数据点
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                where = 'task_id = ?'
                params: list = [task_id]
                if metric_type is not None:
                    where += ' AND metric_type = ?'
                    params.append(metric_type)

                if since_iso is not None:
                    where += ' AND timestamp >= ?'
                    params.append(since_iso)

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

    def get_metric_stats(self, task_id: str, metric_type: str,
                          since_iso: Optional[str] = None) -> Optional[dict]:
        """
        获取任务指定指标的统计信息。

        Args:
            task_id: 任务ID
            metric_type: 指标类型
            since_iso: ISO 格式字符串（可选），只统计 timestamp >= since_iso 的
                       数据点；None 表示统计全部。同样只接受 ISO 字符串，严禁传入
                       epoch float

        Returns:
            Optional[dict]: 范围内无数据或查询失败时返回 None，否则包含
                count / min / max / avg，以及
                first / first_ts（范围内最早一点的值与时间）、
                last / last_ts（范围内最新一点的值与时间）、
                slope_per_hour（最小二乘线性回归斜率，单位 = 指标单位/小时；
                数据点少于 2 个或时间跨度为 0 时为 None）。
                first/last 经 (task_id, metric_type, timestamp) 复合索引
                ORDER BY ... LIMIT 1 取得。
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                where = 'task_id = ? AND metric_type = ?'
                params: list = [task_id, metric_type]
                if since_iso is not None:
                    where += ' AND timestamp >= ?'
                    params.append(since_iso)

                cursor.execute(f'''
                    SELECT timestamp, value FROM data_points
                    WHERE {where} ORDER BY timestamp ASC LIMIT 1
                ''', params)
                first_row = cursor.fetchone()
                if first_row is None:
                    return None
                cursor.execute(f'''
                    SELECT timestamp, value FROM data_points
                    WHERE {where} ORDER BY timestamp DESC LIMIT 1
                ''', params)
                last_row = cursor.fetchone()

                # x 取相对最早一点的小时数：先在 SQL 内用 julianday 差值换算，
                # 避免直接对儒略日（约 2.4e6）求平方和造成的浮点精度损失
                cursor.execute(f'''
                    SELECT COUNT(*) AS cnt, MIN(v) AS min_v, MAX(v) AS max_v,
                           AVG(v) AS avg_v, SUM(x) AS sx, SUM(x * x) AS sxx,
                           SUM(v) AS sy, SUM(x * v) AS sxy
                    FROM (
                        SELECT value AS v,
                               (julianday(timestamp) - julianday(?)) * 24.0 AS x
                        FROM data_points
                        WHERE {where}
                    )
                ''', [first_row['timestamp'], *params])
                agg = cursor.fetchone()

                first_ts = datetime.fromisoformat(first_row['timestamp'])
                last_ts = datetime.fromisoformat(last_row['timestamp'])

                slope = None
                count = agg['cnt']
                if count >= 2 and last_ts > first_ts:
                    denominator = count * agg['sxx'] - agg['sx'] ** 2
                    if denominator > 0:
                        slope = (count * agg['sxy'] - agg['sx'] * agg['sy']) / denominator

                return {
                    'count': count,
                    'min': agg['min_v'],
                    'max': agg['max_v'],
                    'avg': agg['avg_v'],
                    'first': first_row['value'],
                    'first_ts': first_ts,
                    'last': last_row['value'],
                    'last_ts': last_ts,
                    'slope_per_hour': slope,
                }
        except Exception:
            logger.error("获取指标统计失败: task_id=%s", task_id, exc_info=True)
            return None

    def get_last_point_timestamp(self, task_id: str) -> Optional[datetime]:
        """
        获取任务全部指标里最新一条数据点的时间戳（MAX(timestamp)）。

        用于历史页"时间范围"筛选的锚点计算：锚定该任务最后一个数据点的时间，
        而非当前时刻——停止已久的任务选"最近1小时"仍应能看到其最后一小时的数据。
        任务所有指标合在一起取最大值，无法直接走 (task_id, metric_type, timestamp)
        索引的有序扫描，调用方应按任务缓存结果，避免切范围/切指标时重复触发。

        Args:
            task_id: 任务ID

        Returns:
            Optional[datetime]: 解析后的 datetime；该任务无数据点或查询失败时返回 None
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT MAX(timestamp) AS latest FROM data_points WHERE task_id = ?',
                    (task_id,)
                )
                row = cursor.fetchone()
                if not row or row['latest'] is None:
                    return None
                return datetime.fromisoformat(row['latest'])
        except Exception:
            logger.error("获取任务最新数据点时间戳失败: task_id=%s", task_id, exc_info=True)
            return None

    def iter_data_points(self, task_id: str, batch_size: int = 5000) -> Iterator[List[DataPoint]]:
        """
        按 timestamp 升序、分批流式产出任务的全部数据点（自建连接，游标 fetchmany），
        避免大数据量时一次性 fetchall 占用大量内存。

        与本类其余方法不同，这里不吞异常：导出等调用方必须知道读取是否完整，
        出错时直接抛出。提前终止迭代（生成器被关闭）时连接会随之关闭。

        Args:
            task_id: 任务ID
            batch_size: 每批行数

        Yields:
            List[DataPoint]: 一批数据点（非空）
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT task_id, timestamp, value, metric_type FROM data_points
                WHERE task_id = ?
                ORDER BY timestamp ASC, id ASC
            ''', (task_id,))
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    return
                yield [
                    DataPoint(
                        task_id=row['task_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        value=row['value'],
                        metric_type=row['metric_type'] or '',
                    )
                    for row in rows
                ]

    # ========== 标记（事件打点） ==========

    def add_marker(self, task_id: str, timestamp: datetime, label: str) -> bool:
        """
        为任务添加一个时间标记（如"点击了开始扫描"）。

        Returns:
            bool: 是否写入成功
        """
        try:
            with self._get_connection() as conn:
                conn.execute('''
                    INSERT INTO task_markers (task_id, timestamp, label, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (task_id, timestamp.isoformat(), label, datetime.now().isoformat()))
            return True
        except Exception:
            logger.error("添加标记失败: task_id=%s", task_id, exc_info=True)
            return False

    def get_markers(self, task_id: str, since_iso: Optional[str] = None) -> List[Marker]:
        """
        获取任务的标记，按时间升序。

        Args:
            task_id: 任务ID
            since_iso: ISO 格式字符串（可选），只返回 timestamp >= since_iso 的标记
        """
        try:
            with self._get_connection() as conn:
                where = 'task_id = ?'
                params: list = [task_id]
                if since_iso is not None:
                    where += ' AND timestamp >= ?'
                    params.append(since_iso)
                rows = conn.execute(f'''
                    SELECT id, task_id, timestamp, label FROM task_markers
                    WHERE {where} ORDER BY timestamp ASC, id ASC
                ''', params).fetchall()
                return [
                    Marker(
                        task_id=row['task_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        label=row['label'],
                        id=row['id'],
                    )
                    for row in rows
                ]
        except Exception:
            logger.error("获取标记失败: task_id=%s", task_id, exc_info=True)
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
                    point_count = self._delete_task_rows(cursor, task_id)
                    logger.info("启动自动清理: 删除过期任务 task_id=%s 数据点=%d 条",
                                task_id, point_count)

                return len(task_ids)
        except Exception:
            logger.error("启动自动清理失败", exc_info=True)
            return 0

    # ========== 数据库维护 ==========

    def get_db_size_bytes(self) -> int:
        """
        获取数据库占用磁盘的总字节数：主库文件 + WAL 模式下的 -wal/-shm 边车
        文件（三者中实际存在的部分求和）。WAL checkpoint 之前，最近写入的数据
        实际存在 -wal 文件里，只统计主库文件会低估真实占用，故必须三者相加。

        供设置页"清理并压缩数据库"卡片展示当前占用；单个文件缺失或权限异常
        时该部分记 0，不整体失败（与本类其余方法的静默失败语义一致）。

        Returns:
            int: 总字节数
        """
        total = 0
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            try:
                if os.path.exists(path):
                    total += os.path.getsize(path)
            except OSError:
                logger.error("获取数据库文件大小失败: %s", path, exc_info=True)
        return total

    def vacuum(self) -> None:
        """
        压缩数据库文件：先 PRAGMA wal_checkpoint(TRUNCATE) 把 -wal 中的未落盘
        数据合并进主文件并尽量清空 -wal，再执行 VACUUM 重建主文件、回收已删除
        数据占用的空间（已用临时脚本实测：同连接内 checkpoint 后紧跟 VACUUM
        不受 sqlite3 模块隐式事务影响，不会抛 "cannot VACUUM from within a
        transaction"）。

        VACUUM 期间需要对数据库有较独占的访问，调用方（设置页"立即清理"）应
        只在确认当前无运行中监控任务时才提供入口；本方法自身不做任务运行状态
        检查（不越权触达 core 层，遵守分层）。

        静默失败语义：与本类其余方法一致，异常只记日志不向上抛出——调用方
        （后台 QThread）无需再 try/except 包裹一层。
        """
        try:
            with self._get_connection() as conn:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                conn.execute('VACUUM')
        except Exception:
            logger.error("压缩数据库失败", exc_info=True)


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
