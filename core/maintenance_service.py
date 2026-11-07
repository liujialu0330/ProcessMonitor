"""
数据库维护服务

两类维护操作：
- startup_reconcile：每次启动、任何任务启动之前跑一次的校正（导回降级数据、孤儿序列、
  孤儿任务、采样计数、过期清理）
- cleanup_and_vacuum：设置页"清理并压缩数据库"，耗时，由后台线程调用
- rebuild_rollups：设置页"重建汇总"，按样本重算分钟汇总与空洞，耗时，由后台线程调用

另有存储健康的后台作业（core.storage_guard）：-wal 过大时的兜底 checkpoint 与启动后的
quick_check。兜底 checkpoint 经 begin_storage_job() / end_storage_job() 登记，与清理、
重建汇总互斥（登记时维护进行中则被拒，登记期间 begin() 抛 MaintenanceBusy）；quick_check 只用
只读连接、不写库，不登记，维护开始时由守卫推迟。

维护锁（MonitorManager.begin_maintenance / end_maintenance）只经本服务持有与释放，
调用方不直接碰 manager。两种用法（cleanup_and_vacuum 与 rebuild_rollups 相同）：
- 一段式：cleanup_and_vacuum(days) 内部占锁、结束（无论成败）必释放；
- 两段式（界面）：界面线程先 begin() 占锁（被拒抛 MaintenanceBusy），再把
  cleanup_and_vacuum(days, already_locked=True) 交给后台线程，线程结束后由界面线程
  end()。占锁与"开始监控/开始导出"的判断都发生在界面线程，中间不再有"确认之后、
  后台线程真正占锁之前"任务或导出趁机开始的窗口。
"""
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

import config

if TYPE_CHECKING:
    from data.db.maintenance import CheckpointOutcome, QuickCheck
    from core.monitor_manager import MonitorManager
    from data.database import Database

logger = logging.getLogger(__name__)

# 导回降级数据的执行方式（由组合根给出）：(待导回行数, 执行导回的函数) -> 导回行数。
# 执行导回的函数接收进度回调 (阶段名, 已完成, 总数) 或 None；组合根据行数决定是否经进度窗口
LegacyImporter = Callable[[int, Callable[[Optional[Callable[[str, int, int], None]]], int]], int]

# MaintenanceBusy.reason 的取值
BUSY_TASKS_RUNNING = 'tasks_running'
BUSY_EXPORT_RUNNING = 'export_running'
BUSY_MAINTENANCE_RUNNING = 'maintenance_running'
BUSY_STORAGE_JOB_RUNNING = 'storage_job_running'


