"""
全系统进程快照（ntdll.NtQuerySystemInformation(SystemProcessInformation)）

一次系统调用取回全部进程的线程数、句柄数、内存各项、IO 计数与 CPU 时间，代替
psutil 逐进程、逐指标的调用（psutil 的 num_threads() 自己就会取一次全系统快照，
单次约 16 ms）。只读，不需要特权，不打开任何进程句柄。

结构体按类型（c_size_t / c_void_p / c_ulonglong / LARGE_INTEGER）用 ctypes 声明，
由 ctypes 负责对齐；解析时再按 ctypes 给出的字段偏移生成 struct 格式串，用
struct.unpack_from 一次取出整个条目头（比逐字段访问 ctypes 属性快一个数量级）。

启动自检（首次使用时做一次）任一项不符即永久禁用快照，available() 为 False，
调用方回退 psutil：
- 指针宽度必须为 8 字节：32 位 Python（含 WOW64）下的结构布局没有在真机上验证过，
  不冒险解析，直接禁用；
- sizeof(SYSTEM_PROCESS_INFORMATION) 必须为 256（64 位）/ 184（32 位，含 WOW64），
  sizeof(SYSTEM_THREAD_INFORMATION) 必须为 80 / 64，struct 格式串长度与之相同；
- 首个条目的 NextEntryOffset 减去 线程数 × 线程结构大小 不得小于条目头大小；
- 本进程的条目必须存在，其线程数组（紧跟条目头）里每个线程的 ClientId.UniqueProcess
  都必须等于本进程 PID——这同时验证了条目头大小、线程结构大小与 PID 字段偏移。
  只有一个线程时验证不到线程结构的步长（第 0 个线程的偏移与步长无关），所以自检
  发现本进程线程数不足 2 个时，临时起一个空闲线程再检查一遍，检查完结束它。
新版 Windows 在线程数组与映像名之间还有一段附加数据（本机 Windows 11 26200 为 368
字节），因此条目步长只按 NextEntryOffset 走，不用 sizeof + 线程数推算。
"""
import ctypes
import logging
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

SYSTEM_PROCESS_INFORMATION_CLASS = 5
STATUS_SUCCESS = 0
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_BUFFER_TOO_SMALL = 0xC0000023
_RETRY_STATUSES = frozenset({STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_TOO_SMALL})

# 缓冲区：初始 1 MB；长度不足时取 max(ReturnLength + 64 KB, 当前 × 1.25) 重试，最多 8 次
# （两次调用之间进程数可能继续增长）。成功后记住大小作下次初始值。
INITIAL_BUFFER_SIZE = 1 << 20
BUFFER_GROW_MARGIN = 64 * 1024
BUFFER_GROW_FACTOR = 1.25
MAX_QUERY_ATTEMPTS = 8

# 按指针宽度期望的结构大小（ReactOS 对 x86 有 C_ASSERT 0xB8；x64 由本机实测确认）
EXPECTED_PROCESS_INFO_SIZE = {8: 256, 4: 184}
EXPECTED_THREAD_INFO_SIZE = {8: 80, 4: 64}

# FILETIME（1601 年起的 100 ns）→ epoch 秒，与 psutil 的 C 实现同一算式，结果逐位相同
_FILETIME_EPOCH_OFFSET = 116444736000000000
_TICKS_PER_SECOND = 10_000_000
# psutil 对 PID 0（空闲进程）与 4（System）的 create_time() 返回 0.0，进程列表的
# 现状口径也是 0.0；快照里这两个条目带的是开机时刻，按 psutil 口径改为 0.0
_ZERO_CREATE_TIME_PIDS = frozenset({0, 4})


def _pointer_size() -> int:
    """当前 Python 的指针宽度（字节）；单独成函数以便测试替换。"""
    return ctypes.sizeof(ctypes.c_void_p)


# ---------------------------------------------------------------- 结构声明


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),          # 字节数，不含结尾 0
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class CLIENT_ID(ctypes.Structure):
    _fields_ = [
        ("UniqueProcess", ctypes.c_void_p),
        ("UniqueThread", ctypes.c_void_p),
    ]


