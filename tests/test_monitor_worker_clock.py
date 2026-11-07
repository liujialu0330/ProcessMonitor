"""
MonitorWorker 时钟注入：用假时钟驱动采集线程，不真实睡眠。

假时钟的 wait 不阻塞：先给脚本一个"等待期间发生了什么"的机会（暂停、恢复、停止、
时钟跳跃），事件仍未置位就把单调时钟直接推进到截止时间。墙钟由单调时钟派生，因此
落库时间戳的间隔可以精确断言。线程用真实 QThread 运行（start/wait），验证的是生产
路径本身，而不是绕开线程的副本。

覆盖：50 个周期落库 50 条且间隔恒等于周期；含 CPU 指标时首采推迟一个周期；暂停期间
不采样、恢复后立即采；停止后 _teardown 恰好一次、task_stopped 恰好一次。
"""
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import SYSTEM_CLOCK, MonitorWorker, SystemClock, WorkerState
from data.repos.point_repo import PointQuery

T0 = datetime(2026, 11, 6, 9, 0, 0)
METRICS = ['memory_private', 'num_handles']
# 线程等待上限（真实时间）：假时钟下 50 个周期远用不了这么久，只防挂死
JOIN_TIMEOUT_MS = 20000


class SteppingClock:
    """
    假时钟：wait 立即返回，并把单调时钟推进到截止时间。

    - on_wait(event, timeout)：每次 wait 在推进时间之前调用，脚本可在这里暂停/恢复/
      停止任务或拨动时钟；调用后事件已置位则返回 True（不推进时间）；
    - timeout 为 None（暂停中的无超时等待）且无人唤醒时抛 AssertionError——
      真实时钟下这会永久阻塞，测试应当立刻失败而不是挂住；
    - now() = start + (单调时钟 - 起点)，整数秒周期下时间戳间隔精确可比。
    """

    def __init__(self, start: datetime = T0, mono0: float = 1000.0):
        self.start = start
        self._mono0 = mono0
        self.mono = mono0
        self.on_wait: Optional[Callable[[threading.Event, Optional[float]], None]] = None
        self.waits = 0
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        with self.lock:
            return self.mono

    def now(self) -> datetime:
        with self.lock:
            return self.start + timedelta(seconds=self.mono - self._mono0)

    def elapsed(self) -> float:
        """自起点经过的模拟秒数"""
        with self.lock:
            return self.mono - self._mono0

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.mono += seconds

    def wait(self, event: threading.Event, timeout: Optional[float] = None) -> bool:
        self.waits += 1
        if self.on_wait is not None and not event.is_set():
            self.on_wait(event, timeout)
        if event.is_set():
            return True
        if timeout is None:
            raise AssertionError("暂停等待无人唤醒：真实时钟下会永久阻塞")
        self.advance(timeout)
        return False


class ScriptedCollector:
    """
    ProcessCollector 替身：进程永远存活，采集结果由 values(第几次调用) 给出。

    values 返回 None 表示进程已退出、返回空字典表示本周期全部指标不可用；
    after_collect(第几次调用) 在返回前调用，用于"采满 N 次后停止"之类的脚本。
    """

    def __init__(self, metrics: List[str],
                 values: Optional[Callable[[int], Optional[Dict[str, float]]]] = None,
                 after_collect: Optional[Callable[[int], None]] = None):
        self.metrics = list(metrics)
        self.values = values or (lambda n: {m: float(n) for m in self.metrics})
        self.after_collect = after_collect
        self.calls = 0
        self.primed = 0
        self.last_unavailable: Dict[str, object] = {}

    def is_process_running(self) -> bool:
        return True

    def get_create_time(self) -> Optional[float]:
        return None

    def prime_cpu(self) -> None:
        self.primed += 1

    def collect_metrics(self, metric_types):
        self.calls += 1
        result = self.values(self.calls)
        if self.after_collect is not None:
            self.after_collect(self.calls)
        return result


