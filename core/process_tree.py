"""
进程组目标（tree / name）的成员解析与逐实例读数

两种组目标：
- tree：根进程及其全部后代；根进程退出（含 PID 复用）即任务结束，子进程退出不影响；
- name：全部同名进程（名字不区分大小写）；成员归零不结束任务。

每个周期由 ProcessTreeSource.observe 回答"现在组里有哪些进程实例、各自的读数是什么"，
聚合（求和、虚拟累计、成员归并）不在这里，见 core.group_aggregator。接口放在 Protocol
后面，测试注入假实现，不碰真实进程。

生产实现 SnapshotTreeSource 从全系统快照（SnapshotHub，一次系统调用服务全部任务）解析
父 PID 与创建时间，快照不可用或取失败时整周期回退 psutil。

后代判定防 PID 复用：Windows 不会在父进程退出后给子进程换父，子进程记录的父 PID 可能
已被一个全新的进程占用。子进程的创建时间不早于父进程才算后代；已经确认过是成员的
实例（pid + 创建时间都对得上）即使中间的父进程退出了也继续算（沿用它的后代）。
本程序自身永远不算组成员（监控自己没有意义，还会污染读数）。
"""
import json
import logging
import os
import time
import weakref
from dataclasses import dataclass, field
from typing import (Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Protocol,
                    Sequence, Set, Tuple)

import psutil

from core import process_catalog
from core.contracts import UnavailableReason
from core.process_collector import (DEFAULT_SNAPSHOT_MAX_AGE_S, SAMPLERS, MetricNotSupported,
                                    read_snapshot_metric, uses_snapshot)
from core.snapshot_hub import SnapshotHub, default_hub
from data.models import TARGET_NAME, TARGET_TREE
from utils.metrics import MetricType

logger = logging.getLogger(__name__)

# 创建时间比对容差（秒）：与 MonitorWorker 启动时的 PID 复用比对同一口径
CREATE_TIME_TOLERANCE = 0.01
# 永远不算成员的 PID：System Idle Process 与 System（创建时间都是 0.0，父 PID 也是 0）
_NEVER_MEMBER_PIDS = frozenset({0, 4})

# 实例的唯一标识：(pid, 创建时间)；PID 被复用时创建时间不同，是另一个实例
InstanceKey = Tuple[int, float]


@dataclass(frozen=True)
class GroupSpec:
    """组目标的参数。tree 用 root_pid（root_create_time 为用户所选进程的创建时间，手动输入
    PID 时为 None）；name 用 name。持久化为 tasks.target_spec 的 JSON。"""
    kind: str
    root_pid: Optional[int] = None
    root_create_time: Optional[float] = None
    name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind == TARGET_TREE:
            if not isinstance(self.root_pid, int) or isinstance(self.root_pid, bool):
                raise ValueError("tree 目标需要整数 root_pid")
        elif self.kind == TARGET_NAME:
            if not (self.name and self.name.strip()):
                raise ValueError("name 目标需要进程名")
        else:
            raise ValueError(f"进程组目标只能是 tree 或 name，收到 {self.kind!r}")

    @classmethod
    def tree(cls, root_pid: int, root_create_time: Optional[float] = None) -> "GroupSpec":
        return cls(TARGET_TREE, root_pid=root_pid, root_create_time=root_create_time)

    @classmethod
    def by_name(cls, name: str) -> "GroupSpec":
        return cls(TARGET_NAME, name=name.strip())

    def to_json(self) -> str:
        """tasks.target_spec 的 JSON 文本：tree 存根 PID 与创建时间，name 存进程名。"""
        if self.kind == TARGET_TREE:
            data = {'root_pid': self.root_pid, 'root_create_time': self.root_create_time}
        else:
            data = {'name': self.name}
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, kind: str, text: Optional[str]) -> "GroupSpec":
        """由 (target_kind, target_spec) 还原；内容缺失或格式不对抛 ValueError。"""
        try:
            data = json.loads(text) if text else {}
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"target_spec 不是合法 JSON：{exc}") from None
        if not isinstance(data, dict):
            raise ValueError("target_spec 必须是 JSON 对象")
        if kind == TARGET_TREE:
            return cls.tree(data.get('root_pid'), data.get('root_create_time'))
        return cls.by_name(str(data.get('name') or ''))


@dataclass(frozen=True)
class InstanceReading:
    """一个进程实例在本周期的读数。

    values：各指标的原值，已换算到存储单位（字节类为 KB）；不含 CPU 使用率（由 cpu_seconds
    在聚合时按实例差分）与进程总数。unavailable：本周期读不到的指标及原因。
    cpu_seconds：用户态 + 内核态 CPU 时间（秒），没有为 None。
    """
    pid: int
    create_time: float
    name: str
    ppid: int = 0
    values: Mapping[str, float] = field(default_factory=dict)
    unavailable: Mapping[str, UnavailableReason] = field(default_factory=dict)
    cpu_seconds: Optional[float] = None

    @property
    def key(self) -> InstanceKey:
        return (self.pid, self.create_time)


