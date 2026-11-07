"""
长时间监控（崩溃取证 / 重启续接 / 工具重启续采 / 磁盘满）测试用的替身与装配

全部走注入点：假时钟（不真实睡眠）、假退出码探针、假进程查找、按 PID 给脚本的假采集器。
线程仍是真实 QThread，验证的是生产路径本身。
"""
import threading
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence

from core.monitor_task import MonitorWorker
from core.restart_finder import RestartHit, RestartTarget

T0 = datetime(2026, 11, 6, 9, 0, 0)
METRICS = ['memory_private', 'num_handles']
# 线程等待上限（真实时间）：假时钟下远用不了这么久，只防挂死
JOIN_TIMEOUT_MS = 20000
ACCESS_VIOLATION = 0xC0000005


class StepClock:
    """
    假时钟：wait 立即返回并把单调时钟推进到截止时间。
    on_wait(event, timeout) 在推进之前调用，脚本可在其中停止 / 暂停任务或拨动时钟。
    """

    def __init__(self, start: datetime = T0, mono0: float = 1000.0):
        self.start = start
        self._mono0 = mono0
        self.mono = mono0
        self.on_wait: Optional[Callable[[threading.Event, Optional[float]], None]] = None
        self.waits = 0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.mono

    def now(self) -> datetime:
        with self._lock:
            return self.start + timedelta(seconds=self.mono - self._mono0)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.mono += seconds

    def wait(self, event: threading.Event, timeout: Optional[float] = None) -> bool:
        self.waits += 1
        if self.on_wait is not None and not event.is_set():
            self.on_wait(event, timeout)
        if event.is_set():
            return True
        if timeout is None:
            raise AssertionError("无超时等待无人唤醒：真实时钟下会永久阻塞")
        self.advance(timeout)
        return False


class FakeProbe:
    """退出码探针替身：exit_code 返回固定值（None 表示"尚未结束"）。

    has_exited 默认恒为假：脚本里的 "进程退出" 由采集器耗尽样本表示，退出码只是它退出后
    才被读取的值；要模拟"句柄已显示结束而采集器还看不出来"时把 signaled 置真（或给回调）。
    create_time_value / exit_time_value 对应句柄的 GetProcessTimes。
    """

    def __init__(self, code: Optional[int] = None, available: bool = True,
                 create_time: Optional[float] = None, exit_time: Optional[float] = None):
        self.code = code
        self.available = available
        self.closed = 0
        self.reads = 0
        self.signaled = False
        self.create_time_value = create_time
        self.exit_time_value = exit_time

    def exit_code(self) -> Optional[int]:
        self.reads += 1
        return self.code

    def has_exited(self) -> bool:
        return self.signaled

    def create_time(self) -> Optional[float]:
        return self.create_time_value

    def exit_time(self) -> Optional[float]:
        return self.exit_time_value

    def close(self) -> None:
        self.closed += 1


class SegmentCollector:
    """
    一个进程实例的采集器：依次给出 samples 里的值（每个指标同值），耗尽后返回 None（进程已退出）。
    """

    def __init__(self, metrics: Sequence[str], samples: Sequence[float],
                 create_time: Optional[float] = None, on_collect=None):
        self.metrics = list(metrics)
        self.samples = list(samples)
        self.create_time = create_time
        self.on_collect = on_collect
        self.calls = 0
        self.primed = 0
        self.last_unavailable: Dict[str, object] = {}

    def is_process_running(self) -> bool:
        return True

    def get_create_time(self) -> Optional[float]:
        return self.create_time

    def prime_cpu(self) -> None:
        self.primed += 1

    def collect_metrics(self, metric_types):
        if self.calls >= len(self.samples):
            return None
        value = self.samples[self.calls]
        self.calls += 1
        if self.on_collect is not None:
            self.on_collect(self.calls)
        return {m: float(value) for m in self.metrics}

    def alive_instances(self):
        return []


class ScriptedFinder:
    """
    查找新进程的替身：第 n 次调用（从 1 起）按 hits 脚本返回，没有脚本项返回 None。
    hits: {调用序号: RestartHit}
    """

    def __init__(self, hits: Optional[Dict[int, RestartHit]] = None):
        self.hits = dict(hits or {})
        self.calls = 0
        self.targets: List[RestartTarget] = []
        self.on_call: Optional[Callable[[int], None]] = None

    def __call__(self, target: RestartTarget) -> Optional[RestartHit]:
        self.calls += 1
        self.targets.append(target)
        if self.on_call is not None:
            self.on_call(self.calls)
        return self.hits.get(self.calls)


class Rig:
    """装配好的 worker 与它的替身，便于断言。"""

    def __init__(self, worker, clock, probes, collectors, finder):
        self.worker = worker
        self.clock = clock
        self.probes = probes
        self.collectors = collectors
        self.finder = finder
        self.stops: list = []
        self.waiting_changes: List[bool] = []
        self.restarts: List[tuple] = []
        self.stopped_pids: List[int] = []


def build_rig(db, *, pid=100, create_time=None, segments: Dict[int, Sequence[float]],
              codes: Dict[int, Optional[int]], finder: Optional[ScriptedFinder] = None,
              resume: Optional[bool] = False, limit: int = 3, interval: float = 1.0,
              metrics=METRICS, clock: Optional[StepClock] = None, process_name='app.exe',
              group_spec=None, **kwargs) -> Rig:
    """
    segments: {pid: 采样值序列}，每个 pid 一个采集器（该 pid 的实例）；
    codes: {pid: 退出码}，每个 pid 一个探针（缺项视为取不到句柄）。
    """
    from PyQt5.QtCore import Qt

    clock = clock or StepClock()
    collectors = {p: SegmentCollector(metrics, samples, create_time=None)
                  for p, samples in segments.items()}
    probes = {p: (FakeProbe(c) if c is not False else FakeProbe(None, available=False))
              for p, c in codes.items()}
    finder = finder or ScriptedFinder()

    def collector_factory(p, _spec):
        return collectors[p]

    def probe_factory(p):
        return probes.get(p) or FakeProbe(None, available=False)

    worker = MonitorWorker(
        pid=pid, process_name=process_name, metric_types=list(metrics), interval=interval,
        db=db, clock=clock, create_time=create_time, group_spec=group_spec,
        exit_probe_factory=probe_factory, restart_finder=finder,
        collector_factory=collector_factory, resume_on_restart=resume, restart_limit=limit,
        **kwargs)
    rig = Rig(worker, clock, probes, collectors, finder)
    worker.task_stopped.connect(lambda _t, reason: rig.stops.append(reason), Qt.DirectConnection)
    worker.waiting_restart_changed.connect(lambda _t, on: rig.waiting_changes.append(on),
                                           Qt.DirectConnection)
    worker.target_restarted.connect(lambda _t, p, n: rig.restarts.append((p, n)),
                                    Qt.DirectConnection)
    return rig


def run_to_end(worker, started: list) -> None:
    """启动线程并等它结束（登记到 started，用例结束兜底停止）。"""
    started.append(worker)
    worker.start()
    assert worker.wait(JOIN_TIMEOUT_MS), "假时钟驱动的采集线程未在时限内结束"
