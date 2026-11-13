"""
core/system_snapshot：结构自检、缓冲区重试、手工缓冲区解析、不可用降级。

手工缓冲区按真实布局构造：条目头 → 线程数组 → 附加数据（模拟新版 Windows 在线程
与映像名之间的 368 字节）→ 映像名（UTF-16），ImageName.Buffer 写绝对地址，条目
步长只由 NextEntryOffset 给出。
"""
import ctypes
import logging
import os
import sys
import threading

import pytest

from core import system_snapshot as ss
from core.system_snapshot import (
    BUFFER_GROW_MARGIN, INITIAL_BUFFER_SIZE, MAX_QUERY_ATTEMPTS, STATUS_BUFFER_TOO_SMALL,
    STATUS_INFO_LENGTH_MISMATCH, SYSTEM_PROCESS_INFORMATION as SPI,
    SYSTEM_THREAD_INFORMATION as STI, NtProcessSnapshotter, SnapshotError,
    SnapshotUnavailable, build_layout, check_layout, parse_buffer,
)

POINTER_SIZE = ctypes.sizeof(ctypes.c_void_p)
TRAILING = 368          # 线程数组与映像名之间的附加数据
OWN_PID = 4242          # 手工缓冲区里扮演"本进程"的 PID（带 2 个线程）
EPOCH_TICKS = 116444736000000000


def ticks_of(epoch_seconds: float) -> int:
    return int(round(epoch_seconds * 10_000_000)) + EPOCH_TICKS


def entry(pid, name, threads=(), thread_owner=None, **fields):
    """一个条目的描述：threads 为各线程的 ContextSwitches；thread_owner 缺省等于 pid"""
    return dict(pid=pid, name=name, threads=list(threads),
                thread_owner=pid if thread_owner is None else thread_owner, fields=fields)


DEFAULT_ENTRIES = [
    entry(0, "", threads=[7], CreateTime=ticks_of(1_700_000_000.0)),
    entry(OWN_PID, "测试进程.exe", threads=[100, 23],
          CreateTime=ticks_of(1_794_000_000.5), UserTime=12_500_000, KernelTime=7_500_000,
          CycleTime=987654321, BasePriority=8, InheritedFromUniqueProcessId=77,
          HandleCount=345, SessionId=1, PeakVirtualSize=(1 << 32) + 4096,
          VirtualSize=1 << 32, PageFaultCount=12345, PeakWorkingSetSize=20480 * 1024,
          WorkingSetSize=10240 * 1024, QuotaPeakPagedPoolUsage=2560,
          QuotaPagedPoolUsage=1536, QuotaPeakNonPagedPoolUsage=1280,
          QuotaNonPagedPoolUsage=768, PagefileUsage=3072 * 1024 + 512,
          PeakPagefileUsage=4096 * 1024, PrivatePageCount=3072 * 1024 + 512,
          ReadOperationCount=11, WriteOperationCount=22, OtherOperationCount=33,
          ReadTransferCount=5 * 1024 * 1024, WriteTransferCount=3000,
          OtherTransferCount=7 * 1024 + 256),
]


def _entry_size(e) -> int:
    name_bytes = len(e['name'].encode('utf-16-le'))
    size = ctypes.sizeof(SPI) + len(e['threads']) * ctypes.sizeof(STI) + TRAILING
    size += name_bytes + 2 if name_bytes else 0
    return (size + 7) & ~7


def total_size(entries) -> int:
    return sum(_entry_size(e) for e in entries)


def write_entries(buffer, entries, *, name_pointer_shift: int = 0) -> int:
    """把条目写进 ctypes 缓冲区，返回有效长度。name_pointer_shift 用于制造越界指针。"""
    base = ctypes.addressof(buffer)
    pos = 0
    for index, e in enumerate(entries):
        size = _entry_size(e)
        last = index == len(entries) - 1
        spi = SPI.from_buffer(buffer, pos)
        spi.NextEntryOffset = 0 if last else size
        spi.NumberOfThreads = len(e['threads'])
        spi.UniqueProcessId = e['pid'] or None
        for key, value in e['fields'].items():
            setattr(spi, key, value)
        thread_pos = pos + ctypes.sizeof(SPI)
        for k, switches in enumerate(e['threads']):
            sti = STI.from_buffer(buffer, thread_pos + k * ctypes.sizeof(STI))
            sti.ClientId.UniqueProcess = e['thread_owner'] or None
            sti.ClientId.UniqueThread = 1000 + k
            sti.ContextSwitches = switches
        name_bytes = e['name'].encode('utf-16-le')
        if name_bytes:
            name_pos = thread_pos + len(e['threads']) * ctypes.sizeof(STI) + TRAILING
            ctypes.memmove(base + name_pos, name_bytes, len(name_bytes))
            spi.ImageName.Length = len(name_bytes)
            spi.ImageName.MaximumLength = len(name_bytes) + 2
            spi.ImageName.Buffer = base + name_pos + name_pointer_shift
        pos += size
    return pos


