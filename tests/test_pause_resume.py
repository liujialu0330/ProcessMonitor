"""
MonitorWorker 暂停/恢复行为验证

真实起 QThread 监控当前测试进程，验证 pause() 让主循环跳过采集（不再 emit
data_updated）、resume() 立即恢复采集、以及暂停态下 stop() 无需先 resume 即可
干净终止（暂停改为在 Event 上等待，不再 100ms 轮询）。正向等待一律轮询，
"暂停期间不再产生数据"用"计数连续一段时间不变"来确认。
"""
import os
import time

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import MonitorWorker
from tests.helpers import poll_until, record_stop_reasons, wait_stable


@pytest.fixture
def make_task(db, qapp):
    """构造真实 MonitorWorker（显式注入临时库），用例结束后兜底清理，防止某个断言
    失败导致线程遗留"""
    created = []

    def _factory(pid=None, interval=0.1, metric_types=None):
        t = MonitorWorker(
            pid=pid or os.getpid(),
            process_name="pytest-target",
            metric_types=metric_types or ["memory_rss"],
            interval=interval,
            db=db,
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
    reasons = record_stop_reasons(task)
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
    assert reasons == [StopReason.USER]


# ---------- 暂停/恢复系统打点 ----------

def _system_markers(db, task):
    return [(m.label, m.kind) for m in db.markers.list(task.task_id)]


def test_pause_and_resume_write_system_markers(make_task, db):
    """暂停/恢复成功切换时各写一条系统打点（kind=pause/resume），时间取调用时刻"""
    from datetime import datetime

    task = make_task(interval=0.1)
    updates = _count_updates(task)
    task.start()
    assert poll_until(lambda: len(updates) >= 1)

    before = datetime.now()
    assert task.pause() is True
    assert task.resume() is True
    after = datetime.now()

    markers = db.markers.list(task.task_id)
    assert [(m.label, m.kind) for m in markers] == [("暂停", "pause"), ("恢复", "resume")]
    assert all(before <= m.timestamp <= after for m in markers)


def test_system_marker_uses_given_click_time(make_task, db):
    from datetime import datetime

    task = make_task(interval=0.1)
    task.start()
    clicked = datetime(2026, 11, 6, 10, 0, 0)
    assert task.pause(clicked) is True
    assert [m.timestamp for m in db.markers.list(task.task_id)] == [clicked]


def test_rejected_pause_or_resume_writes_no_marker(make_task, db):
    """状态没有切换（未启动、重复暂停、未暂停就恢复）时不写打点"""
    task = make_task(interval=0.1)
    assert task.pause() is False            # 未启动
    task.start()
    assert task.resume() is False           # 运行中直接恢复
    assert task.pause() is True
    assert task.pause() is False            # 重复暂停
    assert _system_markers(db, task) == [("暂停", "pause")]


def test_system_marker_write_failure_does_not_block_pause(make_task, db, monkeypatch):
    """写库失败只记日志：暂停/恢复本身照常成功"""
    from data.errors import StorageError

    def boom(*args, **kwargs):
        raise StorageError("database is locked")

    task = make_task(interval=0.1)
    task.start()
    monkeypatch.setattr(db.markers, "add", boom)
    assert task.pause() is True and task.is_paused()
    assert task.resume() is True and not task.is_paused()
