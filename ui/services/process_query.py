"""
进程查询（ProcessQuery）

监控页的进程列表、按 PID 取进程名、判断进程是否还在、排除本程序自身等查询，
统一经由注入的 ProcessQuery：测试可以给出固定的进程表，不再依赖本机实际进程。
生产实现委托 core.process_catalog（只读 psutil 查询）。
"""
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from core import process_catalog

# (pid, 进程名, 创建时间)：创建时间为 epoch 秒，读取失败时为 None
ProcessEntry = Tuple[int, str, Optional[float]]


@runtime_checkable
class ProcessQuery(Protocol):
    """进程查询服务（只读）。"""

    def list_processes(self) -> List[ProcessEntry]:
        """当前运行中的进程列表，按进程名（不区分大小写）排序。

        创建时间用于启动任务时确认 PID 没有被别的进程复用。
        """
        ...

    def name_of(self, pid: int) -> Optional[str]:
        """进程名；进程不存在或无权访问时返回 None。"""
        ...

    def is_alive(self, pid: int) -> bool:
        """进程是否仍在运行（不存在或无权访问均视为否）。"""
        ...

    def self_pid(self) -> int:
        """本程序自身的 PID：按名重新监控时要排除它，避免监控到自己。"""
        ...

    def find_pids_by_name(self, name: str,
                          exclude_self: bool = True) -> List[Tuple[int, Optional[float]]]:
        """按进程名精确查找（不区分大小写），返回 [(pid, 创建时间)]。

        exclude_self 默认排除本程序自身（按名重新监控时不能监控到自己）。
        """
        ...


class PsutilProcessQuery:
    """生产实现：委托 core.process_catalog（psutil 只读查询）。"""

    def list_processes(self) -> List[ProcessEntry]:
        """当前运行中的进程列表。"""
        return [tuple(entry) for entry in process_catalog.list_processes()]

    def name_of(self, pid: int) -> Optional[str]:
        """进程名；不存在或无权访问时返回 None。"""
        return process_catalog.name_of(pid)

    def is_alive(self, pid: int) -> bool:
        """进程是否仍在运行。"""
        return process_catalog.is_alive(pid)

    def self_pid(self) -> int:
        """本程序自身的 PID。"""
        return process_catalog.self_pid()

    def find_pids_by_name(self, name: str,
                          exclude_self: bool = True) -> List[Tuple[int, Optional[float]]]:
        """按进程名精确查找。"""
        return process_catalog.find_pids_by_name(name, exclude_self=exclude_self)
