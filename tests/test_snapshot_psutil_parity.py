"""
全系统快照与 psutil 对拍（CI windows-latest 每次都跑；非 Windows 跳过）。

两者取值时刻不同，比较用"夹逼"：先读一次 psutil、再取快照、再读一次 psutil，
快照值应落在两次 psutil 读数构成的区间内（再放宽到允许误差）：
- 计数类（线程、句柄）：区间 ±2；
- 内存类：区间 ±5%；
- IO 计数与 CPU 时间、上下文切换（单调不减）：前一次 ≤ 快照 ≤ 后一次；
- create_time：误差 ≤ 0.01 秒；
- 进程列表的 PID 集合与 psutil.pids()：对称差 ≤ 3。
"""
import os
import sys

import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")

COUNT_SLACK = 2
MEMORY_RATIO = 0.05
CPU_EPS = 1e-6
PID_SYMMETRIC_DIFF_MAX = 3

# 快照字段 -> psutil memory_info() 字段
MEMORY_PAIRS = {
    'working_set': 'rss',
    'peak_working_set': 'peak_wset',
    'private_bytes': 'private',
    'pagefile': 'pagefile',
    'peak_pagefile': 'peak_pagefile',
    'paged_pool': 'paged_pool',
    'peak_paged_pool': 'peak_paged_pool',
    'nonpaged_pool': 'nonpaged_pool',
    'peak_nonpaged_pool': 'peak_nonpaged_pool',
}
IO_PAIRS = {
    'read_count': 'read_count', 'write_count': 'write_count', 'other_count': 'other_count',
    'read_bytes': 'read_bytes', 'write_bytes': 'write_bytes', 'other_bytes': 'other_bytes',
}


def _explorer_pid():
    for proc in psutil.process_iter(['name']):
        if (proc.info['name'] or '').lower() == 'explorer.exe':
            return proc.pid
    return None


def _psutil_reading(proc: psutil.Process) -> dict:
    with proc.oneshot():
        return dict(
            mem=proc.memory_info(), io=proc.io_counters(), cpu=proc.cpu_times(),
            threads=proc.num_threads(), handles=proc.num_handles(),
            ctx=proc.num_ctx_switches().voluntary, page_faults=proc.memory_info().num_page_faults,
        )


def _target_pids():
    pids = [pytest.param(os.getpid(), id='self')]
    explorer = _explorer_pid()
    pids.append(pytest.param(explorer, id='explorer', marks=pytest.mark.skipif(
        explorer is None, reason="未找到 explorer.exe")))
    return pids


@pytest.fixture(scope='module')
def snapper():
    from core.system_snapshot import NtProcessSnapshotter
    s = NtProcessSnapshotter()
    assert s.available(), s.unavailable_reason
    return s


def _within(value, a, b, ratio=0.0, slack=0.0):
    low, high = min(a, b), max(a, b)
    return low * (1 - ratio) - slack <= value <= high * (1 + ratio) + slack


@pytest.mark.parametrize('pid', _target_pids())
def test_snapshot_fields_match_psutil(snapper, pid):
    proc = psutil.Process(pid)
    before = _psutil_reading(proc)
    snap = snapper.snapshot()
    after = _psutil_reading(proc)
    sample = snap.processes[pid]

    report = {}
    for attr, key in (('num_threads', 'threads'), ('handle_count', 'handles')):
        report[attr] = (getattr(sample, attr), before[key], after[key])
        assert _within(getattr(sample, attr), before[key], after[key], slack=COUNT_SLACK), report

    for attr, field in MEMORY_PAIRS.items():
        a, b = getattr(before['mem'], field), getattr(after['mem'], field)
        report[attr] = (getattr(sample, attr), a, b)
        # 很小的缓冲池（几百字节）按比例放宽没有意义，再给一个页面的余量
        assert _within(getattr(sample, attr), a, b, ratio=MEMORY_RATIO, slack=4096), report

    for attr, field in IO_PAIRS.items():
        a, b = getattr(before['io'], field), getattr(after['io'], field)
        assert a <= getattr(sample, attr) <= b, (attr, getattr(sample, attr), a, b)

    assert before['page_faults'] <= sample.page_faults <= after['page_faults']
    assert before['cpu'].user - CPU_EPS <= sample.user_time <= after['cpu'].user + CPU_EPS
    assert before['cpu'].system - CPU_EPS <= sample.kernel_time <= after['cpu'].system + CPU_EPS
    switches = snap.context_switches(pid)
    assert before['ctx'] <= switches <= after['ctx'], (switches, before['ctx'], after['ctx'])

    assert abs(sample.create_time - proc.create_time()) <= 0.01
    assert sample.parent_pid == proc.ppid()
    assert sample.name.lower() == proc.name().lower()