class SYSTEM_THREAD_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("KernelTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("CreateTime", ctypes.c_longlong),
        ("WaitTime", ctypes.c_ulong),
        ("StartAddress", ctypes.c_void_p),
        ("ClientId", CLIENT_ID),
        ("Priority", ctypes.c_long),
        ("BasePriority", ctypes.c_long),
        ("ContextSwitches", ctypes.c_ulong),
        ("ThreadState", ctypes.c_ulong),
        ("WaitReason", ctypes.c_ulong),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """条目头；之后紧跟 NumberOfThreads 个 SYSTEM_THREAD_INFORMATION。"""
    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_ulonglong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong),
        ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong),
        ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong),
        ("OtherTransferCount", ctypes.c_longlong),
    ]


# ---------------------------------------------------------------- 产出


class ProcessSample(NamedTuple):
    """一个进程在快照时刻的取值（不可变）。字节类为字节，时间类为秒，计数原样。"""
    pid: int
    parent_pid: int
    # ImageName；PID 0 为空串（显示名由调用方按 psutil 口径补，见 process_catalog）
    name: str
    # epoch 秒，与 psutil.Process.create_time() 一致；PID 0/4 为 0.0
    create_time: float
    num_threads: int
    handle_count: int
    session_id: int
    base_priority: int
    working_set: int
    peak_working_set: int
    # PrivatePageCount：实测单位为字节，与 psutil memory_info().private 相同
    private_bytes: int
    pagefile: int
    peak_pagefile: int
    virtual_size: int
    peak_virtual: int
    paged_pool: int
    peak_paged_pool: int
    nonpaged_pool: int
    peak_nonpaged_pool: int
    page_faults: int
    read_count: int
    write_count: int
    other_count: int
    read_bytes: int
    write_bytes: int
    other_bytes: int
    user_time: float
    kernel_time: float
    cycle_time: int


class TakenAt(NamedTuple):
    """快照时刻：单调时钟（算间隔）与墙钟（epoch 秒）"""
    monotonic: float
    wall: float


@dataclass(frozen=True)
class SystemSnapshot:
    """一次全系统快照。processes 以 PID 为键。"""
    taken_at: TakenAt
    processes: Dict[int, ProcessSample]
    # 线程数组的位置 {pid: (首个线程在 raw 中的偏移, 线程数)}，按需求上下文切换次数用
    _threads: Dict[int, Tuple[int, int]] = field(default_factory=dict, repr=False, compare=False)
    _raw: Optional[memoryview] = field(default=None, repr=False, compare=False)
    _thread_size: int = field(default=0, repr=False, compare=False)
    _ctx_offset: int = field(default=0, repr=False, compare=False)

    def context_switches(self, pid: int) -> Optional[int]:
        """该进程全部线程的上下文切换次数之和（psutil num_ctx_switches().voluntary
        同口径）；进程不在快照里或快照不含线程数组时返回 None。"""
        located = self._threads.get(pid)
        if located is None or self._raw is None:
            return None
        start, count = located
        unpack = _U32.unpack_from
        raw = self._raw
        total = 0
        offset = start + self._ctx_offset
        for _ in range(count):
            total += unpack(raw, offset)[0]
            offset += self._thread_size
        return total


class SnapshotUnavailable(Exception):
    """快照已被禁用（非 Windows、ntdll 不可用、结构自检不通过）；调用方永久回退 psutil。"""


class SnapshotError(Exception):
    """本次取快照失败（缓冲区反复不足、意外的 NTSTATUS）；下次仍可重试。"""


class _LayoutError(Exception):
    """缓冲区内容与结构声明不符：说明解析口径错了，禁用快照。"""


# ---------------------------------------------------------------- 布局（由 ctypes 推出）


_U32 = struct.Struct('<I')

# 需要从条目头取出的字段，按偏移升序（与 _parse 里的元组解包顺序一致）
_HEADER_FIELDS = (
    "NextEntryOffset", "NumberOfThreads", "CycleTime", "CreateTime", "UserTime",
    "KernelTime", "ImageName.Length", "ImageName.Buffer", "BasePriority",
    "UniqueProcessId", "InheritedFromUniqueProcessId", "HandleCount", "SessionId",
    "PeakVirtualSize", "VirtualSize", "PageFaultCount", "PeakWorkingSetSize",
    "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
    "PeakPagefileUsage", "PrivatePageCount", "ReadOperationCount",
    "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
    "WriteTransferCount", "OtherTransferCount",
)

