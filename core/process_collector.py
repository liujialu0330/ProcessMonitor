"""
进程信息采集器
负责从系统中采集进程的各项性能指标

两条路径：
- 注入 SnapshotHub 时，MetricSpec.cost 为 'snapshot' 的指标从全系统进程快照取
  （SNAPSHOT_FIELDS 与 SNAPSHOT_DERIVED），其余（GDI/USER、USS、优先级）仍逐进程调用；
  快照不可用或本周期取失败时整周期回退 psutil；
  CPU 使用率两条路径共用同一个差分起点 _cpu_prev（user+kernel 时间 / 单调时钟），
  回退周期与快照周期口径一致，不会因偶发回退而变成"自预热以来的平均"；
- 不注入 hub 时全部走 psutil（SAMPLERS），与旧版行为一致。
"""
import contextlib
import ctypes
import logging
import math
import operator
import sys
import time
import weakref
from ctypes import wintypes
import psutil
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
from core import process_catalog
from core.contracts import UnavailableReason
from core.system_snapshot import ProcessSample, SnapshotUnavailable, SystemSnapshot
from utils.metrics import COST_SNAPSHOT, REGISTRY, MetricType

if TYPE_CHECKING:
    from core.snapshot_hub import SnapshotHub

logger = logging.getLogger(__name__)

# ---------- GDI/USER 对象数（user32.GetGuiResources）----------
GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87

# 模块级惰性初始化的 Win32 函数，仅 Windows 平台使用
_win32_funcs = None


class MetricNotSupported(Exception):
    """该指标对目标进程不适用（不是权限问题，以管理员运行也无法采集）。

    reason 区分具体原因，界面文案由 ui/formatting 按枚举映射。
    """

    def __init__(self, reason: UnavailableReason = UnavailableReason.NOT_SUPPORTED):
        super().__init__(reason.value)
        self.reason = reason


def _init_win32():
    """惰性加载并声明 Win32 函数原型（显式 argtypes/restype，避免64位句柄截断）。"""
    global _win32_funcs
    if _win32_funcs is not None:
        return _win32_funcs
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD

    _win32_funcs = (kernel32, user32)
    return _win32_funcs


def _gui_resource(pid: int, flag: int) -> float:
    """
    读取进程的 GDI 或 USER 对象数（user32.GetGuiResources）

    Args:
        pid: 进程ID
        flag: GR_GDIOBJECTS(0) 或 GR_USEROBJECTS(1)

    Returns:
        float: 对象数

    Raises:
        psutil.NoSuchProcess: 进程已不存在
        psutil.AccessDenied: 无权限打开进程，或调用失败
        MetricNotSupported: 非 Windows，或进程不在当前会话（如 session 0 的系统服务）
    """
    if sys.platform != "win32":
        raise MetricNotSupported(UnavailableReason.NOT_SUPPORTED)

    kernel32, user32 = _init_win32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if not psutil.pid_exists(pid):
            raise psutil.NoSuchProcess(pid)
        raise psutil.AccessDenied(pid)
    try:
        ctypes.set_last_error(0)
        count = user32.GetGuiResources(handle, flag)
        if count == 0:
            error = ctypes.get_last_error()
            if error == _ERROR_INVALID_PARAMETER:
                # 其他会话的进程（如 session 0 的系统服务）返回 0 且 LastError=87；
                # 这类进程不在当前桌面会话，提权也无法采集，不能提示"权限不足"
                raise MetricNotSupported(UnavailableReason.NOT_IN_SESSION)
            if error != 0:
                raise psutil.AccessDenied(pid)
        return float(count)
    finally:
        kernel32.CloseHandle(handle)


# ---------- 采样函数表 ----------
# 每个指标一个采样函数：入参是 psutil.Process，返回已换算到存储单位的 float
# （字节类存 KB，时间类存秒，计数原样）。采样函数不捕获 psutil 异常，
# 由 ProcessCollector.collect_metrics 统一归类为不可用原因。
# 键必须与 utils.metrics 的指标注册表一致（含已废弃项，旧任务"重新监控"仍要能采）。

Sampler = Callable[[psutil.Process], float]