class MaintenanceBusy(Exception):
    """维护被拒绝：有运行中的监控任务（'tasks_running'）、导出进行中（'export_running'）、
    已有维护在进行（'maintenance_running'）或存储健康的后台作业正在跑（'storage_job_running'）"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReconcileResult:
    """启动校正结果"""
    orphans: int     # 由 running 校正为 stopped 的遗留任务数
    recounted: int   # 重算了采样计数的任务数
    cleaned: int     # 按保留天数删除的过期任务数（未启用清理时为 0）
    imported: int = 0   # 从 data_points 导回的行数（回退 1.4.1 期间写入）
    purged: int = 0     # 清理的孤儿序列数（任务已被 1.4.1 删除）


@dataclass(frozen=True)
class RebuildRollupsResult:
    """重建汇总的结果"""
    series: int      # 重算的序列数


@dataclass(frozen=True)
class CleanupResult:
    """清理并压缩的结果"""
    deleted: int      # 删除的过期任务数（未启用清理时为 0）
    size_before: int  # 操作前数据库占用字节数（主库 + -wal + -shm）
    size_after: int   # 操作后数据库占用字节数


class MaintenanceService:
    """数据库维护（校正、清理、压缩）"""

    def __init__(self, db: "Database", manager: "MonitorManager"):
        self._db = db
        self._manager = manager
        # 正在跑的存储健康后台作业数（只在界面线程增减，与 begin() 串行，无需加锁）
        self._storage_jobs = 0

    def startup_reconcile(self, retention_days: int, *,
                          legacy_importer: Optional[LegacyImporter] = None) -> ReconcileResult:
        """
        启动校正：必须在任何监控任务启动前、每进程只调用一次。

        顺序固定：
        1. 记录本次写入的应用版本（meta.last_writer_version）；
        2. 导回用户回退 1.4.1 期间写进 data_points 的样本（并重算这些任务的计数）——
           必须在孤儿任务校正之前：1.4.1 不维护 last_sample_at，导回后它才有值，
           遗留 running 任务的结束时间才能取到最后一次采样；
        3. 清理 1.4.1 删任务时留下的孤儿序列（它不认识 series / samples 等表）；
        4. 把上次未正常退出遗留的 running 任务校正为 stopped；
        5. 按样本重算 sample_count=0 的任务（如 1.4.1 对已有任务 INSERT OR REPLACE 清零了计数）；
        6. 最后才按保留天数清理——否则"刚崩溃、其实还新鲜"的任务可能被当作过期任务误删。
        各步失败只记日志（由 Database 门面降级为 0 / False），不阻止启动。
        库以只读方式打开时（由更高版本写入，或升级失败仍是旧结构）整个校正跳过：每一步都要
        写库，旧结构上更不能按 v5 的表写。

        Args:
            retention_days: 历史数据保留天数，<= 0 表示不自动清理
            legacy_importer: 导回一步的执行方式（组合根用它在行数多时显示进度窗口）；
                None 时在调用线程里同步导回
        """
        if getattr(self._db, 'schema_mismatch', False):
            logger.warning("数据库仍是旧结构（升级失败），跳过启动校正")
            return ReconcileResult(orphans=0, recounted=0, cleaned=0)
        if getattr(self._db, 'read_only', False):
            logger.info("数据库为只读打开，跳过启动校正")
            return ReconcileResult(orphans=0, recounted=0, cleaned=0)
        self._db.touch_writer(config.APP_VERSION)
        imported = self._import_legacy(legacy_importer)
        purged = self._db.purge_orphan_series()
        orphans = self._db.reconcile_orphan_tasks()
        recounted = self._db.reconcile_sample_counts()
        cleaned = self._db.cleanup_old_tasks(retention_days) if retention_days > 0 else 0
        result = ReconcileResult(orphans=orphans, recounted=recounted, cleaned=cleaned,
                                 imported=imported, purged=purged)
        logger.info("启动校正完成: 导回=%d 行, 孤儿序列=%d, 孤儿任务=%d, 重算计数=%d, "
                    "过期清理=%d", imported, purged, orphans, recounted, cleaned)
        return result

    def _import_legacy(self, importer: Optional[LegacyImporter]) -> int:
        if importer is None:
            return self._db.import_legacy_points()
        pending = self._db.legacy_points_pending()
        if pending <= 0:
            return 0
        return importer(
            pending, lambda on_progress: self._db.import_legacy_points(on_progress=on_progress))

    def begin(self) -> None:
        """
        占用维护锁（应在界面线程调用）：此后监控任务的创建/启动与导出都会被拒绝，
        直到 end()。

        Raises:
            MaintenanceBusy: 已有维护在进行、有运行中的任务或导出；锁状态不变
        """
        if self._manager.is_maintenance_running():
            raise MaintenanceBusy(BUSY_MAINTENANCE_RUNNING)
        if self._storage_jobs > 0:
            raise MaintenanceBusy(BUSY_STORAGE_JOB_RUNNING)
        if not self._manager.begin_maintenance():
            reason = (BUSY_TASKS_RUNNING if self._manager.has_running_tasks()
                      else BUSY_EXPORT_RUNNING)
            raise MaintenanceBusy(reason)

    def end(self) -> None:
        """释放维护锁（与 begin() 成对，应在界面线程调用；未占用时调用无副作用）"""
        self._manager.end_maintenance()

    def cleanup_and_vacuum(self, retention_days: int, *,
                           already_locked: bool = False) -> CleanupResult:
        """
        按保留天数删除过期任务（retention_days > 0 时）后压缩数据库。

        期间必须持有维护锁：监控任务的创建/启动与导出都会被拒绝。

        Args:
            retention_days: 保留天数；<=0 表示永久保留，只压缩不删除
            already_locked: 调用方已经 begin() 占锁。为 True 时本方法既不占锁也不释放，
                由调用方在结束后（无论成败）调用 end()；为 False 时本方法自行占锁并在
                结束时释放

        Raises:
            MaintenanceBusy: 自行占锁被拒绝（仅 already_locked=False），未做任何改动
            StorageError: 清理、压缩或读取占用失败（自行占锁时锁已释放）
        """
        if not already_locked:
            self.begin()
        try:
            maintenance = self._db.maintenance
            size_before = maintenance.size_bytes()
            deleted = maintenance.cleanup_older_than(retention_days) if retention_days > 0 else 0
            maintenance.vacuum()
            size_after = maintenance.size_bytes()
        finally:
            if not already_locked:
                self.end()
        logger.info("清理并压缩完成: 删除任务=%d, 占用 %d -> %d 字节",
                    deleted, size_before, size_after)
        return CleanupResult(deleted=deleted, size_before=size_before, size_after=size_after)

    def rebuild_rollups(self, *, already_locked: bool = False) -> RebuildRollupsResult:
        """
        按样本整体重算分钟汇总与空洞（设置页"重建汇总"）。期间必须持有维护锁：
        与运行中任务的写入互斥，重算期间也不会有导出读到一半新一半旧的汇总。

        Args:
            already_locked: 调用方已经 begin() 占锁（语义同 cleanup_and_vacuum）

        Raises:
            MaintenanceBusy: 自行占锁被拒绝（仅 already_locked=False），未做任何改动
            StorageError: 重算失败（整体回滚，原汇总不变；自行占锁时锁已释放）
        """
        if not already_locked:
            self.begin()
        try:
            series = self._db.maintenance.rebuild_rollups()
        finally:
            if not already_locked:
                self.end()
        return RebuildRollupsResult(series=series)

    # ========== 存储健康后台作业（-wal 兜底 checkpoint 与 quick_check） ==========

    def begin_storage_job(self) -> bool:
        """登记一个存储健康后台作业（界面线程，启动作业线程之前调用）。

        维护（清理、重建汇总）进行中返回 False，调用方跳过这一轮；否则登记成功，此后
        begin() 在作业结束前抛 MaintenanceBusy(BUSY_STORAGE_JOB_RUNNING)。每次成功的
        begin_storage_job 都必须对应一次 end_storage_job（线程 finished 时调用）。
        """
        if self._manager.is_maintenance_running():
            return False
        self._storage_jobs += 1
        return True

    def end_storage_job(self) -> None:
        """注销一个存储健康后台作业（界面线程，作业线程 finished 之后）"""
        self._storage_jobs = max(0, self._storage_jobs - 1)

    def has_storage_job(self) -> bool:
        """是否有存储健康后台作业在跑"""
        return self._storage_jobs > 0

    def is_busy(self) -> bool:
        """数据库维护是否正在进行（守卫据此推迟 quick_check、跳过兜底 checkpoint）"""
        return self._manager.is_maintenance_running()

    def wal_size_bytes(self) -> int:
        """-wal 文件的字节数（不存在为 0）"""
        return self._db.wal_size_bytes()

    def checkpoint_wal(self) -> "CheckpointOutcome":
        """-wal 兜底 checkpoint（工作线程调用；占用维护登记由调用方经 begin_storage_job 负责）。
        先 TRUNCATE、被挡住退到 PASSIVE，前后大小与耗时记日志；不抛 sqlite3 异常。"""
        outcome = self._db.checkpoint_wal()
        if outcome.error:
            logger.warning("-wal 兜底 checkpoint 失败: 模式=%s, %s", outcome.mode, outcome.error)
        else:
            logger.info(
                "-wal 兜底 checkpoint: 模式=%s%s, -wal %d -> %d 字节, 耗时 %.2f 秒",
                outcome.mode, "（TRUNCATE 被挡住，已退到 PASSIVE）" if outcome.fell_back else "",
                outcome.size_before, outcome.size_after, outcome.elapsed_s)
        return outcome

    def new_quick_check(self) -> "QuickCheck":
        """新建一次后台 quick_check（只读连接；run() 在工作线程执行，interrupt() 任意线程）"""
        return self._db.new_quick_check()
