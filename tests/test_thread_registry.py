"""
core/shutdown.py 的 ThreadRegistry：登记、finished 时自动注销并 deleteLater、重复登记幂等、
shutdown_all 的取消/等待顺序与超时。
"""
import logging
import threading

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QThread
from PyQt5.QtWidgets import QApplication

from core.shutdown import PROGRESS_STOPPING_THREADS, RegisteredThread, ThreadRegistry
from tests.helpers import poll_until


class _GateThread(QThread):
    """run() 阻塞到 gate 放行；cancel() 即放行（模拟可取消线程）"""

    def __init__(self, log=None, name=""):
        super().__init__()
        self.gate = threading.Event()
        self.started_evt = threading.Event()
        self.log = log if log is not None else []
        self.name = name

    def run(self):
        self.started_evt.set()
        self.gate.wait(10)

    def cancel(self):
        self.log.append(("cancel", self.name))
        self.gate.set()


def _flush_deletes():
    QApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_register_records_entry_in_order(qapp):
    registry = ThreadRegistry()
    a, b = _GateThread(), _GateThread()
    registry.register(a, name="甲", cancel=a.cancel, timeout_ms=100)
    registry.register(b, name="乙")
    assert registry.entries() == [
        RegisteredThread("甲", a, a.cancel, 100),
        RegisteredThread("乙", b, None, 2000),
    ]


def test_duplicate_registration_is_idempotent(qapp):
    registry = ThreadRegistry()
    t = _GateThread()
    registry.register(t, name="一次")
    registry.register(t, name="两次", timeout_ms=1)
    assert [(e.name, e.timeout_ms) for e in registry.entries()] == [("一次", 2000)]


def test_entries_returns_copy(qapp):
    registry = ThreadRegistry()
    registry.register(_GateThread(), name="x")
    registry.entries().clear()
    assert len(registry.entries()) == 1


def test_finished_unregisters_and_deletes_later(qapp):
    registry = ThreadRegistry()
    t = _GateThread()
    registry.register(t, name="短命")
    t.start()
    assert t.started_evt.wait(5)
    t.gate.set()

    def released():
        _flush_deletes()
        return registry.entries() == [] and sip.isdeleted(t)

    assert poll_until(released, timeout=10)


def test_page_finished_slot_runs_before_delete(qapp):
    """页面先连的 finished 槽先执行：此时线程对象仍有效，可安全清引用"""
    registry = ThreadRegistry()
    t = _GateThread()
    seen = []
    t.finished.connect(lambda: seen.append(sip.isdeleted(t)))
    registry.register(t, name="x")
    t.start()
    t.gate.set()
    assert poll_until(lambda: (_flush_deletes() or True) and sip.isdeleted(t), timeout=10)
    assert seen == [False]


def test_shutdown_all_without_running_threads_is_true(qapp):
    registry = ThreadRegistry()
    registry.register(_GateThread(), name="未启动")
    progress = []
    assert registry.shutdown_all(progress.append) is True
    assert progress == []


def test_never_started_thread_is_dropped(qapp):
    """登记后从未启动的线程永远不会发 finished：cancel_all / shutdown_all 时清掉，
    不再一直留在登记表里；已启动的线程不受影响"""
    registry = ThreadRegistry()
    idle, running = _GateThread(), _GateThread()
    registry.register(idle, name="未启动", cancel=idle.cancel)
    registry.register(running, name="运行中", cancel=running.cancel, timeout_ms=5000)
    running.start()
    assert running.started_evt.wait(5)

    registry.cancel_all()
    assert [e.name for e in registry.entries()] == ["运行中"]
    assert idle.log == []                 # 未启动的线程不调取消回调
    assert registry.shutdown_all() is True

    registry2 = ThreadRegistry()
    registry2.register(_GateThread(), name="未启动")
    registry2.shutdown_all()
    assert registry2.entries() == []


def test_register_already_finished_thread_is_skipped(qapp):
    """已结束的线程不会再发 finished，登记了就永远注销不掉：直接不登记"""
    registry = ThreadRegistry()
    t = _GateThread()
    t.gate.set()
    t.start()
    assert t.wait(5000)
    registry.register(t, name="已结束")
    assert registry.entries() == []
    assert poll_until(lambda: (_flush_deletes() or True) and sip.isdeleted(t), timeout=10)


