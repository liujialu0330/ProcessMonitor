"""
数据库门面

连接、表结构与迁移在 data.db 包中，各表的读写在 data.repos 仓储与 data.maintenance 中。
本类只做两件事：
1. 构造时建连接工厂、跑迁移，并把仓储挂成公开属性（tasks / points / markers /
   analytics / maintenance），新代码直接用这些属性：失败抛 StorageError，时间参数只收 datetime；
2. 保留旧的公开方法，作为对仓储的薄委托，签名与返回语义不变：原来"吞异常返回
   None/[]/0/False"的，这里捕获 StorageError、记日志后返回原值。
例外：vacuum() 失败抛 StorageError（设置页据此提示"清理失败"）；iter_data_points 失败
抛 StorageError（导出必须知道读取是否完整）。
"""
import logging
import os
from datetime import datetime
from typing import Iterator, List, Optional

import config
from data.db.connection import ConnectionFactory
from data.db.migrations.runner import MigrationOutcome, MigrationReport, MigrationRunner
from data.db.schema import SCHEMA_VERSION  # noqa: F401  兼容旧的 from data.database import SCHEMA_VERSION
from data.errors import StorageError
from data.maintenance import Maintenance
from data.models import DataPoint, Marker, MonitorTask
from data.repos.analytics_repo import AnalyticsRepo
from data.repos.marker_repo import MarkerRepo
from data.repos.point_repo import PointQuery, PointRepo
from data.repos.task_repo import TaskRepo

logger = logging.getLogger(__name__)

# 批量写数据点持续失败时的日志节流间隔（首次记堆栈，之后每隔该次数记一条）
SAVE_FAILURE_LOG_INTERVAL = 100


def _since(since_iso: Optional[str]) -> Optional[datetime]:
    """旧接口的 since_iso（ISO 字符串）→ 仓储要求的 datetime；None 表示不过滤"""
    if since_iso is None:
        return None
    if isinstance(since_iso, datetime):
        return since_iso
    return datetime.fromisoformat(since_iso)


