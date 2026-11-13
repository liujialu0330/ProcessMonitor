"""
等待进程启动后自动开始（WaitService）

监控页"等待进程启动后自动开始"与命令行 start --wait 共用：按进程名轮询，进程出现即
交给启动函数建任务。服务本身不建定时器——由调用方以约 1 秒调用 poll()（监控页 Presenter
的等待定时器、无窗口模式与 IPC 的定时器），测试直接调用 poll() 驱动。

- add(request, starter=None, timeout_s=None) -> wait_id：request 只需有 name 属性（其余字段
  原样交给启动函数）；进程名没写扩展名时也试一下加 .exe；
- 同名进程有多个时报告歧义，避免自动监控错误的目标；
- starter(wait_id, match) -> Optional[task_id]：建任务；返回 None 表示没建成（只读库、
  任务数已满等），随后发 failed(wait_id, 'start_failed')；缺省用构造时给的启动函数；
- timeout_s 到点仍未出现 → failed(wait_id, 'timeout')（时间经注入的时钟，只用 now()）；
- cancel(wait_id) 取消，不发信号。

只用 manager 的公开命令（缺省启动函数：create_task + start_task），不接触任务线程对象。
"""
import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

WAIT_FAILED_TIMEOUT = 'timeout'
WAIT_FAILED_START = 'start_failed'
WAIT_FAILED_AMBIGUOUS = 'ambiguous'
WAIT_ID_PREFIX = 'wait-'


@dataclass(frozen=True)
class WaitMatch:
    """进程出现时的匹配结果。"""
    pid: int
    name: str
    create_time: Optional[float]
    request: Any


Starter = Callable[[str, WaitMatch], Optional[str]]


class AmbiguousProcessError(ValueError):
    """同名目标不唯一，必须选择 PID 或改为同名进程组。"""

    def __init__(self, name: str, pids):
        self.pids = tuple(sorted(set(pids)))
        super().__init__(f"找到多个 {name}（PID：{', '.join(map(str, self.pids))}），"
                         "请选择具体 PID，或改用同名进程组")


def candidate_names(name: str) -> List[str]:
    """要查找的进程名：没写扩展名时也试一下加 .exe（多数人只记得程序名）。"""
    names = [name]
    if '.' not in name:
        names.append(f"{name}.exe")
    return names


@dataclass
class _Wait:
    wait_id: str
    request: Any
    starter: Optional[Starter]
    deadline: Optional[datetime]


class WaitService(QObject):
    """等待进程启动的登记表。

    信号：
        started(str, str)：(wait_id, task_id) 进程已出现并建好任务
        failed(str, str)：(wait_id, 原因 WAIT_FAILED_*)
    """

    started = pyqtSignal(str, str)
    failed = pyqtSignal(str, str)

    def __init__(self, manager, process_query, clock, parent: Optional[QObject] = None, *,
                 starter: Optional[Starter] = None, id_prefix: str = WAIT_ID_PREFIX):
        """
        Args:
            manager: MonitorManager（缺省启动函数用它建任务）
            process_query: 有 find_pids_by_name(name) -> [(pid, 创建时间)] 的对象
                （ui.services.ProcessQuery 或 core.process_catalog 模块）
            clock: 提供 now() 的时钟
            starter: 缺省启动函数；None 时用 create_task + start_task
            id_prefix: 等待 ID 前缀（两个服务实例共存时便于区分）
        """
        super().__init__(parent)
        self._manager = manager
        self._query = process_query
        self._clock = clock
        self._starter = starter or self._default_starter
        self._prefix = id_prefix
        self._ids = itertools.count(1)
        self._waits: Dict[str, _Wait] = {}
        self._ambiguity = None

    # ---------- 登记 ----------

    def add(self, request: Any, starter: Optional[Starter] = None,
            timeout_s: Optional[float] = None) -> str:
        wait_id = f"{self._prefix}{next(self._ids)}"
        deadline = (self._clock.now() + timedelta(seconds=timeout_s)
                    if timeout_s is not None else None)
        self._waits[wait_id] = _Wait(wait_id, request, starter, deadline)
        return wait_id

    def cancel(self, wait_id: str) -> bool:
        return self._waits.pop(wait_id, None) is not None

    def wait_ids(self) -> List[str]:
        return list(self._waits)

    def request_of(self, wait_id: str) -> Any:
        entry = self._waits.get(wait_id)
        return entry.request if entry is not None else None

    def has_waits(self) -> bool:
        return bool(self._waits)

    def failure_pids(self, wait_id: str) -> List[int]:
        failure = self._ambiguity
        return list(failure[1].pids) if failure and failure[0] == wait_id else []

    # ---------- 轮询 ----------

    def find(self, name: str) -> Optional[Tuple[int, str, Optional[float]]]:
        """按名查找唯一实例；同名多个抛 AmbiguousProcessError。"""
        for candidate in candidate_names(name):
            matches = self._query.find_pids_by_name(candidate)
            if matches:
                if len({m[0] for m in matches}) > 1:
                    raise AmbiguousProcessError(candidate, (m[0] for m in matches))
                pid, create_time = matches[0]
                return pid, candidate, create_time
        return None

    def poll(self) -> None:
        """查一遍各等待中的进程：出现即移除该等待并交给启动函数；超时的发 failed。"""
        now = self._clock.now()
        for wait_id, entry in list(self._waits.items()):
            if wait_id not in self._waits:
                continue
            try:
                found = self.find(entry.request.name)
            except AmbiguousProcessError as exc:
                self._waits.pop(wait_id, None)
                self._ambiguity = (wait_id, exc)
                try:
                    self.failed.emit(wait_id, f'{WAIT_FAILED_AMBIGUOUS}:{exc}')
                finally:
                    self._ambiguity = None
                continue
            except Exception:
                logger.error("按名查找进程失败: %s", entry.request.name, exc_info=True)
                found = None
            if found is None:
                if entry.deadline is not None and now >= entry.deadline:
                    self._waits.pop(wait_id, None)
                    logger.info("等待进程启动超时: %s (%s)", entry.request.name, wait_id)
                    self.failed.emit(wait_id, WAIT_FAILED_TIMEOUT)
                continue
            pid, name, create_time = found
            self._waits.pop(wait_id, None)
            starter = entry.starter or self._starter
            match = WaitMatch(pid=pid, name=name, create_time=create_time, request=entry.request)
            try:
                task_id = starter(wait_id, match)
            except Exception:
                logger.error("等待到进程后建任务失败: %s", name, exc_info=True)
                task_id = None
            if task_id:
                logger.info("等待的进程已启动并开始监控: %s PID %s (%s)", name, pid, wait_id)
                self.started.emit(wait_id, task_id)
            else:
                self.failed.emit(wait_id, WAIT_FAILED_START)

    # ---------- 缺省启动函数 ----------

    def _default_starter(self, _wait_id: str, match: WaitMatch) -> Optional[str]:
        request = match.request
        task_id = self._manager.create_task(
            match.pid, match.name, list(getattr(request, 'metrics', ()) or ()),
            getattr(request, 'interval', None), note=getattr(request, 'note', '') or '',
            create_time=match.create_time)
        if task_id:
            self._manager.start_task(task_id)
        return task_id
