"""
退出路径与管理器层：等待重启的任务随程序退出而及时结束；会话锁在所有正常退出路径删除。

- 管理器：等待重启 / 续接 / 磁盘满 的信号与 TaskInfo 字段（卡片据此显示）；
- ShutdownCoordinator 在 30 秒期限内收掉等待重启的任务（真实时钟，1 秒轮询不阻塞停止）；
- 会话锁：窗口关闭、托盘退出、会话结束、命令行 quit、无窗口模式 quit 都在收尾后删除，隐藏到托盘不删。
"""
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QApplication

from core.contracts import StopReason, TaskStatus
from core.headless import HeadlessRuntime
from core.monitor_manager import MonitorManager
from core.restart_finder import RestartHit
from core.session_lock import LOCK_FILE_NAME, SessionLock
from core.shutdown import ShutdownCoordinator, ThreadRegistry
from data.database import Database
from tests.factories import make_main_window
from tests.helpers import poll_until, process_events_until, stop_and_join
from tests.soak_doubles import ACCESS_VIOLATION, FakeProbe, SegmentCollector

METRIC = 'memory_private'


class LiveFinder:
    """真实时钟下的查找替身：flag 置位前找不到，之后返回新实例。"""

    def __init__(self, pid=222):
        self.flag = threading.Event()
        self.pid = pid
        self.calls = 0

    def __call__(self, target):
        self.calls += 1
        return RestartHit(self.pid, None, target.name) if self.flag.is_set() else None


@pytest.fixture
def manager(tmp_path, qapp):
    db = Database(str(tmp_path / "exit_paths.db"))
    mgr = MonitorManager(db=db)
    yield mgr
    stop_and_join(mgr)
    db.close()


def _arm(manager, finder, *, first=2, second=2000, code=ACCESS_VIOLATION):
    """第一个进程采 first 次后退出；续接后的第二个进程采 second 次（足够久）。"""
    collectors = {os.getpid(): lambda: SegmentCollector([METRIC], [1.0] * first),
                  222: lambda: SegmentCollector([METRIC], [2.0] * second)}

    def collector_factory(pid, _spec):
        return collectors[pid]()

    manager.set_worker_options(
        collector_factory=collector_factory, restart_finder=finder,
        exit_probe_factory=lambda pid: FakeProbe(code))


def _create(manager, **options):
    return manager.create_task(os.getpid(), 'app.exe', [METRIC], interval=0.05,
                               resume_on_restart=True, restart_limit=3, **options)


# ========== 管理器：状态与信号 ==========

def test_manager_exposes_waiting_restart_and_restart_progress(manager):
    finder = LiveFinder()
    _arm(manager, finder)
    waiting, restarted = [], []
    manager.task_waiting_restart.connect(lambda t, on: waiting.append(on))
    manager.task_restarted.connect(lambda t, n: restarted.append(n))
    tid = _create(manager)
    assert manager.start_task(tid)

    assert poll_until(lambda: manager.get_task_info(tid).waiting_restart, timeout=10)
    info = manager.get_task_info(tid)
    # 卡片据这几项显示"等待重启（第 n/m 次）"与"异常退出：访问冲突 0xC0000005"
    assert info.status is TaskStatus.RUNNING
    assert (info.restart_count, info.restart_limit) == (0, 3)
    assert info.exit_code == ACCESS_VIOLATION
    assert manager.is_task_active(tid)
    assert process_events_until(lambda: waiting == [True], timeout=5)

    finder.flag.set()
    assert poll_until(lambda: not manager.get_task_info(tid).waiting_restart, timeout=10)
    assert process_events_until(lambda: waiting == [True, False] and restarted == [1], timeout=5)
    info = manager.get_task_info(tid)
    assert (info.restart_count, info.pid) == (1, 222)
    assert info.status is TaskStatus.RUNNING


def test_manager_stop_while_waiting_ends_with_crashed_reason(manager):
    finder = LiveFinder()
    _arm(manager, finder)
    stopped = []
    manager.task_stopped.connect(lambda t, reason: stopped.append(reason))
    tid = _create(manager)
    manager.start_task(tid)
    assert poll_until(lambda: manager.get_task_info(tid).waiting_restart, timeout=10)

    begin = time.monotonic()
    manager.stop_task(tid)
    assert manager.join_all(timeout_ms=5000)
    assert time.monotonic() - begin < 3      # 不必等满下一个 1 秒轮询
    assert process_events_until(lambda: bool(stopped), timeout=5)
    assert stopped == [StopReason.CRASHED]


