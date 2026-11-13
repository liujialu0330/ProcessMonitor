"""
进程列表读取线程

psutil 遍历全部进程（含创建时间）冷启动要 2 秒上下，放在界面线程会推迟主窗口
显示、让刷新按钮点下去卡住界面（RULES.md §5：耗时操作不放 UI 线程）。这里只是
把注入的进程查询放到 QThread 里跑，列表经 entries_ready 回到界面线程。

查询对象只要有 list_processes() 即可（界面层的 ProcessQuery 协议），core 不反向依赖
ui；生产实现每次调用都是独立的只读 psutil 遍历，可在工作线程调用。
"""
import logging
from typing import Any, List, Optional

from PyQt5.QtCore import pyqtSignal

from core.workers.base import BackgroundTask, CancelToken

logger = logging.getLogger(__name__)

__all__ = ['ProcessListWorker', 'THREAD_NAME', 'SHUTDOWN_TIMEOUT_MS']

# 登记到线程登记表时的名称与退出等待上限：遍历不可中断，冷启动最慢约 2.5 秒
THREAD_NAME = "读取进程列表"
SHUTDOWN_TIMEOUT_MS = 3000


class ProcessListWorker(BackgroundTask):
    """在工作线程读取一次进程列表：ProcessListWorker(process_query, request_seq=)。

    信号约定：entries_ready(request_seq, entries) 是 run() 的最后一个动作（同时发基类
    的 result）；失败只发基类的 failed，调用方保留旧列表。取消只是让结果不再发出，
    遍历本身无法中途打断，退出时由线程登记表带超时等待。
    """

    # (请求序号, [(pid, 进程名, 创建时间)])
    entries_ready = pyqtSignal(object, object)

    def __init__(self, process_query: Any, parent=None, *,
                 request_seq: Optional[int] = None):
        super().__init__(parent, request_seq=request_seq)
        self._query = process_query

    def execute(self, token: CancelToken) -> List[tuple]:
        entries = list(self._query.list_processes())
        logger.debug("进程列表读取完成：%d 项", len(entries))
        return entries

    def failure_message(self, exc: Exception) -> str:
        return f"读取进程列表失败：{super().failure_message(exc)}"

    def handle_result(self, value: List[tuple]) -> None:
        super().handle_result(value)
        self.entries_ready.emit(self.request_seq, value)
