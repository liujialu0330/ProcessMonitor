"""
core/system_snapshot 与 psutil 的真实进程对拍（测试进程自身 + 一个真实子进程）。

结构偏移一旦错位，手工缓冲区测试发现不了（缓冲区是按同一份结构声明造的），只有拿
真实内核数据与独立来源比才能发现，所以这里用真实进程逐字段对拍。

被测对象
- 测试进程自身（内存、线程、句柄随测试在动，用来验证"会变动字段"的夹逼口径）；
- 子进程（sys.executable 拉起）：分配 48 MB 并逐页写入、开 4 个线程、打开 3 个文件并
  读写，然后阻塞在 stdin 上。它在测量期间几乎静止，用来验证"稳定字段"的精确相等。

对拍方法
- 静态字段（PID、父 PID、进程名、会话号）：精确相等；create_time 容差 2 个 ulp；
- 单调不减字段（峰值、缺页数、IO 计数与字节、CPU 时间、上下文切换）：先取快照 A，
  紧接着读 psutil，再取快照 B，断言 A <= psutil <= B，不留容差；
- 会变动但不单调的字段（线程数、句柄数、工作集、私有字节、页面文件、换页池）：同样
  夹在 A、B 之间，再加一个写明理由的小容差；子进程的稳定字段容差更小。

已对上的字段（快照字段 -> psutil）
- pid / parent_pid -> Process.pid / ppid()，另与 os.getpid()、os.getppid() 独立来源比
- name -> name()
- create_time -> create_time()（相差不超过 2 个 ulp，浮点换算顺序不同所致）
- session_id -> kernel32.ProcessIdToSessionId（psutil 无此项，改与 Win32 接口比）
- base_priority -> nice()（优先级类到基础优先级的固定映射）
- num_threads -> num_threads()
- handle_count -> num_handles()
- working_set / peak_working_set -> memory_info().rss / peak_wset
- private_bytes -> memory_info().private
- pagefile / peak_pagefile -> memory_info().pagefile / peak_pagefile
- paged_pool / peak_paged_pool / nonpaged_pool / peak_nonpaged_pool -> memory_info() 同名
- page_faults -> memory_info().num_page_faults
- read_count / write_count / other_count -> io_counters() 同名
- read_bytes / write_bytes / other_bytes -> io_counters() 同名
- user_time / kernel_time -> cpu_times().user / system
- context_switches(pid) -> num_ctx_switches().voluntary
- PID 4（System）的 create_time 为 0.0、名称为 System（与 psutil 口径一致）

没有对拍的字段与原因
- virtual_size / peak_virtual：psutil 的 vms 在 Windows 上等于私有字节，不是虚拟地址空间
  大小，两者口径不同，没有可比对象；
- cycle_time：psutil 没有 CPU 周期计数；
- num_ctx_switches().involuntary：psutil 在 Windows 上恒为 0，没有信息量；
- PID 0 的名称：快照里为空串，由 process_catalog 按 psutil 口径补，不在本模块职责内；
- cpu_percent / memory_percent：由差分与物理内存派生，不是快照原始字段（派生公式有
  各自的单元测试）。

独立性说明：psutil 的 memory_info / io_counters / num_threads / ppid 等在内部有部分走
的也是 NtQuerySystemInformation，因此与快照"同源"的字段对拍只能保证解析偏移正确，不能
保证内核口径正确；PID、父 PID、会话号、创建时间的下界与上界另与 os / Win32 独立来源比。
"""
import math
import os
import queue
import subprocess
import sys
import threading
import time

import psutil
import pytest

from core import system_snapshot

pytestmark = pytest.mark.skipif(
    sys.platform != 'win32', reason='NtQuerySystemInformation 仅 Windows 可用')

# 子进程分配并写入的内存（字节）与线程、文件数量
CHILD_ALLOC_BYTES = 48 * 1024 * 1024
CHILD_THREADS = 4
CHILD_FILES = 3
CHILD_READY_TIMEOUT_S = 30.0