def test_cancel_all_cancels_running_once_without_waiting(qapp):
    """cancel_all 只发取消、不等待；随后的 shutdown_all 不重复取消"""
    log = []
    registry = ThreadRegistry()
    a, plain = _GateThread(log, "甲"), _GateThread(log, "丙")
    for t in (a, plain):
        t.start()
        assert t.started_evt.wait(5)
    registry.register(a, name="甲", cancel=a.cancel, timeout_ms=5000)
    registry.register(plain, name="丙", timeout_ms=5000)
    try:
        registry.cancel_all()
        assert log == [("cancel", "甲")]
        assert plain.isRunning()
        plain.gate.set()
        assert registry.shutdown_all() is True
        assert log == [("cancel", "甲")]
    finally:
        plain.gate.set()
        plain.wait(5000)


def test_shutdown_all_deadline_caps_total_wait(qapp, caplog):
    """截止时间落实到每次等待：3 个各 5 秒超时的卡住线程，总等待不超过截止时间，
    已到期的不再等待；返回 False 并记日志"""
    import time
    registry = ThreadRegistry()
    stuck = [_GateThread(name=f"卡{i}") for i in range(3)]
    for t in stuck:
        t.start()
        assert t.started_evt.wait(5)
        registry.register(t, name=t.name, timeout_ms=5000)
    try:
        t0 = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="core.shutdown"):
            assert registry.shutdown_all(deadline=t0 + 0.3) is False
        assert time.monotonic() - t0 < 1.5
        assert any("卡2" in r.getMessage() for r in caplog.records)
    finally:
        for t in stuck:
            t.gate.set()
            t.wait(5000)


def test_shutdown_all_cancels_then_waits_in_registration_order(qapp, monkeypatch):
    import core.shutdown as shutdown_module
    log = []
    registry = ThreadRegistry()
    a = _GateThread(log, "甲")
    b = _GateThread(log, "乙")
    c = _GateThread(log, "丙")   # 不可取消：测试里在等待甲时放行
    for t in (a, b, c):
        t.start()
        assert t.started_evt.wait(5)
    registry.register(a, name="甲", cancel=a.cancel, timeout_ms=5000)
    registry.register(c, name="丙", timeout_ms=5000)
    registry.register(b, name="乙", cancel=b.cancel, timeout_ms=5000)

    real = shutdown_module.shutdown_thread

    def spy(thread, cancel_fn=None, timeout_ms=2000):
        log.append(("wait", thread.name, timeout_ms))
        c.gate.set()
        return real(thread, cancel_fn=cancel_fn, timeout_ms=timeout_ms)

    monkeypatch.setattr(shutdown_module, 'shutdown_thread', spy)
    progress = []

    assert registry.shutdown_all(progress.append) is True
    # 先全部取消（并行收尾），再按登记顺序逐个等待
    assert log == [("cancel", "甲"), ("cancel", "乙"),
                   ("wait", "甲", 5000), ("wait", "丙", 5000), ("wait", "乙", 5000)]
    assert progress == [PROGRESS_STOPPING_THREADS]
    assert not any(t.isRunning() for t in (a, b, c))


def test_shutdown_all_timeout_logs_and_returns_false(qapp, caplog):
    registry = ThreadRegistry()
    stuck = _GateThread()
    quick = _GateThread()
    for t in (stuck, quick):
        t.start()
        assert t.started_evt.wait(5)
    registry.register(stuck, name="卡住的线程", timeout_ms=50)
    registry.register(quick, name="可取消", cancel=quick.cancel, timeout_ms=5000)
    try:
        with caplog.at_level(logging.ERROR, logger="core.shutdown"):
            assert registry.shutdown_all() is False
        assert any("卡住的线程" in r.getMessage() for r in caplog.records)
        assert quick.isFinished()   # 一个超时不影响后面的线程
    finally:
        stuck.gate.set()
        stuck.wait(5000)


def test_shutdown_all_cancel_error_is_logged_and_continues(qapp, caplog):
    registry = ThreadRegistry()
    t = _GateThread()
    t.start()
    assert t.started_evt.wait(5)

    def bad_cancel():
        t.gate.set()
        raise RuntimeError("取消失败")

    registry.register(t, name="坏取消", cancel=bad_cancel, timeout_ms=5000)
    with caplog.at_level(logging.ERROR, logger="core.shutdown"):
        assert registry.shutdown_all() is True
    assert any("坏取消" in r.getMessage() for r in caplog.records)


def test_shutdown_all_progress_callback_error_does_not_break(qapp):
    registry = ThreadRegistry()
    t = _GateThread()
    t.start()
    assert t.started_evt.wait(5)
    registry.register(t, name="x", cancel=t.cancel, timeout_ms=5000)

    def bad_progress(text):
        raise ValueError("界面已销毁")

    assert registry.shutdown_all(bad_progress) is True
