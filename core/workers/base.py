"""
后台短命线程的统一写法

此前导出、历史刷新、清理、更新检查、下载五类 worker 各写一套取消、过期判断、
异常处理与信号收尾，释放方式也各不相同（更新线程甚至从不释放）。这里收敛为：

- CancelToken：线程安全的取消标志，worker 在循环里轮询；
- BackgroundTask(QThread)：run() 只调子类的 execute(token)，返回值经 result 发出，
  异常一律捕获、记日志后经 failed 发出；被取消后既不发 result 也不发 failed——
  取消方（通常是退出路径）不再关心结果，槽里也不应再去更新可能已销毁的界面；
- LatestRequestGate："最新请求获胜"：每次发起请求取一个新序号，结果回来时只有
  序号仍是最新的才应用，过期结果直接丢弃。

信号约定：结果/失败信号是 run() 的最后一个动作（子类在 execute 里已完成全部收尾）；
线程对象的释放由线程登记表（core.shutdown.ThreadRegistry）挂在 QThread.finished 上，
页面不要在这些业务信号的槽里释放线程对象。
"""
import logging
import threading
from typing import Any, Optional

from PyQt5.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)


class CancelToken:
    """线程安全的取消标志：任意线程调用 cancel()，工作线程轮询 is_cancelled。"""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        """请求取消（可重复调用）"""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """是否已请求取消"""
        return self._event.is_set()


class TaskError(Exception):
    """预期内的失败：消息直接展示给用户。

    execute 已自行记录了足够的日志（或这类失败本就不值得记堆栈，如"仓库暂无发布
    版本"），基类不再带堆栈记一次 error。
    """


class BackgroundTask(QThread):
    """后台任务线程基类：子类实现 execute(token)，不要覆盖 run()。

    子类若需要保留具名信号（如 export_finished），覆盖 handle_result /
    handle_failure，在其中先调用父类实现再发具名信号。
    """

    # 成功结果（execute 的返回值）
    result = pyqtSignal(object)
    # 失败描述（面向用户的文本）
    failed = pyqtSignal(str)
    # 进度（含义由子类约定，如百分比或已处理条数）
    progress = pyqtSignal(int)

    def __init__(self, parent=None, *, request_seq: Optional[int] = None):
        """
        Args:
            parent: 父对象。一般不传：线程对象由登记表持有引用，挂在页面下反而会在
                页面先于线程销毁时触发"线程仍在运行就被析构"的致命错误
            request_seq: 发起本次请求时取得的序号（LatestRequestGate.next_seq），
                结果回到主线程时据此判断是否过期；不需要过期判断时为 None
        """
        super().__init__(parent)
        self._token = CancelToken()
        self.request_seq = request_seq

    # ---------- 取消 ----------

    @property
    def token(self) -> CancelToken:
        """本线程的取消令牌"""
        return self._token

    def cancel(self) -> None:
        """请求取消：只置标志，execute 在下一个检查点自行结束（不阻塞调用方）"""
        self._token.cancel()

    @property
    def is_cancelled(self) -> bool:
        """是否已请求取消"""
        return self._token.is_cancelled

    # ---------- 子类接口 ----------

    def execute(self, token: CancelToken) -> Any:
        """在工作线程中执行的任务主体；返回值经 result 发出，抛异常即失败。"""
        raise NotImplementedError

    def failure_message(self, exc: Exception) -> str:
        """把异常转换为面向用户的失败描述（子类可覆盖以加前缀）"""
        return str(exc) or type(exc).__name__

    def handle_result(self, value: Any) -> None:
        """成功收尾：发出 result（子类覆盖时先调用父类再发具名信号）"""
        self.result.emit(value)

    def handle_failure(self, exc: Exception) -> None:
        """失败收尾：记日志并发出 failed（子类覆盖时先调用父类再发具名信号）"""
        if not isinstance(exc, TaskError):
            logger.error("后台任务执行失败: %s", type(self).__name__, exc_info=exc)
        self.failed.emit(self.failure_message(exc))

    # ---------- 线程入口 ----------

    def run(self) -> None:
        try:
            value = self.execute(self._token)
        except Exception as exc:
            if self._token.is_cancelled:
                logger.info("后台任务已取消，忽略收尾中的异常: %s (%s)",
                            type(self).__name__, exc)
                return
            try:
                self.handle_failure(exc)
            except Exception:
                logger.error("后台任务失败收尾出错: %s", type(self).__name__, exc_info=True)
            return
        if self._token.is_cancelled:
            return
        try:
            self.handle_result(value)
        except Exception:
            logger.error("后台任务结果收尾出错: %s", type(self).__name__, exc_info=True)


class LatestRequestGate:
    """"最新请求获胜"的序号门：发起请求时 next_seq() 取号，结果回来时 is_current(seq)
    判断是否仍是最新；invalidate() 让所有在途请求过期（如用户切换了查询条件）。

    序号只增不减，线程安全（通常只在主线程使用，加锁成本可忽略）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0

    def next_seq(self) -> int:
        """发起新请求：返回新序号，此前的序号全部过期"""
        with self._lock:
            self._seq += 1
            return self._seq

    def invalidate(self) -> None:
        """使在途请求全部过期（不发起新请求）"""
        with self._lock:
            self._seq += 1

    def is_current(self, seq: Optional[int]) -> bool:
        """seq 是否仍是最新请求的序号"""
        with self._lock:
            return seq is not None and seq == self._seq

    @property
    def current(self) -> int:
        """当前最新序号（0 表示尚未发起过请求）"""
        with self._lock:
            return self._seq