class Database:
    """数据库门面：仓储的持有者 + 旧接口的兼容委托"""

    def __init__(self, db_path: str = None):
        """
        Args:
            db_path: 数据库文件路径，默认使用 config 中的配置
        """
        self.db_path = db_path or config.DB_PATH
        self._factory = ConnectionFactory(self.db_path)
        # 迁移三态标志（互斥，由 migration_report 映射而来，MainWindow 据此提示）：
        # - migration_failed: 迁移失败且未切到新库，本次运行新数据无法保存（通常已还原旧数据；
        #   还原与改名损坏库都失败时则是旧库原样留在原路径，下次启动会重新迁移）
        # - data_reset: 还原备份也失败，损坏库已改名保留，应用以新建的空库运行
        # - backup_aborted: 迁移前备份失败，已中止迁移，旧库原样保留未做任何改动
        self.migration_failed: bool = False
        self.data_reset: bool = False
        self.backup_aborted: bool = False
        self.migration_report: Optional[MigrationReport] = None
        self._save_failures = 0   # save_data_points 连续失败次数（日志节流用）
        self._init_database()

        # 仓储（公开属性）：失败抛 StorageError，时间参数只收 datetime
        self.tasks = TaskRepo(self._factory)
        self.points = PointRepo(self._factory)
        self.markers = MarkerRepo(self._factory)
        self.analytics = AnalyticsRepo(self._factory)
        self.maintenance = Maintenance(self._factory)

    def _get_connection(self):
        """普通连接上下文（commit/rollback/close 语义见 ConnectionFactory.connect）"""
        return self._factory.connect()

    def _transaction(self):
        """显式事务上下文（BEGIN IMMEDIATE，见 ConnectionFactory.transaction）"""
        return self._factory.transaction()

    def _init_database(self):
        """新库建当前结构，旧库按需迁移；把迁移结果映射为三个提示标志"""
        report = MigrationRunner(self._factory).migrate()
        self.migration_report = report
        outcome = report.outcome
        self.backup_aborted = outcome is MigrationOutcome.BACKUP_ABORTED
        self.data_reset = outcome is MigrationOutcome.RESET
        self.migration_failed = outcome in (MigrationOutcome.FAILED_RESTORED,
                                            MigrationOutcome.FAILED_KEPT)

    def close(self) -> None:
        """释放常驻连接（之后仍可继续使用，会按需重新打开）"""
        self._factory.close()

    # ========== 启动校正（调用方须在任何新任务启动前显式调用一次） ==========

    def reconcile_orphan_tasks(self) -> int:
        """遗留的 running 任务校正为 stopped（见 TaskRepo.reconcile_orphans）；失败返回 0"""
        try:
            return self.tasks.reconcile_orphans()
        except StorageError:
            logger.error("孤儿任务校正失败", exc_info=True)
            return 0

    def reconcile_sample_counts(self) -> int:
        """按数据点重算 sample_count=0 的任务（见 PointRepo.recount_all）；失败返回 0"""
        try:
            return self.points.recount_all()
        except StorageError:
            logger.error("采样计数校正失败", exc_info=True)
            return 0

    # ========== 任务 ==========

    def save_task(self, task: MonitorTask) -> bool:
        """UPSERT 任务行（不覆盖 sample_count / last_sample_at）；返回是否成功"""
        try:
            self.tasks.save(task)
            return True
        except StorageError:
            logger.error("保存任务失败: task_id=%s", task.task_id, exc_info=True)
            return False

    def get_task(self, task_id: str) -> Optional[MonitorTask]:
        """按 ID 取任务；不存在或失败返回 None"""
        try:
            return self.tasks.get(task_id)
        except StorageError:
            logger.error("获取任务失败: task_id=%s", task_id, exc_info=True)
            return None

    def get_all_tasks(self) -> List[MonitorTask]:
        """全部任务，按开始时间倒序；失败返回 []"""
        try:
            return self.tasks.list_all()
        except StorageError:
            logger.error("获取所有任务失败", exc_info=True)
            return []

    def get_tasks_with_data(self) -> List[MonitorTask]:
        """至少采样过一次的任务，按开始时间倒序（旧库自动回退）；失败返回 []"""
        try:
            return self.tasks.list_with_data()
        except StorageError:
            logger.error("获取有数据的任务失败", exc_info=True)
            return []

    def update_task_status(self, task_id: str, status: str,
                           end_time: Optional[datetime] = None) -> bool:
        """更新任务状态（end_time 为 None 时不改结束时间）；返回是否执行成功"""
        try:
            self.tasks.update_status(task_id, status, end_time)
            return True
        except StorageError:
            logger.error("更新任务状态失败: task_id=%s", task_id, exc_info=True)
            return False

    def delete_task(self, task_id: str) -> bool:
        """在一个事务内删除任务及其数据点、打点；返回是否执行成功"""
        try:
            self.tasks.delete(task_id)
            return True
        except StorageError:
            logger.error("删除任务失败: task_id=%s", task_id, exc_info=True)
            return False

    # ========== 数据点 ==========

    def save_data_point(self, data_point: DataPoint) -> bool:
        """保存单个数据点；返回是否成功"""
        try:
            self.points.append([data_point])
            return True
        except StorageError:
            logger.error("保存数据点失败: task_id=%s", data_point.task_id, exc_info=True)
            return False

    def save_data_points(self, data_points: List[DataPoint]) -> bool:
        """批量保存数据点（同一事务内维护采样计数）；返回是否成功，持续失败时日志节流"""
        try:
            self.points.append(data_points)
            self._save_failures = 0
            return True
        except StorageError:
            # 持续失败（如磁盘满）时每个采集周期都会走到这里：
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
        任务的数据点，按时间升序。limit 指定（> 0）时为范围内最近 limit 条；
        since_iso 为 ISO 字符串（新代码请用 points.fetch(PointQuery) 传 datetime）。
        失败返回 []。
        """
        query = PointQuery(task_id, metric_type, since=_since(since_iso),
                           limit=limit if limit and limit > 0 else None)
        try:
            return self.points.fetch(query)
        except StorageError:
            logger.error("获取数据点失败: task_id=%s", task_id, exc_info=True)
            return []

    def iter_data_points(self, task_id: str,
                         batch_size: int = 5000) -> Iterator[List[DataPoint]]:
        """按时间升序分批流式产出全部数据点；失败抛 StorageError（不吞异常）"""
        return self.points.iter_batches(task_id, batch_size)

    def get_data_point_count(self, task_id: str, metric_type: Optional[str] = None) -> int:
        """数据点行数（metric_type 为 None 时统计全部指标）；失败返回 0"""
        try:
            return self.analytics.point_count(task_id, metric_type)
        except StorageError:
            logger.error("获取数据点数量失败: task_id=%s", task_id, exc_info=True)
            return 0

    def get_sample_count(self, task_id: str) -> int:
        """采集次数（同一时间戳的多指标算一次，旧库自动回退）；失败返回 0"""
        try:
            return self.analytics.sample_count(task_id)
        except StorageError:
            logger.error("获取采集次数失败: task_id=%s", task_id, exc_info=True)
            return 0

    def get_task_data_points_bucketed(self, task_id: str, metric_type: Optional[str] = None,
                                      max_buckets: int = 2000,
                                      since_iso: Optional[str] = None) -> List[DataPoint]:
        """按行号分桶、每桶取 MIN/MAX 两行的降采样（见 AnalyticsRepo.legacy_buckets）；失败返回 []"""
        try:
            return self.analytics.legacy_buckets(
                task_id, metric_type, max_buckets, since=_since(since_iso))
        except StorageError:
            logger.error("获取分桶数据点失败: task_id=%s", task_id, exc_info=True)
            return []

    def get_metric_stats(self, task_id: str, metric_type: str,
                         since_iso: Optional[str] = None) -> Optional[dict]:
        """指标统计 dict（见 AnalyticsRepo.legacy_stats）；范围内无数据或失败返回 None"""
        try:
            return self.analytics.legacy_stats(task_id, metric_type, since=_since(since_iso))
        except StorageError:
            logger.error("获取指标统计失败: task_id=%s", task_id, exc_info=True)
            return None

    def get_last_point_timestamp(self, task_id: str) -> Optional[datetime]:
        """任务全部指标里最新数据点的时间；无数据或失败返回 None"""
        try:
            return self.analytics.last_point_timestamp(task_id)
        except StorageError:
            logger.error("获取任务最新数据点时间戳失败: task_id=%s", task_id, exc_info=True)
            return None

    # ========== 打点 ==========

    def add_marker(self, task_id: str, timestamp: datetime, label: str) -> bool:
        """添加用户打点；返回是否成功（要打点 ID 请用 markers.add）"""
        try:
            self.markers.add(task_id, timestamp, label)
            return True
        except StorageError:
            logger.error("添加标记失败: task_id=%s", task_id, exc_info=True)
            return False

    def get_markers(self, task_id: str, since_iso: Optional[str] = None) -> List[Marker]:
        """任务的打点，按时间升序；since_iso 为 ISO 字符串；失败返回 []"""
        try:
            return self.markers.list(task_id, since=_since(since_iso))
        except StorageError:
            logger.error("获取标记失败: task_id=%s", task_id, exc_info=True)
            return []

    # ========== 维护 ==========

    def cleanup_old_tasks(self, retention_days: int) -> int:
        """删除已停止且超过保留天数的任务（<=0 禁用）；返回删除数，失败返回 0"""
        try:
            return self.maintenance.cleanup_older_than(retention_days)
        except StorageError:
            logger.error("启动自动清理失败", exc_info=True)
            return 0

    def get_db_size_bytes(self) -> int:
        """主库 + -wal + -shm 的总字节数；失败返回 0"""
        try:
            return self.maintenance.size_bytes()
        except StorageError:
            logger.error("获取数据库文件大小失败", exc_info=True)
            return 0

    def vacuum(self) -> None:
        """checkpoint 后 VACUUM 压缩数据库；失败抛 StorageError（调用方据此提示失败）"""
        self.maintenance.vacuum()


# 单元测试
if __name__ == "__main__":
    # 完整断言在 tests/test_database.py、tests/test_database_facade.py（tmp_path 临时库，
    # 不写项目 data\monitor.db）；此处直接复用同一批用例，避免断言双份维护。
    import sys as _sys
    import pytest as _pytest

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _exit_code = _pytest.main([
        os.path.join(_project_root, "tests", "test_database.py"),
        os.path.join(_project_root, "tests", "test_database_facade.py"),
        "-v",
    ])
    _sys.exit(_exit_code)