class FakeQuery:
    """NtQuerySystemInformation 替身：前 mismatches 次报长度不足，之后写入条目。"""

    def __init__(self, entries=None, mismatches=0, status=STATUS_INFO_LENGTH_MISMATCH,
                 needed=None, fail_status=None):
        self.entries = DEFAULT_ENTRIES if entries is None else entries
        self.mismatches = mismatches
        self.status = status
        self.needed = needed
        self.fail_status = fail_status
        self.sizes = []

    def __call__(self, buffer, size):
        self.sizes.append(size)
        assert ctypes.sizeof(buffer) == size
        if self.fail_status is not None:
            return self.fail_status, 0
        need = self.needed or total_size(self.entries)
        if len(self.sizes) <= self.mismatches or size < total_size(self.entries):
            return self.status, need
        return 0, write_entries(buffer, self.entries)


def _hand_buffer(entries=None, **kwargs):
    entries = DEFAULT_ENTRIES if entries is None else entries
    buffer = ctypes.create_string_buffer(total_size(entries) + 64)
    length = write_entries(buffer, entries, **kwargs)
    return buffer, length


# ========== 结构 ==========

@pytest.mark.skipif(POINTER_SIZE != 8, reason="64 位布局")
def test_x64_layout_matches_reference_table():
    """与 phnt / ReactOS 推算的 x64 偏移表一致（资料核对笔记）"""
    assert ctypes.sizeof(SPI) == 256
    assert ctypes.sizeof(STI) == 80
    offsets = {name: getattr(SPI, name).offset for name, _ in SPI._fields_}
    assert offsets['ImageName'] == 56
    assert offsets['BasePriority'] == 72
    assert offsets['UniqueProcessId'] == 80
    assert offsets['HandleCount'] == 96
    assert offsets['PeakVirtualSize'] == 112
    assert offsets['PageFaultCount'] == 128
    assert offsets['PrivatePageCount'] == 200
    assert offsets['ReadOperationCount'] == 208
    assert offsets['OtherTransferCount'] == 248
    assert ss.UNICODE_STRING.Buffer.offset == 8


def test_struct_format_spans_whole_header():
    layout = build_layout()
    assert layout.header.size == ctypes.sizeof(SPI)
    assert layout.header_size == ss.EXPECTED_PROCESS_INFO_SIZE[POINTER_SIZE]
    assert layout.thread_size == ss.EXPECTED_THREAD_INFO_SIZE[POINTER_SIZE]


# ========== 解析手工缓冲区 ==========

def test_parse_two_entries_with_threads_and_trailing_data():
    buffer, length = _hand_buffer()
    snap = parse_buffer(buffer, length, build_layout())

    assert set(snap.processes) == {0, OWN_PID}
    idle = snap.processes[0]
    assert idle.name == ''                  # PID 0 没有映像名
    assert idle.create_time == 0.0          # PID 0/4 按 psutil 口径为 0.0
    assert idle.num_threads == 1

    s = snap.processes[OWN_PID]
    assert s.name == "测试进程.exe"
    assert s.create_time == pytest.approx(1_794_000_000.5, abs=1e-6)
    assert (s.num_threads, s.handle_count, s.session_id, s.parent_pid) == (2, 345, 1, 77)
    assert s.base_priority == 8
    assert (s.working_set, s.peak_working_set) == (10240 * 1024, 20480 * 1024)
    assert s.private_bytes == s.pagefile == 3072 * 1024 + 512
    assert s.peak_pagefile == 4096 * 1024
    assert (s.virtual_size, s.peak_virtual) == (1 << 32, (1 << 32) + 4096)
    assert (s.paged_pool, s.peak_paged_pool) == (1536, 2560)
    assert (s.nonpaged_pool, s.peak_nonpaged_pool) == (768, 1280)
    assert s.page_faults == 12345
    assert (s.read_count, s.write_count, s.other_count) == (11, 22, 33)
    assert (s.read_bytes, s.write_bytes, s.other_bytes) == (5 * 1024 * 1024, 3000, 7 * 1024 + 256)
    assert (s.user_time, s.kernel_time) == (1.25, 0.75)
    assert s.cycle_time == 987654321

    # 上下文切换 = 各线程之和（psutil num_ctx_switches().voluntary 同口径）
    assert snap.context_switches(OWN_PID) == 123
    assert snap.context_switches(0) == 7
    assert snap.context_switches(999) is None


