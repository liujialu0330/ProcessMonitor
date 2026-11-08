"""
用例超时看门狗（不依赖 pytest-timeout 等第三方插件）。

每个用例开始时用 faulthandler.dump_traceback_later 设一个定时器，结束时取消：用例卡住超过
限定秒数，faulthandler 的 C 线程（不需要 GIL，主线程死锁在 C 调用里也能触发）把所有线程的
栈写到 pytest 捕获之外的标准错误副本里，然后默认直接结束进程（退出码 1）——CI 不会被一个
卡死的用例拖到作业超时、又看不到卡在哪。

环境变量：
- PM_TEST_TIMEOUT_S：默认每用例秒数，缺省 60；0 或负数关闭看门狗。
- PM_TEST_TIMEOUT_EXIT：缺省 1，超时转储栈后结束进程；设为 0 则只转储、用例继续跑
  （本地想看看它最后会不会自己结束时用）。
- 带 perf / soak / gui_real 标记的用例，限时放宽为默认值的 10 倍；slow 为 3 倍（它们本来就长）。
"""
import faulthandler
import os
import sys
from typing import Optional

import pytest

DEFAULT_TIMEOUT_S = 60.0
TIMEOUT_ENV = 'PM_TEST_TIMEOUT_S'
EXIT_ENV = 'PM_TEST_TIMEOUT_EXIT'
# 这些标记的用例本来就长，限时按倍数放宽
RELAXED_FACTORS = {'perf': 10, 'soak': 10, 'gui_real': 10, 'slow': 3}

_dump_file = None   # 超时转储写入的文件对象：pytest 捕获之外的标准错误副本


def configured_timeout(environ=None) -> float:
    """读取 PM_TEST_TIMEOUT_S；缺省或无法解析时用默认值，<= 0 表示关闭"""
    environ = os.environ if environ is None else environ
    raw = environ.get(TIMEOUT_ENV, '').strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def exit_on_timeout(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return environ.get(EXIT_ENV, '1').strip() not in ('0', 'false', 'no')


def timeout_for(markers, environ=None) -> float:
    """某个用例的限时（秒）；<= 0 表示不设"""
    base = configured_timeout(environ)
    if base <= 0:
        return 0.0
    factor = max((RELAXED_FACTORS.get(name, 1) for name in markers), default=1)
    return base * factor


def _target_file():
    """超时转储的目标：标准错误的副本。pytest 默认把 fd 2 重定向到临时文件，直接用
    sys.stderr 会让转储随捕获一起被吞掉；这里在会话开始时（捕获尚未接管前）复制 fd 2。"""
    global _dump_file
    if _dump_file is None:
        try:
            _dump_file = os.fdopen(os.dup(2), 'w', encoding='utf-8', errors='replace')
        except OSError:
            _dump_file = sys.__stderr__
    return _dump_file


def prepare() -> None:
    """会话开始时调用一次，抢在输出捕获之前复制标准错误"""
    _target_file()


def arm(markers, environ=None) -> Optional[float]:
    """为一个用例设定定时器；返回实际限时（秒），未设置返回 None。转储里主线程的栈会带着
    卡住的用例函数与文件名，不需要另外打印用例 id（超时时 Python 层已无法再执行代码）。"""
    seconds = timeout_for(markers, environ)
    if seconds <= 0:
        return None
    target = _target_file()
    faulthandler.dump_traceback_later(
        seconds, repeat=False, file=target, exit=exit_on_timeout(environ))
    return seconds


def disarm() -> None:
    faulthandler.cancel_dump_traceback_later()


# ---------- pytest 接线：根 conftest 直接引入这两个对象，自测的临时项目也用同一份 ----------

def pytest_configure(config):
    """会话开始时抢在输出捕获接管之前复制标准错误，超时转储才不会被捕获吞掉"""
    prepare()


@pytest.fixture(autouse=True)
def _test_watchdog(request):
    """每个用例开始设定时器、结束取消；卡住时转储全部线程栈，默认随后结束进程"""
    arm([m.name for m in request.node.iter_markers()])
    try:
        yield
    finally:
        disarm()
