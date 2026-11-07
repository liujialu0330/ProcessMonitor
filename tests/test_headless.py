"""
无窗口模式运行时（core/headless.py 的 HeadlessRuntime）：

空闲退出（注入单调时钟）、有任务或进行中的命令行请求时不算空闲、退出只走一次
ShutdownCoordinator 且退出码为 0、中断信号经事件循环退出、每秒驱动自动打点服务。
（端到端的"无可见顶层窗口"断言在 test_ipc_end_to_end。）
"""
import inspect
import signal

import pytest

from core import headless
from core.headless import HeadlessRuntime
from tests.helpers import process_events_until


class FakeManager:
    """AutoMarkerService 需要的信号与查询；任务数由测试设置。"""

    def __init__(self):
        from PyQt5.QtCore import QObject, pyqtSignal

        class Signals(QObject):
            task_stopped = pyqtSignal(str, object)
            markers_changed = pyqtSignal(str)
            data_updated = pyqtSignal(str, dict)
            task_removed = pyqtSignal(str)
            task_started = pyqtSignal(str)
        self._signals = Signals()
        for name in ('task_stopped', 'markers_changed', 'data_updated', 'task_removed',
                     'task_started'):
            setattr(self, name, getattr(self._signals, name))
        self.tasks = 0

    def get_task_count(self):
        return self.tasks

    def list_task_infos(self):
        return []

    def get_task_info(self, task_id):
        return None


class FakeCoordinator:
    runs = []

    def __init__(self, manager, registry, db=None):
        self.args = (manager, registry, db)

    def run(self, on_progress=None):
        FakeCoordinator.runs.append(self.args)
        return True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def make_runtime(qapp, monkeypatch):
    FakeCoordinator.runs = []
    created = []

    def make(idle_exit_s=0.0, busy=None):
        manager = FakeManager()
        clock = Clock()
        exits = []
        runtime = HeadlessRuntime(manager, 'registry', 'db', idle_exit_s=idle_exit_s,
                                  monotonic=clock, busy=busy, quit_app=exits.append,
                                  coordinator_factory=FakeCoordinator)
        ticks = []
        monkeypatch.setattr(runtime.auto_marker, 'tick', lambda: ticks.append(1))
        created.append(runtime)
        return runtime, manager, clock, exits, ticks

    yield make
    for runtime in created:
        runtime._tick_timer.stop()
        runtime._signal_timer.stop()
        runtime.deleteLater()


