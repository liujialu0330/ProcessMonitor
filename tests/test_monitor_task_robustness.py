"""
MonitorTask 调度与健壮性：
Event + 单调时钟截止时间调度（无累积漂移、落后不补采）、run() 的 try/finally 只收尾一次、
任务行补写、flush 失败日志节流、PID 复用检测
"""
import logging
import os
import statistics
import time
from types import SimpleNamespace

import psutil
import pytest
from PyQt5.QtCore import Qt

from core.monitor_task import (
    FLUSH_FAILURE_LOG_INTERVAL, MonitorTask,
)
from core.process_collector import ProcessCollector
from data.models import DataPoint
from datetime import datetime
from tests.helpers import poll_until


class _FakeCollector:
    """可编排每次采集行为的假采集器；记录每次采集的单调时间"""

    def __init__(self, on_call=None):
        self.call_times = []
        self.last_unavailable = {}
        self._on_call = on_call

    def is_process_running(self):
        return True

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        self.call_times.append(time.monotonic())
        if self._on_call is not None:
            self._on_call(len(self.call_times))
        return {"memory_rss": 1.0}


@pytest.fixture
def make_task(db, qapp):
    created = []

    def _factory(collector, interval=0.1, task_db=None, metric_types=None,
                 create_time=None):
        t = MonitorTask(pid=os.getpid(), process_name="fake.exe",
                        metric_types=metric_types or ["memory_rss"], interval=interval,
                        db=task_db if task_db is not None else db,
                        create_time=create_time)
        t.collector = collector
        created.append(t)
        return t

    yield _factory

    for t in created:
        if t.isRunning():
            t.stop()
            t.wait(5000)


# ========== 调度 ==========

def test_scheduler_has_no_cumulative_drift(make_task):
    """截止时间每周期 += interval：采集本身耗时不会累积成漂移"""
    interval = 0.1
    collector = _FakeCollector(on_call=lambda n: time.sleep(0.03))   # 每次采集耗时 30ms
    task = make_task(collector, interval=interval)

    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 12, timeout=10)
    task.stop()
    assert task.wait(5000)

    times = collector.call_times[:12]
    span = times[-1] - times[0]
    # 旧实现是"采集耗时 + 睡眠"，每周期约 130ms；截止时间调度应保持 100ms 节拍
    assert abs(span - 11 * interval) < 0.15, f"11 个周期耗时 {span:.3f}s，应约 1.1s"
    assert statistics.median(b - a for a, b in zip(times, times[1:])) == pytest.approx(
        interval, abs=0.03)


def test_scheduler_does_not_catch_up_after_stall(make_task):
    """落后超过一个周期（如休眠唤醒）：重置截止时间，不连续补采"""
    interval = 0.1

    def stall_on_third(n):
        if n == 3:
            time.sleep(0.8)   # 8 个周期的停顿

    collector = _FakeCollector(on_call=stall_on_third)
    task = make_task(collector, interval=interval)

    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 9, timeout=15)
    task.stop()
    assert task.wait(5000)

    times = collector.call_times
    gaps_after_stall = [b - a for a, b in zip(times[2:], times[3:])]
    # 停顿结束后若补采，会出现多个紧挨着的采样（间隔≈0）；重置后至多 1 个
    assert sum(1 for g in gaps_after_stall if g < 0.05) <= 1
    assert gaps_after_stall[-1] == pytest.approx(interval, abs=0.05)


def test_no_msleep_polling_left():
    """100ms 分片 msleep 与暂停轮询已删除"""
    import inspect
    import core.monitor_task as module
    assert "msleep" not in inspect.getsource(module)


# ========== run() 的 try/finally ==========