_SIGNED_TYPES = (ctypes.c_long, ctypes.c_longlong, ctypes.c_int, ctypes.c_short, ctypes.c_byte)
_UNSIGNED_CODES = {1: 'B', 2: 'H', 4: 'I', 8: 'Q'}
_SIGNED_CODES = {1: 'b', 2: 'h', 4: 'i', 8: 'q'}


def _field_layout(struct_type, dotted: str) -> Tuple[int, type]:
    """dotted 字段（如 'ImageName.Buffer'）相对 struct_type 起点的偏移与 ctypes 类型。"""
    offset = 0
    current = struct_type
    ctype = None
    for part in dotted.split('.'):
        descriptor = getattr(current, part)
        offset += descriptor.offset
        ctype = dict((f[0], f[1]) for f in current._fields_)[part]
        current = ctype
    return offset, ctype


def _code_for(ctype) -> str:
    size = ctypes.sizeof(ctype)
    table = _SIGNED_CODES if ctype in _SIGNED_TYPES else _UNSIGNED_CODES
    return table[size]


def _build_struct(struct_type, names) -> struct.Struct:
    """按 ctypes 给出的偏移生成小端、无隐式对齐的格式串（空隙用 'x' 填），总长等于 sizeof。"""
    layout = [(*_field_layout(struct_type, name), name) for name in names]
    if [entry[0] for entry in layout] != sorted(entry[0] for entry in layout):
        raise _LayoutError("字段表未按偏移升序排列")
    fmt = ['<']
    cursor = 0
    for offset, ctype, name in layout:
        if offset < cursor:
            raise _LayoutError(f"字段 {name} 与前一字段重叠")
        fmt.append('x' * (offset - cursor))
        fmt.append(_code_for(ctype))
        cursor = offset + ctypes.sizeof(ctype)
    fmt.append('x' * (ctypes.sizeof(struct_type) - cursor))
    return struct.Struct(''.join(fmt))


@dataclass(frozen=True)
class Layout:
    """解析用的布局常量（由 ctypes 结构推出，自检时核对）"""
    header: struct.Struct
    header_size: int
    thread_size: int
    thread_pid_offset: int
    thread_pid_code: str
    ctx_offset: int


def build_layout() -> Layout:
    header = _build_struct(SYSTEM_PROCESS_INFORMATION, _HEADER_FIELDS)
    pid_offset, pid_type = _field_layout(SYSTEM_THREAD_INFORMATION, "ClientId.UniqueProcess")
    ctx_offset, _ = _field_layout(SYSTEM_THREAD_INFORMATION, "ContextSwitches")
    return Layout(
        header=header,
        header_size=ctypes.sizeof(SYSTEM_PROCESS_INFORMATION),
        thread_size=ctypes.sizeof(SYSTEM_THREAD_INFORMATION),
        thread_pid_offset=pid_offset,
        thread_pid_code='<' + _code_for(pid_type),
        ctx_offset=ctx_offset,
    )


# ---------------------------------------------------------------- 解析


def _filetime_to_epoch(ticks: int) -> float:
    return (ticks - _FILETIME_EPOCH_OFFSET) / _TICKS_PER_SECOND