def test_system_pid_4_create_time_is_zero():
    entries = [entry(4, "System", threads=[1], CreateTime=ticks_of(1_700_000_000.0))]
    buffer, length = _hand_buffer(entries)
    assert parse_buffer(buffer, length, build_layout()).processes[4].create_time == 0.0


def test_filetime_conversion_matches_psutil_formula():
    """(ticks - 116444736000000000) / 1e7，与 psutil C 实现逐位相同"""
    ticks = 134385077439972351
    assert ss._filetime_to_epoch(ticks) == (ticks - EPOCH_TICKS) / 10_000_000


def test_name_pointer_outside_entry_is_layout_error():
    buffer, length = _hand_buffer(name_pointer_shift=4096)
    with pytest.raises(ss._LayoutError):
        parse_buffer(buffer, length, build_layout())


def test_truncated_buffer_is_layout_error():
    buffer, length = _hand_buffer()
    with pytest.raises(ss._LayoutError):
        parse_buffer(buffer, length - 300, build_layout())


# ========== 自检 ==========

def test_check_layout_passes_on_consistent_buffer():
    buffer, length = _hand_buffer()
    info = check_layout(buffer, length, build_layout(), OWN_PID)
    assert info['process_info_size'] == ctypes.sizeof(SPI)
    assert info['first_number_of_threads'] == 1
    assert info['first_trailing_bytes'] == TRAILING
    assert info['process_count'] == 2


def test_check_layout_rejects_thread_owner_mismatch():
    """线程 ClientId 与 PID 不符说明条目头或线程结构大小判断错了"""
    entries = [DEFAULT_ENTRIES[0], {**DEFAULT_ENTRIES[1], 'thread_owner': 1}]
    buffer, length = _hand_buffer(entries)
    with pytest.raises(ss._LayoutError, match="ClientId"):
        check_layout(buffer, length, build_layout(), OWN_PID)


def test_check_layout_requires_own_process():
    buffer, length = _hand_buffer()
    with pytest.raises(ss._LayoutError, match="本进程"):
        check_layout(buffer, length, build_layout(), OWN_PID + 1)


def test_check_layout_rejects_unexpected_struct_size(monkeypatch):
    monkeypatch.setitem(ss.EXPECTED_PROCESS_INFO_SIZE, POINTER_SIZE, 999)
    buffer, length = _hand_buffer()
    with pytest.raises(ss._LayoutError, match="结构大小"):
        check_layout(buffer, length, build_layout(), OWN_PID)


def test_snapshotter_with_bad_layout_disables_and_logs_once(caplog):
    entries = [DEFAULT_ENTRIES[0], {**DEFAULT_ENTRIES[1], 'thread_owner': 1}]
    snapper = NtProcessSnapshotter(FakeQuery(entries), own_pid=OWN_PID)
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        assert snapper.available() is False
        assert snapper.available() is False
        with pytest.raises(SnapshotUnavailable):
            snapper.snapshot()
    assert "结构自检不通过" in snapper.unavailable_reason
    assert len([r for r in caplog.records if "已禁用" in r.getMessage()]) == 1


# ========== 缓冲区重试 ==========

def test_length_mismatch_twice_then_success_grows_and_remembers_size():
    needed = INITIAL_BUFFER_SIZE + 300_000
    query = FakeQuery(mismatches=2, needed=needed)
    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    assert snapper.available() is True        # 自检本身也经历了两次长度不足
    first = max(needed + BUFFER_GROW_MARGIN, int(INITIAL_BUFFER_SIZE * 1.25))
    second = max(needed + BUFFER_GROW_MARGIN, int(first * 1.25))
    assert query.sizes == [INITIAL_BUFFER_SIZE, first, second]
    assert snapper.buffer_size == second

    snap = snapper.snapshot()
    assert query.sizes[-1] == second          # 记住的大小作为下次初始值，一次成功
    assert len(query.sizes) == 4
    assert snap.processes[OWN_PID].name == "测试进程.exe"


def test_buffer_too_small_status_is_also_retried():
    query = FakeQuery(mismatches=1, status=STATUS_BUFFER_TOO_SMALL, needed=10)
    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    assert snapper.available() is True
    # ReturnLength 很小时按 1.25 倍增长
    assert query.sizes == [INITIAL_BUFFER_SIZE, int(INITIAL_BUFFER_SIZE * 1.25)]