CHILD_SCRIPT = r"""
import os, sys, threading
buf = bytearray({alloc})
for i in range(0, len(buf), 4096):
    buf[i] = 1
stop = threading.Event()
for _ in range({threads}):
    threading.Thread(target=stop.wait, daemon=True).start()
files = []
for i in range({files}):
    f = open(os.path.join(sys.argv[1], 'parity_%d.bin' % i), 'w+b')
    f.write(b'x' * 65536 * (i + 1))
    f.flush()
    f.seek(0)
    f.read()
    files.append(f)
print('READY %d %d' % (os.getpid(), os.getppid()), flush=True)
sys.stdin.readline()
""".format(alloc=CHILD_ALLOC_BYTES, threads=CHILD_THREADS, files=CHILD_FILES)

# Windows 进程优先级类 -> 基础优先级（SYSTEM_PROCESS_INFORMATION.BasePriority）
PRIORITY_CLASS_TO_BASE = {
    psutil.IDLE_PRIORITY_CLASS: 4,
    psutil.BELOW_NORMAL_PRIORITY_CLASS: 6,
    psutil.NORMAL_PRIORITY_CLASS: 8,
    psutil.ABOVE_NORMAL_PRIORITY_CLASS: 10,
    psutil.HIGH_PRIORITY_CLASS: 13,
    psutil.REALTIME_PRIORITY_CLASS: 24,
}

# ---- 容差（字节或个数）。夹在两次快照之间之外再放宽的量；每项理由见使用处 ----
KB = 1024
# 测试进程自身
SELF_TOL_THREADS = 1          # pytest / Python 运行时偶尔临时起停 1 个辅助线程
SELF_TOL_HANDLES = 4          # psutil 每次读取都会临时 OpenProcess，实测瞬时多出至多 1 个，留 4 的余量
SELF_TOL_WORKING_SET = 512 * KB   # 解释器持续分配对象，两次读取之间工作集可能涨落数十到数百 KB
SELF_TOL_PRIVATE = 512 * KB       # 同上，私有字节随堆增长
SELF_TOL_POOL = 16 * KB       # 换页池 / 非换页池配额随句柄数同步小幅波动（每个句柄约百字节）
# 子进程（阻塞等待，几乎静止）
CHILD_TOL_THREADS = 0         # 子进程线程全部阻塞，数量固定
CHILD_TOL_HANDLES = 0         # 子进程没有人去开关它的句柄
CHILD_TOL_WORKING_SET = 128 * KB       # 系统可能在读取间隙修剪空闲进程的工作集（实测未发生）
CHILD_TOL_PRIVATE = 0         # 私有提交量只在子进程自己分配时变化，阻塞后固定
CHILD_TOL_POOL = 0            # 子进程静止，配额不变


@pytest.fixture(scope='module')
def snapshotter():
    snap = system_snapshot.NtProcessSnapshotter()
    if not snap.available():
        pytest.skip(f'全系统快照不可用：{snap.unavailable_reason}')
    return snap


class Target:
    """一个被对拍的进程：PID、psutil 句柄、是否是子进程、独立来源给出的父 PID。"""

    def __init__(self, label, pid, expected_ppid, born_after, born_before):
        self.label = label
        self.pid = pid
        self.proc = psutil.Process(pid)
        self.is_child = label == 'child'
        self.expected_ppid = expected_ppid
        # create_time 必须落在 [born_after, born_before]（墙钟，独立于快照的下界与上界）
        self.born_after = born_after
        self.born_before = born_before

    @property
    def tol(self):
        if self.is_child:
            return dict(threads=CHILD_TOL_THREADS, handles=CHILD_TOL_HANDLES,
                        working_set=CHILD_TOL_WORKING_SET, private=CHILD_TOL_PRIVATE,
                        pool=CHILD_TOL_POOL)
        return dict(threads=SELF_TOL_THREADS, handles=SELF_TOL_HANDLES,
                    working_set=SELF_TOL_WORKING_SET, private=SELF_TOL_PRIVATE,
                    pool=SELF_TOL_POOL)