def test_run_teardown_exactly_once_when_body_raises(make_task, monkeypatch):
    """主体抛出未预期异常：仍收尾恰好一次并 emit task_stopped 一次"""
    class _ExplodingDB:
        def save_task(self, *a, **k):
            raise RuntimeError("db 爆炸")

        def update_task_status(self, *a, **k):
            return True

        def save_data_points(self, *a, **k):
            return True

    task = make_task(_FakeCollector(), task_db=_ExplodingDB())
    teardowns, stopped, errors = [], [], []
    original = task._teardown
    monkeypatch.setattr(task, '_teardown', lambda reason: (teardowns.append(reason), original(reason)))
    task.task_stopped.connect(lambda tid, r: stopped.append(r), Qt.DirectConnection)
    task.error_occurred.connect(lambda tid, m: errors.append(m), Qt.DirectConnection)

    task.run()   # 同线程直接执行

    assert teardowns == ["内部错误"]
    assert stopped == ["内部错误"]
    assert len(errors) == 1 and "未预期" in errors[0]
    assert task.is_running() is False
    assert task.task_model.status == 'stopped'


def test_run_normal_stop_teardown_once(make_task, monkeypatch):
    collector = _FakeCollector()
    task = make_task(collector, interval=0.05)
    teardowns = []
    original = task._teardown
    monkeypatch.setattr(task, '_teardown', lambda reason: (teardowns.append(reason), original(reason)))

    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 2)
    task.stop()
    assert task.wait(5000)

    assert teardowns == ["用户停止"]


def test_run_process_not_exist_stops_with_reason(make_task):
    collector = _FakeCollector()
    collector.is_process_running = lambda: False
    task = make_task(collector)
    stopped, errors = [], []
    task.task_stopped.connect(lambda tid, r: stopped.append(r), Qt.DirectConnection)
    task.error_occurred.connect(lambda tid, m: errors.append(m), Qt.DirectConnection)

    task.run()

    assert stopped == ["进程不存在"] and len(errors) == 1
    assert collector.call_times == []


def test_collect_returning_none_stops_as_process_terminated(make_task):
    """collect_metrics 返回 None（进程终止/PID 复用）：按"进程已终止"停止"""
    collector = _FakeCollector()
    collector.collect_metrics = lambda metric_types: None
    task = make_task(collector, interval=0.05)
    stopped = []
    task.task_stopped.connect(lambda tid, r: stopped.append(r), Qt.DirectConnection)
    task._running = True   # 同线程直接 run()，等价于 start() 置位运行标志

    task.run()

    assert stopped == ["进程已终止"]


# ========== 任务行补写 ==========

def test_task_row_written_late_before_flush(make_task, db):
    """启动时 save_task 失败：后续 flush 前补写任务行，sample_count 不丢，最终状态正确"""
    collector = _FakeCollector()
    task = make_task(collector, interval=0.05)
    real_save_task = db.save_task
    calls = []

    def flaky_save_task(model):
        calls.append(model.status)
        if len(calls) == 1:
            return False   # 启动那次失败
        return real_save_task(model)

    task.db = _ProxyDB(db, save_task=flaky_save_task)
    stopped = []
    task.task_stopped.connect(lambda tid, r: stopped.append(r), Qt.DirectConnection)

    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 3)
    task.stop()
    assert task.wait(5000)

    assert len(calls) >= 2
    saved = db.get_task(task.task_id)
    assert saved is not None and saved.status == 'stopped'
    assert saved.start_time is not None
    # 每次采样 1 个指标 1 个数据点；sample_count 必须与落库的采样次数一致
    assert saved.sample_count == db.get_data_point_count(task.task_id) >= 3
    assert stopped == ["用户停止"]


class _ProxyDB:
    """把 Database 的其余方法透传，只覆写指定方法"""

    def __init__(self, real, **overrides):
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


def test_flush_blocked_until_task_row_exists(make_task, db):
    """任务行一直写不进去时，数据点不落库（保留缓冲待重试），避免 sample_count 失真"""
    task = make_task(_FakeCollector())
    task.db = _ProxyDB(db, save_task=lambda model: False)
    task._data_buffer.append(DataPoint(task.task_id, datetime.now(), 1.0, "memory_rss"))

    task._flush_buffer()

    assert len(task._data_buffer) == 1
    assert db.get_data_point_count(task.task_id) == 0
    assert task._flush_fail_count == 1