def _kb(num_bytes: float) -> float:
    """字节换算为 KB（数据库与 CSV 的字节类指标统一按 KB 存储）"""
    return num_bytes / 1024


def _mem_kb(field: str) -> Sampler:
    """memory_info() 某个字节字段，换算为 KB"""
    return lambda p: _kb(getattr(p.memory_info(), field))


def _io_kb(field: str) -> Sampler:
    """io_counters() 某个字节字段，换算为 KB"""
    return lambda p: _kb(getattr(p.io_counters(), field))


def _io_count(field: str) -> Sampler:
    """io_counters() 某个次数字段，原样返回"""
    return lambda p: float(getattr(p.io_counters(), field))


def _num_handles(p: psutil.Process) -> float:
    """句柄数；非 Windows 没有 num_handles，退回文件描述符数量"""
    try:
        return float(p.num_handles())
    except AttributeError:
        return float(p.num_fds())


def _gui_objects(flag: int) -> Sampler:
    return lambda p: _gui_resource(p.pid, flag)


SAMPLERS: Dict[str, Sampler] = {
    # 内存类。私有字节 = PrivateUsage = 任务管理器"提交大小"；
    # psutil 的 vms/pagefile/private 数值相同，只采 private
    MetricType.MEMORY_RSS: _mem_kb("rss"),
    MetricType.MEMORY_PERCENT: lambda p: float(p.memory_percent()),
    MetricType.MEMORY_PEAK_WSET: _mem_kb("peak_wset"),
    MetricType.MEMORY_PRIVATE: _mem_kb("private"),
    MetricType.MEMORY_PEAK_PAGEFILE: _mem_kb("peak_pagefile"),
    MetricType.MEMORY_PAGED_POOL: _mem_kb("paged_pool"),
    MetricType.MEMORY_PEAK_PAGED_POOL: _mem_kb("peak_paged_pool"),
    MetricType.MEMORY_NONPAGED_POOL: _mem_kb("nonpaged_pool"),
    MetricType.MEMORY_PEAK_NONPAGED_POOL: _mem_kb("peak_nonpaged_pool"),
    MetricType.MEMORY_NUM_PAGE_FAULTS: lambda p: float(p.memory_info().num_page_faults),
    MetricType.MEMORY_USS: lambda p: _kb(p.memory_full_info().uss),
    # CPU 类。cpu_percent(interval=None) 非阻塞、基于上次调用以来的增量，
    # 首次调用固定返回 0，所以任务开始前要先 prime_cpu()
    MetricType.CPU_PERCENT: lambda p: float(p.cpu_percent(interval=None)),
    MetricType.CPU_USER_TIME: lambda p: float(p.cpu_times().user),
    MetricType.CPU_SYSTEM_TIME: lambda p: float(p.cpu_times().system),
    MetricType.CPU_PRIORITY: lambda p: float(p.nice()),
    # 系统资源类
    MetricType.NUM_THREADS: lambda p: float(p.num_threads()),
    MetricType.NUM_HANDLES: _num_handles,
    MetricType.NUM_GDI_OBJECTS: _gui_objects(GR_GDIOBJECTS),
    MetricType.NUM_USER_OBJECTS: _gui_objects(GR_USEROBJECTS),
    MetricType.NUM_CTX_SWITCHES_VOL: lambda p: float(p.num_ctx_switches().voluntary),
    MetricType.NUM_CTX_SWITCHES_INVOL: lambda p: float(p.num_ctx_switches().involuntary),
    # IO 类
    MetricType.IO_READ_COUNT: _io_count("read_count"),
    MetricType.IO_WRITE_COUNT: _io_count("write_count"),
    MetricType.IO_OTHER_COUNT: _io_count("other_count"),
    MetricType.IO_READ_BYTES: _io_kb("read_bytes"),
    MetricType.IO_WRITE_BYTES: _io_kb("write_bytes"),
    MetricType.IO_OTHER_BYTES: _io_kb("other_bytes"),
}


