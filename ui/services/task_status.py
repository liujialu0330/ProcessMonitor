"""
任务运行状态查询（TaskStatusProvider）

页面只需要知道"某个任务是否仍在运行"，不需要拿到 core 侧的任务对象或 QThread：
经由该服务查询，测试可以直接给出一组"运行中"的任务 ID。
"""
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.contracts import TaskStatus

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager


@runtime_checkable
class TaskStatusProvider(Protocol):
    """任务运行状态查询服务。"""

    def is_running(self, task_id: str) -> bool:
        """任务线程是否仍在运行（含暂停中与停止收尾中；已结束或不在 manager 中为否）。"""
        ...


class ManagerTaskStatus:
    """生产实现：取 MonitorManager.get_task_info 快照，状态为 RUNNING 即视为运行中。

    停止收尾中仍算运行：此时库里的 end_time 可能还没写入，页面若按"已停止"去回查
    数据库会读到不完整的结束信息。
    """

    def __init__(self, manager: "MonitorManager"):
        self._manager = manager

    def is_running(self, task_id: str) -> bool:
        """任务线程是否仍在运行。"""
        info = self._manager.get_task_info(task_id)
        return info is not None and info.status is TaskStatus.RUNNING
