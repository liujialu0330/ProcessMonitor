"""
真实进程：退出取证与重启续接走生产实现（Win32 退出码句柄、psutil 采集、进程目录查找），
只有时钟是真的、没有任何替身。

- 子进程以 0xC0000005 退出（os._exit 与真实的非法内存访问两种）：任务以 CRASHED 结束，
  tasks.exit_code 与"异常退出"打点正确；
- 普通退出码 0、被 taskkill 式终止的退出码 1：仍是 PROCESS_EXITED；
- 同名程序退出后再启动：同一任务接着记录，restart 打点写明新 PID。

用一份改名的 python.exe 副本当被测程序，进程名唯一，不会与别的 python 进程混淆；
所有子进程在 finally 中回收。
"""
import os
import shutil
import subprocess
import sys
import uuid

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import MonitorWorker
from data.repos.point_repo import PointQuery
from tests.helpers import poll_until

pytestmark = [pytest.mark.skipif(sys.platform != 'win32', reason="退出码句柄是 Win32 实现"),
              pytest.mark.real_exit_probe]

METRIC = 'memory_rss'
JOIN_MS = 30000
CRASH_AFTER_S = 1.0

CRASH_SCRIPT = (
    "import ctypes,time;"
    "ctypes.windll.kernel32.SetErrorMode(3);"
    f"time.sleep({CRASH_AFTER_S});"
    "ctypes.windll.kernel32.CreateThread(None,0,None,None,0,None);"
    "time.sleep(30)")


@pytest.fixture
def exe(tmp_path):
    """唯一进程名的 python.exe 副本。"""
    name = f"soakprobe_{uuid.uuid4().hex[:8]}.exe"
    target = tmp_path / name
    shutil.copy(sys.executable, target)
    return str(target)


@pytest.fixture
def spawn(exe):
    children = []
    env = dict(os.environ, PYTHONHOME=sys.base_prefix, PYTHONIOENCODING='utf-8')

    def _spawn(script: str) -> subprocess.Popen:
        child = subprocess.Popen([exe, '-c', script], env=env, stdin=subprocess.DEVNULL,
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


def _worker(db, child, exe, **options):
    worker = MonitorWorker(pid=child.pid, process_name=os.path.basename(exe),
                           metric_types=[METRIC], interval=0.2, db=db, **options)
    stops = []
    worker.task_stopped.connect(lambda _t, reason: stops.append(reason), Qt.DirectConnection)
    return worker, stops


@pytest.fixture
def started(qapp):
    workers = []
    yield workers
    for w in workers:
        w.stop()
        w.wait(5000)


def _run(worker, started):
    started.append(worker)
    worker.start()


def _wait_stopped(worker):
    assert worker.wait(JOIN_MS), "采集线程未在时限内结束"


def test_real_access_violation_is_crashed(db, spawn, exe, started):
    child = spawn(CRASH_SCRIPT)
    worker, stops = _worker(db, child, exe)
    _run(worker, started)
    _wait_stopped(worker)

    assert stops == [StopReason.CRASHED]
    task = db.tasks.get(worker.task_id)
    assert task.exit_code == 0xC0000005
    assert task.stop_reason == 'crashed'
    labels = [m.label for m in db.markers.list(worker.task_id) if m.kind == 'crash']
    assert labels == ["异常退出 0xC0000005（访问冲突）"]
    # 崩溃前采到了数据
    assert len(db.points.fetch(PointQuery(worker.task_id, METRIC))) >= 2


def test_os_exit_with_access_violation_code_is_crashed(db, spawn, exe, started):
    """os._exit 的参数是 C int，0xC0000005 写成有符号值 -1073741819"""
    child = spawn("import time,os; time.sleep(1.0); os._exit(-1073741819)")
    worker, stops = _worker(db, child, exe)
    _run(worker, started)
    _wait_stopped(worker)
    assert stops == [StopReason.CRASHED]
    assert db.tasks.get(worker.task_id).exit_code == 0xC0000005


@pytest.mark.parametrize("code", [0, 2])
def test_real_ordinary_exit_is_process_exited(db, spawn, exe, started, code):
    child = spawn(f"import time,os; time.sleep(1.0); os._exit({code})")
    worker, stops = _worker(db, child, exe)
    _run(worker, started)
    _wait_stopped(worker)
    assert stops == [StopReason.PROCESS_EXITED]
    task = db.tasks.get(worker.task_id)
    assert task.exit_code == code
    assert [m for m in db.markers.list(worker.task_id) if m.kind == 'crash'] == []


def test_real_forced_kill_exit_code_1_is_not_a_crash(db, spawn, exe, started):
    """taskkill /F 与 TerminateProcess 的退出码是 1：用户 / 脚本主动结束，不算崩溃"""
    child = spawn("import time; time.sleep(60)")
    worker, stops = _worker(db, child, exe)
    _run(worker, started)
    assert poll_until(lambda: len(db.points.fetch(PointQuery(worker.task_id, METRIC))) >= 2,
                      timeout=15)
    child.kill()
    _wait_stopped(worker)
    assert stops == [StopReason.PROCESS_EXITED]
    assert db.tasks.get(worker.task_id).exit_code == 1


def test_real_same_name_restart_resumes_same_task(db, spawn, exe, started):
    first = spawn("import time; time.sleep(1.5)")
    worker, stops = _worker(db, first, exe, resume_on_restart=True, restart_limit=1)
    _run(worker, started)
    assert poll_until(worker.is_waiting_restart, timeout=20), "第一个进程退出后应进入等待重启"

    second = spawn("import time; time.sleep(1.5)")
    _wait_stopped(worker)

    tid = worker.task_id
    assert stops == [StopReason.PROCESS_EXITED]       # 第二个进程退出时已达上限
    task = db.tasks.get(tid)
    assert task.task_id == tid and task.pid == second.pid
    restart = [m for m in db.markers.list(tid) if m.kind == 'restart']
    assert [m.label for m in restart] == [f"第 2 次启动 PID {second.pid}"]
    stamps = [p.timestamp for p in db.points.fetch(PointQuery(tid, METRIC))]
    assert stamps == sorted(stamps)
    # 重启打点前后都有样本（第一个进程的、第二个进程的），时间戳单调
    assert any(t <= restart[0].timestamp for t in stamps)
    assert any(t > restart[0].timestamp for t in stamps)


def test_real_restart_finder_ignores_other_programs(db, spawn, exe, started, tmp_path):
    """同名但路径不同的进程不算同一个程序：不续接，继续等待，直到用户停止"""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_exe = other_dir / os.path.basename(exe)
    shutil.copy(sys.executable, other_exe)

    first = spawn("import time; time.sleep(1.0)")
    worker, stops = _worker(db, first, exe, resume_on_restart=True, restart_limit=2)
    _run(worker, started)
    assert poll_until(worker.is_waiting_restart, timeout=20)

    env = dict(os.environ, PYTHONHOME=sys.base_prefix)
    impostor = subprocess.Popen([str(other_exe), '-c', 'import time; time.sleep(8)'], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        assert not poll_until(lambda: not worker.is_waiting_restart(), timeout=3.5)
        assert worker.restart_count == 0
        worker.stop()
        _wait_stopped(worker)
        assert stops == [StopReason.PROCESS_EXITED]
    finally:
        impostor.kill()
        impostor.wait(timeout=10)
