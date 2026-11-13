"""
被测进程重启后的查找

被测进程退出、任务进入"等待重启"后，采集线程每秒调用一次查找函数：按进程名（与"等待进程
启动后自动开始"同一套规则：没写扩展名时也试 .exe，同名多个取最新启动的）找"旧进程退出之后
才启动"的同名进程。什么样的进程才算"重启"，条件是收紧的——宁可多等一会儿，也不能把别的
进程认成被测进程、让数据串到一起：

- 创建时间必须读得到：创建时间未知的候选一律不接；
- 创建时间不得早于旧进程的退出时刻减 2 秒（容差覆盖时钟读数与句柄时间的细小差别）。退出
  时刻优先取退出码句柄的 ExitTime，取不到时用最后一次成功采样的时刻。这样"同一个程序早已
  运行的另一个实例"，以及旧进程退出前就存在的同名进程（Chromium 式的多进程程序，子进程与
  主进程同名同路径）都不会被接上；
- 父进程是旧进程（或进程树任务里旧树的任何成员）的候选不接：它们是旧进程自己派生的子进程，
  即使在退出前几秒才创建（落在容差内），也不是"重启"；
- 任务启动时记下了可执行文件路径的，路径不同的同名进程（别的程序恰好同名）不算。候选的路径
  读不到（权限不足）时不排除它——宁可续接也不要因为读不到路径而一直等下去。

查找函数的接口是 RestartFinder：一个可调用对象 (RestartTarget) -> Optional[RestartHit]，
测试注入假实现，生产用 ProcessRestartFinder。
"""
import logging
from dataclasses import dataclass
from typing import Callable, FrozenSet, Optional, Protocol, Tuple

import psutil

from core import process_catalog
from core.wait_service import candidate_names

logger = logging.getLogger(__name__)

# 候选的创建时间可以早于"旧进程退出时刻"多少秒（容差，见模块说明）
EXIT_TOLERANCE_S = 2.0
# 与旧进程创建时间比较时的容差（与采集器的 PID 复用比对同一量级）
CREATE_TIME_TOLERANCE_S = 0.01


@dataclass(frozen=True)
class RestartTarget:
    """要等的进程。

    name / exe_path：进程名与上一个实例的可执行文件路径（可能未知）；
    last_pid / last_create_time：上一个实例的 PID 与创建时间（可能未知）；
    exit_time：上一个实例的退出时刻（epoch 秒，退出码句柄的 ExitTime，可能未知）；
    last_seen_time：最后一次成功采样的时刻（epoch 秒），exit_time 未知时用它；
    ancestor_pids：旧进程自己（进程树任务还有旧树的全部成员）的 PID——它们的子进程不是"重启"。
    """
    name: str
    exe_path: Optional[str]
    last_pid: int
    last_create_time: Optional[float]
    exit_time: Optional[float] = None
    last_seen_time: Optional[float] = None
    ancestor_pids: FrozenSet[int] = frozenset()

    def not_before(self) -> Optional[float]:
        """候选的创建时间下限（epoch 秒）；旧进程的退出时刻与最后采样时刻都未知时为 None。"""
        reference = self.exit_time if self.exit_time is not None else self.last_seen_time
        return reference - EXIT_TOLERANCE_S if reference is not None else None


@dataclass(frozen=True)
class RestartHit:
    """找到的新实例。"""
    pid: int
    create_time: Optional[float]
    name: str


class RestartFinder(Protocol):
    def __call__(self, target: RestartTarget) -> Optional[RestartHit]:
        ...


def exe_path_of(pid: int) -> Optional[str]:
    """进程的可执行文件路径；读不到（已退出、无权限）为 None。"""
    try:
        return psutil.Process(pid).exe() or None
    except (psutil.Error, OSError):
        return None


def _same_path(a: str, b: str) -> bool:
    return a.replace('/', '\\').rstrip('\\').lower() == b.replace('/', '\\').rstrip('\\').lower()


class ProcessRestartFinder:
    """生产实现：经进程目录（快照）按名查找。"""

    def __init__(self, process_query=process_catalog,
                 exe_of: Callable[[int], Optional[str]] = exe_path_of):
        self._query = process_query
        self._exe_of = exe_of

    def __call__(self, target: RestartTarget) -> Optional[RestartHit]:
        found: list = []
        for candidate in candidate_names(target.name):
            for pid, create_time, parent_pid in self._query.find_instances_by_name(candidate):
                if not self.is_new_instance(target, pid, create_time, parent_pid):
                    continue
                if not self._path_matches(target, pid):
                    continue
                found.append((create_time, pid, candidate))
            if found:
                break
        if not found:
            return None
        # 同名多个取最新启动的
        created, pid, name = max(found, key=lambda item: item[0])
        if len(found) > 1:
            logger.info("重启后找到 %d 个同名进程 %s，取最新启动的 PID %s", len(found), name, pid)
        return RestartHit(pid=pid, create_time=created, name=name)

    @staticmethod
    def is_new_instance(target: RestartTarget, pid: int, create_time: Optional[float],
                        parent_pid: int = 0) -> bool:
        """候选是不是"旧进程退出之后才启动的新实例"（规则见模块说明）。"""
        if create_time is None or pid == target.last_pid:
            return False
        if parent_pid and parent_pid in target.ancestor_pids:
            return False
        if (target.last_create_time is not None
                and create_time <= target.last_create_time + CREATE_TIME_TOLERANCE_S):
            return False
        not_before = target.not_before()
        return not_before is None or create_time >= not_before

    def _path_matches(self, target: RestartTarget, pid: int) -> bool:
        if not target.exe_path:
            return True
        path = self._exe_of(pid)
        return path is None or _same_path(path, target.exe_path)


def default_restart_finder() -> RestartFinder:
    return ProcessRestartFinder()


__all__: Tuple[str, ...] = ('RestartTarget', 'RestartHit', 'RestartFinder',
                            'ProcessRestartFinder', 'default_restart_finder', 'exe_path_of')
