"""
存储守卫：磁盘空间、-wal 兜底 checkpoint、后台 quick_check

长时间浸泡监控最怕两件事：磁盘悄悄写满，库在写入中途出错；数据库自己出了问题却没人知道。
StorageGuard 是界面线程上的 QObject，一个 1 秒定时器驱动三项检查（用注入的单调时钟决定
到没到点，测试不必真等）：

1. 磁盘空间（默认每 30 秒）：数据库所在磁盘剩余空间低于 1 GB 提示一次（low_space），回升到
   1.2 GB 以上再跌破才会再次提示，防抖动刷屏；低于 200 MB 经 MonitorManager 的公开接口
   pause_task 暂停全部运行中任务并发 space_critical（界面据此显示写明剩余空间与数据库路径的
   常驻提示）；回升到 300 MB 以上发 space_recovered，但不自动恢复任务，由用户手动继续。守卫
   自己记住暂停了哪些任务（paused_task_ids / space_paused_changed），界面据此标出"因磁盘空间
   不足暂停"；用户手动恢复、任务结束或被移除时，对应任务从集合里去掉。
   仍低于 200 MB 时，用户手动恢复或新建的任务会在下一次检查被再次暂停（只记日志，不重复提示）。
2. -wal 兜底 checkpoint（默认每 5 分钟）：-wal 超过 64 MB 时在后台线程做一次
   TRUNCATE（被挡住退到 PASSIVE），前后大小与耗时记日志。维护进行中跳过；作业期间维护锁被占
   （MaintenanceService.begin_storage_job），清理与重建汇总开始不了，二者不会同时碰库。
3. quick_check（启动后默认 2 分钟，只跑一次）：后台线程、只读连接。结果写日志；不是 ok 时发
   integrity_problem（只发一次），界面在主窗口与设置页各显示一次警告。

数据层抛 StorageFullError（SQLITE_FULL）时，采集线程把它经 report_storage_full() 报给守卫，
走与"低于 200 MB"相同的路径（暂停全部运行中任务、发 space_critical）。

线程与退出：所有后台作业都是 BackgroundTask，启动前登记到线程登记表（退出时统一取消并带
超时等待）；quick_check 的取消会直接中止 SQLite 语句，不会卡住退出路径。stop() 停定时器并取消
在途作业（幂等），无窗口模式与有窗口模式都在退出收尾之前调用。

无窗口模式不连任何界面，守卫只记日志；本模块不依赖 ui 包。只在界面线程使用。
"""
import logging
import os
import shutil
import time
from typing import Callable, FrozenSet, List, Optional, Set

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from core.workers.base import BackgroundTask, CancelToken

logger = logging.getLogger(__name__)

MB = 1024 * 1024
GB = 1024 * MB

# 磁盘剩余空间阈值
WARN_FREE_BYTES = 1 * GB                 # 低于它提示一次
WARN_REARM_FREE_BYTES = int(1.2 * GB)    # 回升到它以上才重新武装提示
PAUSE_FREE_BYTES = 200 * MB              # 低于它暂停全部运行中任务
RECOVER_FREE_BYTES = 300 * MB            # 回升到它以上视为空间已恢复
# -wal 超过它做兜底 checkpoint
WAL_LIMIT_BYTES = 64 * MB

# 各项检查的周期与延迟（秒）
DISK_CHECK_INTERVAL_S = 30.0
WAL_CHECK_INTERVAL_S = 300.0
QUICK_CHECK_DELAY_S = 120.0
# quick_check 遇到维护进行中或兜底 checkpoint 在跑时的推迟（秒）
QUICK_CHECK_RETRY_S = 30.0
# 驱动定时器的间隔（毫秒）
TICK_INTERVAL_MS = 1000

# 线程登记表里的名称与退出时等待上限（毫秒）
WAL_THREAD_NAME = "WAL 兜底 checkpoint"
WAL_THREAD_TIMEOUT_MS = 3000
QUICK_CHECK_THREAD_NAME = "数据库完整性检查"
QUICK_CHECK_THREAD_TIMEOUT_MS = 3000