# ---------- 快照取值表 ----------
# 指标键 -> ProcessSample 字段名：值原样取出，字节类再换算为 KB（_SNAPSHOT_KB_KEYS）。
# 与 psutil 的对应关系：memory_info() 的 rss/peak_wset/private/peak_pagefile/
# paged_pool/peak_paged_pool/nonpaged_pool/peak_nonpaged_pool/num_page_faults、
# cpu_times() 的 user/system、num_threads()、num_handles()、io_counters() 六项
# 在 psutil 内部读的就是同一组内核计数（private = PrivateUsage = PrivatePageCount，
# 实测单位为字节）。
SNAPSHOT_FIELDS: Dict[str, str] = {
    MetricType.MEMORY_RSS: 'working_set',
    MetricType.MEMORY_PEAK_WSET: 'peak_working_set',
    MetricType.MEMORY_PRIVATE: 'private_bytes',
    MetricType.MEMORY_PEAK_PAGEFILE: 'peak_pagefile',
    MetricType.MEMORY_PAGED_POOL: 'paged_pool',
    MetricType.MEMORY_PEAK_PAGED_POOL: 'peak_paged_pool',
    MetricType.MEMORY_NONPAGED_POOL: 'nonpaged_pool',
    MetricType.MEMORY_PEAK_NONPAGED_POOL: 'peak_nonpaged_pool',
    MetricType.MEMORY_NUM_PAGE_FAULTS: 'page_faults',
    MetricType.CPU_USER_TIME: 'user_time',
    MetricType.CPU_SYSTEM_TIME: 'kernel_time',
    MetricType.NUM_THREADS: 'num_threads',
    MetricType.NUM_HANDLES: 'handle_count',
    MetricType.IO_READ_COUNT: 'read_count',
    MetricType.IO_WRITE_COUNT: 'write_count',
    MetricType.IO_OTHER_COUNT: 'other_count',
    MetricType.IO_READ_BYTES: 'read_bytes',
    MetricType.IO_WRITE_BYTES: 'write_bytes',
    MetricType.IO_OTHER_BYTES: 'other_bytes',
}

_SNAPSHOT_KB_KEYS = frozenset({
    MetricType.MEMORY_RSS, MetricType.MEMORY_PEAK_WSET, MetricType.MEMORY_PRIVATE,
    MetricType.MEMORY_PEAK_PAGEFILE, MetricType.MEMORY_PAGED_POOL,
    MetricType.MEMORY_PEAK_PAGED_POOL, MetricType.MEMORY_NONPAGED_POOL,
    MetricType.MEMORY_PEAK_NONPAGED_POOL,
    MetricType.IO_READ_BYTES, MetricType.IO_WRITE_BYTES, MetricType.IO_OTHER_BYTES,
})

# 从快照派生、不是单个字段的指标（由 ProcessCollector._snapshot_value 计算）：
# CPU 使用率（user+kernel 差分）、内存使用率（工作集 / 物理内存）、上下文切换（线程求和）
SNAPSHOT_DERIVED = frozenset({
    MetricType.CPU_PERCENT, MetricType.MEMORY_PERCENT,
    MetricType.NUM_CTX_SWITCHES_VOL, MetricType.NUM_CTX_SWITCHES_INVOL,
})


def _snapshot_reader(key: str, attr: str) -> Callable[[ProcessSample], float]:
    getter = operator.attrgetter(attr)
    if key in _SNAPSHOT_KB_KEYS:
        return lambda s: getter(s) / 1024
    return lambda s: float(getter(s))


_SNAPSHOT_READERS: Dict[str, Callable[[ProcessSample], float]] = {
    key: _snapshot_reader(key, attr) for key, attr in SNAPSHOT_FIELDS.items()
}

# 未给出截止时间时向 hub 取快照允许的最大年龄（秒），与 1 秒周期的 SnapshotHub.max_age_for 一致
DEFAULT_SNAPSHOT_MAX_AGE_S = 0.5
# 快照与进程身份的创建时间允许误差（秒），与 MonitorWorker 启动时的比对口径相同
_CREATE_TIME_TOLERANCE = 0.01

# 整机物理内存（字节）：内存使用率的分母，开机后不变，取一次缓存
_total_physical_memory: Optional[int] = None