def parse_buffer(buffer, length: int, layout: Layout,
                 taken_at: Optional[TakenAt] = None) -> SystemSnapshot:
    """
    解析 NtQuerySystemInformation 填好的缓冲区（ctypes 可写缓冲区，ImageName.Buffer
    是指向其内部的绝对地址）。只读前 length 字节；越界或步长异常抛 _LayoutError。
    """
    if taken_at is None:
        taken_at = TakenAt(time.monotonic(), time.time())
    base = ctypes.addressof(buffer)
    view = memoryview(buffer).cast('B')
    if length > len(view):
        raise _LayoutError("ReturnLength 超过缓冲区大小")
    view = view[:length].toreadonly()
    unpack = layout.header.unpack_from
    header_size = layout.header_size
    thread_size = layout.thread_size
    processes: Dict[int, ProcessSample] = {}
    threads: Dict[int, Tuple[int, int]] = {}
    new_sample = tuple.__new__
    pos = 0
    while True:
        if pos + header_size > length:
            raise _LayoutError(f"条目越界：偏移 {pos} + {header_size} > {length}")
        (next_offset, num_threads, cycle_time, create_ticks, user_ticks, kernel_ticks,
         name_length, name_pointer, base_priority, pid, parent_pid, handle_count,
         session_id, peak_virtual, virtual_size, page_faults, peak_working_set,
         working_set, peak_paged_pool, paged_pool, peak_nonpaged_pool, nonpaged_pool,
         pagefile, peak_pagefile, private_bytes, read_count, write_count, other_count,
         read_bytes, write_bytes, other_bytes) = unpack(view, pos)

        if next_offset and next_offset < header_size:
            raise _LayoutError(f"NextEntryOffset {next_offset} 小于条目头大小 {header_size}")
        end = pos + next_offset if next_offset else length
        threads_end = pos + header_size + num_threads * thread_size
        if threads_end > end:
            raise _LayoutError(f"PID {pid} 的线程数组越出条目（{num_threads} 个线程）")

        name = ''
        if name_length:
            name_offset = name_pointer - base
            if name_offset < threads_end or name_offset + name_length > end:
                raise _LayoutError(f"PID {pid} 的映像名指针不在本条目内")
            name = bytes(view[name_offset:name_offset + name_length]).decode(
                'utf-16-le', 'replace')

        pid = pid or 0
        create_time = (0.0 if pid in _ZERO_CREATE_TIME_PIDS
                       else _filetime_to_epoch(create_ticks))
        processes[pid] = new_sample(ProcessSample, (
            pid, parent_pid or 0, name, create_time, num_threads, handle_count,
            session_id, base_priority, working_set, peak_working_set, private_bytes,
            pagefile, peak_pagefile, virtual_size, peak_virtual, paged_pool,
            peak_paged_pool, nonpaged_pool, peak_nonpaged_pool, page_faults,
            read_count, write_count, other_count, read_bytes, write_bytes, other_bytes,
            user_ticks / _TICKS_PER_SECOND, kernel_ticks / _TICKS_PER_SECOND, cycle_time,
        ))
        threads[pid] = (pos + header_size, num_threads)

        if not next_offset:
            break
        pos += next_offset

    return SystemSnapshot(taken_at=taken_at, processes=processes, _threads=threads,
                          _raw=view, _thread_size=thread_size, _ctx_offset=layout.ctx_offset)


def check_layout(buffer, length: int, layout: Layout, own_pid: int) -> Dict[str, int]:
    """
    对一份真实缓冲区做结构自检，通过时返回诊断信息（首条目步长等），否则抛 _LayoutError。
    见模块说明的三项检查。
    """
    pointer_size = _pointer_size()
    expected_header = EXPECTED_PROCESS_INFO_SIZE.get(pointer_size)
    expected_thread = EXPECTED_THREAD_INFO_SIZE.get(pointer_size)
    if layout.header_size != expected_header or layout.thread_size != expected_thread:
        raise _LayoutError(
            f"结构大小不符：进程 {layout.header_size}（期望 {expected_header}），"
            f"线程 {layout.thread_size}（期望 {expected_thread}）")
    if layout.header.size != layout.header_size:
        raise _LayoutError(f"格式串长度 {layout.header.size} 与 sizeof {layout.header_size} 不符")

    view = memoryview(buffer).cast('B')
    if length < layout.header_size:
        raise _LayoutError("返回长度小于一个条目头")
    first_next, first_threads = struct.unpack_from('<II', view, 0)
    if first_next and first_next - first_threads * layout.thread_size < layout.header_size:
        raise _LayoutError(
            f"首条目 NextEntryOffset {first_next} - {first_threads} × {layout.thread_size} "
            f"小于条目头 {layout.header_size}")

    snapshot = parse_buffer(buffer, length, layout)
    own = snapshot._threads.get(own_pid)
    if own is None:
        raise _LayoutError(f"快照里没有本进程（PID {own_pid}）")
    start, count = own
    if count < 1:
        raise _LayoutError("本进程的线程数为 0")
    for index in range(count):
        owner = struct.unpack_from(
            layout.thread_pid_code, view,
            start + index * layout.thread_size + layout.thread_pid_offset)[0]
        if owner != own_pid:
            raise _LayoutError(
                f"本进程第 {index} 个线程的 ClientId.UniqueProcess 为 {owner}，期望 {own_pid}")
    return {
        'pointer_size': pointer_size,
        'own_threads': count,
        # 本进程至少两个线程时，线程结构大小（数组步长）才真正被验证过
        'thread_stride_verified': count >= 2,
        'process_info_size': layout.header_size,
        'thread_info_size': layout.thread_size,
        'first_next_entry_offset': first_next,
        'first_number_of_threads': first_threads,
        # 线程数组之后、下一条目之前的字节数（含附加数据与映像名）
        'first_trailing_bytes': (first_next - layout.header_size
                                 - first_threads * layout.thread_size) if first_next else 0,
        'process_count': len(snapshot.processes),
    }


