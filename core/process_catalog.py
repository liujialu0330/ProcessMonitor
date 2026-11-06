"""
进程目录（只读查询）

列出本机进程、按 PID 取名称、判断是否存活、按名称查找、取本程序自身 PID。
与采样无关的进程查询都在这里；ProcessCollector 只负责对一个已选定进程采集指标。
所有函数只读 psutil，不修改任何进程。
"""
import os
from typing import List, NamedTuple, Optional, Tuple

import psutil


class ProcessEntry(NamedTuple):
    """进程列表的一项；是元组，可按 (pid, name, create_time) 解包"""
    pid: int
    name: str
    # 进程创建时间（epoch 秒）；读取失败时为 None（调用方据此跳过身份比对）
    create_time: Optional[float]


def list_processes() -> List[ProcessEntry]:
    """
    当前运行中的进程，按进程名（不区分大小写）排序；进程名为空的跳过。

    带上创建时间：列表可能已过期，从选中到真正开始采集之间 PID 可能被别的进程
    复用，监控任务据此确认采到的仍是所选的那个进程。
    """
    entries: List[ProcessEntry] = []
    for proc in psutil.process_iter(['pid', 'name', 'create_time']):
        try:
            name = proc.info['name']
            if name and name.strip():
                entries.append(ProcessEntry(proc.info['pid'], name, proc.info.get('create_time')))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    entries.sort(key=lambda e: e.name.lower())
    return entries


def name_of(pid: int) -> Optional[str]:
    """进程名；进程不存在或无权访问时返回 None"""
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except (ValueError, OverflowError):
        # 负数或超出范围的 PID
        return None


def is_alive(pid: int) -> bool:
    """进程是否仍在运行（不存在或无权访问均视为否）"""
    try:
        return psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except (ValueError, OverflowError):
        return False


def self_pid() -> int:
    """本程序自身的 PID"""
    return os.getpid()


def find_pids_by_name(name: str, exclude_self: bool = True) -> List[Tuple[int, Optional[float]]]:
    """
    按进程名精确查找（不区分大小写），返回 [(pid, 创建时间)]，顺序同 list_processes。

    Args:
        name: 进程名（如 python.exe）
        exclude_self: 是否排除本程序自身。按名重新监控时必须排除：被测的同名进程
            （如 python.exe）退出后，不能因为只剩本程序同名而自动监控起自己
    """
    wanted = name.lower()
    own = self_pid() if exclude_self else None
    return [(e.pid, e.create_time) for e in list_processes()
            if e.name.lower() == wanted and e.pid != own]


def search_by_name(keyword: str) -> List[Tuple[int, str]]:
    """按进程名部分匹配（不区分大小写），返回 [(pid, 进程名)]，按进程名排序"""
    wanted = keyword.lower()
    return [(e.pid, e.name) for e in list_processes() if wanted in e.name.lower()]
