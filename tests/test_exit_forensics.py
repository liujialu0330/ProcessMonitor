"""
退出取证：NTSTATUS 异常码的中文说明、异常退出判定、退出码文本，以及真实 Win32 探针。

纯函数部分用表驱动；探针部分起真实的 Python 子进程（os._exit 指定退出码），用后一定回收。
"""
import contextlib
import subprocess
import sys

import pytest

from core.exit_forensics import (
    CRASH_MARKER_PREFIX, EXCEPTION_TEXTS, NullExitCodeProbe, Win32ExitCodeProbe,
    crash_marker_label, exception_text, format_exit_code, is_abnormal_exit, normalize_exit_code,
    open_exit_probe,
)
from tests.helpers import poll_until


# ========== 纯函数 ==========

@pytest.mark.parametrize("code, text", [
    (0xC0000005, "访问冲突"),
    (0xC0000409, "栈缓冲区溢出"),
    (0xC00000FD, "栈溢出"),
    (0xC0000374, "堆损坏"),
    (0x80000003, "断点"),
    (0xE06D7363, "C++ 未捕获异常"),
    (0xE0434352, ".NET 未处理异常"),
])
def test_known_exception_codes_have_chinese_text(code, text):
    assert exception_text(code) == text
    assert is_abnormal_exit(code)


def test_ctrl_c_exit_is_not_a_crash_but_keeps_its_text():
    """0xC000013A（STATUS_CONTROL_C_EXIT）是用户按 Ctrl+C 终止，不是崩溃：不触发崩溃判定，
    退出码照常记录并带文字说明"""
    assert not is_abnormal_exit(0xC000013A)
    assert not is_abnormal_exit(-1073741510)         # 有符号写法
    assert exception_text(0xC000013A) == ""
    assert format_exit_code(0xC000013A) == "0xC000013A（被 Ctrl+C 终止）"


@pytest.mark.parametrize("code", [0x80004005, 0x80070005, 0x8007000E, 0x80131500])
def test_hresult_exit_codes_are_not_crashes(code):
    """程序自己用 HRESULT 当返回值（0x8007xxxx 等）：不是崩溃，十六进制显示"""
    assert not is_abnormal_exit(code)
    assert exception_text(code) == ""
    assert format_exit_code(code) == f"0x{code:08X}"


@pytest.mark.parametrize("code", [0x80000001, 0x80000002, 0x80000003, 0x80000004])
def test_predefined_warning_exceptions_are_crashes(code):
    assert is_abnormal_exit(code)


def test_exception_table_codes_are_all_abnormal():
    """表里的每个码都必须被判为异常退出，否则查得到说明却不会触发崩溃判定"""
    assert EXCEPTION_TEXTS
    for code in EXCEPTION_TEXTS:
        assert is_abnormal_exit(code), hex(code)


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42, 255, 259, 0x40010005, 0x0FFFFFFF, None])
def test_ordinary_exit_codes_are_not_abnormal(code):
    """含 taskkill /f 的退出码 1、abort 的 3、DBG_CONTROL_C 的 0x40010005：都不算崩溃"""
    assert not is_abnormal_exit(code)
    assert exception_text(code) == ""


def test_signed_code_is_normalized_to_unsigned():
    """GetExitCodeProcess 偶尔以有符号数给出；-1073741819 与 0xC0000005 是同一个值"""
    assert normalize_exit_code(-1073741819) == 0xC0000005
    assert is_abnormal_exit(-1073741819)
    assert exception_text(-1073741819) == "访问冲突"
    assert normalize_exit_code(None) is None


def test_unknown_exception_code_still_abnormal_with_generic_text():
    assert is_abnormal_exit(0xC0DEAD00)
    assert exception_text(0xC0DEAD00) == "未知异常"
    assert format_exit_code(0xC0DEAD00) == "0xC0DEAD00（未知异常）"


def test_format_exit_code_texts():
    assert format_exit_code(0xC0000005) == "0xC0000005（访问冲突）"
    assert format_exit_code(-1073741819) == "0xC0000005（访问冲突）"
    assert format_exit_code(0) == "0"
    assert format_exit_code(1) == "1"
    assert format_exit_code(None) == "未知退出码"


def test_crash_marker_label_mentions_code_and_text():
    label = crash_marker_label(0xC0000005)
    assert label == "异常退出 0xC0000005（访问冲突）"
    assert label.startswith(CRASH_MARKER_PREFIX)


# ========== 探针 ==========

def test_null_probe_is_unavailable_and_closable():
    probe = NullExitCodeProbe()
    assert probe.available is False
    assert probe.exit_code() is None
    probe.close()
    probe.close()


def test_probe_for_invalid_pid_is_unavailable():
    probe = open_exit_probe(0)
    assert probe.available is False
    assert probe.exit_code() is None
    probe.close()


# os._exit 的参数是 C int：0xC0000005 要写成有符号的 -1073741819 才不会溢出
CRASH_CHILD = ("import ctypes,time;"
               "ctypes.windll.kernel32.SetErrorMode(3);"
               "ctypes.windll.kernel32.CreateThread(None,0,None,None,0,None);"
               "time.sleep(30)")


def _reap(child: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=10)


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, stdin=subprocess.PIPE)


@pytest.mark.skipif(sys.platform != 'win32', reason="退出码句柄是 Win32 实现")
def test_win32_probe_reads_exit_code_of_real_process_only_after_exit():
    """进程还在运行时读不到（None，不会把 259 当退出码）；退出后读到 os._exit 给的码"""
    child = _spawn("import os,sys; sys.stdin.read(); os._exit(-1073741819)")
    probe = Win32ExitCodeProbe(child.pid)
    try:
        assert probe.available
        assert probe.exit_code() is None
        child.stdin.close()
        assert poll_until(lambda: probe.exit_code() is not None, timeout=15)
        assert probe.exit_code() == 0xC0000005
        # 句柄在手时，即使 Popen 对象已回收，退出码仍然读得到
        child.wait(timeout=10)
        assert probe.exit_code() == 0xC0000005
    finally:
        probe.close()
        _reap(child)
    assert probe.available is False
    probe.close()    # 重复关闭无副作用
    assert probe.exit_code() is None


@pytest.mark.skipif(sys.platform != 'win32', reason="退出码句柄是 Win32 实现")
@pytest.mark.parametrize("exit_code", [0, 1, 7, 259])
def test_win32_probe_ordinary_exit_codes(exit_code):
    """退出码恰为 259（STILL_ACTIVE）的已结束进程也能读到，不与"仍在运行"混淆"""
    child = _spawn(f"import os; os._exit({exit_code})")
    probe = Win32ExitCodeProbe(child.pid)
    try:
        assert probe.available
        assert poll_until(lambda: probe.exit_code() is not None, timeout=15)
        assert probe.exit_code() == exit_code
    finally:
        probe.close()
        _reap(child)


@pytest.mark.skipif(sys.platform != 'win32', reason="退出码句柄是 Win32 实现")
def test_win32_probe_real_access_violation():
    """真的在新线程里跳到空地址：进程被系统异常终止（0xC0000005），与 os._exit 伪造的不同，
    走的是真实的崩溃路径"""
    child = _spawn(CRASH_CHILD)
    probe = Win32ExitCodeProbe(child.pid)
    try:
        assert poll_until(lambda: probe.exit_code() is not None, timeout=20)
        value = probe.exit_code()
        assert is_abnormal_exit(value)
        assert normalize_exit_code(value) == 0xC0000005
    finally:
        probe.close()
        _reap(child)
