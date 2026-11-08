"""
core/shutdown.py 的 ShutdownCoordinator：固定收尾顺序、总预算按截止时间落实到每次
等待、进度文案、db.close、任何一步出错都不抛异常。多数用例用假 manager / registry /
db、不起真实线程；总耗时用例用真实 MonitorManager.join_all 与真实 ThreadRegistry。
"""
import logging
import threading
import time

import pytest
from PyQt5.QtCore import QThread

import core.shutdown as shutdown_module
from core.monitor_manager import MonitorManager
from core.shutdown import (
    PROGRESS_SAVING, PROGRESS_STOPPING_TASKS, PROGRESS_STOPPING_THREADS,
    ShutdownCoordinator, ThreadRegistry,
)


class _FakeClock:
    """替换 time.monotonic：各假对象调用 advance 模拟耗时"""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, ms):
        self.now += ms / 1000.0


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(shutdown_module.time, 'monotonic', fake.monotonic)
    return fake


class _FakeManager:
    def __init__(self, log, clock=None, stop_ms=0, join_ok=True,
                 stop_error=None, join_error=None):
        self.log, self.clock = log, clock
        self.stop_ms, self.join_ok = stop_ms, join_ok
        self.stop_error, self.join_error = stop_error, join_error

    def stop_all_tasks(self):
        self.log.append("stop_all_tasks")
        if self.clock is not None:
            self.clock.advance(self.stop_ms)
        if self.stop_error:
            raise self.stop_error

    def join_all(self, timeout_ms, *, deadline=None):
        self.log.append(("join_all", timeout_ms))
        self.join_deadline = deadline
        if self.join_error:
            raise self.join_error
        return self.join_ok


class _FakeRegistry:
    def __init__(self, log, ok=True, error=None):
        self.log, self.ok, self.error = log, ok, error

    def cancel_all(self):
        self.log.append("cancel_all")

    def shutdown_all(self, on_progress=None, *, deadline=None):
        self.log.append("shutdown_all")
        self.deadline = deadline
        if on_progress is not None:
            on_progress("正在结束后台操作…")
        if self.error:
            raise self.error
        return self.ok


class _FakeDb:
    def __init__(self, log, error=None):
        self.log, self.error = log, error

    def close(self):
        self.log.append("db.close")
        if self.error:
            raise self.error


def test_runs_steps_in_fixed_order(clock):
    log = []
    coordinator = ShutdownCoordinator(
        _FakeManager(log), _FakeRegistry(log), task_timeout_ms=3000, db=_FakeDb(log))
    assert coordinator.run() is True
    assert log == ["stop_all_tasks", "cancel_all", ("join_all", 3000), "shutdown_all",
                   "db.close"]


def test_progress_texts_in_order(clock):
    log, progress = [], []
    ShutdownCoordinator(_FakeManager(log), _FakeRegistry(log), db=_FakeDb(log)).run(
        progress.append)
    assert progress == [PROGRESS_STOPPING_TASKS, "正在结束后台操作…", PROGRESS_SAVING]
    assert PROGRESS_STOPPING_TASKS == "正在停止监控任务…"
    assert PROGRESS_SAVING == "正在保存…"


def test_without_db_skips_close_and_saving_text(clock):
    log, progress = [], []
    assert ShutdownCoordinator(_FakeManager(log), _FakeRegistry(log)).run(
        progress.append) is True
    assert "db.close" not in log
    assert PROGRESS_SAVING not in progress


def test_same_deadline_passed_to_every_wait(clock):
    """预算在开始时换算成截止时间，任务 join 与登记表等待拿到同一个截止时间
    （由它们把每一次等待截到剩余时间以内）"""
    log = []
    manager, registry = _FakeManager(log), _FakeRegistry(log)
    ShutdownCoordinator(manager, registry, task_timeout_ms=3000,
                        total_budget_ms=30000).run()
    assert manager.join_deadline == pytest.approx(1000.0 + 30.0)
    assert registry.deadline == manager.join_deadline


def test_budget_exhausted_still_runs_steps_logs_and_returns_false(clock, caplog):
    """预算在第一步就耗尽：后续步骤照常调用（拿到已过期的截止时间、不再等待），
    进度顺序不变，仍关闭数据库；记日志并返回 False"""
    log, progress = [], []
    manager = _FakeManager(log, clock=clock, stop_ms=31000)
    coordinator = ShutdownCoordinator(manager, _FakeRegistry(log),
                                      total_budget_ms=30000, db=_FakeDb(log))
    with caplog.at_level(logging.ERROR, logger="core.shutdown"):
        assert coordinator.run(progress.append) is False
    assert log == ["stop_all_tasks", "cancel_all", ("join_all", 3000), "shutdown_all",
                   "db.close"]
    assert manager.join_deadline < clock.now
    assert progress == [PROGRESS_STOPPING_TASKS, "正在结束后台操作…", PROGRESS_SAVING]
    assert any("预算" in r.getMessage() for r in caplog.records)


