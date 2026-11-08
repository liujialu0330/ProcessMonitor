"""
用例超时看门狗（tests/watchdog.py）自测：环境变量解析与限时倍数（快速单元），以及在临时项目里放
一个真会卡住的用例，验证超时后打出所有线程的栈并结束进程（子进程，标 slow）。
"""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from tests import watchdog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_default_and_invalid_timeout_fall_back_to_60_seconds():
    assert watchdog.configured_timeout({}) == 60.0
    assert watchdog.configured_timeout({'PM_TEST_TIMEOUT_S': ' 12.5 '}) == 12.5
    assert watchdog.configured_timeout({'PM_TEST_TIMEOUT_S': '不是数字'}) == 60.0


def test_zero_or_negative_timeout_disables_the_watchdog(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.faulthandler, 'dump_traceback_later',
                        lambda *a, **k: calls.append((a, k)))
    assert watchdog.arm([], {'PM_TEST_TIMEOUT_S': '0'}) is None
    assert watchdog.arm([], {'PM_TEST_TIMEOUT_S': '-1'}) is None
    assert calls == []


def test_long_running_markers_get_a_relaxed_limit():
    env = {'PM_TEST_TIMEOUT_S': '10'}
    assert watchdog.timeout_for([], env) == 10
    assert watchdog.timeout_for(['slow'], env) == 30
    assert watchdog.timeout_for(['perf'], env) == 100
    assert watchdog.timeout_for(['soak', 'slow'], env) == 100      # 取最宽的一个
    assert watchdog.timeout_for(['perf'], {'PM_TEST_TIMEOUT_S': '0'}) == 0


def test_arm_sets_a_faulthandler_timer_that_exits_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.faulthandler, 'dump_traceback_later',
                        lambda seconds, **kwargs: calls.append((seconds, kwargs)))
    assert watchdog.arm(['slow'], {'PM_TEST_TIMEOUT_S': '20'}) == 60
    assert watchdog.arm([], {'PM_TEST_TIMEOUT_S': '20', 'PM_TEST_TIMEOUT_EXIT': '0'}) == 20
    (first_seconds, first), (second_seconds, second) = calls
    assert (first_seconds, first['exit'], first['repeat']) == (60, True, False)
    assert (second_seconds, second['exit']) == (20, False)


def test_disarm_cancels_the_pending_dump(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.faulthandler, 'cancel_dump_traceback_later',
                        lambda: calls.append('cancel'))
    watchdog.disarm()
    assert calls == ['cancel']


# ---------- 真卡住的用例：子进程里跑临时项目 ----------

_CONFTEST = '''
import sys
sys.path.insert(0, {root!r})
from tests.watchdog import pytest_configure, _test_watchdog   # noqa: F401
'''

_HANGING_TEST = '''
import threading
import time


def _stuck_helper_loop(stop):
    stop.wait(60)


def test_hangs_forever():
    stop = threading.Event()
    helper = threading.Thread(target=_stuck_helper_loop, args=(stop,), daemon=True)
    helper.start()
    time.sleep({sleep_s})        # 卡在这里：主线程不返回
    stop.set()
'''


def _run_temp_project(tmp_path, *, timeout_s, sleep_s, extra_env=None):
    (tmp_path / 'conftest.py').write_text(_CONFTEST.format(root=ROOT), encoding='utf-8')
    (tmp_path / 'test_hang.py').write_text(
        textwrap.dedent(_HANGING_TEST).format(sleep_s=sleep_s), encoding='utf-8')
    env = dict(os.environ, PM_TEST_TIMEOUT_S=str(timeout_s), PYTHONIOENCODING='utf-8')
    env.update(extra_env or {})
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
         '--rootdir', str(tmp_path), str(tmp_path / 'test_hang.py')],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=60)
    return result, time.monotonic() - started


@pytest.mark.slow   # 起子进程跑 pytest，约 3 秒；快速覆盖：上面的单元用例（参数解析、倍数、arm / disarm 调用）
def test_hanging_case_dumps_all_thread_stacks_and_exits(tmp_path):
    result, elapsed = _run_temp_project(tmp_path, timeout_s=1, sleep_s=30)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert elapsed < 25, "超时后应结束进程，而不是等卡住的用例自己返回（它要睡 30 秒）"
    assert 'Timeout' in output
    assert 'test_hangs_forever' in output          # 主线程栈里有卡住的用例
    assert '_stuck_helper_loop' in output          # 其他线程的栈也在


@pytest.mark.slow   # 同上
def test_dump_only_mode_lets_the_case_finish(tmp_path):
    result, _elapsed = _run_temp_project(
        tmp_path, timeout_s=1, sleep_s=2.5, extra_env={'PM_TEST_TIMEOUT_EXIT': '0'})
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert 'Timeout' in output and '_stuck_helper_loop' in output
    assert '1 passed' in output