@dataclass(frozen=True)
class Observation:
    """一个周期的观察结果。

    taken_monotonic：快照（或读数）所在的单调时钟，CPU 使用率差分的时间基准；
    instances：按组目标解析出的成员实例（tree 为根与后代，name 为同名进程）；
    root_alive：仅 tree 有意义，根进程是否仍是原来那个（PID 被复用算已退出）；name 恒为 True；
    root_create_time：tree 的根进程创建时间（根不在为 None）；
    source：'snapshot' / 'psutil'（诊断与测试用）。
    """
    taken_monotonic: float
    instances: Tuple[InstanceReading, ...] = ()
    root_alive: bool = True
    root_create_time: Optional[float] = None
    source: str = 'snapshot'


class ProcessTreeSource(Protocol):
    """进程组成员的来源（可注入；测试用假实现）。

    observe 每个采集周期调用一次：解析组目标当前的成员并读出 metrics 里的各指标。
    root_create_time 是调用方已确认的根进程创建时间，非 None 时根的创建时间必须与它
    一致，否则按"根已退出"处理（PID 被复用）；None 表示尚未确认（任务启动时的首次观察，
    由调用方从返回的 Observation.root_create_time 取得并锁定）。not_before 是本周期
    可接受的最早快照时刻（SnapshotHub.not_before_for），None 不限制。
    取不到任何数据时抛异常（采集线程按"本周期采集失败"处理，不停任务）。
    """

    def observe(self, spec: GroupSpec, metrics: Sequence[str], *,
                root_create_time: Optional[float] = None,
                not_before: Optional[float] = None) -> Observation: ...


# ---------------------------------------------------------------- 成员解析（纯函数）


class ProcLite(NamedTuple):
    """成员解析用的最小进程信息（字段名与 ProcessSample 一致，二者可混用）。"""
    pid: int
    parent_pid: int
    name: str
    create_time: float


def same_create_time(a: Optional[float], b: Optional[float]) -> bool:
    """两个创建时间是否属于同一个进程实例（容差见 CREATE_TIME_TOLERANCE）。"""
    return a is not None and b is not None and abs(a - b) <= CREATE_TIME_TOLERANCE


def resolve_tree(procs: Mapping[int, Any], root_pid: int,
                 root_create_time: Optional[float] = None,
                 known: Iterable[InstanceKey] = (),
                 exclude: Iterable[int] = ()) -> Optional[List[int]]:
    """
    解析 tree 目标的成员 PID（根在最前，其余按发现顺序）；根不存在、或给了
    root_create_time 而与现在的根不一致（PID 复用）时返回 None。

    procs：{pid: 带 parent_pid / create_time 属性的条目}。后代 = 从根（以及仍存活的
    known 成员）出发，沿"父 PID 指向它"的关系向下找，且子进程的创建时间不早于父进程
    （防止父 PID 已被新进程复用的孤儿被错认）。exclude 里的 PID（本程序自身）不进成员。
    """
    root = procs.get(root_pid)
    if root is None or root_pid in _NEVER_MEMBER_PIDS:
        return None
    if root_create_time is not None and not same_create_time(root.create_time,
                                                             root_create_time):
        return None
    excluded = set(exclude)
    children: Dict[int, List[int]] = {}
    for pid, proc in procs.items():
        parent = proc.parent_pid
        if pid == parent or pid in _NEVER_MEMBER_PIDS:
            continue
        children.setdefault(parent, []).append(pid)

    members: List[int] = [root_pid]
    seen: Set[int] = {root_pid}
    queue: List[int] = [root_pid]
    for pid, create_time in known:
        proc = procs.get(pid)
        if (proc is not None and pid not in seen and pid not in excluded
                and pid not in _NEVER_MEMBER_PIDS
                and same_create_time(proc.create_time, create_time)):
            seen.add(pid)
            members.append(pid)
            queue.append(pid)
    while queue:
        parent_pid = queue.pop(0)
        parent = procs[parent_pid]
        for child_pid in children.get(parent_pid, ()):
            if child_pid in seen or child_pid in excluded:
                continue
            if procs[child_pid].create_time < parent.create_time:
                continue   # 比"父进程"还早出生：记录的父 PID 已被复用，不是真后代
            seen.add(child_pid)
            members.append(child_pid)
            queue.append(child_pid)
    return members


def resolve_by_name(procs: Mapping[int, Any], name: str,
                    exclude: Iterable[int] = ()) -> List[int]:
    """name 目标的成员 PID：映像名与 name 相同（不区分大小写）的进程，按 PID 升序；
    exclude（本程序自身）与系统空闲 / System 进程不算。"""
    wanted = name.strip().lower()
    excluded = set(exclude)
    return sorted(pid for pid, proc in procs.items()
                  if pid not in excluded and pid not in _NEVER_MEMBER_PIDS
                  and proc.name.lower() == wanted)