def _physical_memory_total() -> int:
    global _total_physical_memory
    if _total_physical_memory is None:
        _total_physical_memory = psutil.virtual_memory().total
    return _total_physical_memory


def _uses_snapshot(metric_type: str) -> bool:
    """该指标在快照路径下是否从快照取（按注册表的 cost 标注，且采集器有取值方法）。"""
    spec = REGISTRY.get(metric_type)
    return (spec is not None and spec.cost == COST_SNAPSHOT
            and (metric_type in _SNAPSHOT_READERS or metric_type in SNAPSHOT_DERIVED))


def uses_snapshot(metric_type: str) -> bool:
    """该指标在快照路径下是否从快照取（进程组采集与单进程采集共用同一判据）。"""
    return _uses_snapshot(metric_type)


def read_snapshot_metric(metric_type: str, snapshot: SystemSnapshot,
                         sample: ProcessSample) -> Optional[float]:
    """
    从快照里一个进程的条目取指标（已换算到存储单位）；取不到返回 None。
    不含 CPU 使用率：它要两次快照做差分，由各采集方自己按实例维护差分起点。
    """
    reader = _SNAPSHOT_READERS.get(metric_type)
    if reader is not None:
        return reader(sample)
    if metric_type == MetricType.MEMORY_PERCENT:
        return sample.working_set / _physical_memory_total() * 100
    if metric_type == MetricType.NUM_CTX_SWITCHES_VOL:
        switches = snapshot.context_switches(sample.pid)
        return float(switches) if switches is not None else None
    if metric_type == MetricType.NUM_CTX_SWITCHES_INVOL:
        # psutil 在 Windows 上此项恒为 0（指标已废弃，仅为旧任务"重新监控"保留）
        return 0.0
    return None


# collect 内部的哨兵：本周期改走 psutil 原路径
_FALLBACK = object()


def _monotonic() -> float:
    """CPU 差分用高精度单调时钟，与快照的 TakenAt.monotonic 同源。"""
    return time.perf_counter()


class _NoCpuBaseline(Exception):
    """CPU 使用率没有可用的差分起点（或窗口长度不是正数）：本周期该指标记为不可用。"""