def format_size(num_bytes: float) -> str:
    """字节数 → 易读文本（B / KB / MB / GB / TB，KB 及以上保留 1 位小数）"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class _WalCheckpointWorker(BackgroundTask):
    """-wal 兜底 checkpoint。一次调用最多等两个忙等待上限（各 1 秒），不提供中途取消；
    线程启动前已被取消则什么都不做。"""

    def __init__(self, maintenance, parent=None):
        super().__init__(parent)
        self._maintenance = maintenance

    def execute(self, token: CancelToken):
        if token.is_cancelled:
            return None
        return self._maintenance.checkpoint_wal()


class _QuickCheckWorker(BackgroundTask):
    """后台 quick_check。取消会中止 SQLite 语句（QuickCheck.interrupt），线程随即结束。"""

    def __init__(self, maintenance, parent=None):
        super().__init__(parent)
        # 在构造（界面线程）时创建，cancel() 无论何时调用都有对象可中断
        self._check = maintenance.new_quick_check()

    def cancel(self) -> None:
        super().cancel()
        self._check.interrupt()

    def execute(self, token: CancelToken):
        if token.is_cancelled:
            return None
        return self._check.run()


class StorageGuard(QObject):
    """存储守卫（见模块说明）。"""

    # 剩余空间（字节）跌破 1 GB：每次下探只发一次
    # 字节数用 object 传：pyqtSignal(int) 是 C 的 32 位 int，剩余空间超过 2 GB 会溢出成负数
    low_space = pyqtSignal(object)
    # 进入"空间严重不足"：(剩余字节, 本次由守卫新暂停的任务 id 元组——可能为空)
    space_critical = pyqtSignal(object, object)
    # 剩余空间回升到 300 MB 以上（任务不会自动恢复）：剩余字节
    space_recovered = pyqtSignal(object)
    # 守卫暂停的任务集合有变化：当前全部 id（frozenset）
    space_paused_changed = pyqtSignal(object)
    # quick_check 未通过（只发一次）：给人看的摘要
    integrity_problem = pyqtSignal(str)
    # quick_check 跑完（含通过、未能完成）：QuickCheckResult
    quick_check_finished = pyqtSignal(object)

    def __init__(self, manager, maintenance, registry, db_path: str, parent=None, *,
                 read_only: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 disk_usage: Callable = shutil.disk_usage,
                 wal_size: Optional[Callable[[], int]] = None,
                 disk_interval_s: float = DISK_CHECK_INTERVAL_S,
                 wal_interval_s: float = WAL_CHECK_INTERVAL_S,
                 quick_check_delay_s: Optional[float] = QUICK_CHECK_DELAY_S,
                 wal_limit_bytes: int = WAL_LIMIT_BYTES,
                 tick_interval_ms: int = TICK_INTERVAL_MS):
        """
        Args:
            manager: MonitorManager（用 list_task_infos / is_task_active / is_task_paused /
                pause_task 与 task_resumed / task_stopped / task_removed 信号）
            maintenance: MaintenanceService（维护互斥、wal_size_bytes、checkpoint_wal、
                new_quick_check）
            registry: 线程登记表（后台作业启动前登记）
            db_path: 数据库文件路径；磁盘空间看它所在的目录
            read_only: 只读库（没有写入，也没有可收的 -wal）：不做兜底 checkpoint
            clock: 单调时钟（秒），决定各项检查到没到点
            disk_usage: shutil.disk_usage 的形状：(路径) -> 带 free 属性的对象
            wal_size: 取 -wal 字节数的函数；缺省用 maintenance.wal_size_bytes
            quick_check_delay_s: 启动后多久跑 quick_check；None 表示不跑
        """
        super().__init__(parent)
        self._manager = manager
        self._maintenance = maintenance
        self._registry = registry
        self.db_path = db_path
        self._dir = os.path.dirname(os.path.abspath(db_path))
        self._read_only = read_only
        self._clock = clock
        self._disk_usage = disk_usage
        self._wal_size = wal_size if wal_size is not None else maintenance.wal_size_bytes
        self._disk_interval_s = disk_interval_s
        self._wal_interval_s = wal_interval_s
        self._quick_check_delay_s = quick_check_delay_s
        self._wal_limit_bytes = wal_limit_bytes

        self._started = False
        self._stopped = False
        self._next_disk = 0.0
        self._next_wal = 0.0
        self._quick_check_at: Optional[float] = None

        # 磁盘空间状态
        self._warned = False        # 1 GB 提示已发、尚未回升到 1.2 GB
        self._critical = False      # 处于"严重不足"，直到回升到 300 MB 以上
        self._disk_error_logged = False
        self.last_free_bytes: Optional[int] = None
        self._paused_ids: Set[str] = set()

        # 后台作业
        self._wal_worker: Optional[_WalCheckpointWorker] = None
        self._qc_worker: Optional[_QuickCheckWorker] = None
        self.integrity_summary: Optional[str] = None   # 已提示过的 quick_check 摘要
        self.last_quick_check = None                    # 最近一次 QuickCheckResult

        self._timer = QTimer(self)
        self._timer.setInterval(tick_interval_ms)
        self._timer.timeout.connect(self.tick)

        for name in ('task_resumed', 'task_stopped', 'task_removed'):
            sig = getattr(manager, name, None)
            if sig is not None:
                sig.connect(self._on_task_left_pause)

    # ========== 生命周期 ==========

    def start(self) -> None:
        """开始定时检查：首次磁盘检查在下一次 tick（调用方先连好信号再 start）。重复调用无副作用"""
        if self._started or self._stopped:
            return
        self._started = True
        now = self._clock()
        self._next_disk = now
        self._next_wal = now + self._wal_interval_s
        self._quick_check_at = (None if self._quick_check_delay_s is None
                                else now + self._quick_check_delay_s)
        self._timer.start()
        logger.info("存储守卫已启动（磁盘 %.0f 秒 / -wal %.0f 秒一次，quick_check %s）",
                    self._disk_interval_s, self._wal_interval_s,
                    "不运行" if self._quick_check_at is None
                    else f"启动后 {self._quick_check_delay_s:.0f} 秒")

    def stop(self) -> None:
        """停止定时器并取消在途后台作业（幂等，不等待：等待由线程登记表在退出收尾里做）"""
        if self._stopped:
            return
        self._stopped = True
        self._timer.stop()
        for worker in (self._qc_worker, self._wal_worker):
            if worker is not None:
                try:
                    worker.cancel()
                except RuntimeError:
                    pass   # 线程对象已被删除
        logger.info("存储守卫已停止")

    @property
    def is_running(self) -> bool:
        return self._started and not self._stopped

    def tick(self) -> None:
        """每秒一次：到点的检查各做一次。任何一项出错只记日志，不影响其他项。"""
        if self._stopped:
            return
        now = self._clock()
        if now >= self._next_disk:
            self._next_disk = now + self._disk_interval_s
            self._guarded(self.check_disk, "磁盘空间检查")
        if now >= self._next_wal:
            self._next_wal = now + self._wal_interval_s
            self._guarded(self.check_wal, "-wal 兜底检查")
        if self._quick_check_at is not None and now >= self._quick_check_at:
            self._guarded(lambda: self._maybe_start_quick_check(now), "quick_check 启动")

    @staticmethod
    def _guarded(func: Callable[[], None], what: str) -> None:
        try:
            func()
        except Exception:
            logger.error("存储守卫：%s失败", what, exc_info=True)

    # ========== 磁盘空间 ==========

    @property
    def is_critical(self) -> bool:
        """是否处于"空间严重不足"（低于 200 MB 起，到回升 300 MB 以上止）"""
        return self._critical

    def paused_task_ids(self) -> FrozenSet[str]:
        """守卫因空间不足暂停、用户尚未手动恢复的任务 id"""
        return frozenset(self._paused_ids)

    def check_disk(self) -> Optional[int]:
        """读一次剩余空间并套用阈值状态机；返回剩余字节（读不到为 None）"""
        try:
            free = int(self._disk_usage(self._dir).free)
        except OSError:
            if not self._disk_error_logged:
                self._disk_error_logged = True
                logger.warning("无法读取磁盘剩余空间: %s", self._dir, exc_info=True)
            return None
        self._disk_error_logged = False
        self.last_free_bytes = free
        self._apply_free_space(free)
        return free

    def _apply_free_space(self, free: int) -> None:
        if free < PAUSE_FREE_BYTES:
            newly = self._pause_running_tasks()
            if not self._critical:
                self._enter_critical(free, newly, "剩余空间低于 200 MB")
            elif newly:
                logger.warning("磁盘剩余空间仍低于 200 MB（%s），再次暂停了 %d 个任务: %s",
                               format_size(free), len(newly), ", ".join(newly))
            return
        if self._critical and free >= RECOVER_FREE_BYTES:
            self._critical = False
            logger.info("磁盘剩余空间已回升到 %s，可继续监控（守卫暂停的任务不会自动恢复，需手动继续）",
                        format_size(free))
            self.space_recovered.emit(free)
        if free < WARN_FREE_BYTES:
            if not self._warned:
                self._warned = True
                logger.warning("磁盘剩余空间不足 1 GB: %s（%s）", format_size(free), self._dir)
                self.low_space.emit(free)
        elif self._warned and free >= WARN_REARM_FREE_BYTES:
            self._warned = False

    def _enter_critical(self, free: int, newly_paused: List[str], reason: str) -> None:
        self._critical = True
        self._warned = True    # 直接跌穿 1 GB 时不再补发一次较轻的提示
        logger.error("磁盘空间严重不足（%s）：剩余 %s，数据库 %s，已暂停 %d 个任务: %s",
                     reason, format_size(free), self.db_path, len(newly_paused),
                     ", ".join(newly_paused) or "无")
        self.space_critical.emit(free, tuple(newly_paused))

    def _pause_running_tasks(self) -> List[str]:
        """暂停全部运行中（未暂停、未在停止）的任务，返回本次新暂停的 id"""
        newly: List[str] = []
        for info in self._manager.list_task_infos():
            task_id = info.task_id
            if (not self._manager.is_task_active(task_id)
                    or self._manager.is_task_paused(task_id)):
                continue
            try:
                paused = self._manager.pause_task(task_id)
            except Exception:
                logger.error("因磁盘空间不足暂停任务失败: %s", task_id, exc_info=True)
                continue
            if paused:
                newly.append(task_id)
                self._paused_ids.add(task_id)
        if newly:
            self.space_paused_changed.emit(frozenset(self._paused_ids))
        return newly

    def _on_task_left_pause(self, task_id: str, *_args) -> None:
        """任务被恢复、已结束或已移除：它不再是"因空间不足暂停"的任务"""
        if task_id in self._paused_ids:
            self._paused_ids.discard(task_id)
            self.space_paused_changed.emit(frozenset(self._paused_ids))

    @pyqtSlot()
    def report_storage_full(self) -> None:
        """数据层报告"磁盘已满"（StorageFullError / SQLITE_FULL）：与"低于 200 MB"同一路径——
        暂停全部运行中任务并进入严重不足状态。可由采集线程经信号（队列连接）调用。

        剩余空间以磁盘实际读数为准（读不到沿用上次的值或 0）；若实测空间其实充裕
        （配额、max_page_count 等原因），下一次检查会按"已回升"处理。
        """
        if self._stopped:
            return
        free = self._read_free()
        newly = self._pause_running_tasks()
        if not self._critical:
            self._enter_critical(free, newly, "数据层报告磁盘已满")
        elif newly:
            logger.warning("数据层报告磁盘已满，再次暂停了 %d 个任务: %s",
                           len(newly), ", ".join(newly))

    def _read_free(self) -> int:
        """读剩余空间但不套用状态机（report_storage_full 用）；读不到返回上次的值或 0"""
        try:
            self.last_free_bytes = int(self._disk_usage(self._dir).free)
        except OSError:
            logger.warning("无法读取磁盘剩余空间: %s", self._dir, exc_info=True)
        return self.last_free_bytes or 0

    # ========== -wal 兜底 checkpoint ==========

    def check_wal(self) -> bool:
        """-wal 超过阈值就在后台线程做一次兜底 checkpoint；返回是否启动了作业。

        跳过的情形：只读库、已有 checkpoint 或 quick_check 在跑、数据库维护进行中、没超过阈值。
        """
        if self._read_only or self._stopped:
            return False
        if self._wal_worker is not None or self._qc_worker is not None:
            return False
        if self._maintenance.is_busy():
            logger.info("数据库维护进行中，跳过本轮 -wal 兜底 checkpoint")
            return False
        size = self._wal_size()
        if size <= self._wal_limit_bytes:
            return False
        if not self._maintenance.begin_storage_job():
            logger.info("数据库维护进行中，跳过本轮 -wal 兜底 checkpoint")
            return False
        logger.warning("-wal 已达 %s（超过 %s），启动兜底 checkpoint",
                       format_size(size), format_size(self._wal_limit_bytes))
        worker = _WalCheckpointWorker(self._maintenance)
        self._wal_worker = worker
        worker.finished.connect(lambda w=worker: self._on_wal_finished(w))
        try:
            self._registry.register(worker, name=WAL_THREAD_NAME, cancel=worker.cancel,
                                    timeout_ms=WAL_THREAD_TIMEOUT_MS)
            worker.start()
        except Exception:
            # 线程没能启动就不会有 finished，登记必须在这里撤销
            self._wal_worker = None
            self._maintenance.end_storage_job()
            raise
        return True

    def _on_wal_finished(self, worker) -> None:
        self._maintenance.end_storage_job()
        if self._wal_worker is worker:
            self._wal_worker = None

    @property
    def wal_job_running(self) -> bool:
        return self._wal_worker is not None

    # ========== quick_check ==========

    def _maybe_start_quick_check(self, now: float) -> bool:
        """到点后启动 quick_check；维护进行中或兜底 checkpoint 在跑时推迟 30 秒"""
        if self._stopped or self._qc_worker is not None:
            return False
        if self._wal_worker is not None or self._maintenance.is_busy():
            self._quick_check_at = now + QUICK_CHECK_RETRY_S
            return False
        self._quick_check_at = None    # 只跑一次
        worker = _QuickCheckWorker(self._maintenance)
        self._qc_worker = worker
        worker.result.connect(self._on_quick_check_result)
        worker.finished.connect(lambda w=worker: self._on_quick_check_finished(w))
        try:
            self._registry.register(worker, name=QUICK_CHECK_THREAD_NAME, cancel=worker.cancel,
                                    timeout_ms=QUICK_CHECK_THREAD_TIMEOUT_MS)
            worker.start()
        except Exception:
            self._qc_worker = None
            raise
        logger.info("开始后台数据库完整性检查（quick_check，只读连接）")
        return True

    def _on_quick_check_finished(self, worker) -> None:
        if self._qc_worker is worker:
            self._qc_worker = None

    def _on_quick_check_result(self, result) -> None:
        if result is None:
            return
        self.last_quick_check = result
        if result.interrupted:
            logger.info("数据库完整性检查被中止（耗时 %.2f 秒）", result.elapsed_s)
        elif result.error:
            logger.warning("数据库完整性检查未能完成（耗时 %.2f 秒）: %s",
                           result.elapsed_s, result.error)
        elif result.ok:
            logger.info("数据库完整性检查通过（quick_check = ok，耗时 %.2f 秒）", result.elapsed_s)
        else:
            logger.error("数据库完整性检查未通过（耗时 %.2f 秒）：%s",
                         result.elapsed_s, "；".join(result.messages))
            if self.integrity_summary is None:
                self.integrity_summary = result.summary
                self.integrity_problem.emit(self.integrity_summary)
        self.quick_check_finished.emit(result)

    @property
    def quick_check_running(self) -> bool:
        return self._qc_worker is not None