# ---------------------------------------------------------------- 生产实现


class _InstanceGone(Exception):
    """读数过程中发现实例已经退出（快照与读取之间的窗口）。"""


class SnapshotTreeSource:
    """
    生产用的 ProcessTreeSource：成员解析与大部分指标来自全系统快照，快照里没有的指标
    （GDI/USER 对象数、USS、基本优先级）逐实例用 psutil 读；快照不可用或取失败时整个周期
    回退 psutil。有状态（记住已确认的成员、缓存 psutil.Process 对象），每个任务一个实例。
    """

    def __init__(self, hub: Optional[SnapshotHub] = None, *,
                 max_age_s: float = DEFAULT_SNAPSHOT_MAX_AGE_S,
                 exclude_pids: Optional[Iterable[int]] = None):
        self._hub = hub
        self._max_age_s = max_age_s
        self._exclude = frozenset(exclude_pids if exclude_pids is not None
                                  else (os.getpid(),))
        self._known: Dict[int, float] = {}
        self._processes: Dict[InstanceKey, psutil.Process] = {}
        # 上一次取用的快照（弱引用）：再拿到同一份就强制取新的，否则累计量会在两个时间戳上写同一个值
        self._last_snapshot: Optional[weakref.ref] = None
        self._fallback_logged = False
        self._failed_metrics: Set[str] = set()

    # ---------- 入口 ----------

    def observe(self, spec: GroupSpec, metrics: Sequence[str], *,
                root_create_time: Optional[float] = None,
                not_before: Optional[float] = None) -> Observation:
        snapshot = self._take_snapshot(not_before)
        if snapshot is not None:
            return self._observe_snapshot(snapshot, spec, metrics, root_create_time)
        return self._observe_psutil(spec, metrics, root_create_time)

    # ---------- 快照路径 ----------

    def _take_snapshot(self, not_before: Optional[float]):
        """向 hub 取本周期的快照；不可用或取失败返回 None（调用方回退 psutil）。"""
        hub = self._hub if self._hub is not None else default_hub()
        try:
            if not hub.available():
                self._log_fallback("全系统快照不可用（%s），进程组改用 psutil 采集",
                                   hub.unavailable_reason)
                return None
            if not_before is None:
                snapshot = hub.get(self._max_age_s)
            else:
                snapshot = hub.get(self._max_age_s, not_before=not_before)
                last = self._last_snapshot() if self._last_snapshot is not None else None
                if last is snapshot:
                    snapshot = hub.refresh()
            try:
                self._last_snapshot = weakref.ref(snapshot)
            except TypeError:
                self._last_snapshot = None
            return snapshot
        except Exception:
            self._log_fallback("取全系统快照失败，进程组本周期改用 psutil 采集（之后不再重复记录）",
                               exc_info=True)
            return None

    def _log_fallback(self, message: str, *args, exc_info: bool = False) -> None:
        if not self._fallback_logged:
            self._fallback_logged = True
            logger.warning(message, *args, exc_info=exc_info)

    def _observe_snapshot(self, snapshot, spec: GroupSpec, metrics: Sequence[str],
                          root_create_time: Optional[float]) -> Observation:
        procs = snapshot.processes
        root_alive, root_ct, pids = self._resolve(procs, spec, root_create_time)
        snapshot_metrics = [m for m in metrics if uses_snapshot(m)
                            and m != MetricType.CPU_PERCENT]
        process_metrics = [m for m in metrics if not uses_snapshot(m) and m in SAMPLERS
                           and m != MetricType.CPU_PERCENT]
        readings: List[InstanceReading] = []
        for pid in pids:
            sample = procs[pid]
            values: Dict[str, float] = {}
            unavailable: Dict[str, UnavailableReason] = {}
            for metric in snapshot_metrics:
                value = read_snapshot_metric(metric, snapshot, sample)
                if value is None:
                    unavailable[metric] = UnavailableReason.ERROR
                else:
                    values[metric] = value
            try:
                if process_metrics:
                    self._read_with_psutil(sample.pid, sample.create_time, process_metrics,
                                           values, unavailable)
            except _InstanceGone:
                continue
            readings.append(InstanceReading(
                pid=pid, create_time=sample.create_time,
                name=process_catalog.display_name(pid, sample.name),
                ppid=sample.parent_pid, values=values, unavailable=unavailable,
                cpu_seconds=sample.user_time + sample.kernel_time))
        self._finish(readings)
        return Observation(snapshot.taken_at.monotonic, tuple(readings), root_alive,
                           root_ct, 'snapshot')

    # ---------- psutil 路径 ----------

    def _observe_psutil(self, spec: GroupSpec, metrics: Sequence[str],
                        root_create_time: Optional[float]) -> Observation:
        procs: Dict[int, ProcLite] = {}
        for proc in psutil.process_iter(['pid', 'ppid', 'name', 'create_time']):
            info = proc.info
            name, create_time = info.get('name'), info.get('create_time')
            if not name or create_time is None:
                continue
            procs[info['pid']] = ProcLite(info['pid'], info.get('ppid') or 0, name,
                                          create_time)
        root_alive, root_ct, pids = self._resolve(procs, spec, root_create_time)
        wanted = [m for m in metrics if m in SAMPLERS and m != MetricType.CPU_PERCENT]
        taken = time.monotonic()
        readings: List[InstanceReading] = []
        for pid in pids:
            proc = procs[pid]
            values: Dict[str, float] = {}
            unavailable: Dict[str, UnavailableReason] = {}
            cpu_seconds: Optional[float] = None
            try:
                handle = self._handle(pid, proc.create_time)
                try:
                    times = handle.cpu_times()
                    cpu_seconds = times.user + times.system
                except psutil.NoSuchProcess:
                    raise _InstanceGone() from None
                except psutil.Error:
                    pass   # 读不到 CPU 时间：该实例本周期不参与 CPU 使用率
                self._read_with_psutil(pid, proc.create_time, wanted, values, unavailable)
            except _InstanceGone:
                continue
            readings.append(InstanceReading(
                pid=pid, create_time=proc.create_time, name=proc.name, ppid=proc.parent_pid,
                values=values, unavailable=unavailable, cpu_seconds=cpu_seconds))
        self._finish(readings)
        return Observation(taken, tuple(readings), root_alive, root_ct, 'psutil')

    # ---------- 共用 ----------

    def _resolve(self, procs: Mapping[int, Any], spec: GroupSpec,
                 root_create_time: Optional[float]):
        """(根是否仍在, 根创建时间, 成员 PID 列表)。"""
        if spec.kind == TARGET_TREE:
            pids = resolve_tree(procs, spec.root_pid, root_create_time,
                                known=self._known.items(), exclude=self._exclude)
            if pids is None:
                self._known.clear()
                return False, None, []
            return True, procs[spec.root_pid].create_time, pids
        return True, None, resolve_by_name(procs, spec.name, self._exclude)

    def _finish(self, readings: Sequence[InstanceReading]) -> None:
        """记住本周期的成员（tree 的后代判定靠它），清掉已退出实例的 psutil 句柄缓存。"""
        self._known = {r.pid: r.create_time for r in readings}
        alive = {r.key for r in readings}
        for key in [k for k in self._processes if k not in alive]:
            del self._processes[key]

    def _handle(self, pid: int, create_time: float) -> psutil.Process:
        """(pid, 创建时间) 对应的 psutil.Process；PID 已被别的进程复用或进程已退出抛
        _InstanceGone。"""
        key = (pid, create_time)
        handle = self._processes.get(key)
        if handle is None:
            try:
                handle = psutil.Process(pid)
                if not same_create_time(handle.create_time(), create_time):
                    raise _InstanceGone()
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OverflowError):
                raise _InstanceGone() from None
            self._processes[key] = handle
        return handle

    def _read_with_psutil(self, pid: int, create_time: float, metrics: Sequence[str],
                          values: Dict[str, float],
                          unavailable: Dict[str, UnavailableReason]) -> None:
        """逐指标用 psutil 读一个实例；失败归类为不可用原因（与单进程采集同一套归类）。"""
        if not metrics:
            return
        handle = self._handle(pid, create_time)
        try:
            with handle.oneshot():
                for metric in metrics:
                    self._read_one(handle, metric, values, unavailable)
        except psutil.NoSuchProcess:
            raise _InstanceGone() from None

    def _read_one(self, handle: psutil.Process, metric: str, values: Dict[str, float],
                  unavailable: Dict[str, UnavailableReason]) -> None:
        sampler = SAMPLERS.get(metric)
        if sampler is None:
            return
        try:
            values[metric] = sampler(handle)
        except psutil.AccessDenied:
            unavailable[metric] = UnavailableReason.ACCESS_DENIED
        except psutil.ZombieProcess:
            # ZombieProcess 是 NoSuchProcess 的子类，必须先捕获
            unavailable[metric] = UnavailableReason.PROCESS_EXITING
        except psutil.NoSuchProcess:
            raise
        except MetricNotSupported as exc:
            unavailable[metric] = exc.reason
        except Exception:
            if metric not in self._failed_metrics:
                self._failed_metrics.add(metric)
                logger.exception("进程组采集指标 %s 失败（PID %s）", metric, handle.pid)
            unavailable[metric] = UnavailableReason.ERROR
