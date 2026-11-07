"""
MonitorWorker 显式状态（WorkerState）迁移全覆盖：
WAITING -> RUNNING <-> PAUSED -> STOPPING -> STOPPED，WAITING -> STOPPED；
非法迁移一律拒绝且不改状态；is_running()/is_paused() 只由状态派生；
收尾在发出 task_stopped 之前置 STOPPED。
"""
import os
import threading

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import (
    ACTIVE_STATES, LIVE_STATES, MonitorWorker, WorkerState,
)
from tests.helpers import poll_until


class _GateCollector:
    """可被门闩卡在采集中的假采集器；end_after 次后报告进程终止"""

    def __init__(self, gate: threading.Event = None, end_after: int = None):
        self.gate = gate
        self.end_after = end_after
        self.calls = 0
        self.entered = threading.Event()
        self.last_unavailable = {}

    def is_process_running(self):
        return True

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.end_after is not None and self.calls > self.end_after:
            return None
        return {"memory_rss": 1.0}


@pytest.fixture
def make_worker(db, qapp):
    created = []

    def _factory(collector=None, interval=0.05):
        w = MonitorWorker(pid=os.getpid(), process_name="state.exe",
                          metric_types=["memory_rss"], interval=interval, db=db)
        w.collector = collector or _GateCollector()
        created.append(w)
        return w

    yield _factory

    for w in created:
        if w.collector.gate is not None:
            w.collector.gate.set()
        w.stop()
        w.wait(5000)


def _record_state_at_stop(worker) -> list:
    """task_stopped 发出瞬间（工作线程内同步执行）的状态"""
    seen = []
    worker.task_stopped.connect(
        lambda _tid, _reason: seen.append(worker.state), Qt.DirectConnection)
    return seen


# ========== 状态集合与派生查询 ==========

def test_state_sets():
    # 等待重启时线程仍活着、任务仍算进行中，退出收尾与维护互斥都要把它算上
    assert ACTIVE_STATES == {WorkerState.RUNNING, WorkerState.PAUSED, WorkerState.WAITING_RESTART}
    assert LIVE_STATES == {WorkerState.RUNNING, WorkerState.PAUSED, WorkerState.WAITING_RESTART,
                           WorkerState.STOPPING}


@pytest.mark.parametrize("state, running, paused", [
    (WorkerState.WAITING, False, False),
    (WorkerState.RUNNING, True, False),
    (WorkerState.PAUSED, True, True),
    (WorkerState.STOPPING, False, False),
    (WorkerState.STOPPED, False, False),
])
def test_queries_are_derived_from_state(make_worker, state, running, paused):
    worker = make_worker()
    worker._state = state
    assert worker.is_running() is running
    assert worker.is_paused() is paused
    worker._state = WorkerState.WAITING   # 未启动，收尾无需等待


# ========== WAITING ==========

def test_new_worker_is_waiting(make_worker):
    worker = make_worker()
    assert worker.state is WorkerState.WAITING
    assert worker.is_running() is False and worker.is_paused() is False


def test_waiting_rejects_pause_and_resume(make_worker):
    worker = make_worker()
    assert worker.pause() is False
    assert worker.resume() is False
    assert worker.state is WorkerState.WAITING


def test_waiting_stop_goes_straight_to_stopped_and_cannot_start(make_worker):
    worker = make_worker()
    stopped = []
    worker.task_stopped.connect(lambda *a: stopped.append(a), Qt.DirectConnection)

    assert worker.stop() is True
    assert worker.state is WorkerState.STOPPED
    assert worker.stop() is False

    worker.start()   # 已停止的对象不可复用：忽略
    assert worker.isRunning() is False and worker.state is WorkerState.STOPPED
    worker.run()     # 直接 run 也不收尾、不发信号
    assert stopped == []


# ========== RUNNING / PAUSED ==========

def test_start_moves_to_running_before_thread_runs(make_worker):
    worker = make_worker()
    worker.start()
    assert worker.state is WorkerState.RUNNING   # start() 返回时即已迁移
    assert worker.is_running() is True
    worker.start()                               # 重复启动被忽略
    assert worker.state is WorkerState.RUNNING


def test_pause_resume_cycle(make_worker):
    collector = _GateCollector()
    worker = make_worker(collector)
    worker.start()
    assert collector.entered.wait(5)

    assert worker.pause() is True
    assert worker.state is WorkerState.PAUSED
    assert worker.is_running() is True and worker.is_paused() is True
    assert worker.pause() is False               # 已暂停
    assert worker.state is WorkerState.PAUSED

    assert worker.resume() is True
    assert worker.state is WorkerState.RUNNING and worker.is_paused() is False
    assert worker.resume() is False              # 未暂停