# ========== flush 日志节流 ==========

def test_flush_failure_logging_is_throttled(make_task, caplog):
    class _FailingDB:
        def save_task(self, *a, **k):
            return True

        def save_data_points(self, points):
            return False

    task = make_task(_FakeCollector(), task_db=_FailingDB())

    with caplog.at_level(logging.ERROR, logger="core.monitor_task"):
        for _ in range(250):
            task._data_buffer.append(DataPoint(task.task_id, datetime.now(), 1.0, "memory_rss"))
            task._flush_buffer()

    flush_logs = [r for r in caplog.records if "flush 失败" in r.getMessage()]
    assert FLUSH_FAILURE_LOG_INTERVAL == 100
    assert len(flush_logs) == 3   # 第 1、100、200 次
    assert "连续第100次" in flush_logs[1].getMessage()


# ========== PID 复用 ==========

def test_pid_reuse_detected_returns_none():
    """is_running() 为 False（进程已退出或 PID 被新进程占用）：返回 None，任务据此停止"""
    c = ProcessCollector(os.getpid())
    c._process = SimpleNamespace(is_running=lambda: False)
    assert c.collect_metrics(["memory_rss"]) is None


def test_is_running_checked_every_round(monkeypatch):
    c = ProcessCollector(os.getpid())
    c.collect_metrics(["memory_rss"])   # 初始化 _process
    calls = []
    original = c._process.is_running
    monkeypatch.setattr(c._process, 'is_running', lambda: (calls.append(1), original())[1])

    for _ in range(3):
        assert c.collect_metrics(["memory_rss"]) is not None

    assert len(calls) == 3


def test_is_running_access_denied_treated_as_alive(monkeypatch):
    c = ProcessCollector(os.getpid())
    c.collect_metrics(["memory_rss"])

    def _denied():
        raise psutil.AccessDenied(1)

    monkeypatch.setattr(c._process, 'is_running', _denied)

    values = c.collect_metrics(["memory_rss"])
    assert values is not None and values["memory_rss"] > 0


def test_task_stops_when_pid_reused(make_task, monkeypatch):
    """端到端：采集器判定 PID 已被复用 -> 任务以"进程已终止"停止"""
    collector = ProcessCollector(os.getpid())
    task = make_task(collector, interval=0.05)
    stopped = []
    task.task_stopped.connect(lambda tid, r: stopped.append(r), Qt.DirectConnection)
    collector.collect_metrics(["memory_rss"])   # 让 _process 存在
    monkeypatch.setattr(collector._process, 'is_running', lambda: False)
    monkeypatch.setattr(collector, 'is_process_running', lambda: True)
    task._running = True   # 同线程直接 run()，等价于 start() 置位运行标志

    task.run()

    assert stopped == ["进程已终止"]


# ========== CPU 预热后的首采时间 ==========

class _PrimeRecordingCollector(_FakeCollector):
    """记录 prime_cpu 的调用时间"""

    def __init__(self):
        super().__init__()
        self.prime_times = []

    def prime_cpu(self):
        self.prime_times.append(time.monotonic())


def test_first_collect_waits_one_interval_after_cpu_prime(make_task):
    """含 CPU 指标：预热后隔满一个周期才首采，否则首个 CPU 样本统计窗口近乎为零"""
    interval = 0.3
    collector = _PrimeRecordingCollector()
    task = make_task(collector, interval=interval,
                     metric_types=["cpu_percent", "memory_rss"])

    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 1, timeout=5)
    task.stop()
    assert task.wait(5000)

    assert len(collector.prime_times) == 1
    assert collector.call_times[0] - collector.prime_times[0] >= interval * 0.9