class ProcessCollector:
    """进程信息采集器"""

    def __init__(self, pid: int, hub: Optional["SnapshotHub"] = None, *,
                 max_age_s: float = DEFAULT_SNAPSHOT_MAX_AGE_S):
        """
        初始化采集器

        Args:
            pid: 进程ID
            hub: 快照中心（可选）。注入后 cost 为 'snapshot' 的指标从全系统快照取；
                None 时全部走 psutil
            max_age_s: 没有 snapshot_not_before 时向 hub 取快照允许的最大年龄（秒），
                见 SnapshotHub.max_age_for
        """
        self.pid = pid
        self._process: Optional[psutil.Process] = None
        self._last_io_counters = None  # 用于计算IO增量
        # 最近一次 collect_metrics 中无法采集的指标：{指标类型: 原因}
        self.last_unavailable: Dict[str, UnavailableReason] = {}
        # 已记录过异常日志的指标，避免持续失败时每个周期都刷日志
        self._logged_failures = set()
        self._hub = hub
        self._max_age_s = max_age_s
        # 本周期可接受的最早快照时刻（hub 时钟），由 MonitorWorker 每周期设置，见
        # SnapshotHub.not_before_for；None 时退回按 max_age_s 判年龄
        self.snapshot_not_before: Optional[float] = None
        # 上一次取用的快照（弱引用，不拖住大缓冲区）：再次拿到同一份就强制取新的，
        # 否则累计量会在两个时间戳上写同一个值，速率曲线出现 0 与翻倍
        self._last_snapshot: Optional[weakref.ref] = None
        # 进程身份：首次在快照里见到本进程时确定的创建时间（psutil 已绑定进程时以它为准）
        self._identity_create_time: Optional[float] = None
        # CPU 使用率差分的上一个点：(user+kernel 秒, 快照单调时刻)
        self._cpu_prev: Optional[Tuple[float, float]] = None
        # 回退 psutil 只记一次日志
        self._fallback_logged = False
        # 最近一次 collect_metrics 的数据来源：'snapshot' / 'psutil'（诊断与测试用）
        self.last_source: Optional[str] = None

    def is_process_running(self) -> bool:
        """
        检查进程是否正在运行

        Returns:
            bool: 进程是否存在且运行中
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            return self._process.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def get_create_time(self) -> Optional[float]:
        """
        读取采集器所绑定进程的创建时间（与 psutil.Process.create_time() 同口径）

        Returns:
            Optional[float]: 创建时间（epoch 秒）；进程不存在或无权限读取时返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            return self._process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def get_process_name(self) -> Optional[str]:
        """
        获取进程名称

        Returns:
            Optional[str]: 进程名称，失败返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            return self._process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _collect_one(self, metric_type: str) -> Optional[float]:
        """
        采集单个性能指标（内部方法，不捕获psutil异常，由调用方统一处理）

        Args:
            metric_type: 指标类型（来自MetricType）

        Returns:
            Optional[float]: 指标值，未知指标返回None
        """
        if self._process is None:
            self._process = psutil.Process(self.pid)
        if metric_type == MetricType.CPU_PERCENT and self._hub is not None:
            return self._cpu_percent_from_times(self._process)
        sampler = SAMPLERS.get(metric_type)
        if sampler is None:
            # 未知指标类型（如更高版本写入的键）：跳过，不写入结果
            return None
        return sampler(self._process)

    def prime_cpu(self):
        """
        预热CPU采集：记下 CPU 使用率的差分起点，监控含CPU指标的任务前先调用一次。

        不注入 hub：cpu_percent(interval=None)首次调用固定返回0，丢弃首个无效值。
        注入 hub（快照路径与回退 psutil 路径共用差分起点）：起点取自快照；目标进程比
        缓存的快照还新（快照里没有它）时强制取一份新的；仍然没有就不设起点，首个 CPU
        点按不可用处理（不写 0.0）。快照不可用或取失败时改用 psutil 的 cpu_times() 作起点。
        """
        if self._hub is None:
            try:
                if self._process is None:
                    self._process = psutil.Process(self.pid)
                self._process.cpu_percent(interval=None)
            except psutil.Error:
                # 预热失败不影响主流程，采集时会再次处理异常
                pass
            return
        self._cpu_prev = None
        try:
            if not self._hub.available():
                raise SnapshotUnavailable(self._hub.unavailable_reason)
            snapshot = self._acquire_snapshot()
            sample = snapshot.processes.get(self.pid)
            if sample is None:
                snapshot = self._hub.refresh()
                self._remember_snapshot(snapshot)
                sample = snapshot.processes.get(self.pid)
            if sample is not None:
                self._cpu_prev = (sample.user_time + sample.kernel_time,
                                  snapshot.taken_at.monotonic)
        except Exception:
            # 快照取不到：用 psutil 的 cpu_times() 作起点；再失败则采集时按"无起点"处理
            try:
                if self._process is None:
                    self._process = psutil.Process(self.pid)
                times = self._process.cpu_times()
                self._cpu_prev = (times.user + times.system, _monotonic())
            except psutil.Error:
                pass

    def collect_metrics(self, metric_types: List[str]) -> Optional[Dict[str, float]]:
        """
        批量采集多个性能指标（同一周期共用oneshot缓存）

        Args:
            metric_types: 指标类型列表（来自MetricType）

        Returns:
            Optional[Dict[str, float]]: {指标类型: 指标值}字典（按请求顺序）；
                进程不存在（含 PID 已被另一个进程复用）时返回None（调用方据此停止任务）。
                采集失败的指标不放入字典（不会记为0），原因记录在
                self.last_unavailable（每次调用开头清空）。
        """
        self.last_unavailable = {}
        if self._hub is not None:
            result = self._collect_from_snapshot(metric_types)
            if result is not _FALLBACK:
                self.last_source = 'snapshot'
                return result
        self.last_source = 'psutil'
        return self._collect_from_psutil(metric_types)

    def _collect_from_psutil(self, metric_types: List[str]) -> Optional[Dict[str, float]]:
        """原路径：全部指标逐进程经 psutil 采集。"""
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            # PID 复用检测：is_running() 会比对进程创建时间，PID 被新进程占用时为 False，
            # 否则会把另一个进程的数据记到本任务名下
            try:
                if not self._process.is_running():
                    return None
            except psutil.AccessDenied:
                pass  # 无法确认时按"仍在运行"处理，不误停任务

            values: Dict[str, float] = {}
            with self._process.oneshot():
                for metric_type in metric_types:
                    if not self._sample_with_psutil(metric_type, values):
                        return None
            return values

        except psutil.NoSuchProcess:
            # 创建Process对象时进程已不存在
            return None

    def _sample_with_psutil(self, metric_type: str, values: Dict[str, float]) -> bool:
        """
        用 psutil 采一个指标，成功写入 values，失败记入 last_unavailable。

        Returns:
            bool: 进程确实不存在时为 False（调用方整体返回 None），否则 True
        """
        try:
            value = self._collect_one(metric_type)
        except psutil.AccessDenied:
            # 权限不足：跳过该指标（不记0），进程可能仍存在，不中断
            self.last_unavailable[metric_type] = UnavailableReason.ACCESS_DENIED
            return True
        except psutil.ZombieProcess:
            # 注意：ZombieProcess是NoSuchProcess的子类，必须先于父类捕获
            self.last_unavailable[metric_type] = UnavailableReason.PROCESS_EXITING
            return True
        except psutil.NoSuchProcess:
            # 进程确实不存在，整体返回None停止任务
            return False
        except MetricNotSupported as e:
            # 指标对该进程不适用（非权限问题），跳过且不提示提权
            self.last_unavailable[metric_type] = e.reason
            return True
        except _NoCpuBaseline:
            self.last_unavailable[metric_type] = UnavailableReason.ERROR
            return True
        except Exception:
            # 其他异常，进程可能仍存在，跳过该指标继续
            if metric_type not in self._logged_failures:
                self._logged_failures.add(metric_type)
                logger.exception(
                    "采集指标 %s 失败（PID %s）", metric_type, self.pid)
            self.last_unavailable[metric_type] = UnavailableReason.ERROR
            return True

        if value is not None:
            values[metric_type] = value
        # 未知指标（value为None）跳过，不写入结果
        return True

    # ---------- 快照路径 ----------

    def _log_fallback_once(self, message: str, *args, exc_info: bool = False) -> None:
        if not self._fallback_logged:
            self._fallback_logged = True
            logger.warning(message, *args, exc_info=exc_info)

    def _acquire_snapshot(self) -> SystemSnapshot:
        """
        向 hub 取本周期的快照。

        按截止时间取（snapshot_not_before 已设置，即监控线程的采样周期）时，hub 给出的
        恰是本采集器上次用过的那一份（如本周期落后太多、命中了自己上次取的快照）要强制取
        新的：否则累计量会在两个时间戳上写同一个值，速率曲线出现 0 与翻倍。按年龄取
        （没有截止时间）的调用方明确接受复用，不做这个检查。
        """
        hub = self._hub
        not_before = self.snapshot_not_before
        if not_before is None:
            snapshot = hub.get(self._max_age_s)
        else:
            snapshot = hub.get(self._max_age_s, not_before=not_before)
            last = self._last_snapshot() if self._last_snapshot is not None else None
            if last is snapshot:
                snapshot = hub.refresh()
        self._remember_snapshot(snapshot)
        return snapshot

    def _remember_snapshot(self, snapshot: SystemSnapshot) -> None:
        try:
            self._last_snapshot = weakref.ref(snapshot)
        except TypeError:
            self._last_snapshot = None

    def _collect_from_snapshot(self, metric_types: List[str]):
        """
        快照路径。返回值同 collect_metrics；返回 _FALLBACK 表示本周期改走 psutil
        （快照不可用、取失败，或进程在但快照里没有它）。
        """
        hub = self._hub
        try:
            if not hub.available():
                self._log_fallback_once(
                    "PID %s：全系统快照不可用（%s），改用 psutil 逐项采集",
                    self.pid, hub.unavailable_reason)
                return _FALLBACK
            snapshot = self._acquire_snapshot()
        except Exception:
            self._log_fallback_once(
                "PID %s：取全系统快照失败，本周期改用 psutil 逐项采集（之后不再重复记录）",
                self.pid, exc_info=True)
            return _FALLBACK

        sample = snapshot.processes.get(self.pid)
        if sample is None:
            return self._recheck_missing()
        if sample.num_threads == 0:
            # 已退出、但还被别的句柄（包括任务自己为取退出码持有的那个）握着的进程：系统
            # 进程表里仍留着它的条目，线程数为 0。它不是在运行的进程，按已退出处理——
            # 否则任务会一直采到一份冻结的读数，永远发现不了退出。只影响本采集器对目标的
            # 判断，进程列表不改（列表里多一个僵尸条目无害，且别处还依赖原样的快照）
            return None
        if not self._same_process(sample):
            # 快照里这个 PID 的创建时间与绑定的进程不同：PID 已被新进程复用
            return None

        values: Dict[str, float] = {}
        psutil_keys = [m for m in metric_types
                       if not _uses_snapshot(m) and m in SAMPLERS]
        try:
            if psutil_keys and self._process is None:
                self._process = psutil.Process(self.pid)
            context = self._process.oneshot() if psutil_keys else contextlib.nullcontext()
        except psutil.NoSuchProcess:
            return None
        with context:
            for metric_type in metric_types:
                if _uses_snapshot(metric_type):
                    value = self._snapshot_value(metric_type, snapshot, sample)
                    if value is None:
                        self.last_unavailable[metric_type] = UnavailableReason.ERROR
                    else:
                        values[metric_type] = value
                elif metric_type not in SAMPLERS:
                    continue   # 未知指标（如更高版本写入的键）：跳过，不写入结果
                elif not self._sample_with_psutil(metric_type, values):
                    return None
        return values

    def _recheck_missing(self):
        """快照里没有本进程：用 psutil 复核。确已不在返回 None，仍在则本周期回退 psutil。"""
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            running = self._process.is_running()
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied:
            running = True   # 无法确认时按"仍在运行"处理，不误停任务
        return _FALLBACK if running else None

    def _same_process(self, sample: ProcessSample) -> bool:
        """快照里的该 PID 是否仍是本采集器绑定的进程（按创建时间比对）。"""
        if self._identity_create_time is None:
            if self._process is not None:
                try:
                    self._identity_create_time = self._process.create_time()
                except (psutil.Error, AttributeError, TypeError):
                    pass
            if self._identity_create_time is None:
                self._identity_create_time = sample.create_time
        return abs(sample.create_time - self._identity_create_time) <= _CREATE_TIME_TOLERANCE

    def _snapshot_value(self, metric_type: str, snapshot: SystemSnapshot,
                        sample: ProcessSample) -> Optional[float]:
        """从快照取一个指标（已换算到存储单位）；取不到返回 None。"""
        if metric_type == MetricType.CPU_PERCENT:
            return self._cpu_percent_from(snapshot, sample)
        return read_snapshot_metric(metric_type, snapshot, sample)

    def _cpu_percent_from(self, snapshot: SystemSnapshot,
                          sample: ProcessSample) -> Optional[float]:
        """快照路径的 CPU 使用率；无差分起点或窗口不是正数时返回 None（记为不可用）。"""
        return self._cpu_percent_step(sample.user_time + sample.kernel_time,
                                      snapshot.taken_at.monotonic)

    def _cpu_percent_from_times(self, process: psutil.Process) -> float:
        """回退 psutil 周期的 CPU 使用率：cpu_times() 加单调时钟，与快照路径共用差分起点。"""
        times = process.cpu_times()
        percent = self._cpu_percent_step(times.user + times.system, _monotonic())
        if percent is None:
            raise _NoCpuBaseline()
        return percent

    def _cpu_percent_step(self, total: float, now: float) -> Optional[float]:
        """
        CPU 使用率 = (user+kernel 时间增量) / 墙钟间隔 × 100，保留 1 位小数。

        与 psutil.Process.cpu_percent(interval=None) 同口径：按单核计，多线程可超过
        100%（psutil 先除以 核数 × 间隔 再乘回核数，结果相同）。没有起点（未预热且
        目标进程不在快照里）时记下本点作起点并返回 None；窗口长度不是正数（同一时刻
        的两份快照）时保留原起点、返回 None，下次仍按完整窗口计算——都不写 0.0。
        """
        previous = self._cpu_prev
        if previous is None:
            self._cpu_prev = (total, now)
            return None
        elapsed = now - previous[1]
        if elapsed <= 0:
            return None
        self._cpu_prev = (total, now)
        percent = max(0.0, total - previous[0]) / elapsed * 100
        return round(percent, 1) if math.isfinite(percent) else None

    def collect_metric(self, metric_type: str) -> Optional[float]:
        """
        采集指定的性能指标（单指标兼容接口，语义与旧版一致）

        Args:
            metric_type: 指标类型（来自MetricType）

        Returns:
            Optional[float]: 指标值，失败返回None
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            if metric_type == MetricType.CPU_PERCENT:
                # 单指标采集保持旧语义：使用0.1秒阻塞间隔，更准确
                return self._process.cpu_percent(interval=0.1)

            return self._collect_one(metric_type)

        except psutil.NoSuchProcess:
            # 进程确实不存在
            return None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            # 访问被拒绝或僵尸进程，进程可能仍存在但暂时无法访问
            # 返回0而不是None，避免误判为进程终止
            return 0.0
        except Exception:
            # 其他异常，进程可能仍存在
            return 0.0

    def get_process_info(self) -> Optional[Dict[str, Any]]:
        """
        获取进程的基本信息

        Returns:
            Optional[Dict]: 进程信息字典，包含name, pid, status等
        """
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)

            return {
                'pid': self.pid,
                'name': self._process.name(),
                'status': self._process.status(),
                'create_time': self._process.create_time(),
                'username': self._process.username() if hasattr(self._process, 'username') else None,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    # 下面两个静态方法是旧接口：进程查询已移到 core.process_catalog，这里只做委托

    @staticmethod
    def get_all_processes() -> List[Tuple[int, str, Optional[float]]]:
        """(pid, name, create_time) 列表，按进程名排序；见 process_catalog.list_processes"""
        return process_catalog.list_processes()

    @staticmethod
    def find_process_by_name(name: str) -> List[Tuple[int, str]]:
        """按进程名部分匹配查找 (pid, name)；见 process_catalog.search_by_name"""
        return process_catalog.search_by_name(name)


# 单元测试代码（可选）
if __name__ == "__main__":
    import os

    # 测试当前进程
    current_pid = os.getpid()
    print(f"当前进程PID: {current_pid}")

    collector = ProcessCollector(current_pid)

    # 测试进程名
    print(f"进程名: {collector.get_process_name()}")

    # 测试单指标接口（旧语义）
    print(f"工作集内存: {collector.collect_metric(MetricType.MEMORY_RSS):.2f} KB")
    print(f"CPU使用率: {collector.collect_metric(MetricType.CPU_PERCENT):.1f} %")
    print(f"线程数: {collector.collect_metric(MetricType.NUM_THREADS):.0f}")

    # 测试多指标批量采集接口
    collector.prime_cpu()
    values = collector.collect_metrics([
        MetricType.MEMORY_RSS,
        MetricType.CPU_PERCENT,
        MetricType.NUM_THREADS,
    ])
    print("\n批量采集结果:")
    for metric_type, value in values.items():
        print(f"  {metric_type}: {value:.2f}")

    # 测试不存在的进程（应返回None）
    dead_collector = ProcessCollector(999999)
    print(f"\n不存在进程的批量采集结果: {dead_collector.collect_metrics([MetricType.MEMORY_RSS])}")

    # 测试获取所有进程
    all_processes = ProcessCollector.get_all_processes()
    print(f"\n系统中共有 {len(all_processes)} 个进程")
    print("前5个进程:")
    for pid, name, _create_time in all_processes[:5]:
        print(f"  PID: {pid}, Name: {name}")

    # 测试查找进程
    print("\n查找名称包含 'python' 的进程:")
    python_processes = ProcessCollector.find_process_by_name("python")
    for pid, name in python_processes:
        print(f"  PID: {pid}, Name: {name}")