def test_pause_is_rejected_for_waiting_task(manager):
    finder = LiveFinder()
    _arm(manager, finder)
    tid = _create(manager)
    manager.start_task(tid)
    assert poll_until(lambda: manager.get_task_info(tid).waiting_restart, timeout=10)
    assert manager.pause_task(tid) is False
    assert manager.get_task_info(tid).waiting_restart


def test_set_restart_policy_overrides_for_running_task(manager):
    finder = LiveFinder()
    _arm(manager, finder, first=200)
    tid = manager.create_task(os.getpid(), 'app.exe', [METRIC], interval=0.05,
                              resume_on_restart=False, restart_limit=1)
    assert manager.set_restart_policy(tid, True, 5) is True
    assert manager.get_task(tid).restart_policy() == (True, 5)
    assert manager.set_restart_policy('nope', True, 5) is False


# ========== 程序退出时等待重启的任务及时结束 ==========

def test_shutdown_coordinator_finishes_waiting_restart_task_promptly(manager):
    finder = LiveFinder()
    _arm(manager, finder)
    tid = _create(manager)
    manager.start_task(tid)
    assert poll_until(lambda: manager.get_task_info(tid).waiting_restart, timeout=10)

    registry = ThreadRegistry()
    coordinator = ShutdownCoordinator(manager, registry, db=None, total_budget_ms=30000)
    begin = time.monotonic()
    ok = coordinator.run()
    elapsed = time.monotonic() - begin

    assert ok is True
    assert elapsed < 5            # 远小于 30 秒期限：停止唤醒了 1 秒轮询
    record = manager.db.tasks.get(tid)
    assert record.status == 'stopped'
    assert record.stop_reason == 'crashed'
    assert record.exit_code == ACCESS_VIOLATION


# ========== 会话锁：正常退出路径都删除 ==========

@pytest.fixture
def lock_window(tmp_path, qapp, monkeypatch):
    db = Database(str(tmp_path / "lock_window.db"))
    win = make_main_window(db, MonitorManager(db=db))
    win.about_group.check_update = lambda *args, **kwargs: None
    lock = SessionLock(str(tmp_path))
    assert lock.acquire()
    win.attach_session_lock(lock)
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    win.show()
    path = tmp_path / LOCK_FILE_NAME
    assert path.exists()
    yield win, path
    stop_and_join(win.monitor_manager)


def test_lock_removed_on_window_close(lock_window):
    win, path = lock_window
    win.close()
    assert not path.exists()


def test_lock_removed_on_tray_exit(lock_window):
    win, path = lock_window
    win._quit_from_tray()
    assert not path.exists()


def test_lock_removed_on_command_line_quit(lock_window):
    win, path = lock_window
    win.request_quit()
    assert not path.exists()


def test_lock_removed_on_session_end(lock_window):
    win, path = lock_window
    win._on_commit_data_request(MagicMock())
    win.closeEvent(MagicMock())
    assert not path.exists()


def test_lock_kept_when_hiding_to_tray_and_when_close_cancelled(lock_window):
    """隐藏到托盘、取消关闭都不是退出：程序还在跑，锁必须保留"""
    win, path = lock_window
    win.services.preferences.tray = True
    win._tray_available = lambda: True
    win._hide_to_tray = lambda event: event.ignore()
    win.closeEvent(MagicMock())
    assert path.exists()


def test_lock_removed_even_if_shutdown_raises(lock_window, monkeypatch):
    win, path = lock_window

    def boom(self, on_progress=None):
        raise RuntimeError("收尾炸了")

    monkeypatch.setattr(ShutdownCoordinator, 'run', boom)
    with pytest.raises(RuntimeError):
        win._run_shutdown()
    assert not path.exists()


def test_headless_quit_runs_exit_hooks_after_cleanup(manager, monkeypatch):
    """无窗口模式 quit：收尾（停任务）之后才执行退出钩子（删锁），且钩子出错不妨碍退出"""
    events = []
    quit_calls = []
    original_stop = manager.stop_all_tasks

    def stop_all():
        events.append('stop_tasks')
        original_stop()

    manager.stop_all_tasks = stop_all
    runtime = HeadlessRuntime(
        manager, ThreadRegistry(), manager.db, idle_exit_s=0,
        quit_app=lambda code: quit_calls.append(code),
        exit_hooks=[lambda: events.append('hook_ok'),
                    lambda: (_ for _ in ()).throw(OSError("删锁失败")),
                    lambda: events.append('hook_after_error')])
    runtime.quit()
    assert events[0] == 'stop_tasks'
    assert events[1:] == ['hook_ok', 'hook_after_error']
    assert quit_calls == [0]
