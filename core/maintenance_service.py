"""
数据库维护服务

两类维护操作：
- startup_reconcile：每次启动、任何任务启动之前跑一次的校正（孤儿任务、采样计数、过期清理）
- cleanup_and_vacuum：设置页"清理并压缩数据库"，耗时，由后台线程调用

维护锁（MonitorManager.begin_maintenance / end_maintenance）只经本服务持有与释放，
调用方不直接碰 manager。两种用法：
- 一段式：cleanup_and_vacuum(days) 内部占锁、结束（无论成败）必释放；
- 两段式（界面）：界面线程先 begin() 占锁（被拒抛 MaintenanceBusy），再把
  cleanup_and_vacuum(days, already_locked=True) 交给后台线程，线程结束后由界面线程
  end()。占锁与"开始监控/开始导出"的判断都发生在界面线程，中间不再有"确认之后、
  后台线程真正占锁之前"任务或导出趁机开始的窗口。
"""
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager
    from data.database import Database

logger = logging.getLogger(__name__)

# MaintenanceBusy.reason 的取值
BUSY_TASKS_RUNNING = 'tasks_running'
BUSY_EXPORT_RUNNING = 'export_running'
BUSY_MAINTENANCE_RUNNING = 'maintenance_running'


class MaintenanceBusy(Exception):
    """维护被拒绝：有运行中的监控任务（'tasks_running'）、导出进行中（'export_running'）
    或已有维护在进行（'maintenance_running'）"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ReconcileResult:
    """启动校正结果"""
    orphans: int     # 由 running 校正为 stopped 的遗留任务数
    recounted: int   # 重算了采样计数的任务数
    cleaned: int     # 按保留天数删除的过期任务数（未启用清理时为 0）


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

    def startup_reconcile(self, retention_days: int) -> ReconcileResult:
        """
        启动校正：必须在任何监控任务启动前、每进程只调用一次。

        顺序固定：先把上次未正常退出遗留的 running 任务校正为 stopped，再按数据点重算
        sample_count=0 的任务（回退旧版本运行后再升级回来的情况），最后才按保留天数清理——
        否则"刚崩溃、其实还新鲜"的任务可能被当作过期任务误删。
        各步失败只记日志（由 Database 门面降级为 0），不阻止启动。

        Args:
            retention_days: 历史数据保留天数，<= 0 表示不自动清理
        """
        orphans = self._db.reconcile_orphan_tasks()
        recounted = self._db.reconcile_sample_counts()
        cleaned = self._db.cleanup_old_tasks(retention_days) if retention_days > 0 else 0
        result = ReconcileResult(orphans=orphans, recounted=recounted, cleaned=cleaned)
        logger.info("启动校正完成: 孤儿任务=%d, 重算计数=%d, 过期清理=%d",
                    orphans, recounted, cleaned)
        return result

    def begin(self) -> None:
        """
        占用维护锁（应在界面线程调用）：此后监控任务的创建/启动与导出都会被拒绝，
        直到 end()。

        Raises:
            MaintenanceBusy: 已有维护在进行、有运行中的任务或导出；锁状态不变
        """
        if self._manager.is_maintenance_running():
            raise MaintenanceBusy(BUSY_MAINTENANCE_RUNNING)
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
