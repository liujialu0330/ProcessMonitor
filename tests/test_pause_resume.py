"""
MonitorTask 暂停/恢复行为验证

真实起 QThread 监控当前测试进程，验证 pause() 让主循环跳过采集（不再 emit
data_updated）、resume() 立即恢复采集、以及暂停态下 stop() 无需先 resume 即可
干净终止（暂停改为在 Event 上等待，不再 100ms 轮询）。正向等待一律轮询，
"暂停期间不再产生数据"用"计数连续一段时间不变"来确认。
"""
import os
import time

import pytest
from PyQt5.QtCore import Qt

from core.monitor_task import MonitorTask
from tests.helpers import poll_until, wait_stable


@pytest.fixture
def make_task(tmp_path, qapp):
    """构造真实 MonitorTask（数据库由根 conftest 隔离到临时目录），用例结束后
    兜底清理，防止某个断言失败导致线程遗留"""
    created = []

    def _factory(pid=None, interval=0.1, metric_types=None):
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
    updates = []
    task.data_updated.connect(lambda tid, values: updates.append(values), Qt.DirectConnection)
    return updates


def test_pause_sets_flag_and_skips_collection(make_task):
    """pause() 置位 is_paused()，且暂停期间不再产生新的 data_updated（采集被跳过）"""
    task = make_task(interval=0.1)
    updates = _count_updates(task)

    task.start()
    assert poll_until(lambda: len(updates) >= 3), "任务应已正常运行"

    task.pause()
    assert task.is_paused() is True

    # 暂停发生前最后一个周期可能仍在收尾；确认计数稳定下来（>=0.4s 无新数据）
    assert wait_stable(lambda: len(updates), quiet=0.4), "暂停后仍持续产生数据"


def test_resume_restarts_collection(make_task):
    """resume() 复位 is_paused()，暂停期间静止的采集在恢复后重新产生新数据"""
    task = make_task(interval=0.1)
    updates = _count_updates(task)

    task.start()
    assert poll_until(lambda: len(updates) >= 1)
    task.pause()
    assert wait_stable(lambda: len(updates), quiet=0.4)
    count_while_paused = len(updates)

    task.resume()
    assert task.is_paused() is False

    assert poll_until(lambda: len(updates) > count_while_paused), "恢复后应重新产生数据"


def test_resume_collects_immediately_with_long_interval(make_task):
    """长周期任务恢复后立即采集一次，而不是等满剩余周期"""
    task = make_task(interval=30.0)
    updates = _count_updates(task)

    task.start()
    assert poll_until(lambda: len(updates) >= 1)
    task.pause()
    assert wait_stable(lambda: len(updates), quiet=0.2)
    count_while_paused = len(updates)

    task.resume()

    assert poll_until(lambda: len(updates) > count_while_paused, timeout=5), (
        "恢复应唤醒暂停等待并立即采集，而非等待 30s 周期")


def test_pause_interrupts_current_wait(make_task):
    """pause() 唤醒周期等待：长周期任务暂停后不会在原截止时间到点时再采一次"""
    task = make_task(interval=0.5)
    updates = _count_updates(task)

    task.start()
    assert poll_until(lambda: len(updates) >= 1)
    task.pause()
    count = len(updates)

    # 超过一个完整周期仍不应有新数据
    time.sleep(0.8)
    assert len(updates) == count


def test_stop_while_paused_terminates_promptly(make_task):
    """暂停态下调用 stop()：线程被事件唤醒后干净终止，无需先 resume"""
    task = make_task(interval=30.0)
    updates = _count_updates(task)
    task.start()
    assert poll_until(lambda: len(updates) >= 1)

    task.pause()
    assert wait_stable(lambda: len(updates), quiet=0.2)
    assert task.is_paused() is True

    t0 = time.perf_counter()
    task.stop()
    finished = task.wait(5000)
    duration = time.perf_counter() - t0

    assert finished is True
    assert task.isRunning() is False
    assert duration < 1.0
