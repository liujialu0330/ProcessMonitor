"""
进程树任务的真实进程端到端：一个 Python 子进程再拉起 2 个孙进程，用生产的成员来源
（全系统快照 + psutil 回退）跑几个真实周期，断言成员、进程总数与留痕；再结束根进程，
断言任务以"进程退出"收尾。

进程清理：测试里拉起的整棵进程树（含 Windows venv 重定向器多出来的中间进程）在 finally 里
逐个 kill 并等待消失，断言没有遗留。
"""
import os
import subprocess
import sys

import psutil
import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import MonitorWorker
from core.process_tree import GroupSpec
from data.repos.point_repo import PointQuery
from tests.helpers import poll_until
from tests.test_monitor_worker_clock import started  # noqa: F401
from utils.metrics import MetricType, member_series_key

MEM = MetricType.MEMORY_PRIVATE
COUNT = 'process_count'

PARENT_SCRIPT = """
import subprocess, sys, time
kids = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        for _ in range(2)]
print(' '.join(str(k.pid) for k in kids), flush=True)
time.sleep(120)
"""


def kill_known(identities) -> None:
    """按 (pid, 创建时间) 杀掉登记过的进程（创建时间对不上说明 PID 已被复用，不动它）并等它们
    消失。根进程先死时孙进程成了孤儿、查不到父子关系，所以必须按开始时登记的名单杀。"""
    victims = []
    for pid, create_time in identities:
        try:
            proc = psutil.Process(pid)
            if abs(proc.create_time() - create_time) < 0.01:
                victims.append(proc)
        except psutil.NoSuchProcess:
            continue
    for victim in victims:
        try:
            victim.kill()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(victims, timeout=10)
    assert not alive, f"测试进程未清理干净：{[p.pid for p in alive]}"


@pytest.fixture
def process_tree():
    """根进程 + 它拉起的 2 个孙进程；返回 (根 Popen, 孙进程 PID 列表)。结束后整棵树清掉
    （含 Windows venv 重定向器多出来的中间进程：按拉起后登记的全部后代清理）。"""
    proc = subprocess.Popen([sys.executable, '-c', PARENT_SCRIPT], stdout=subprocess.PIPE,
                            text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    identities = []
    try:
        kids = [int(x) for x in proc.stdout.readline().split()]
        assert len(kids) == 2, "孙进程没有按预期拉起"
        parent = psutil.Process(proc.pid)
        identities = [(p.pid, p.create_time())
                      for p in parent.children(recursive=True) + [parent]]
        yield proc, kids
    finally:
        if not identities and psutil.pid_exists(proc.pid):
            # 登记名单之前就失败了：至少把根进程清掉
            identities = [(proc.pid, psutil.Process(proc.pid).create_time())]
        kill_known(identities)
        proc.wait(10)
        proc.stdout.close()


def series(db, task_id, metric, member=None):
    key = metric if member is None else member_series_key(metric, member)
    return [p.value for p in db.points.fetch(PointQuery(task_id, key))]


@pytest.mark.real_exit_probe
def test_tree_task_over_real_processes(db, qapp, started, process_tree):  # noqa: F811
    root, kids = process_tree
    create_time = psutil.Process(root.pid).create_time()
    worker = MonitorWorker(
        pid=root.pid, process_name=psutil.Process(root.pid).name(), metric_types=[MEM, COUNT],
        interval=0.5, db=db, create_time=create_time,
        group_spec=GroupSpec.tree(root.pid, create_time))
    stops = []
    worker.task_stopped.connect(lambda _tid, reason: stops.append(reason), Qt.DirectConnection)
    started.append(worker)
    worker.start()

    # 至少采到 3 个周期，且进程总数 >= 3（根 + 2 个孙进程；venv 重定向器可能让数量更多）
    assert poll_until(lambda: len(series(db, worker.task_id, COUNT)) >= 3, timeout=30)
    counts = series(db, worker.task_id, COUNT)
    assert min(counts) >= 3
    assert all(c == counts[0] for c in counts)   # 进程树没变，数量稳定

    members = worker.group_members()
    assert members and sum(m.instance_count for m in members) == counts[-1]
    name = psutil.Process(root.pid).name().lower()
    assert name in {m.member_key for m in members}
    # 合计内存 = 各成员之和，且确实读到了（> 0）
    total = series(db, worker.task_id, MEM)
    assert total and total[-1] > 0
    parts = [series(db, worker.task_id, MEM, m.member_key)[-1] for m in members]
    assert abs(sum(parts) - total[-1]) < 1e-6
    # 本测试进程自身（pytest 的 python.exe）与根同名，但永远不是成员
    assert os.getpid() not in {r.pid for r in db.members.list_instances(worker.task_id)}

    # 结束根进程：任务以"被测进程退出"收尾（孙进程成了孤儿，但根没了）
    kill_tree_root_only(root)
    assert worker.wait(20000), "根进程退出后任务没有结束"
    assert stops == [StopReason.PROCESS_EXITED]

    rows = {r.pid: r for r in db.members.list_instances(worker.task_id)}
    assert root.pid in rows and set(kids) <= set(rows), "孙进程应出现在 task_members 留痕里"
    assert all(r.last_ms >= r.first_ms for r in rows.values())


def kill_tree_root_only(proc: subprocess.Popen) -> None:
    """只结束根进程本身（不动后代）。"""
    psutil.Process(proc.pid).kill()
    proc.wait(10)
