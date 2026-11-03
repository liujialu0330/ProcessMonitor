"""
MonitorTask 停止竞态与停止延迟测量
真实起 QThread 监控当前测试进程（或短生命周期子进程），验证 teardown 在工作线程
归位后的行为：双路径停止不重复 emit、task_stopped 触发时数据已落库、stop()->wait()
不挂起、stop() 近乎立即返回、完全停止不与采集间隔成正比（Event 唤醒，无轮询）。
时序断言一律用轮询等待，不依赖固定 sleep。
"""
import os
import subprocess
import sys
import time

import pytest
from PyQt5.QtCore import Qt

from core.monitor_task import MonitorTask
from tests.helpers import poll_until


@pytest.fixture
def make_task(tmp_path, qapp):
    """构造真实 MonitorTask（数据库由根 conftest 隔离到临时目录），用例结束后
    兜底清理，防止某个断言失败导致线程遗留"""
    created = []

    def _factory(pid=None, interval=0.2, metric_types=None):
        t = MonitorTask(
            pid=pid or os.getpid(),
            process_name="pytest-target",
            metric_types=metric_types or ["memory_rss"],
            interval=interval,
        )
        created.append(t)
        return t

    yield _factory

    for t in created:
        if t.isRunning():
            t.stop()
            t.wait(3000)


def _count_updates(task):
    """直连统计 data_updated 次数（在工作线程内同步执行，反映真实时序）"""
    updates = []
    task.data_updated.connect(lambda tid, values: updates.append(values), Qt.DirectConnection)
    return updates


def test_double_stop_call_is_idempotent(make_task):
    """用户连续两次调用 stop()：teardown 只执行一次，task_stopped 只 emit 一次"""
    task = make_task(interval=0.2)
    updates = _count_updates(task)
    stopped_events = []
    task.task_stopped.connect(
        lambda tid, reason: stopped_events.append((tid, reason)), Qt.DirectConnection)

    task.start()
    assert poll_until(lambda: len(updates) >= 1), "线程未进入主循环"
    task.stop()
    task.stop()  # 第二次调用是纯粹的 no-op（_running 已是 False）

    assert task.wait(5000) is True
    assert len(stopped_events) == 1


def test_stop_then_wait_does_not_hang(make_task):
    """stop() 后 wait 必须返回 True（不挂起），验证 teardown 归位未引入死锁"""
    task = make_task(interval=0.3)
    updates = _count_updates(task)
    task.start()
    assert poll_until(lambda: len(updates) >= 1)

    task.stop()

    assert task.wait(5000) is True
    assert task.isRunning() is False


def test_task_stopped_fires_after_data_already_persisted(make_task):
    """
    task_stopped 触发时，收尾 flush 与状态落库均已完成。
    用 Qt.DirectConnection 让槽函数在发出信号的工作线程内同步执行，
    这样断言的时序才真正反映"落库先于emit"，而不会被跨线程队列时序掩盖。
    """
    task = make_task(interval=0.15)
    updates = _count_updates(task)
    observed = {}

    def _on_stopped(task_id, reason):
        observed['buffer_empty'] = (task._data_buffer == [])
        observed['db_status'] = task.db.get_task(task_id).status

    task.task_stopped.connect(_on_stopped, Qt.DirectConnection)

    task.start()
    assert poll_until(lambda: len(updates) >= 1), "确保 buffer 曾经有数据"
    task.stop()
    assert task.wait(5000) is True

    assert observed.get('buffer_empty') is True
    assert observed.get('db_status') == 'stopped'


def test_process_death_and_user_stop_race_single_emit(make_task):
    """
    进程消亡与用户点停止在时间上接近重叠：teardown 仍只有一条代码路径（run()
    的 finally），不会因为两种停止诱因同时出现而重复 emit。
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    try:
        task = make_task(pid=child.pid, interval=0.05)
        updates = _count_updates(task)
        stopped_events = []
        task.task_stopped.connect(
            lambda tid, reason: stopped_events.append(reason), Qt.DirectConnection)

        task.start()
        assert poll_until(lambda: len(updates) >= 1)
        # 等到子进程自然退出的时间点附近再停止
        poll_until(lambda: child.poll() is not None, timeout=5)
        task.stop()

        assert task.wait(5000) is True
        assert len(stopped_events) == 1
    finally:
        child.kill()
        child.wait(timeout=5)


def test_stop_call_returns_almost_immediately(make_task):
    """stop() 应近乎立即返回（只置标志并唤醒，不做任何收尾）"""
    task = make_task(interval=2.0)
    updates = _count_updates(task)
    task.start()
    assert poll_until(lambda: len(updates) >= 1)

    t0 = time.perf_counter()
    task.stop()
    stop_call_duration = time.perf_counter() - t0

    task.wait(5000)

    assert stop_call_duration < 0.1, (
        f"stop() 耗时 {stop_call_duration:.4f}s，应近乎立即返回")


def test_full_stop_latency_not_proportional_to_interval(make_task):
    """
    完全停止（stop() 调用到线程真正退出）耗时不与采集间隔成正比：线程在 Event 上
    等待，stop() 置位即醒，只剩收尾 flush/写状态的开销。
    """
    interval = 5.0
    task = make_task(interval=interval)
    updates = _count_updates(task)
    task.start()
    # 首次采样后线程进入 interval 等待阶段（此时剩余约 5s）
    assert poll_until(lambda: len(updates) >= 1)

    t0 = time.perf_counter()
    task.stop()
    finished = task.wait(5000)
    full_stop_duration = time.perf_counter() - t0

    assert finished is True
    assert full_stop_duration < 1.0, (
        f"完全停止耗时 {full_stop_duration:.4f}s，interval={interval}s，应被事件立即唤醒")
