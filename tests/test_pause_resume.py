"""
MonitorTask 暂停/恢复行为验证（v1.3.0 批3 C4）

真实起 QThread 监控当前测试进程，验证 pause() 让主循环跳过采集（不再 emit
data_updated）、resume() 恢复采集、以及暂停态下 stop() 仍能在轮询间隔内
（<=100ms 量级）干净终止而无需先 resume。core 层的 pause/resume/is_paused/
暂停轮询实现本身未改动（v1.3.0 方案裁决：core 不改），本文件只是把这三条
行为钉死为回归用例，测试模式复用 tests/test_stop_race.py 的 make_task 夹具。
"""
import os
import time

import pytest
from PyQt5.QtCore import Qt

import config
from core.monitor_task import MonitorTask


@pytest.fixture
def make_task(tmp_path, monkeypatch, qapp):
    """构造真实 MonitorTask（tmp 库，不碰项目真实 data\\monitor.db），用例结束后
    兜底清理，防止某个断言失败导致线程遗留"""
    monkeypatch.setattr(config, 'DB_PATH', str(tmp_path / "pause_resume.db"))
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


def test_pause_sets_flag_and_skips_collection(make_task):
    """pause() 置位 is_paused()，且暂停期间不再产生新的 data_updated（采集被跳过）"""
    task = make_task(interval=0.1)
    updates = []
    task.data_updated.connect(
        lambda tid, values: updates.append(values), Qt.DirectConnection)

    task.start()
    time.sleep(0.35)  # 留出若干个采集周期，确认任务已正常运行
    assert len(updates) > 0

    task.pause()
    assert task.is_paused() is True

    time.sleep(0.15)  # 留出暂停发生前"最后一个"采集周期收尾的时间窗口
    count_after_pause_settle = len(updates)

    time.sleep(0.3)  # 若干个采集周期时长，暂停态不应再有新数据
    assert len(updates) == count_after_pause_settle


def test_resume_restarts_collection(make_task):
    """resume() 复位 is_paused()，暂停期间静止的采集在恢复后重新产生新数据"""
    task = make_task(interval=0.1)
    updates = []
    task.data_updated.connect(
        lambda tid, values: updates.append(values), Qt.DirectConnection)

    task.start()
    time.sleep(0.15)
    task.pause()

    time.sleep(0.15)  # 留出暂停发生前"最后一个"采集周期收尾的时间窗口
    count_while_paused = len(updates)
    time.sleep(0.2)   # 确认暂停期间确实没有新数据（排除误判恢复效果的可能）
    assert len(updates) == count_while_paused

    task.resume()
    assert task.is_paused() is False

    time.sleep(0.35)  # 留出若干个采集周期，恢复后应产生新数据
    assert len(updates) > count_while_paused


def test_stop_while_paused_terminates_within_timeout(make_task):
    """暂停态下调用 stop()：线程应在 100ms 轮询间隔量级内干净终止，无需先 resume"""
    task = make_task(interval=2.0)  # 长周期，确保暂停时线程确实卡在 msleep(100) 轮询里
    task.start()
    time.sleep(0.1)

    task.pause()
    time.sleep(0.15)  # 确认已进入暂停轮询（而不是仍在未暂停前的长 interval 睡眠分片中）
    assert task.is_paused() is True

    t0 = time.perf_counter()
    task.stop()
    finished = task.wait(3000)
    duration = time.perf_counter() - t0

    assert finished is True
    assert task.isRunning() is False
    # 暂停态是 msleep(100) 轮询，停止应在约 100ms 量级内退出（留宽松余量应对
    # 调度抖动，核心断言是"远小于未暂停时 2.0s 的 interval"）
    assert duration < 0.5