def make_clocked_worker(db, clock, metrics=METRICS, interval=1.0, collector_factory=None):
    """构造注入假时钟与假采集器的 MonitorWorker，并统计 _teardown / task_stopped 次数"""
    worker = MonitorWorker(pid=os.getpid(), process_name='clock.exe',
                           metric_types=metrics, interval=interval, db=db, clock=clock)
    worker.collector = (collector_factory(worker) if collector_factory
                        else ScriptedCollector(metrics))
    worker.teardowns = []
    worker.stops = []
    original = worker._teardown

    def counting_teardown(reason):
        worker.teardowns.append(reason)
        original(reason)

    worker._teardown = counting_teardown
    worker.task_stopped.connect(
        lambda _tid, reason: worker.stops.append(reason), Qt.DirectConnection)
    return worker


def stop_after(worker_ref: list, n: int) -> Callable[[int], None]:
    """采满 n 次后在工作线程内请求停止"""
    def _hook(calls: int) -> None:
        if calls >= n:
            worker_ref[0].stop()
    return _hook


@pytest.fixture
def started(qapp):
    """登记启动过的 worker，用例结束兜底停止并等待，防止断言失败遗留线程"""
    workers = []
    yield workers
    for w in workers:
        w.stop()
        w.wait(5000)


def _run_to_end(worker, started) -> float:
    started.append(worker)
    begin = time.perf_counter()
    worker.start()
    assert worker.wait(JOIN_TIMEOUT_MS), "假时钟驱动的采集线程未在时限内结束"
    return time.perf_counter() - begin


def _timestamps(db, task_id: str, metric: str) -> List[datetime]:
    return [p.timestamp for p in db.points.fetch(PointQuery(task_id, metric))]


# ========== 默认时钟 ==========

def test_default_clock_is_system_clock(db, qapp):
    worker = MonitorWorker(pid=os.getpid(), process_name='x.exe',
                           metric_types=METRICS, interval=1.0, db=db)
    assert worker._clock is SYSTEM_CLOCK
    assert isinstance(SYSTEM_CLOCK, SystemClock)


def test_system_clock_wait_delegates_to_event():
    event = threading.Event()
    assert SYSTEM_CLOCK.wait(event, 0) is False
    event.set()
    assert SYSTEM_CLOCK.wait(event, None) is True
    assert SYSTEM_CLOCK.monotonic() <= time.monotonic()
    assert abs((datetime.now() - SYSTEM_CLOCK.now()).total_seconds()) < 5


# ========== 50 个周期 ==========

def test_fake_clock_drives_50_cycles_with_exact_spacing(db, started):
    """50 个周期落库 50 条，时间戳间隔恒等于周期，计数与最后采样时间一致，不真实睡眠"""
    interval = 5.0
    clock = SteppingClock()
    ref = []
    worker = make_clocked_worker(
        db, clock, interval=interval,
        collector_factory=lambda w: ScriptedCollector(METRICS, after_collect=stop_after(ref, 50)))
    ref.append(worker)

    elapsed = _run_to_end(worker, started)

    for metric in METRICS:
        stamps = _timestamps(db, worker.task_id, metric)
        assert len(stamps) == 50
        assert stamps[0] == T0   # 不含 CPU 指标：启动即采
        assert {b - a for a, b in zip(stamps, stamps[1:])} == {timedelta(seconds=interval)}
    task = db.tasks.get(worker.task_id)
    assert task.sample_count == 50
    assert task.last_sample_at == T0 + timedelta(seconds=49 * interval)
    assert task.start_time == T0
    assert task.end_time == T0 + timedelta(seconds=49 * interval)
    assert task.status == 'stopped'
    assert clock.waits == 49   # 每周期一次等待，首采前不等
    assert worker.teardowns == [StopReason.USER]
    assert worker.stops == [StopReason.USER]
    assert worker.state is WorkerState.STOPPED
    # 模拟 245 秒，真实耗时只取决于落库
    assert elapsed < 10