# ========== STOPPING -> STOPPED ==========

def test_running_stop_passes_through_stopping(make_worker):
    gate = threading.Event()
    collector = _GateCollector(gate)
    worker = make_worker(collector)
    at_stop = _record_state_at_stop(worker)
    worker.start()
    assert collector.entered.wait(5)

    assert worker.stop() is True
    assert worker.state is WorkerState.STOPPING
    assert worker.isRunning() is True and worker.is_running() is False
    # 停止收尾中：其余迁移一律拒绝
    assert worker.stop() is False
    assert worker.pause() is False and worker.resume() is False
    worker.start()
    assert worker.state is WorkerState.STOPPING

    gate.set()
    assert worker.wait(5000) is True
    assert worker.state is WorkerState.STOPPED
    assert at_stop == [WorkerState.STOPPED], "task_stopped 发出前必须已置 STOPPED"


def test_paused_stop_terminates(make_worker):
    collector = _GateCollector()
    worker = make_worker(collector, interval=30.0)
    worker.start()
    assert collector.entered.wait(5)
    assert worker.pause() is True

    assert worker.stop() is True
    assert worker.state in (WorkerState.STOPPING, WorkerState.STOPPED)
    assert worker.wait(5000) is True
    assert worker.state is WorkerState.STOPPED


def test_natural_end_goes_through_stopping_during_teardown(make_worker, monkeypatch):
    """主循环自行结束（进程退出）：收尾期间是 STOPPING（仍在写库，计数要算它），之后 STOPPED"""
    worker = make_worker(_GateCollector(end_after=1))
    during_teardown = []
    original = worker._teardown
    monkeypatch.setattr(worker, '_teardown',
                        lambda reason: (during_teardown.append((worker.state, reason)),
                                        original(reason)))
    worker.start()
    assert worker.wait(5000) is True

    assert during_teardown == [(WorkerState.STOPPING, StopReason.PROCESS_EXITED)]
    assert worker.state is WorkerState.STOPPED


def test_stopped_even_if_teardown_raises(make_worker, monkeypatch):
    """收尾被替换成抛异常的实现：run() 的 finally 仍兜底置 STOPPED"""
    worker = make_worker(_GateCollector(end_after=0))

    def boom(reason):
        raise RuntimeError("收尾爆炸")

    monkeypatch.setattr(worker, '_teardown', boom)
    with pytest.raises(RuntimeError):
        worker.run()
    assert worker.state is WorkerState.STOPPED


def test_body_exception_ends_stopped(make_worker):
    class _ExplodingDB:
        def save_task(self, *a, **k):
            raise RuntimeError("db 爆炸")

        def update_task_status(self, *a, **k):
            return True

    worker = make_worker()
    worker.db = _ExplodingDB()
    at_stop = _record_state_at_stop(worker)

    worker.run()   # WAITING 下直接 run 视同已 start

    assert worker.state is WorkerState.STOPPED
    assert at_stop == [WorkerState.STOPPED]


def test_inline_run_from_waiting_promotes_to_running(make_worker):
    collector = _GateCollector(end_after=2)
    worker = make_worker(collector)
    worker.run()
    assert collector.calls == 3
    assert worker.state is WorkerState.STOPPED


# ========== 并发 ==========

def test_concurrent_requests_end_in_stopped(make_worker):
    """多线程同时 pause/resume/stop：状态始终合法，最终 STOPPED 且只收尾一次"""
    worker = make_worker(_GateCollector(), interval=0.01)
    stopped = []
    worker.task_stopped.connect(lambda *a: stopped.append(a), Qt.DirectConnection)
    worker.start()
    assert poll_until(lambda: worker.collector.calls >= 1)

    observed = set()
    go = threading.Event()

    def hammer():
        go.wait()
        for i in range(300):
            (worker.pause if i % 2 else worker.resume)()
            observed.add(worker.state)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    go.set()
    worker.stop()
    for t in threads:
        t.join(10)

    assert worker.wait(5000) is True
    assert worker.state is WorkerState.STOPPED
    assert observed <= set(WorkerState)
    assert len(stopped) == 1


def test_worker_requires_injected_db(qapp):
    """db 必填：不再回退自建 Database()（否则会绕过组合根连到默认路径的库）"""
    with pytest.raises(TypeError):
        MonitorWorker(pid=os.getpid(), process_name="x.exe", metric_types=["memory_rss"])
    with pytest.raises(TypeError):
        MonitorWorker(pid=os.getpid(), process_name="x.exe", metric_types=["memory_rss"],
                      db=None)