def test_idle_exit_after_threshold(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime(idle_exit_s=30)
    runtime._idle_since = clock()
    clock.t += 29
    runtime.tick()
    assert exits == []
    clock.t += 1
    runtime.tick()
    assert exits == [0]
    assert runtime.exit_code == 0
    assert FakeCoordinator.runs == [(manager, 'registry', 'db')]


def test_zero_means_never_idle_exit(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime(idle_exit_s=0)
    runtime._idle_since = clock()
    clock.t += 10 ** 6
    runtime.tick()
    assert exits == []


def test_running_task_resets_idle_timer(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime(idle_exit_s=10)
    runtime._idle_since = clock()
    manager.tasks = 1
    clock.t += 20
    runtime.tick()
    assert exits == []
    manager.tasks = 0
    clock.t += 9
    runtime.tick()
    assert exits == [], "空闲计时从任务结束时重新开始"
    clock.t += 1
    runtime.tick()
    assert exits == [0]


def test_pending_ipc_request_counts_as_busy(make_runtime):
    state = {'busy': True}
    runtime, manager, clock, exits, ticks = make_runtime(idle_exit_s=5,
                                                         busy=lambda: state['busy'])
    runtime._idle_since = clock()
    clock.t += 60
    runtime.tick()
    assert exits == []
    state['busy'] = False
    clock.t += 5
    runtime.tick()
    assert exits == [0]


def test_busy_check_failure_is_not_idle(make_runtime):
    def broken():
        raise RuntimeError("坏")
    runtime, manager, clock, exits, ticks = make_runtime(idle_exit_s=1, busy=broken)
    assert runtime.is_idle() is False


def test_tick_drives_auto_marker(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime()
    runtime.tick()
    runtime.tick()
    assert ticks == [1, 1]


def test_auto_marker_failure_does_not_break_tick(make_runtime, monkeypatch):
    runtime, manager, clock, exits, ticks = make_runtime(idle_exit_s=1)

    def boom():
        raise RuntimeError("坏")
    monkeypatch.setattr(runtime.auto_marker, 'tick', boom)
    runtime._idle_since = clock()
    clock.t += 2
    runtime.tick()
    assert exits == [0]


def test_quit_runs_once(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime()
    runtime.quit()
    runtime.quit()
    assert exits == [0]
    assert len(FakeCoordinator.runs) == 1


def test_quit_exits_zero_even_if_coordinator_fails(make_runtime, monkeypatch):
    runtime, manager, clock, exits, ticks = make_runtime()

    def broken(*args, **kwargs):
        raise RuntimeError("收尾失败")
    runtime._coordinator_factory = broken
    runtime.quit()
    assert exits == [0]


def test_start_installs_signal_handlers_and_timers(make_runtime, monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, 'signal', lambda num, handler: installed.setdefault(num, handler))
    runtime, manager, clock, exits, ticks = make_runtime()
    runtime.start()
    assert runtime._tick_timer.isActive() and runtime._signal_timer.isActive()
    assert signal.SIGINT in installed
    if hasattr(signal, 'SIGBREAK'):
        assert signal.SIGBREAK in installed


def test_signal_quits_through_event_loop(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime()
    runtime._on_signal(signal.SIGINT, None)
    assert exits == [], "信号处理函数里不直接收尾"
    assert process_events_until(lambda: exits, 2.0)
    assert exits == [0] and len(FakeCoordinator.runs) == 1


def test_module_does_not_depend_on_ui():
    source = inspect.getsource(headless)
    assert 'import ui' not in source and 'from ui' not in source


def test_stage_snapshots_are_written_in_headless_mode(qapp):
    """无窗口模式同样写阶段快照：自动打点到期 → 写入器；渲染函数由组合根注入。"""
    from PyQt5.QtCore import QObject, pyqtSignal

    class FakeWriter(QObject):
        written = pyqtSignal(str, object)
        failed = pyqtSignal(str, str)

        def __init__(self):
            super().__init__()
            self.writes = []

        def write(self, task_id, directory, index):
            self.writes.append((task_id, directory, index))
            return True

    writer = FakeWriter()
    runtime = HeadlessRuntime(FakeManager(), 'registry', 'db', snapshot_writer=writer,
                              coordinator_factory=FakeCoordinator)
    try:
        runtime.auto_marker.stage_snapshot_due.emit("t1", r"D:\snap", 2)
        writer.failed.emit("t1", "磁盘已满")      # 只记日志，不抛
        assert writer.writes == [("t1", r"D:\snap", 2)]
    finally:
        runtime.deleteLater()

    def renderer(*_args):
        return None

    built = HeadlessRuntime(FakeManager(), 'registry', 'db', stage_renderer=renderer,
                            coordinator_factory=FakeCoordinator)
    try:
        assert built.snapshot_writer._renderer is renderer
    finally:
        built.deleteLater()



# ---------- 评审修复：会话结束收尾、无窗口模式不弹异常框 ----------

class _FakeSessionApp:
    """只有 commitDataRequest 信号的应用替身（真 QApplication 的信号不能随手 emit）。"""

    def __init__(self):
        from PyQt5.QtCore import QObject, pyqtSignal

        class Signals(QObject):
            commitDataRequest = pyqtSignal(object)
        self._signals = Signals()
        self.commitDataRequest = self._signals.commitDataRequest


def test_session_end_runs_shutdown(make_runtime):
    runtime, manager, clock, exits, ticks = make_runtime()
    app = _FakeSessionApp()
    assert runtime.install_session_end_handler(app) is True
    app.commitDataRequest.emit(object())
    # 直接连接：信号返回前已走完收尾（会话管理器不会等下一轮事件循环）
    assert exits == [0] and len(FakeCoordinator.runs) == 1


def test_start_hooks_session_end(make_runtime, monkeypatch):
    monkeypatch.setattr(signal, 'signal', lambda *a: None)
    runtime, manager, clock, exits, ticks = make_runtime()
    hooked = []
    monkeypatch.setattr(runtime, 'install_session_end_handler',
                        lambda app=None: hooked.append(app) or True)
    runtime.start()
    assert hooked == [None]                     # 缺省接到当前 QApplication 上


def test_session_end_hooks_real_application(make_runtime, qapp):
    runtime, *_ = make_runtime()
    assert runtime.install_session_end_handler() is True


def test_session_end_handler_without_app_is_skipped(make_runtime):
    runtime, *_ = make_runtime()
    assert runtime.install_session_end_handler(object()) is False


def test_crash_handler_headless_suppresses_message_box(qapp, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    from utils import crash_handler
    shown = []
    monkeypatch.setattr(QMessageBox, 'critical', staticmethod(lambda *a, **k: shown.append(a)))
    monkeypatch.setattr(crash_handler, '_headless', False)
    monkeypatch.setattr(crash_handler, '_log_dir', 'C:\\logs')
    crash_handler._maybe_show_message_box()
    assert len(shown) == 1                      # 有窗口模式照常弹
    crash_handler.set_headless(True)
    assert crash_handler.is_headless() is True
    crash_handler._maybe_show_message_box()
    assert len(shown) == 1                      # 无窗口模式没人能点"确定"，不弹