def test_retries_exhausted_is_transient_error():
    query = FakeQuery()
    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    assert snapper.available()
    query.mismatches = 10 ** 6                # 之后一直报长度不足
    with pytest.raises(SnapshotError, match="重试"):
        snapper.snapshot()
    assert len(query.sizes) == 1 + MAX_QUERY_ATTEMPTS
    assert snapper.available() is True        # 偶发失败不禁用


def test_unexpected_status_is_transient_error():
    query = FakeQuery()
    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    assert snapper.available()
    query.fail_status = 0xC0000022            # STATUS_ACCESS_DENIED
    with pytest.raises(SnapshotError, match="C0000022"):
        snapper.snapshot()
    assert snapper.available() is True


def test_parse_failure_after_self_check_disables():
    query = FakeQuery()
    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    assert snapper.available()

    def broken(buffer, size):
        status, length = FakeQuery()(buffer, size)
        return status, length - 300           # 截断：最后一个条目越界
    snapper._query = broken
    with pytest.raises(SnapshotUnavailable):
        snapper.snapshot()
    assert snapper.available() is False
    assert "解析失败" in snapper.unavailable_reason


# ========== 不可用的平台与环境 ==========

def test_non_windows_is_unavailable(caplog):
    snapper = NtProcessSnapshotter(platform="linux")
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        assert snapper.available() is False
        with pytest.raises(SnapshotUnavailable, match="非 Windows"):
            snapper.snapshot()
        snapper.available()
    assert len([r for r in caplog.records if "已禁用" in r.getMessage()]) == 1


def test_missing_ntdll_is_unavailable(monkeypatch):
    def missing():
        raise OSError("找不到 ntdll")
    monkeypatch.setattr(ss, "_load_nt_query", missing)
    snapper = NtProcessSnapshotter(platform="win32")
    assert snapper.available() is False
    assert "ntdll" in snapper.unavailable_reason


def test_query_exception_during_self_check_is_unavailable():
    def boom(buffer, size):
        raise RuntimeError("意外")
    snapper = NtProcessSnapshotter(boom, own_pid=OWN_PID)
    assert snapper.available() is False
    assert "自检异常" in snapper.unavailable_reason


# ========== 本机真实快照 ==========

@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")
def test_real_snapshot_contains_self():
    snapper = NtProcessSnapshotter()
    assert snapper.available(), snapper.unavailable_reason
    assert snapper.diagnostics['process_info_size'] == ss.EXPECTED_PROCESS_INFO_SIZE[POINTER_SIZE]
    snap = snapper.snapshot()
    own = snap.processes[os.getpid()]
    assert own.num_threads >= 1
    assert own.private_bytes > 0 and own.working_set > 0
    assert snap.context_switches(os.getpid()) > 0
    assert 0 in snap.processes
    assert snap.taken_at.wall > 0


# ========== 评审修复：条目步长、32 位、线程步长自检 ==========

def _with_first_next_offset(value):
    """好缓冲区的首条目 NextEntryOffset 改成 value"""
    buffer, length = _hand_buffer()
    ctypes.c_ulong.from_buffer(buffer, 0).value = value
    return buffer, length


@pytest.mark.parametrize("bad_offset", [1, 100, ctypes.sizeof(SPI) - 1])
def test_next_entry_offset_between_zero_and_header_size_is_layout_error(bad_offset):
    """NextEntryOffset 落在 (0, 条目头大小) 之间：步长不可能这么短，按解析错误抛出"""
    buffer, length = _with_first_next_offset(bad_offset)
    with pytest.raises(ss._LayoutError, match="NextEntryOffset"):
        parse_buffer(buffer, length, build_layout())


def test_bad_next_entry_offset_after_self_check_disables_snapshot_and_collector_falls_back():
    """自检之后某次快照的步长异常：快照永久禁用，采集器回退 psutil 且数据照常"""
    from core.process_collector import ProcessCollector
    from core.snapshot_hub import SnapshotHub
    from tests.test_samplers import _FAKE_PID, _fake_process
    from utils.metrics import MetricType

    good = FakeQuery()
    state = {'bad': False}

    def query(buffer, size):
        status, length = good(buffer, size)
        if state['bad'] and status == 0:
            ctypes.c_ulong.from_buffer(buffer, 0).value = 100
        return status, length

    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    assert snapper.available() is True
    state['bad'] = True
    collector = ProcessCollector(_FAKE_PID, hub=SnapshotHub(snapper))
    collector._process = _fake_process()
    assert collector.collect_metrics([MetricType.NUM_THREADS]) == {MetricType.NUM_THREADS: 17.0}
    assert collector.last_source == 'psutil'
    assert snapper.available() is False
    assert "NextEntryOffset" in snapper.unavailable_reason