def test_private_page_count_is_bytes_equal_to_pagefile(snapper):
    """PrivatePageCount 的单位是字节：全部进程都与 PagefileUsage（psutil 的 private/
    pagefile 来源 PrivateUsage）相等；若是页数，本进程会被放大 4096 倍"""
    snap = snapper.snapshot()
    assert all(s.private_bytes == s.pagefile for s in snap.processes.values())
    own = snap.processes[os.getpid()]
    psutil_private = psutil.Process().memory_info().private
    assert own.private_bytes == pytest.approx(psutil_private, rel=MEMORY_RATIO)


def test_pid_set_matches_psutil(snapper):
    from core import process_catalog
    from core.snapshot_hub import SnapshotHub
    before = set(psutil.pids())
    snap_pids = set(snapper.snapshot().processes)
    listed = {e.pid for e in process_catalog.list_processes(SnapshotHub(snapper))}
    after = set(psutil.pids())
    # 两次 psutil 之间的进程变动不计入
    stable = before & after
    churn = before ^ after
    assert len((snap_pids ^ stable) - churn) <= PID_SYMMETRIC_DIFF_MAX
    # 列表只少了空名进程（psutil 口径同样跳过）
    assert len((listed ^ stable) - churn) <= PID_SYMMETRIC_DIFF_MAX


@pytest.mark.slow   # 逐个比对整机进程（开发机上约 2 秒）；快速覆盖：test_snapshot_psutil_realproc_parity（本进程与子进程逐字段）
def test_create_times_match_psutil_for_all_processes(snapper):
    snap = snapper.snapshot()
    mismatched = []
    for proc in psutil.process_iter(['pid', 'create_time']):
        sample = snap.processes.get(proc.info['pid'])
        created = proc.info['create_time']
        if sample is None or created is None:
            continue
        if abs(sample.create_time - created) > 0.01:
            mismatched.append((proc.info['pid'], sample.create_time, created))
    assert mismatched == []


def test_collector_snapshot_and_psutil_paths_agree():
    """同一进程：快照路径与 psutil 路径的泄漏预设 5 项一致（计数 ±2、内存 ±5%）"""
    from core.process_collector import ProcessCollector
    from core.snapshot_hub import SnapshotHub
    from utils.metrics import LEAK_CHECK_PRESET, MetricType
    pid = os.getpid()
    by_psutil = ProcessCollector(pid).collect_metrics(LEAK_CHECK_PRESET)
    snapshot_collector = ProcessCollector(pid, hub=SnapshotHub(), max_age_s=0.0)
    by_snapshot = snapshot_collector.collect_metrics(LEAK_CHECK_PRESET)
    assert snapshot_collector.last_source == 'snapshot'
    assert set(by_snapshot) == set(by_psutil)
    for key, value in by_psutil.items():
        if key == MetricType.MEMORY_PRIVATE:
            assert by_snapshot[key] == pytest.approx(value, rel=MEMORY_RATIO)
        else:
            assert abs(by_snapshot[key] - value) <= COUNT_SLACK, key