# ---------------------------------------------------------------- 系统调用


# query(buffer, size) -> (NTSTATUS（无符号）, ReturnLength)
QueryFunc = Callable[[object, int], Tuple[int, int]]


def _load_nt_query() -> QueryFunc:
    ntdll = ctypes.WinDLL("ntdll")
    func = ntdll.NtQuerySystemInformation
    func.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                     ctypes.POINTER(ctypes.c_ulong)]
    func.restype = ctypes.c_long

    def query(buffer, size: int) -> Tuple[int, int]:
        returned = ctypes.c_ulong(0)
        status = func(SYSTEM_PROCESS_INFORMATION_CLASS, buffer, size, ctypes.byref(returned))
        return status & 0xFFFFFFFF, returned.value

    return query


class NtProcessSnapshotter:
    """
    取全系统进程快照。线程安全（内部串行），通常经 SnapshotHub 共享使用。

    Args:
        query: 系统调用替身（测试用），签名见 QueryFunc；None 用 ntdll
        own_pid: 自检时要求出现在快照里的 PID（默认本进程）
        platform: 平台名（测试用）
    """

    def __init__(self, query: Optional[QueryFunc] = None, *, own_pid: Optional[int] = None,
                 platform: Optional[str] = None):
        self._query = query
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._platform = platform if platform is not None else sys.platform
        self._lock = threading.Lock()
        self._checked = False
        self._disabled_reason: Optional[str] = None
        self._layout: Optional[Layout] = None
        self._buffer_size = INITIAL_BUFFER_SIZE
        # 自检通过后的诊断信息（结构大小、首条目步长等），供汇报与排查
        self.diagnostics: Dict[str, int] = {}

    # ---------- 状态 ----------

    def available(self) -> bool:
        """快照是否可用（首次调用时做自检）。"""
        self._ensure_checked()
        return self._disabled_reason is None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """不可用的原因（中文）；可用时为 None。"""
        self._ensure_checked()
        return self._disabled_reason

    @property
    def buffer_size(self) -> int:
        """下次查询的初始缓冲区大小（上次成功时的大小）。"""
        return self._buffer_size

    def _disable(self, reason: str, exc_info: bool = False) -> None:
        if self._disabled_reason is None:
            self._disabled_reason = reason
            logger.warning("全系统进程快照已禁用，改用 psutil 逐进程采集：%s", reason,
                           exc_info=exc_info)

    def _ensure_checked(self) -> None:
        if self._checked:
            return
        with self._lock:
            if self._checked:
                return
            try:
                self._self_check_locked()
            finally:
                self._checked = True

    def _self_check_locked(self) -> None:
        if self._query is None and self._platform != "win32":
            self._disable("非 Windows 平台")
            return
        if _pointer_size() != 8:
            self._disable(f"当前为 {_pointer_size() * 8} 位 Python（含 WOW64），"
                          "全系统进程快照的结构布局只在 64 位下验证过")
            return
        try:
            self._layout = build_layout()
        except Exception as e:   # 结构声明本身有问题
            self._disable(f"结构布局推导失败：{e}", exc_info=True)
            return
        if self._query is None:
            try:
                self._query = _load_nt_query()
            except (OSError, AttributeError) as e:
                self._disable(f"无法加载 ntdll.NtQuerySystemInformation：{e}")
                return
        try:
            buffer, length = self._fill_buffer_locked()
            self.diagnostics = check_layout(buffer, length, self._layout, self._own_pid)
            if not self.diagnostics['thread_stride_verified'] and self._own_pid == os.getpid():
                self.diagnostics = self._check_with_idle_thread_locked()
        except _LayoutError as e:
            self._disable(f"结构自检不通过：{e}")
        except SnapshotError as e:
            self._disable(f"自检时取快照失败：{e}")
        except Exception as e:
            self._disable(f"自检异常：{e}", exc_info=True)
        else:
            logger.info("全系统进程快照自检通过：%s", self.diagnostics)

    def _check_with_idle_thread_locked(self) -> Dict[str, int]:
        """本进程线程数不足 2 个：临时起一个空闲线程，让快照里本进程有两个以上的线程
        再检查一遍（否则验证不到线程结构的步长），检查完结束该线程。"""
        stop = threading.Event()
        idle = threading.Thread(target=stop.wait, name="snapshot-selfcheck-idle", daemon=True)
        idle.start()
        try:
            buffer, length = self._fill_buffer_locked()
            diagnostics = check_layout(buffer, length, self._layout, self._own_pid)
        finally:
            stop.set()
            idle.join(1.0)
        if not diagnostics['thread_stride_verified']:
            raise _LayoutError(
                f"本进程只有 {diagnostics['own_threads']} 个线程，无法验证线程结构大小")
        return diagnostics

    # ---------- 取快照 ----------

    def snapshot(self) -> SystemSnapshot:
        """
        取一次全系统快照。

        Raises:
            SnapshotUnavailable: 快照已禁用（含本次解析发现结构不符而禁用）
            SnapshotError: 本次失败，可下次再试
        """
        self._ensure_checked()
        if self._disabled_reason is not None:
            raise SnapshotUnavailable(self._disabled_reason)
        with self._lock:
            buffer, length = self._fill_buffer_locked()
            taken_at = TakenAt(time.monotonic(), time.time())
            try:
                return parse_buffer(buffer, length, self._layout, taken_at)
            except _LayoutError as e:
                self._disable(f"解析失败：{e}")
                raise SnapshotUnavailable(self._disabled_reason) from e

    def _fill_buffer_locked(self):
        """调用系统接口填满缓冲区，返回 (缓冲区, 有效长度)。"""
        size = self._buffer_size
        last_status = None
        for _ in range(MAX_QUERY_ATTEMPTS):
            buffer = ctypes.create_string_buffer(size)
            status, returned = self._query(buffer, size)
            if status == STATUS_SUCCESS:
                self._buffer_size = size
                return buffer, (returned if 0 < returned <= size else size)
            if status not in _RETRY_STATUSES:
                raise SnapshotError(f"NtQuerySystemInformation 返回 0x{status:08X}")
            last_status = status
            size = max(returned + BUFFER_GROW_MARGIN, int(size * BUFFER_GROW_FACTOR))
        raise SnapshotError(
            f"缓冲区重试 {MAX_QUERY_ATTEMPTS} 次仍不足（最后状态 0x{last_status:08X}，"
            f"缓冲区 {size} 字节）")


