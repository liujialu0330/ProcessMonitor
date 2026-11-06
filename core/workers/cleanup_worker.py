"""
"清理并压缩数据库"后台线程

按保留天数删除过期任务（retention_days>0 时）再 VACUUM，避免大库操作阻塞 UI 线程。
维护锁（有运行中任务或导出时拒绝、维护期间拒绝新任务与导出）只经 MaintenanceService
持有与释放，本线程与页面都不直接操作 manager。界面用两段式：界面线程先
MaintenanceService.begin() 占锁，再以 already_locked=True 启动本线程，线程结束
（QThread.finished，无论成败）后由界面线程 end()；already_locked=False 时由
cleanup_and_vacuum 自行占锁与释放。

结果三选一、恰好发一个且是 run() 的最后动作：
- result(CleanupResult)：成功；
- busy(reason)：被维护锁拒绝（MaintenanceBusy.reason，如 'tasks_running'、'export_running'；
  只在 already_locked=False 时可能出现）；
- failed(str)：任何其他失败（如 StorageError），不再像旧实现那样压缩失败也提示"完成"。
"""
import logging

from PyQt5.QtCore import pyqtSignal

from core.maintenance_service import CleanupResult, MaintenanceBusy, MaintenanceService
from core.workers.base import BackgroundTask, CancelToken

logger = logging.getLogger(__name__)


class CleanupWorker(BackgroundTask):
    """清理并压缩数据库（无取消机制：VACUUM 中途打断会让数据库停在中间态）。"""

    # 被维护锁拒绝：参数为 MaintenanceBusy.reason
    busy = pyqtSignal(str)

    def __init__(self, maintenance_service: MaintenanceService, retention_days: int,
                 parent=None, *, already_locked: bool = False):
        """
        Args:
            maintenance_service: 维护服务（持有维护锁、执行清理与压缩）
            retention_days: 保留天数；<=0 表示永久保留，只压缩不删除
            parent: 父对象
            already_locked: 调用方已在界面线程 begin() 占锁；本线程不占也不释放，
                调用方须在线程结束后 end()
        """
        super().__init__(parent)
        self.maintenance_service = maintenance_service
        self.retention_days = retention_days
        self.already_locked = already_locked

    def execute(self, token: CancelToken) -> CleanupResult:
        return self.maintenance_service.cleanup_and_vacuum(
            self.retention_days, already_locked=self.already_locked)

    def handle_failure(self, exc: Exception) -> None:
        if isinstance(exc, MaintenanceBusy):
            reason = str(getattr(exc, 'reason', '') or '')
            logger.info("清理数据库被拒绝: %s", reason)
            self.busy.emit(reason)
            return
        super().handle_failure(exc)
