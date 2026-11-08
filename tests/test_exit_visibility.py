"""
被测进程退出后，任务必须在一两个周期内发现（快照路径 + 退出码句柄）。

背景：任务为了读退出码会一直握着被测进程的句柄。句柄不放，已结束的进程对象仍存在；如果
全系统快照里还留着它的条目（线程数为 0 的"僵尸"），快照路径就读到一份冻结的数据，任务
永远发现不了退出。本模块覆盖三道防线与一次真实进程的实测：

- 实测（Win32 真进程 + 真 SnapshotHub）：握着句柄的已结束进程，本机的快照里没有它的条目，
  采集器按"已退出"处理，任务在 2 个周期内进入取证 / 等待重启——这是目前的实际行为；
- 防线 1：每次采样前先对握着的句柄做 WaitForSingleObject(h, 0)，已显示结束就按退出处理，
  不管采集器还读得到什么；
- 防线 2：快照条目线程数为 0 的目标，采集器按已退出处理（只针对被监控的目标，进程列表不变）；
- 防线 3：进程树任务的根 / 成员线程数为 0 同理（见 test_process_tree.py 的僵尸用例）。
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.exit_forensics import Win32ExitCodeProbe
from core.process_collector import ProcessCollector
from core.restart_finder import RestartHit
from core.snapshot_hub import SnapshotHub
from data.repos.point_repo import PointQuery
from tests.doubles import (FakeSnapshotSource, ManualMonotonic, fake_process_sample,
                           fake_system_snapshot)
from tests.helpers import poll_until
from tests.soak_doubles import (ACCESS_VIOLATION, ScriptedFinder, build_rig, run_to_end)

METRIC = 'memory_private'
FAKE_PID = 4242


@pytest.fixture
def started(qapp):
    workers = []
    yield workers
    for w in workers:
        w.stop()
        w.wait(5000)


# ========== 防线 2：快照里线程数为 0 的目标 ==========

def _collector_over(sample_fields):
    sample = fake_process_sample(FAKE_PID, **sample_fields)
    hub = SnapshotHub(FakeSnapshotSource(snapshots=[fake_system_snapshot([sample])]),
                      clock=ManualMonotonic())
    return ProcessCollector(FAKE_PID, hub=hub, max_age_s=-1.0)


def test_collector_reads_a_live_snapshot_entry():
    """对照：线程数正常的条目照常取值（证明下一个用例的 None 来自线程数为 0 这个条件）"""
    values = _collector_over({'num_threads': 5}).collect_metrics([METRIC])
    assert values is not None and METRIC in values


def test_collector_treats_zero_thread_snapshot_entry_as_exited():
    assert _collector_over({'num_threads': 0}).collect_metrics([METRIC]) is None


# ========== 防线 1：握着的句柄已显示结束 ==========

def test_signaled_handle_is_exit_even_if_collector_still_reads_values(db, started):
    """采集器仍能读到值（冻结的读数），但句柄已显示结束：不再采样，直接进入取证"""
    rig = build_rig(db, segments={100: [1.0] * 50}, codes={100: ACCESS_VIOLATION},
                    resume=False)
    rig.collectors[100].on_collect = lambda n: setattr(rig.probes[100], 'signaled', n >= 2)
    run_to_end(rig.worker, started)

    assert rig.stops == [StopReason.CRASHED]
    # 第 2 次采样后句柄被置为"已结束"：下个周期开头就发现，没有第 3 次采样
    assert rig.collectors[100].calls == 2
    assert db.tasks.get(rig.worker.task_id).exit_code == ACCESS_VIOLATION


def test_signaled_handle_leads_into_restart_wait(db, started):
    finder = ScriptedFinder({2: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1.0] * 50, 200: [2.0, 3.0]},
                    codes={100: ACCESS_VIOLATION, 200: 0}, finder=finder, resume=True, limit=1)
    rig.collectors[100].on_collect = lambda n: setattr(rig.probes[100], 'signaled', n >= 2)
    run_to_end(rig.worker, started)

    assert rig.restarts == [(200, 1)]
    assert rig.collectors[100].calls == 2
    assert rig.stops == [StopReason.PROCESS_EXITED]


# ========== 实测：真进程 + 真 SnapshotHub ==========

pytestmark_win = pytest.mark.skipif(sys.platform != 'win32', reason="退出码句柄是 Win32 实现")


@pytest.fixture
def real_exe(tmp_path):
    name = f"exitvis_{uuid.uuid4().hex[:8]}.exe"
    target = tmp_path / name
    shutil.copy(sys.executable, target)
    return str(target)


@pytest.fixture
def spawn(real_exe):
    children = []
    env = dict(os.environ, PYTHONHOME=sys.base_prefix, PYTHONIOENCODING='utf-8')

    def _spawn(script="import time; time.sleep(60)") -> subprocess.Popen:
        child = subprocess.Popen([real_exe, '-c', script], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        children.append(child)
        return child

    yield _spawn
    for child in children:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@pytestmark_win
def test_probe_has_exited_flips_when_process_ends(spawn):
    child = spawn()
    probe = Win32ExitCodeProbe(child.pid)
    try:
        assert probe.available
        assert probe.has_exited() is False
        child.kill()
        child.wait(timeout=10)
        assert poll_until(probe.has_exited, timeout=5)
        assert probe.exit_time() is not None
        assert probe.create_time() is not None and probe.create_time() < probe.exit_time()
    finally:
        probe.close()


@pytestmark_win
def test_probe_create_time_matches_psutil(spawn):
    import psutil
    child = spawn()
    probe = Win32ExitCodeProbe(child.pid)
    try:
        assert probe.create_time() == pytest.approx(
            psutil.Process(child.pid).create_time(), abs=0.05)
    finally:
        probe.close()


@pytestmark_win
def test_held_handle_process_is_not_alive_to_the_snapshot_collector(spawn):
    """记录实测：握着句柄的已结束进程，快照路径的采集器按"已退出"处理（本机的全系统
    快照在进程结束后就不再列出它，没有线程数为 0 的僵尸条目；万一出现，防线 2 兜住）"""
    child = spawn()
    time.sleep(0.6)
    hub = SnapshotHub()
    collector = ProcessCollector(child.pid, hub=hub)
    probe = Win32ExitCodeProbe(child.pid)       # 与任务一样握着句柄
    try:
        assert collector.collect_metrics(['memory_private', 'num_handles']) is not None
        child.kill()
        child.wait(timeout=10)
        assert poll_until(probe.has_exited, timeout=5)
        entry = hub.refresh().processes.get(child.pid)
        assert entry is None or entry.num_threads == 0
        assert collector.collect_metrics(['memory_private', 'num_handles']) is None
    finally:
        probe.close()


@pytest.mark.slow   # 真实进程 + 真实快照中心，约 1.6 秒；快速覆盖：本文件里线程数为 0 / 句柄已结束的假快照用例与 test_soak_watch
@pytestmark_win
@pytest.mark.real_exit_probe
def test_real_exit_with_snapshot_hub_is_noticed_within_two_cycles(db, spawn, real_exe, started):
    """真实进程 + 真实快照中心 + 真实退出码句柄：结束被测进程后，任务在 2 个周期内进入
    "等待重启"（不含快照网格对齐的至多 1 秒）。"""
    from core.monitor_task import MonitorWorker
    interval = 0.5
    child = spawn()
    waiting = threading.Event()
    waited_at = []
    finder = ScriptedFinder()               # 一直找不到：任务停在等待重启
    worker = MonitorWorker(
        pid=child.pid, process_name=os.path.basename(real_exe), metric_types=[METRIC],
        interval=interval, db=db, hub=SnapshotHub(), restart_finder=finder,
        resume_on_restart=True, restart_limit=1)

    def on_waiting(_task, on):
        if on:
            waited_at.append(time.monotonic())
            waiting.set()

    worker.waiting_restart_changed.connect(on_waiting, Qt.DirectConnection)
    started.append(worker)
    worker.start()
    try:
        assert poll_until(lambda: worker.task_id and len(
            db.points.fetch(PointQuery(worker.task_id, METRIC))) >= 2, timeout=15)
        killed_at = time.monotonic()
        child.kill()
        assert waiting.wait(timeout=15), "被测进程结束后任务没有进入等待重启"
        assert waited_at[0] - killed_at <= 2 * interval + 1.0
    finally:
        worker.stop()
        assert worker.wait(10000)
