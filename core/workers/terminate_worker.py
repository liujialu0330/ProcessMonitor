"""
"停止时关闭被测程序"的后台线程

ProcessLauncher.terminate 会先等被测程序响应 WM_CLOSE（最多数秒）再结束进程，不能放在
界面线程。监控页与命令处理器在任务因用户停止而结束后各自调用 close_in_background：
线程登记到线程登记表（退出时一并收尾），结果只记日志。
"""
import logging
from typing import Optional, Set

from core.workers.base import BackgroundTask, CancelToken

logger = logging.getLogger(__name__)

THREAD_NAME = "关闭被测程序"
# 退出路径上等它的上限：WM_CLOSE 的等待加结束进程的确认
THREAD_TIMEOUT_MS = 8000


class TerminateWorker(BackgroundTask):
    """在后台关闭一个被测程序；结果为 TERMINATE_* 字符串。关闭过程不可中断。"""

    def __init__(self, launcher, pid: int, create_time: Optional[float], parent=None):
        super().__init__(parent)
        self.launcher = launcher
        self.pid = pid
        self.create_time = create_time

    def execute(self, token: CancelToken) -> str:
        outcome = self.launcher.terminate(self.pid, self.create_time)
        logger.info("停止时关闭被测程序: pid=%s 结果=%s", self.pid, outcome)
        return outcome


# 没有登记表时由这里持有线程引用，线程结束后释放（运行中的 QThread 被回收会致命中止）
_unregistered: Set[TerminateWorker] = set()


def _release(worker: TerminateWorker) -> None:
    _unregistered.discard(worker)
    worker.deleteLater()


def close_in_background(launcher, pid: int, create_time: Optional[float],
                        registry=None) -> TerminateWorker:
    """启动关闭线程并返回它；有线程登记表时登记（退出时收尾并释放），否则由本模块持有
    引用直到线程结束。"""
    worker = TerminateWorker(launcher, pid, create_time)
    if registry is not None:
        registry.register(worker, name=THREAD_NAME, timeout_ms=THREAD_TIMEOUT_MS)
    else:
        _unregistered.add(worker)
        worker.finished.connect(lambda w=worker: _release(w))
    worker.start()
    return worker