def test_cpu_metric_delays_first_sample_by_one_interval(db, started):
    """含 CPU 指标：预热一次，首个样本推迟一个周期（假时钟下精确为 1 个周期）"""
    metrics = ['cpu_percent', 'num_threads']
    clock = SteppingClock()
    ref = []
    worker = make_clocked_worker(
        db, clock, metrics=metrics, interval=2.0,
        collector_factory=lambda w: ScriptedCollector(metrics, after_collect=stop_after(ref, 5)))
    ref.append(worker)

    _run_to_end(worker, started)

    assert worker.collector.primed == 1
    stamps = _timestamps(db, worker.task_id, 'cpu_percent')
    assert stamps == [T0 + timedelta(seconds=2.0 * k) for k in range(1, 6)]


# ========== 暂停 ==========

def test_pause_stops_sampling_and_resume_samples_immediately(db, started):
    """第 20 次采集后的等待中暂停，模拟 30 秒后恢复：暂停期间没有样本，恢复时刻立即采一次"""
    clock = SteppingClock()
    ref = []
    worker = make_clocked_worker(
        db, clock, interval=1.0,
        collector_factory=lambda w: ScriptedCollector(METRICS, after_collect=stop_after(ref, 50)))
    ref.append(worker)
    paused = []

    def on_wait(event, timeout):
        if timeout is None:
            # 暂停中的无超时等待：模拟用户 30 秒后点"恢复"
            clock.advance(30)
            assert worker.resume() is True
        elif worker.collector.calls == 20 and not paused:
            paused.append(clock.now())
            assert worker.pause() is True

    clock.on_wait = on_wait
    _run_to_end(worker, started)

    pause_at = T0 + timedelta(seconds=19)
    resume_at = pause_at + timedelta(seconds=30)
    markers = {m.kind: m.timestamp for m in db.markers.list(worker.task_id)}
    assert markers == {'pause': pause_at, 'resume': resume_at}

    stamps = _timestamps(db, worker.task_id, 'memory_private')
    assert len(stamps) == 50
    assert not [ts for ts in stamps if pause_at < ts < resume_at]
    assert resume_at in stamps   # 恢复后立即采一次
    deltas = [b - a for a, b in zip(stamps, stamps[1:])]
    assert deltas.count(timedelta(seconds=30)) == 1
    assert deltas.count(timedelta(seconds=1)) == 48
    assert db.analytics.gaps(worker.task_id, 1.0) == [(pause_at, resume_at)]
    assert db.tasks.get(worker.task_id).sample_count == 50
    assert worker.teardowns == [StopReason.USER]


# ========== 停止与收尾 ==========

def test_stop_during_wait_tears_down_exactly_once(db, started):
    """在等待中停止（stop 置位事件唤醒），之后再重复停止：收尾与 task_stopped 都恰好一次"""
    clock = SteppingClock()
    worker = make_clocked_worker(db, clock, interval=1.0)

    def on_wait(event, timeout):
        if worker.collector.calls == 10:
            assert worker.stop() is True
            assert worker.stop() is False

    clock.on_wait = on_wait
    _run_to_end(worker, started)
    assert worker.stop() is False

    assert worker.teardowns == [StopReason.USER]
    assert worker.stops == [StopReason.USER]
    assert worker.state is WorkerState.STOPPED
    assert len(_timestamps(db, worker.task_id, 'num_handles')) == 10
    task = db.tasks.get(worker.task_id)
    assert task.status == 'stopped'
    assert task.end_time == T0 + timedelta(seconds=9)   # 停止发生在第 10 次采集后的等待里


def test_process_exit_tears_down_exactly_once(db, started):
    """采集器报告进程退出（返回 None）：主循环自行结束，收尾一次，原因为进程退出"""
    clock = SteppingClock()
    worker = make_clocked_worker(
        db, clock, interval=1.0,
        collector_factory=lambda w: ScriptedCollector(
            METRICS, values=lambda n: None if n > 50 else {m: 1.0 for m in METRICS}))

    _run_to_end(worker, started)

    assert worker.teardowns == [StopReason.PROCESS_EXITED]
    assert worker.stops == [StopReason.PROCESS_EXITED]
    assert db.tasks.get(worker.task_id).sample_count == 50
