"""
线程登记表与退出协调器

此前各页面各自持有后台线程、各自连 deleteLater，主窗口退出时再逐页调用
background_threads() 收尾：页面多一个线程就要改两处，漏登记的线程会被入口的
os._exit 在写库/写文件中途掐断。现在：

- ThreadRegistry：页面启动短命线程时登记一次（名称、取消回调、等待超时），线程
  结束（QThread.finished）时自动注销并 deleteLater；退出时 shutdown_all 按登记
  顺序统一收尾。登记表持有线程的强引用，线程对象不必再挂在页面下防回收。
- ShutdownCoordinator：真退出时按固定顺序收尾（停监控任务 → 带超时 join 任务 →
  登记表里的短命线程 → 关闭数据库），有总时间预算，超时只记日志，绝不抛异常。

放在 core 而非 app 组装层：主窗口（ui）运行时要构造协调器，ui 不得依赖 app。
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from PyQt5.QtCore import QObject, QThread

from utils.thread_utils import shutdown_thread

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[str], None]]

# 退出进度文案（经 on_progress 报给界面）
PROGRESS_STOPPING_TASKS = "正在停止监控任务…"
PROGRESS_STOPPING_THREADS = "正在结束后台操作…"
PROGRESS_SAVING = "正在保存…"


def _report(on_progress: ProgressCallback, text: str) -> None:
    """报告进度；回调本身出错只记日志，不影响退出流程"""
    if on_progress is None:
        return
    try:
        on_progress(text)
    except Exception:
        logger.error("退出进度回调失败", exc_info=True)


@dataclass
class RegisteredThread:
    """一个已登记、需要在退出时收尾的短命线程。

    Attributes:
        name: 线程用途（日志与进度文案用）
        thread: 线程对象
        cancel: 取消回调，等待前先调用；None 表示无取消机制、只等待
        timeout_ms: 等待超时（毫秒），超时只记日志不再等待
    """
    name: str
    thread: QThread
    cancel: Optional[Callable[[], None]]
    timeout_ms: int


class ThreadRegistry(QObject):
    """短命后台线程的登记表（只在主线程使用）。"""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._entries: List[RegisteredThread] = []

    def register(self, thread: QThread, *, name: str,
                 cancel: Optional[Callable[[], None]] = None,
                 timeout_ms: int = 2000) -> None:
        """登记线程（应在 start() 之前调用）。

        线程结束时自动注销并 deleteLater，调用方不要再自行连接 deleteLater，也不要
        在业务信号（结果/失败）的槽里释放线程对象。同一线程重复登记只保留第一次。
        """
        if self._find(thread) is not None:
            return
        self._entries.append(RegisteredThread(name, thread, cancel, timeout_ms))
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))

    def entries(self) -> List[RegisteredThread]:
        """当前已登记（尚未结束）的线程，按登记顺序"""
        return list(self._entries)

    def _find(self, thread: QThread) -> Optional[RegisteredThread]:
        for entry in self._entries:
            if entry.thread is thread:
                return entry
        return None

    def _on_thread_finished(self, thread: QThread) -> None:
        """QThread.finished（主线程）：注销并延迟释放线程对象"""
        entry = self._find(thread)
        if entry is not None:
            self._entries.remove(entry)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass  # 对象已被删除

    def shutdown_all(self, on_progress: ProgressCallback = None) -> bool:
        """退出时收尾全部已登记线程：先对仍在运行的线程逐个调用取消回调（让它们
        并行收尾），再按登记顺序逐个带超时等待。超时只记日志。

        Returns:
            bool: 全部线程都在各自超时前结束返回 True
        """
        entries = self.entries()
        running = [e for e in entries if self._is_running(e)]
        if not running:
            return True
        _report(on_progress, PROGRESS_STOPPING_THREADS)

        for entry in running:
            if entry.cancel is None:
                continue
            try:
                entry.cancel()
            except Exception:
                logger.error("后台线程取消回调失败: %s", entry.name, exc_info=True)

        all_finished = True
        for entry in running:
            try:
                finished = shutdown_thread(entry.thread, timeout_ms=entry.timeout_ms)
            except RuntimeError:
                finished = True   # 对象已被删除，线程必然已结束
            if not finished:
                all_finished = False
                logger.error("退出时后台线程未在 %dms 内结束: %s",
                             entry.timeout_ms, entry.name)
        return all_finished

    @staticmethod
    def _is_running(entry: RegisteredThread) -> bool:
        try:
            return entry.thread.isRunning()
        except RuntimeError:
            return False


class ShutdownCoordinator:
    """真退出路径的收尾顺序（closeEvent 只调用 run）。

    固定顺序：
    1. manager.stop_all_tasks()：给全部监控任务置停止标志；
    2. manager.join_all(task_timeout_ms)：带超时等待任务线程收尾落库；
    3. registry.shutdown_all()：登记表里的短命线程（下载、检查更新、导出、清理、
       历史刷新）取消并带超时等待；
    4. db.close()（给了 db 时）。

    total_budget_ms 是整个流程的时间预算：每步开始前检查，超出后跳过剩余的等待步骤
    （仍会尝试关闭数据库），记日志并返回 False。任何一步出错只记日志，绝不抛异常——
    调用方随后必须照常退出。
    """

    def __init__(self, manager, registry: ThreadRegistry, *,
                 task_timeout_ms: int = 3000, total_budget_ms: int = 30000, db=None):
        self.manager = manager
        self.registry = registry
        self.task_timeout_ms = task_timeout_ms
        self.total_budget_ms = total_budget_ms
        self.db = db

    def run(self, on_progress: ProgressCallback = None) -> bool:
        """执行退出收尾。

        Returns:
            bool: 全部步骤在预算内正常完成返回 True；有超时、失败或预算耗尽返回 False
        """
        deadline = time.monotonic() + self.total_budget_ms / 1000.0
        ok = True

        def remaining_ms() -> int:
            return int((deadline - time.monotonic()) * 1000)

        def budget_left(step: str) -> bool:
            nonlocal ok
            if remaining_ms() > 0:
                return True
            ok = False
            logger.error("退出收尾超出总预算 %dms，跳过：%s", self.total_budget_ms, step)
            return False

        _report(on_progress, PROGRESS_STOPPING_TASKS)
        try:
            self.manager.stop_all_tasks()
        except Exception:
            ok = False
            logger.error("退出时停止监控任务失败", exc_info=True)

        if budget_left("等待监控任务结束"):
            try:
                timeout = min(self.task_timeout_ms, remaining_ms())
                if not self.manager.join_all(timeout):
                    ok = False
                    logger.error("退出时有监控任务未在 %dms 内结束", timeout)
            except Exception:
                ok = False
                logger.error("退出时等待监控任务失败", exc_info=True)

        if budget_left("结束后台线程"):
            try:
                # 包一层：进度回调出错不应让登记表的收尾中断
                safe_progress = ((lambda text: _report(on_progress, text))
                                 if on_progress is not None else None)
                if not self.registry.shutdown_all(safe_progress):
                    ok = False
            except Exception:
                ok = False
                logger.error("退出时结束后台线程失败", exc_info=True)

        if self.db is not None:
            _report(on_progress, PROGRESS_SAVING)
            try:
                self.db.close()
            except Exception:
                ok = False
                logger.error("退出时关闭数据库失败", exc_info=True)

        if remaining_ms() < 0:
            ok = False
            logger.error("退出收尾超出总预算 %dms", self.total_budget_ms)
        return ok
