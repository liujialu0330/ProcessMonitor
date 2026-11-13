"""
重启查找：什么样的同名进程才算"被测进程重启了"。

规则收紧的原因：认错进程会让两个进程的数据串到同一个任务里。判定依据是旧进程的退出时刻
（句柄的 ExitTime，取不到用最后一次成功采样的时刻）、父进程关系、创建时间是否可知。
"""
import os
import shutil
import subprocess
import sys
import uuid

import pytest

from core import process_catalog
from core.restart_finder import (EXIT_TOLERANCE_S, ProcessRestartFinder, RestartHit,
                                 RestartTarget)

# 旧进程：PID 100，1000 秒创建，2000 秒退出
OLD_PID = 100
OLD_CREATE = 1000.0
OLD_EXIT = 2000.0


class FakeQuery:
    """find_instances_by_name 的替身：instances 为 [(pid, 创建时间, 父 pid)]（名字都是 app.exe）。"""

    def __init__(self, *instances):
        self.instances = list(instances)

    def find_instances_by_name(self, name, **_kwargs):
        return list(self.instances) if name.lower() == 'app.exe' else []


def _target(**overrides):
    fields = dict(name='app.exe', exe_path=None, last_pid=OLD_PID, last_create_time=OLD_CREATE,
                  exit_time=OLD_EXIT, last_seen_time=OLD_EXIT - 1.0,
                  ancestor_pids=frozenset({OLD_PID}))
    fields.update(overrides)
    return RestartTarget(**fields)


def _find(target, *instances, exe_of=lambda pid: None):
    return ProcessRestartFinder(FakeQuery(*instances), exe_of=exe_of)(target)


# ========== 不该接的 ==========

def test_chromium_child_created_after_main_process_is_not_taken():
    """多进程程序：子进程与主进程同名同路径，在主进程之后很久创建、主进程退出时它还在"""
    assert _find(_target(), (101, OLD_CREATE + 500.0, OLD_PID)) is None


def test_child_of_old_process_created_within_tolerance_is_not_taken():
    """退出前一两秒才派生的子进程落在容差内，但它的父进程是旧进程：不是重启"""
    created = OLD_EXIT - 1.0
    assert created >= OLD_EXIT - EXIT_TOLERANCE_S
    assert _find(_target(), (101, created, OLD_PID)) is None


def test_child_of_old_tree_member_is_not_taken_for_tree_tasks():
    target = _target(ancestor_pids=frozenset({OLD_PID, 150}))
    assert _find(target, (151, OLD_EXIT - 0.5, 150)) is None


def test_instance_that_was_running_long_before_is_not_taken():
    """同一个程序早已在运行的另一个实例（创建时间早于旧进程，或早于它退出前很久）"""
    target = _target()
    assert _find(target, (300, OLD_CREATE - 500.0, 1)) is None
    assert _find(target, (301, OLD_CREATE + 400.0, 1)) is None


def test_candidate_with_unknown_create_time_is_never_taken():
    assert _find(_target(), (400, None, 1)) is None
    assert _find(_target(exit_time=None, last_seen_time=None, last_create_time=None),
                 (400, None, 1)) is None


def test_old_pid_itself_is_not_taken():
    assert _find(_target(), (OLD_PID, OLD_EXIT + 5.0, 1)) is None


def test_other_program_with_same_name_but_different_path_is_not_taken():
    target = _target(exe_path=r'C:\a\app.exe')
    assert _find(target, (400, OLD_EXIT + 3.0, 1), exe_of=lambda pid: r'C:\b\app.exe') is None


# ========== 该接的 ==========

def test_real_restart_after_exit_is_taken():
    hit = _find(_target(), (400, OLD_EXIT + 3.0, 1))
    assert hit == RestartHit(pid=400, create_time=OLD_EXIT + 3.0, name='app.exe')


def test_restart_created_just_before_exit_by_other_parent_is_taken_within_tolerance():
    """重启的新实例与旧进程的退出几乎同时（自更新程序先拉起新的再退出旧的）：容差内、父进程不是旧进程"""
    hit = _find(_target(), (400, OLD_EXIT - 1.0, 1))
    assert hit is not None and hit.pid == 400


def test_unreadable_path_does_not_block_resume():
    target = _target(exe_path=r'C:\a\app.exe')
    assert _find(target, (400, OLD_EXIT + 3.0, 1), exe_of=lambda pid: None) is not None


def test_newest_valid_instance_wins():
    hit = _find(_target(), (400, OLD_EXIT + 3.0, 1), (401, OLD_EXIT + 9.0, 1),
                (402, OLD_EXIT - 100.0, 1))
    assert hit.pid == 401


def test_falls_back_to_last_sample_time_when_exit_time_is_unknown():
    target = _target(exit_time=None, last_seen_time=1990.0)
    assert target.not_before() == 1990.0 - EXIT_TOLERANCE_S
    assert _find(target, (300, 1950.0, 1)) is None
    assert _find(target, (400, 2003.0, 1)).pid == 400


def test_without_any_reference_time_accepts_instances_newer_than_the_old_one():
    target = _target(exit_time=None, last_seen_time=None)
    assert target.not_before() is None
    assert _find(target, (300, OLD_CREATE - 1.0, 1)) is None
    assert _find(target, (400, OLD_CREATE + 50.0, 1)).pid == 400


# ========== 进程目录：真进程 ==========

@pytest.mark.skipif(sys.platform != 'win32', reason="用改名的 python.exe 副本，仅在 Windows 验证")
def test_find_instances_by_name_reports_create_time_and_parent(tmp_path):
    name = f"findinst_{uuid.uuid4().hex[:8]}.exe"
    copy = tmp_path / name
    shutil.copy(sys.executable, copy)
    env = dict(os.environ, PYTHONHOME=sys.base_prefix)
    child = subprocess.Popen([str(copy), '-c', 'import time; time.sleep(30)'], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        import time as _time
        import psutil
        deadline = _time.monotonic() + 10
        found = []
        while _time.monotonic() < deadline and not found:
            found = process_catalog.find_instances_by_name(name)
            _time.sleep(0.1)
        assert [item[0] for item in found] == [child.pid]
        pid, created, parent = found[0]
        assert created == pytest.approx(psutil.Process(child.pid).create_time(), abs=0.05)
        assert parent == os.getpid()
        # 同一个查找函数：旧进程的子进程不被认成重启
        target = RestartTarget(name=name, exe_path=None, last_pid=os.getpid(),
                               last_create_time=None, exit_time=created - 100.0,
                               ancestor_pids=frozenset({os.getpid()}))
        assert ProcessRestartFinder()(target) is None
        fresh = RestartTarget(name=name, exe_path=None, last_pid=1, last_create_time=None,
                              exit_time=created - 5.0, ancestor_pids=frozenset({1}))
        hit = ProcessRestartFinder()(fresh)
        assert hit is not None and hit.pid == child.pid
    finally:
        child.kill()
        child.wait(timeout=10)
