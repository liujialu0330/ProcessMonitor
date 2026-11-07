"""
启动与关闭被测程序（core.process_launcher）：

- 真实启动 sys.executable 子进程，拿到 PID 与创建时间，再 terminate 结束它；
- 创建时间对不上（PID 被复用）时不动该进程；缺创建时间时同样不关；
- 有顶层窗口时先投递 WM_CLOSE，自行退出算 closed；没窗口直接结束；
- 启动失败（未填、相对路径、文件不存在、目录不存在、立即退出、OSError）都给中文原因；
- LaunchSpec 的 JSON 往返与字段校验，参数拆分 / 拼接；
- 后台关闭线程（TerminateWorker）登记到线程登记表。
"""
import os
import subprocess
import sys

import psutil
import pytest
from PyQt5.QtWidgets import QApplication

from core.process_launcher import (
    ERR_CWD_NOT_FOUND, ERR_EXE_EMPTY, ERR_EXE_NOT_ABSOLUTE, ERR_EXE_NOT_FOUND,
    ERR_ELEVATION_REQUIRED, ERR_BAD_EXE, TERMINATE_CLOSED, TERMINATE_FAILED, TERMINATE_GONE,
    TERMINATE_KILLED, TERMINATE_REUSED, LaunchFormatError, LaunchResult, LaunchSpec,
    ProcessLauncher, join_args, split_args,
)
from core.shutdown import ThreadRegistry
from core.workers.terminate_worker import THREAD_NAME, close_in_background
from tests.doubles import FakeLauncher
from tests.helpers import poll_until

SLEEPER_ARGS = ("-c", "import time; time.sleep(30)")


def _sleeper_spec(**kwargs) -> LaunchSpec:
    return LaunchSpec(exe=sys.executable, args=SLEEPER_ARGS, **kwargs)


@pytest.fixture
def launched():
    """记下测试里启动的 PID，结束时兜底结束，避免残留 30 秒的子进程。"""
    pids = []
    yield pids
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


def _no_windows(_pid):
    return []


# ---------- 真实启动与关闭 ----------

def test_launch_real_process_reports_pid_and_create_time(launched):
    launcher = ProcessLauncher(settle_s=0.2, window_finder=_no_windows)

    result = launcher.launch(_sleeper_spec())
    launched.append(result.pid)

    assert result.ok and result.error is None
    proc = psutil.Process(result.pid)
    assert proc.is_running()
    # 创建时间与 psutil 读到的一致：监控任务据此做 PID 复用保护
    assert abs(proc.create_time() - result.create_time) < 0.01
    assert result.name.lower().startswith("python")
    # 缺省工作目录 = 程序所在目录
    assert os.path.normcase(proc.cwd()) == os.path.normcase(os.path.dirname(sys.executable))


def test_launch_uses_given_cwd_and_args(launched, tmp_path):
    launcher = ProcessLauncher(settle_s=0.2, window_finder=_no_windows)

    result = launcher.launch(_sleeper_spec(cwd=str(tmp_path)))
    launched.append(result.pid)

    proc = psutil.Process(result.pid)
    assert os.path.normcase(proc.cwd()) == os.path.normcase(str(tmp_path))
    assert proc.cmdline()[1:] == list(SLEEPER_ARGS)


def test_terminate_without_windows_kills_process(launched):
    launcher = ProcessLauncher(settle_s=0.2, window_finder=_no_windows)
    result = launcher.launch(_sleeper_spec())
    launched.append(result.pid)

    outcome = launcher.terminate(result.pid, result.create_time)

    assert outcome == TERMINATE_KILLED
    assert not psutil.pid_exists(result.pid) or \
        psutil.Process(result.pid).status() == psutil.STATUS_ZOMBIE