# ---------------------------------------------------------------- 模块级默认实例


_default: Optional[NtProcessSnapshotter] = None
_default_lock = threading.Lock()


def default_snapshotter() -> NtProcessSnapshotter:
    """进程内共享的快照器（惰性创建；自检在首次 available()/snapshot() 时做）。"""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = NtProcessSnapshotter()
    return _default


def available() -> bool:
    """默认快照器是否可用。"""
    return default_snapshotter().available()


def snapshot() -> SystemSnapshot:
    """用默认快照器取一次全系统快照（异常见 NtProcessSnapshotter.snapshot）。"""
    return default_snapshotter().snapshot()


def unavailable_reason() -> Optional[str]:
    """默认快照器不可用的原因；可用时为 None。"""
    return default_snapshotter().unavailable_reason


__all__ = [
    'ProcessSample', 'SystemSnapshot', 'TakenAt', 'SnapshotUnavailable', 'SnapshotError',
    'NtProcessSnapshotter', 'default_snapshotter', 'available', 'snapshot',
    'unavailable_reason', 'parse_buffer', 'check_layout', 'build_layout', 'Layout',
    'SYSTEM_PROCESS_INFORMATION', 'SYSTEM_THREAD_INFORMATION', 'UNICODE_STRING', 'CLIENT_ID',
    'STATUS_INFO_LENGTH_MISMATCH', 'STATUS_BUFFER_TOO_SMALL', 'INITIAL_BUFFER_SIZE',
    'BUFFER_GROW_MARGIN', 'BUFFER_GROW_FACTOR', 'MAX_QUERY_ATTEMPTS',
]
