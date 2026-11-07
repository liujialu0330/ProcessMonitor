"""
进程目录（只读查询）

列出本机进程、按 PID 取名称、判断是否存活、按名称查找、取本程序自身 PID。
与采样无关的进程查询都在这里；ProcessCollector 只负责对一个已选定进程采集指标。
所有函数只读，不修改任何进程。

进程列表优先取全系统快照（core.snapshot_hub，一次系统调用，冷启动约十几毫秒），
快照不可用或取失败时回退 psutil.process_iter（冷启动约 2 秒）；两条路径的口径相同：
(pid, 进程名, 创建时间)，进程名为空的跳过，按进程名不区分大小写排序，PID 0 显示为
"System Idle Process"、PID 0/4 的创建时间为 0.0（与 psutil 一致）。
按 PID 取名称与判断存活只查单个进程，仍直接用 psutil（不受快照缓存年龄影响）。
"""
import logging
import os
from typing import TYPE_CHECKING, List, NamedTuple, Optional, Tuple

import psutil

from core import snapshot_hub

if TYPE_CHECKING:
    from core.snapshot_hub import SnapshotHub

logger = logging.getLogger(__name__)

# 进程列表允许使用的快照最大年龄（秒）：监控页刷新列表有 2 秒节流，半秒内的快照足够新，
# 又能与同一时刻的采集线程共用一份
LIST_MAX_AGE_S = 0.5

# psutil 对这两个 PID 的固定显示名（任务管理器与 Process Hacker 同样如此显示）；
# 快照里 PID 0 的映像名为空
_PSUTIL_FIXED_NAMES = {0: "System Idle Process", 4: "System"}

_fallback_logged = False


class ProcessEntry(NamedTuple):
    """进程列表的一项；是元组，可按 (pid, name, create_time) 解包"""
    pid: int
    name: str
    # 进程创建时间（epoch 秒）；读取失败时为 None（调用方据此跳过身份比对）
    create_time: Optional[float]


def _default_hub() -> "SnapshotHub":
    """进程列表默认使用的快照中心（与监控任务共用的进程内实例）。"""
    return snapshot_hub.default_hub()


def display_name(pid: int, image_name: str) -> str:
    """快照映像名换成与 psutil name() 一致的显示名（PID 0/4 用固定名）。"""
    return _PSUTIL_FIXED_NAMES.get(pid, image_name)


def _from_snapshot(hub: Optional["SnapshotHub"]) -> Optional[List[ProcessEntry]]:
    """从快照取进程列表；快照不可用或取失败返回 None（调用方回退 psutil）。"""
    global _fallback_logged
    if hub is None:
        hub = _default_hub()
    if hub is None:
        return None
    try:
        if not hub.available():
            return None
        snapshot = hub.get(LIST_MAX_AGE_S)
    except Exception:
        if not _fallback_logged:
            _fallback_logged = True
            logger.warning("从全系统快照读取进程列表失败，改用 psutil（之后不再重复记录）",
                           exc_info=True)
        return None
    entries: List[ProcessEntry] = []
    for pid, sample in snapshot.processes.items():
        name = display_name(pid, sample.name)
        if name and name.strip():
            entries.append(ProcessEntry(pid, name, sample.create_time))
    return entries


def _from_psutil() -> List[ProcessEntry]:
    entries: List[ProcessEntry] = []
    for proc in psutil.process_iter(['pid', 'name', 'create_time']):
        try:
            name = proc.info['name']
            if name and name.strip():
                entries.append(ProcessEntry(proc.info['pid'], name, proc.info.get('create_time')))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return entries


def list_processes(hub: Optional["SnapshotHub"] = None) -> List[ProcessEntry]:
    """
    当前运行中的进程，按进程名（不区分大小写）排序；进程名为空的跳过。

    带上创建时间：列表可能已过期，从选中到真正开始采集之间 PID 可能被别的进程
    复用，监控任务据此确认采到的仍是所选的那个进程。

    Args:
        hub: 快照中心；None 用进程内共享实例。快照不可用时回退 psutil
    """
    entries = _from_snapshot(hub)
    if entries is None:
        entries = _from_psutil()
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


def find_pids_by_name(name: str, exclude_self: bool = True,
                      hub: Optional["SnapshotHub"] = None) -> List[Tuple[int, Optional[float]]]:
    """
    按进程名精确查找（不区分大小写），返回 [(pid, 创建时间)]，顺序同 list_processes。

    Args:
        name: 进程名（如 python.exe）
        exclude_self: 是否排除本程序自身。按名重新监控时必须排除：被测的同名进程
            （如 python.exe）退出后，不能因为只剩本程序同名而自动监控起自己
        hub: 同 list_processes
    """
    wanted = name.lower()
    own = self_pid() if exclude_self else None
    return [(e.pid, e.create_time) for e in list_processes(hub)
            if e.name.lower() == wanted and e.pid != own]


def search_by_name(keyword: str,
                   hub: Optional["SnapshotHub"] = None) -> List[Tuple[int, str]]:
    """按进程名部分匹配（不区分大小写），返回 [(pid, 进程名)]，按进程名排序"""
    wanted = keyword.lower()
    return [(e.pid, e.name) for e in list_processes(hub) if wanted in e.name.lower()]