def test_terminate_refuses_when_create_time_does_not_match(launched):
    """PID 复用保护：创建时间对不上说明已不是当初启动的程序，绝不能结束它。"""
    closed = []
    launcher = ProcessLauncher(settle_s=0.2, window_finder=lambda pid: [1],
                               closer=lambda hwnds: closed.append(hwnds) or 1)
    result = launcher.launch(_sleeper_spec())
    launched.append(result.pid)

    outcome = launcher.terminate(result.pid, result.create_time + 5.0)

    assert outcome == TERMINATE_REUSED
    assert closed == []
    assert psutil.Process(result.pid).is_running()


def test_terminate_refuses_without_create_time(launched):
    launcher = ProcessLauncher(settle_s=0.2, window_finder=_no_windows)
    result = launcher.launch(_sleeper_spec())
    launched.append(result.pid)

    assert launcher.terminate(result.pid, None) == TERMINATE_FAILED
    assert psutil.Process(result.pid).is_running()


def test_terminate_gone_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(10)
    create_time = 1.0
    launcher = ProcessLauncher(window_finder=_no_windows)

    assert launcher.terminate(proc.pid, create_time) in (TERMINATE_GONE, TERMINATE_REUSED)


def test_terminate_posts_wm_close_first_and_counts_self_exit_as_closed(launched):
    """有顶层窗口时先礼后兵：投递 WM_CLOSE 后进程自己退出 → closed，不走结束进程。"""
    posted = []

    def closer(hwnds):
        # 模拟程序响应 WM_CLOSE 自行退出
        posted.append(list(hwnds))
        psutil.Process(result.pid).kill()
        return len(hwnds)

    launcher = ProcessLauncher(settle_s=0.2, close_timeout_s=5.0,
                               window_finder=lambda pid: [111, 222], closer=closer)
    result = launcher.launch(_sleeper_spec())
    launched.append(result.pid)

    assert launcher.terminate(result.pid, result.create_time) == TERMINATE_CLOSED
    assert posted == [[111, 222]]


def test_terminate_kills_when_window_ignores_close(launched):
    launcher = ProcessLauncher(settle_s=0.2, close_timeout_s=0.3,
                               window_finder=lambda pid: [111], closer=lambda hwnds: 1)
    result = launcher.launch(_sleeper_spec())
    launched.append(result.pid)

    assert launcher.terminate(result.pid, result.create_time) == TERMINATE_KILLED


@pytest.mark.skipif(sys.platform != "win32", reason="WM_CLOSE 只在 Windows 上有意义")
def test_real_window_closes_on_wm_close(launched):
    """真实顶层窗口（Tk）：EnumWindows 找得到、WM_CLOSE 后自行退出。"""
    script = ("import tkinter as tk; r = tk.Tk(); r.title('pm-launcher-test'); "
              "r.after(30000, r.destroy); r.mainloop()")
    launcher = ProcessLauncher(settle_s=0.3, close_timeout_s=5.0)
    result = launcher.launch(LaunchSpec(exe=sys.executable, args=("-c", script)))
    launched.append(result.pid)
    assert result.ok, result.error
    from core.process_launcher import top_level_windows
    assert poll_until(lambda: top_level_windows(result.pid), timeout=10)

    assert launcher.terminate(result.pid, result.create_time) == TERMINATE_CLOSED


# ---------- 启动失败的中文原因 ----------

def test_launch_rejects_empty_and_relative_and_missing(tmp_path):
    launcher = ProcessLauncher()

    assert launcher.launch(LaunchSpec(exe="  ")).error == ERR_EXE_EMPTY
    assert launcher.launch(LaunchSpec(exe="demo.exe")).error == \
        ERR_EXE_NOT_ABSOLUTE.format(exe="demo.exe")
    missing = str(tmp_path / "没有这个程序.exe")
    assert launcher.launch(LaunchSpec(exe=missing)).error == ERR_EXE_NOT_FOUND.format(exe=missing)
    bad_cwd = str(tmp_path / "没有这个目录")
    assert launcher.launch(_sleeper_spec(cwd=bad_cwd)).error == \
        ERR_CWD_NOT_FOUND.format(cwd=bad_cwd)


