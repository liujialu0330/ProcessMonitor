"""
core/shutdown.py 的 ShutdownCoordinator：固定收尾顺序、总预算超时、进度文案、
db.close、任何一步出错都不抛异常。用假 manager / registry / db，不起真实线程。
"""
import logging

import pytest

import core.shutdown as shutdown_module
from core.shutdown import (
    PROGRESS_SAVING, PROGRESS_STOPPING_TASKS, ShutdownCoordinator,
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

    def join_all(self, timeout_ms):
        self.log.append(("join_all", timeout_ms))
        if self.join_error:
            raise self.join_error
        return self.join_ok


class _FakeRegistry:
    def __init__(self, log, ok=True, error=None):
        self.log, self.ok, self.error = log, ok, error

    def shutdown_all(self, on_progress=None):
        self.log.append("shutdown_all")
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
    assert log == ["stop_all_tasks", ("join_all", 3000), "shutdown_all", "db.close"]


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


def test_join_timeout_capped_by_remaining_budget(clock):
    log = []
    manager = _FakeManager(log, clock=clock, stop_ms=29000)
    ShutdownCoordinator(manager, _FakeRegistry(log), task_timeout_ms=3000,
                        total_budget_ms=30000).run()
    assert ("join_all", 1000) in log


def test_budget_exhausted_skips_waits_logs_and_returns_false(clock, caplog):
    log = []
    manager = _FakeManager(log, clock=clock, stop_ms=31000)
    coordinator = ShutdownCoordinator(manager, _FakeRegistry(log),
                                      total_budget_ms=30000, db=_FakeDb(log))
    with caplog.at_level(logging.ERROR, logger="core.shutdown"):
        assert coordinator.run() is False
    # 等待步骤被跳过，但仍尝试关闭数据库
    assert log == ["stop_all_tasks", "db.close"]
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

    assert log == ["stop_all_tasks", ("join_all", 3000), "shutdown_all", "db.close"]
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
