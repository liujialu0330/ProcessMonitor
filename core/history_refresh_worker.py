"""
历史数据查询的后台线程外壳

查询本身在 core.history_query.run_history_query（同步、与线程无关）；这里只负责把它
放到 QThread 里跑，结果打包成快照经信号发回主线程。数据量到 50 万行量级时这些查询
在主线程执行会让界面卡顿数秒（RULES.md §5：耗时操作不放 UI 线程），所以分析页的
所有加载都经它执行，绝不在工作线程触碰任何 Qt 控件。

Database 的仓储每次操作都新建 sqlite 连接，可直接在工作线程调用（不共享连接对象）。
"""
import logging

from PyQt5.QtCore import pyqtSignal

from core.history_query import HistoryRequest, HistorySnapshot, run_history_query
from core.workers.base import BackgroundTask, CancelToken
from data.errors import StorageError

logger = logging.getLogger(__name__)

__all__ = ['HistoryRefreshWorker']


class HistoryRefreshWorker(BackgroundTask):
    """
    历史数据查询后台线程：HistoryRefreshWorker(db, request)。

    信号约定：result_ready 是 run() 的最后一个动作，成功与失败都经它返回一个
    HistorySnapshot（失败时 error 非空），同时也发基类的 result/failed。请求序号存于
    request_seq，调用方用 LatestRequestGate 判断结果是否过期。查询只读、不改库，
    取消只是让结果不再发出；退出时由线程登记表带超时 join 兜底，线程对象的释放由
    登记表挂在 QThread.finished 上。
    """

    # 结果信号：携带 HistorySnapshot
    result_ready = pyqtSignal(object)

    def __init__(self, db, request: HistoryRequest, parent=None):
        if not isinstance(request, HistoryRequest):
            raise TypeError("HistoryRefreshWorker 需要 (db, HistoryRequest)")
        super().__init__(parent, request_seq=request.request_seq)
        self._db = db
        self._request = request

    @property
    def request(self) -> HistoryRequest:
        """本线程执行的查询条件"""
        return self._request

    def execute(self, token: CancelToken) -> HistorySnapshot:
        return run_history_query(self._db, self._request)

    def handle_result(self, value: HistorySnapshot) -> None:
        super().handle_result(value)
        self.result_ready.emit(value)

    def handle_failure(self, exc: Exception) -> None:
        logger.error("历史数据查询失败", exc_info=exc)
        message = self.failure_message(exc)
        self.failed.emit(message)
        self.result_ready.emit(HistorySnapshot(
            request=self._request, error=message,
            storage_error=isinstance(exc, StorageError)))