def test_join_timeout_returns_false_but_continues(clock):
    log = []
    coordinator = ShutdownCoordinator(_FakeManager(log, join_ok=False),
                                      _FakeRegistry(log), db=_FakeDb(log))
    assert coordinator.run() is False
    assert log[-2:] == ["shutdown_all", "db.close"]


def test_registry_timeout_returns_false(clock):
    log = []
    assert ShutdownCoordinator(_FakeManager(log), _FakeRegistry(log, ok=False)).run() is False


@pytest.mark.parametrize("broken", ["stop", "join", "registry", "db"])
def test_step_errors_never_raise_and_later_steps_still_run(clock, caplog, broken):
    log = []
    err = RuntimeError("boom")
    manager = _FakeManager(log, stop_error=err if broken == "stop" else None,
                           join_error=err if broken == "join" else None)
    registry = _FakeRegistry(log, error=err if broken == "registry" else None)
    db = _FakeDb(log, error=err if broken == "db" else None)

    with caplog.at_level(logging.ERROR, logger="core.shutdown"):
        assert ShutdownCoordinator(manager, registry, db=db).run() is False

    assert log == ["stop_all_tasks", "cancel_all", ("join_all", 3000), "shutdown_all",
                   "db.close"]
    assert any(r.exc_info for r in caplog.records)


def test_progress_callback_error_never_raises(clock):
    log = []

    def bad(text):
        raise ValueError("界面已销毁")

    assert ShutdownCoordinator(_FakeManager(log), _FakeRegistry(log),
                               db=_FakeDb(log)).run(bad) is True
    assert log[-1] == "db.close"


def test_with_real_registry_and_no_threads(qapp, clock):
    """真实 ThreadRegistry（无登记线程）也能直接用"""
    from core.shutdown import ThreadRegistry
    log = []
    assert ShutdownCoordinator(_FakeManager(log), ThreadRegistry()).run() is True


def test_cancel_all_error_never_raises(clock, caplog):
    log = []

    class _BadCancelRegistry(_FakeRegistry):
        def cancel_all(self):
            super().cancel_all()
            raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="core.shutdown"):
        assert ShutdownCoordinator(_FakeManager(log), _BadCancelRegistry(log),
                                   db=_FakeDb(log)).run() is False
    assert log == ["stop_all_tasks", "cancel_all", ("join_all", 3000), "shutdown_all",
                   "db.close"]


# ========== 总期限落实到每一次等待（真实时钟） ==========

class _StuckTask:
    """永不结束的假任务线程：wait 睡满给定超时后返回 False"""

    def __init__(self, log, name):
        self.log, self.name = log, name

    def isRunning(self):
        return True

    def stop(self):
        pass

    def wait(self, timeout_ms=None):
        self.log.append(("wait_task", self.name))
        time.sleep(timeout_ms / 1000.0)
        return False


class _StuckThread(QThread):
    """永不结束的登记表线程：取消回调只记录、不放行（测试结束时才放行）"""

    def __init__(self, log, name):
        super().__init__()
        self.log, self.name = log, name
        self.gate = threading.Event()
        self.started_evt = threading.Event()

    def run(self):
        self.started_evt.set()
        self.gate.wait(30)

    def cancel(self):
        self.log.append(("cancel", self.name))


def test_total_budget_bounds_every_wait(qapp, db):
    """5 个永不结束的任务 + 3 个永不结束的登记表线程：单项超时（3s/5s）累加远超
    预算，但总耗时不超过 0.6 秒预算加少量余量；登记表线程在等待任务之前就已统一取消；
    进度文案顺序不变，仍关闭数据库"""
    log, progress = [], []
    manager = MonitorManager(db=db)
    manager._tasks = {f"t{i}": _StuckTask(log, f"t{i}") for i in range(5)}
    registry = ThreadRegistry()
    threads = [_StuckThread(log, f"线程{i}") for i in range(3)]
    try:
        for thread in threads:
            registry.register(thread, name=thread.name, cancel=thread.cancel,
                              timeout_ms=5000)
            thread.start()
            assert thread.started_evt.wait(5)

        coordinator = ShutdownCoordinator(manager, registry, task_timeout_ms=3000,
                                          total_budget_ms=600, db=_FakeDb(log))
        t0 = time.monotonic()
        assert coordinator.run(progress.append) is False
        elapsed = time.monotonic() - t0

        assert elapsed <= 0.6 + 0.5
        assert progress == [PROGRESS_STOPPING_TASKS, PROGRESS_STOPPING_THREADS,
                            PROGRESS_SAVING]
        cancels = [entry for entry in log if entry[0] == "cancel"]
        assert cancels == [("cancel", "线程0"), ("cancel", "线程1"), ("cancel", "线程2")]
        assert log[:3] == cancels           # 取消先于任何等待发出，且只发一次
        assert log[-1] == "db.close"
    finally:
        manager._tasks = {}
        for thread in threads:
            thread.gate.set()
            thread.wait(5000)