def test_launch_reports_immediate_exit():
    launcher = ProcessLauncher(settle_s=2.0)

    result = launcher.launch(LaunchSpec(
        exe=sys.executable, args=("-c", "import sys; sys.exit(3)")))

    assert not result.ok and result.pid is None
    assert "立即退出" in result.error and "3" in result.error


@pytest.mark.parametrize("exc, expected", [
    (OSError(22, "x", None, 740), ERR_ELEVATION_REQUIRED),
    (OSError(22, "x", None, 193), ERR_BAD_EXE.format(exe=sys.executable)),
    (PermissionError(13, "拒绝访问"), "没有权限"),
    (OSError(5, "奇怪的错误"), "启动程序失败"),
])
def test_launch_maps_os_errors_to_chinese(exc, expected):
    def popen(*_args, **_kwargs):
        raise exc

    result = ProcessLauncher(popen=popen).launch(_sleeper_spec())

    assert result.pid is None and expected in result.error


def test_launch_does_not_capture_output_or_change_console():
    seen = {}

    def popen(command, **kwargs):
        seen.update(kwargs, command=command)
        raise OSError(5, "停在这里")

    ProcessLauncher(popen=popen).launch(_sleeper_spec())

    assert seen["command"] == [sys.executable, *SLEEPER_ARGS]
    assert seen["creationflags"] == 0
    assert "stdout" not in seen and "stderr" not in seen and "stdin" not in seen
    assert seen["cwd"] == os.path.dirname(sys.executable)


# ---------- LaunchSpec ----------

def test_launch_spec_json_round_trip():
    spec = LaunchSpec(exe=r"D:\app\演示 程序.exe", args=("--mode", "a b", ""), cwd=r"D:\data")

    assert LaunchSpec.from_json(spec.to_json()) == spec
    assert LaunchSpec.from_dict(spec.to_dict()) == spec
    assert spec.name == "演示 程序.exe"


def test_launch_spec_normalizes_blank_cwd():
    spec = LaunchSpec(exe=r"  D:\app\a.exe ", cwd="   ")

    assert spec.exe == r"D:\app\a.exe" and spec.cwd is None
    assert spec.effective_cwd() == os.path.dirname(r"D:\app\a.exe")


@pytest.mark.parametrize("payload", [
    "[]", '{"exe": ""}', '{"exe": 1}', '{"exe": "a", "args": "x"}',
    '{"exe": "a", "args": [1]}', '{"exe": "a", "cwd": 2}', '{"exe": "a", "env": {}}', "不是 JSON",
])
def test_launch_spec_rejects_bad_json(payload):
    with pytest.raises(LaunchFormatError):
        LaunchSpec.from_json(payload)


def test_split_and_join_args_round_trip():
    assert split_args('--port 8080 "C:\\Program Files\\x" -v') == \
        ["--port", "8080", "C:\\Program Files\\x", "-v"]
    assert split_args("") == [] and split_args(None) == []
    args = ["--name", "a b", "plain"]
    assert split_args(join_args(args)) == args


# ---------- 后台关闭 ----------

def test_close_in_background_registers_thread_and_terminates(qapp):
    launcher = FakeLauncher()
    registry = ThreadRegistry()

    close_in_background(launcher, 4321, 99.5, registry)

    # 结束通知经排队信号回到界面线程，处理事件之前登记一定还在
    assert [entry.name for entry in registry._entries] == [THREAD_NAME]
    assert poll_until(lambda: (QApplication.processEvents(), launcher.terminated)[1], timeout=5)
    assert launcher.terminated == [(4321, 99.5)]
    # 线程结束后自动注销
    assert poll_until(lambda: (QApplication.processEvents(), not registry._entries)[1], timeout=5)


def test_launch_result_ok_flag():
    assert LaunchResult(pid=1, create_time=1.0).ok
    assert not LaunchResult(error="x").ok
    assert not LaunchResult(pid=1, error="x").ok
