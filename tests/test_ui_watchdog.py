"""
界面卡死看门狗（utils.crash_handler.install_ui_watchdog）：界面线程阻塞超过阈值后转储文件里
有全部线程栈（含当前测试函数名），进程不退出；事件循环正常运转时续期不触发；stop() 后不再
触发；没有 crash.log 时不安装。

faulthandler.dump_traceback_later 真实调用，但文件指向临时目录；pytest 只用
faulthandler.enable（未配置 faulthandler_timeout），与定时转储互不干扰。每个用例结束
都 stop() 并取消定时转储，避免影响后续用例。
"""
import time

import pytest
from PyQt5.QtCore import QEventLoop, QTimer

import utils.crash_handler as crash_handler
from utils.crash_handler import install_ui_watchdog


@pytest.fixture
def crash_file(qapp, tmp_path):
    path = tmp_path / "crash.log"
    f = open(path, 'a', encoding='utf-8')
    installed = []

    def _install(timeout_s=1.0, beat_ms=None):
        wd = install_ui_watchdog(qapp, timeout_s=timeout_s, file=f, beat_ms=beat_ms)
        installed.append(wd)
        return wd

    yield path, _install
    for wd in installed:
        if wd is not None:
            wd.stop()
    f.close()


def _spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def test_blocked_ui_thread_dumps_all_thread_stacks(crash_file):
    path, install = crash_file
    wd = install(timeout_s=0.3)
    assert wd is not None and wd.running
    time.sleep(0.6)            # 阻塞界面线程：QTimer 无法续期（超过阈值即转储，不会因机器忙而不触发）
    wd.stop()

    text = path.read_text(encoding='utf-8', errors='replace')
    assert "Timeout" in text
    assert "Thread" in text
    assert "test_blocked_ui_thread_dumps_all_thread_stacks" in text


def test_normal_event_loop_keeps_renewing_without_dump(crash_file):
    """续期由界面线程的 QTimer 驱动；只要按时续期，跨过多个阈值也不会转储。

    续期节奏由测试直接驱动（等价于 QTimer 按时触发），不依赖事件循环真的调度到：全套测试
    后段残留控件很多，进程的 Win32 定时器配额可能用尽（registerTimer 失败、QTimer 永不
    触发），原先"空转事件循环数续期次数"的写法会因此偶发失败。QTimer 的接线单独断言。"""
    path, install = crash_file
    wd = install(timeout_s=1.0, beat_ms=100)
    # 接线：定时器按 beat_ms 运行，timeout 即续期
    assert wd._timer.isActive() and wd._timer.interval() == 100
    before = wd.beats
    wd._timer.timeout.emit()
    assert wd.beats == before + 1

    # 每 0.1 秒续期一次、持续 1.5 秒（跨过阈值 1.5 倍；单次间隔离阈值有 10 倍余量）：真实的定时转储始终没有到期
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        time.sleep(0.1)
        wd.beat()
    wd.stop()

    assert wd.beats >= 10
    assert wd.seconds_since_beat() is not None
    assert "Thread" not in path.read_text(encoding='utf-8', errors='replace')


def test_beat_interval_follows_threshold(qapp, tmp_path):
    """续期间隔：默认不超过 1 秒，阈值短时取阈值的三分之一；指定值也受这两条限制。"""
    with open(tmp_path / "crash.log", 'a', encoding='utf-8') as f:
        assert crash_handler.UiWatchdog(10.0, f).beat_ms == 1000
        assert crash_handler.UiWatchdog(1.0, f).beat_ms == 333
        assert crash_handler.UiWatchdog(3.0, f, beat_ms=100).beat_ms == 100
        assert crash_handler.UiWatchdog(0.3, f, beat_ms=500).beat_ms == 100
        assert crash_handler.UiWatchdog(3.0, f, beat_ms=1).beat_ms == 50


def test_stop_cancels_pending_dump(crash_file):
    path, install = crash_file
    wd = install(timeout_s=0.3)
    wd.stop()
    wd.stop()                  # 可重复调用
    time.sleep(0.5)
    assert not wd.running
    assert path.read_text(encoding='utf-8', errors='replace') == ""
    assert crash_handler.active_ui_watchdog() is None


def test_reinstall_replaces_previous_watchdog(crash_file):
    _path, install = crash_file
    first = install(timeout_s=5.0)
    second = install(timeout_s=5.0)
    assert not first.running and second.running
    assert crash_handler.active_ui_watchdog() is second


def test_default_beat_interval_is_one_second(crash_file):
    _path, install = crash_file
    assert install(timeout_s=10.0).beat_ms == 1000


def test_not_installed_without_crash_log(qapp, monkeypatch):
    monkeypatch.setattr(crash_handler, '_crash_log_file', None)
    assert install_ui_watchdog(qapp, timeout_s=1.0) is None