def test_32bit_pointer_disables_snapshot_without_querying_and_logs_once(monkeypatch, caplog):
    """指针 4 字节（32 位 Python / WOW64）：结构布局未验证，不调用系统接口，直接禁用并回退"""
    from core.process_collector import ProcessCollector
    from core.snapshot_hub import SnapshotHub
    from tests.test_samplers import _FAKE_PID, _fake_process
    from utils.metrics import MetricType

    monkeypatch.setattr(ss, "_pointer_size", lambda: 4)
    query = FakeQuery()
    snapper = NtProcessSnapshotter(query, own_pid=OWN_PID)
    with caplog.at_level(logging.WARNING, logger=ss.__name__):
        assert snapper.available() is False
        assert snapper.available() is False
        with pytest.raises(SnapshotUnavailable):
            snapper.snapshot()
    assert query.sizes == []
    assert "32 位" in snapper.unavailable_reason
    assert len([r for r in caplog.records if "已禁用" in r.getMessage()]) == 1

    collector = ProcessCollector(_FAKE_PID, hub=SnapshotHub(snapper))
    collector._process = _fake_process()
    assert collector.collect_metrics([MetricType.NUM_THREADS]) == {MetricType.NUM_THREADS: 17.0}
    assert collector.last_source == 'psutil'


def test_check_layout_reports_whether_thread_stride_was_verified():
    single = [DEFAULT_ENTRIES[0], {**DEFAULT_ENTRIES[1], 'threads': [5]}]
    buffer, length = _hand_buffer(single)
    info = check_layout(buffer, length, build_layout(), OWN_PID)
    assert (info['own_threads'], info['thread_stride_verified']) == (1, False)
    buffer, length = _hand_buffer()
    info = check_layout(buffer, length, build_layout(), OWN_PID)
    assert (info['own_threads'], info['thread_stride_verified']) == (2, True)


def _own_process_query(thread_count_of, seen):
    """本进程（真实 PID）的线程数由 thread_count_of() 决定的 NtQuerySystemInformation 替身"""
    def query(buffer, size):
        count = thread_count_of()
        seen.append(count)
        entries = [DEFAULT_ENTRIES[0], entry(os.getpid(), "本进程.exe", threads=[5] * count)]
        return 0, write_entries(buffer, entries)
    return query


def _real_thread_count() -> int:
    """真正由 threading 创建的线程数：不算 _DummyThread——别的用例遗留的 Qt 工作线程在收尾时
    调一下 logging / current_thread 就会给应用多登记一个 Dummy-N，让计数在用例中途变化"""
    return len([t for t in threading.enumerate() if not isinstance(t, threading._DummyThread)])


def test_self_check_starts_idle_thread_when_own_process_has_one_thread():
    """本进程只有一个线程时线程结构步长验证不到：自检临时起空闲线程再查一遍，查完结束它"""
    from tests.helpers import wait_stable
    wait_stable(_real_thread_count, quiet=0.1, timeout=3.0)   # 让前面用例遗留的线程先收尾
    baseline = _real_thread_count()
    seen = []
    # 假内核：线程数 = 当前 Python 线程数 - 基线 + 1（主线程算 1 个）
    query = _own_process_query(lambda: _real_thread_count() - baseline + 1, seen)
    snapper = NtProcessSnapshotter(query)
    assert snapper.available() is True, snapper.unavailable_reason
    assert seen == [1, 2]
    assert snapper.diagnostics['thread_stride_verified'] is True
    assert _real_thread_count() == baseline
    assert not [t for t in threading.enumerate() if t.name == "snapshot-selfcheck-idle"]


def test_self_check_does_not_start_thread_when_enough_threads():
    seen = []
    snapper = NtProcessSnapshotter(_own_process_query(lambda: 3, seen))
    assert snapper.available() is True
    assert seen == [3]


def test_self_check_disables_when_stride_still_unverifiable_after_idle_thread():
    seen = []
    snapper = NtProcessSnapshotter(_own_process_query(lambda: 1, seen))
    assert snapper.available() is False
    assert "线程" in snapper.unavailable_reason and len(seen) == 2
    assert threading.active_count() >= 1
    assert not [t for t in threading.enumerate() if t.name == "snapshot-selfcheck-idle"]