def test_first_collect_is_immediate_without_cpu_metric(make_task):
    """不含 CPU 指标：不预热，启动即采一次（不必白等一个周期）"""
    interval = 5.0
    collector = _PrimeRecordingCollector()
    task = make_task(collector, interval=interval)

    started_at = time.monotonic()
    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 1, timeout=2)
    task.stop()
    assert task.wait(5000)

    assert collector.prime_times == []
    assert collector.call_times[0] - started_at < 1.0


# ========== 开始前的进程身份校验（PID 复用） ==========

class _IdentityCollector(_FakeCollector):
    """可指定"该 PID 当前进程"创建时间的假采集器。采满 3 次后报告进程终止，
    保证同线程直接 run() 的用例即使身份校验缺失也会结束（而不是挂住）"""

    def __init__(self, actual_create_time):
        super().__init__()
        self.actual_create_time = actual_create_time

    def get_create_time(self):
        return self.actual_create_time

    def collect_metrics(self, metric_types):
        if len(self.call_times) >= 3:
            return None
        return super().collect_metrics(metric_types)


def _run_inline(task):
    stopped, errors = [], []
    task.task_stopped.connect(lambda tid, r: stopped.append(r), Qt.DirectConnection)
    task.error_occurred.connect(lambda tid, m: errors.append(m), Qt.DirectConnection)
    return stopped, errors


def test_create_time_mismatch_stops_before_collecting(make_task, db):
    """所选进程已退出、PID 被新进程复用：任务以"进程已终止"结束，不写任何数据点"""
    collector = _IdentityCollector(actual_create_time=2000.0)
    task = make_task(collector, interval=0.05, create_time=1000.0)
    stopped, errors = _run_inline(task)
    task._running = True   # 同线程直接 run()，等价于 start() 置位运行标志

    task.run()

    assert stopped == ["进程已终止"]
    assert len(errors) == 1 and "其他进程" in errors[0]
    assert collector.call_times == []
    assert db.get_sample_count(task.task_id) == 0
    assert task.task_model.status == 'stopped'


@pytest.mark.parametrize("expected, actual", [
    (1000.0, 1000.0),    # 一致：正常采集
    (None, 2000.0),      # 手动输入 PID，无创建时间：跳过比对
    (1000.0, None),      # 读不到当前创建时间（如权限不足）：无法确认，不误停
])
def test_create_time_match_or_unknown_runs_normally(make_task, expected, actual):
    collector = _IdentityCollector(actual_create_time=actual)
    task = make_task(collector, interval=0.05, create_time=expected)
    stopped, errors = _run_inline(task)

    task.start()
    assert poll_until(lambda: len(collector.call_times) >= 2)
    task.stop()
    assert task.wait(5000)

    assert stopped == ["用户停止"] and errors == []


def test_real_collector_detects_reused_pid_identity(make_task):
    """真实采集器：传入与当前进程不符的创建时间，同样在首采前结束"""
    collector = ProcessCollector(os.getpid())
    real_create_time = psutil.Process(os.getpid()).create_time()
    assert collector.get_create_time() == pytest.approx(real_create_time)

    task = make_task(collector, interval=0.05, create_time=real_create_time - 3600)
    stopped, _ = _run_inline(task)
    samples = []
    task.data_updated.connect(lambda tid, v: samples.append(v), Qt.DirectConnection)

    task.start()
    finished = task.wait(2000)
    if not finished:   # 校验缺失时任务会一直采集当前进程，收尾后再断言失败
        task.stop()
        task.wait(5000)

    assert finished and stopped == ["进程已终止"] and samples == []


def test_get_all_processes_includes_create_time():
    own = [p for p in ProcessCollector.get_all_processes() if p[0] == os.getpid()]
    assert len(own) == 1
    pid, name, create_time = own[0]
    assert name and create_time == pytest.approx(psutil.Process(pid).create_time())