def _start_child(tmp_dir):
    """拉起子进程并等到它报告就绪；返回 (Popen, 子进程自报的 pid, 自报的父 pid, 出生时间窗)。"""
    before = time.time() - 1.0   # 创建时间下界留 1 秒：FILETIME 精度与墙钟读数先后
    proc = subprocess.Popen(
        [sys.executable, '-c', CHILD_SCRIPT, str(tmp_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines: 'queue.Queue[bytes]' = queue.Queue()

    def pump():
        for raw in iter(proc.stdout.readline, b''):
            lines.put(raw)
        lines.put(b'')

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + CHILD_READY_TIMEOUT_S
    seen = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f'子进程 {CHILD_READY_TIMEOUT_S}s 内未就绪，输出：{seen}')
        try:
            raw = lines.get(timeout=remaining)
        except queue.Empty:
            raise AssertionError(f'子进程 {CHILD_READY_TIMEOUT_S}s 内未就绪，输出：{seen}')
        if raw == b'':
            raise AssertionError(f'子进程提前退出，输出：{seen}')
        text = raw.decode('utf-8', 'replace').strip()
        seen.append(text)
        if text.startswith('READY '):
            _, pid, ppid = text.split()
            break
    after = time.time() + 1.0
    return proc, int(pid), int(ppid), before, after


def _stop_child(proc, reported_pid):
    """终止并回收子进程；对 venv 启动器场景，另按自报 PID 清理真正干活的进程。"""
    try:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        if reported_pid != proc.pid:
            try:
                psutil.Process(reported_pid).kill()
            except psutil.Error:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail('子进程在 kill 后 10 秒内仍未退出')
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


@pytest.fixture(scope='module')
def child_process(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp('parity_child')
    proc, pid, ppid, before, after = _start_child(tmp_dir)
    try:
        time.sleep(0.3)   # 让子进程的线程全部进入等待、IO 落定
        yield proc, pid, ppid, before, after
    finally:
        _stop_child(proc, pid)


@pytest.fixture(params=['self', 'child'])
def target(request, snapshotter):
    if request.param == 'self':
        return Target('self', os.getpid(), os.getppid(),
                      born_after=0.0, born_before=time.time() + 1.0)
    proc, pid, ppid, before, after = request.getfixturevalue('child_process')
    return Target('child', pid, ppid, born_after=before, born_before=after)


def _read_psutil(p):
    """一次读完全部可对拍的 psutil 字段。"""
    mem = p.memory_info()
    io = p.io_counters()
    cpu = p.cpu_times()
    return {
        'num_threads': p.num_threads(),
        'handle_count': p.num_handles(),
        'working_set': mem.rss, 'peak_working_set': mem.peak_wset,
        'private_bytes': mem.private,
        'pagefile': mem.pagefile, 'peak_pagefile': mem.peak_pagefile,
        'paged_pool': mem.paged_pool, 'peak_paged_pool': mem.peak_paged_pool,
        'nonpaged_pool': mem.nonpaged_pool, 'peak_nonpaged_pool': mem.peak_nonpaged_pool,
        'page_faults': mem.num_page_faults,
        'read_count': io.read_count, 'write_count': io.write_count,
        'other_count': io.other_count,
        'read_bytes': io.read_bytes, 'write_bytes': io.write_bytes,
        'other_bytes': io.other_bytes,
        'user_time': cpu.user, 'kernel_time': cpu.system,
        'ctx_voluntary': p.num_ctx_switches().voluntary,
    }


def sandwich(snapshotter, target):
    """快照 A -> psutil -> 快照 B；返回 (A 的样本, B 的样本, psutil 取值, A 快照, B 快照)。"""
    snap_a = snapshotter.snapshot()
    values = _read_psutil(target.proc)
    snap_b = snapshotter.snapshot()
    return snap_a.processes[target.pid], snap_b.processes[target.pid], values, snap_a, snap_b


def _sample_ctx(snap, pid):
    total = snap.context_switches(pid)
    assert total is not None, f'快照里没有 PID {pid} 的线程数组'
    return total


def assert_bracketed(label, lo_value, hi_value, ps_value, tol=0):
    """psutil 的值必须夹在两次快照之间（加容差）。单调字段 tol 为 0。"""
    lo = min(lo_value, hi_value) - tol
    hi = max(lo_value, hi_value) + tol
    assert lo <= ps_value <= hi, (
        f'{label}：psutil={ps_value}，快照 A={lo_value}，快照 B={hi_value}，容差 {tol}')


# ---------------------------------------------------------------- 静态字段


def test_static_fields_equal(snapshotter, target):
    sample = snapshotter.snapshot().processes[target.pid]
    p = target.proc

    assert sample.pid == target.pid == p.pid
    # 父 PID 与 os.getppid / 子进程自报的父 PID（CreateToolhelp32Snapshot 路径）独立比，
    # 同时与 psutil 比
    assert sample.parent_pid == target.expected_ppid
    assert sample.parent_pid == p.ppid()
    # 进程名：psutil 取可执行文件名，快照取映像名，同一进程应逐字相同
    assert sample.name == p.name()
    assert sample.name.lower() == os.path.basename(sys.executable).lower()
    # create_time：两边读的是同一个 FILETIME（100 ns 整数刻度），但换算成 epoch 秒的浮点
    # 算式不同（快照先整数相减再除，psutil 用高低位分别乘系数），结果可能差 1 个 ulp
    # （约 2.4e-7 秒，实测出现过）。容差取 2 个 ulp，远小于 100 ns 刻度之外的任何真实误差
    assert abs(sample.create_time - p.create_time()) <= 2 * math.ulp(sample.create_time)
    # 独立来源给出的上下界：不是 0，也不会晚于现在
    assert target.born_after <= sample.create_time <= target.born_before

    # 会话号：psutil 没有，改与 kernel32.ProcessIdToSessionId 比
    import ctypes
    session = ctypes.c_ulong(0)
    assert ctypes.windll.kernel32.ProcessIdToSessionId(target.pid, ctypes.byref(session))
    assert sample.session_id == session.value

    # 基础优先级：psutil 给的是优先级类，映射到 BasePriority
    assert sample.base_priority == PRIORITY_CLASS_TO_BASE[p.nice()]


def test_system_process_identity(snapshotter):
    """PID 4（System）：create_time 按 psutil 口径为 0.0，名称为 System，父 PID 为 0。"""
    sample = snapshotter.snapshot().processes[4]
    try:
        p = psutil.Process(4)
        ps_create, ps_name, ps_ppid = p.create_time(), p.name(), p.ppid()
    except psutil.AccessDenied:
        pytest.skip('无权读取 System 进程信息')
    assert sample.create_time == ps_create == 0.0
    assert sample.name == ps_name == 'System'
    assert sample.parent_pid == ps_ppid


# ---------------------------------------------------------------- 单调不减字段


MONOTONIC_FIELDS = (
    # (快照字段, psutil 字段)
    ('peak_working_set', 'peak_working_set'),
    ('peak_pagefile', 'peak_pagefile'),
    ('peak_paged_pool', 'peak_paged_pool'),
    ('peak_nonpaged_pool', 'peak_nonpaged_pool'),
    ('page_faults', 'page_faults'),
    ('read_count', 'read_count'),
    ('write_count', 'write_count'),
    ('other_count', 'other_count'),
    ('read_bytes', 'read_bytes'),
    ('write_bytes', 'write_bytes'),
    ('other_bytes', 'other_bytes'),
    ('user_time', 'user_time'),
    ('kernel_time', 'kernel_time'),
)


@pytest.mark.parametrize('field,ps_key', MONOTONIC_FIELDS, ids=[f[0] for f in MONOTONIC_FIELDS])
def test_monotonic_counters_bracketed(snapshotter, target, field, ps_key):
    # 峰值、缺页、IO 与 CPU 时间只增不减：psutil 的读数必须落在前后两次快照之间，零容差
    sample_a, sample_b, values, _, _ = sandwich(snapshotter, target)
    assert_bracketed(f'{target.label}.{field}', getattr(sample_a, field),
                     getattr(sample_b, field), values[ps_key])


def test_context_switches_bracketed(snapshotter, target):
    # 全部线程的上下文切换次数之和只增不减（线程退出后其计数并入进程，不会倒退）；
    # 对应 psutil num_ctx_switches().voluntary
    _, _, values, snap_a, snap_b = sandwich(snapshotter, target)
    assert_bracketed(f'{target.label}.context_switches',
                     _sample_ctx(snap_a, target.pid), _sample_ctx(snap_b, target.pid),
                     values['ctx_voluntary'])


# ---------------------------------------------------------------- 会变动字段


def test_threads_and_handles(snapshotter, target):
    sample_a, sample_b, values, _, _ = sandwich(snapshotter, target)
    tol = target.tol
    assert_bracketed(f'{target.label}.num_threads', sample_a.num_threads,
                     sample_b.num_threads, values['num_threads'], tol['threads'])
    assert_bracketed(f'{target.label}.handle_count', sample_a.handle_count,
                     sample_b.handle_count, values['handle_count'], tol['handles'])


def test_memory_fields(snapshotter, target):
    sample_a, sample_b, values, _, _ = sandwich(snapshotter, target)
    tol = target.tol
    for field, key in (('working_set', 'working_set'),):
        assert_bracketed(f'{target.label}.{field}', getattr(sample_a, field),
                         getattr(sample_b, field), values[key], tol['working_set'])
    for field in ('private_bytes', 'pagefile'):
        assert_bracketed(f'{target.label}.{field}', getattr(sample_a, field),
                         getattr(sample_b, field), values[field], tol['private'])
    for field in ('paged_pool', 'nonpaged_pool'):
        assert_bracketed(f'{target.label}.{field}', getattr(sample_a, field),
                         getattr(sample_b, field), values[field], tol['pool'])


def test_private_bytes_unit_is_bytes(snapshotter, target):
    # PrivatePageCount 的单位是字节而不是页数：与 psutil 的 private 相差不到一个数量级，
    # 且不会等于（字节数 / 4096）。用相对误差守住"没有被误当成页"
    sample_a, sample_b, values, _, _ = sandwich(snapshotter, target)
    ratio = sample_a.private_bytes / max(values['private_bytes'], 1)
    assert 0.5 < ratio < 2.0, f'private_bytes 快照/psutil 比值 {ratio}'


# ---------------------------------------------------------------- 子进程的负载确实被看见


def test_child_workload_is_visible(snapshotter, child_process):
    _, pid, _, _, _ = child_process
    sample = snapshotter.snapshot().processes[pid]
    # 分配了 48 MB 并逐页写入：工作集与私有字节都应不小于它
    assert sample.working_set >= CHILD_ALLOC_BYTES
    assert sample.private_bytes >= CHILD_ALLOC_BYTES
    # 主线程 + 4 个等待线程
    assert sample.num_threads >= CHILD_THREADS + 1
    # 读写过 3 个文件：写计数与写字节至少覆盖写入量（64K + 128K + 192K）
    assert sample.write_count >= CHILD_FILES
    assert sample.write_bytes >= 65536 * (1 + 2 + 3)
    assert sample.read_count >= 1 and sample.read_bytes > 0
    # 自身手工构造的对拍也不能是"全 0 对全 0"
    assert sample.handle_count > CHILD_FILES
