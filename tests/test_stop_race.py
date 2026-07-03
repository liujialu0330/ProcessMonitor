"""
MonitorTask 停止竞态与停止延迟测量
真实起 QThread 监控当前测试进程（或短生命周期子进程），验证 P2-5 teardown 归位
后的行为：双路径停止不重复 emit、task_stopped 触发时数据已落库、stop()->wait()
不挂起、stop() 响应速度不劣化于 v1.0.6 基线（近乎立即返回，完全停止不与采集
间隔成正比）。
"""
import os
import subprocess
import sys
import time

import pytest
from PyQt5.QtCore import Qt

import config
from core.monitor_task import MonitorTask


@pytest.fixture
def make_task(tmp_path, monkeypatch, qapp):
    """构造真实 MonitorTask（tmp 库，不碰项目真实 data\\monitor.db），会话结束/用例
    结束后兜底清理，防止某个断言失败导致线程遗留"""
    monkeypatch.setattr(config, 'DB_PATH', str(tmp_path / "stop_race.db"))
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


def test_double_stop_call_is_idempotent(make_task):
    """用户连续两次调用 stop()：teardown 只执行一次，task_stopped 只 emit 一次"""
    task = make_task(interval=0.2)
    stopped_events = []
    task.task_stopped.connect(
        lambda tid, reason: stopped_events.append((tid, reason)), Qt.DirectConnection)

    task.start()
    time.sleep(0.1)  # 确保线程已进入主循环
    task.stop()
    task.stop()  # 第二次调用是纯粹的 no-op（_running 已是 False）

    finished = task.wait(3000)

    assert finished is True
    assert len(stopped_events) == 1


def test_stop_then_wait_does_not_hang(make_task):
    """stop() 后 wait(3000) 必须返回 True（不挂起），验证 teardown 归位未引入死锁"""
    task = make_task(interval=0.3)
    task.start()
    time.sleep(0.1)

    task.stop()
    finished = task.wait(3000)

    assert finished is True
    assert task.isRunning() is False


def test_task_stopped_fires_after_data_already_persisted(make_task):
    """
    task_stopped 触发时，收尾 flush 与状态落库均已完成。
    用 Qt.DirectConnection 让槽函数在发出信号的工作线程内同步执行，
    这样断言的时序才真正反映"落库先于emit"，而不会被跨线程队列时序掩盖。
    """
    task = make_task(interval=0.15)
    observed = {}

    def _on_stopped(task_id, reason):
        observed['buffer_empty'] = (task._data_buffer == [])
        observed['db_status'] = task.db.get_task(task_id).status

    task.task_stopped.connect(_on_stopped, Qt.DirectConnection)

    task.start()
    time.sleep(0.2)  # 留出至少一个采集周期，确保 buffer 曾经有数据
    task.stop()
    task.wait(3000)

    assert observed.get('buffer_empty') is True
    assert observed.get('db_status') == 'stopped'


def test_process_death_and_user_stop_race_single_emit(make_task):
    """
    进程消亡与用户点停止在时间上接近重叠：teardown 仍只有一条代码路径（run()
    主循环退出后统一调用），不会因为两种停止诱因同时出现而重复 emit。
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.08)"])
    task = make_task(pid=child.pid, interval=0.05)

    stopped_events = []
    task.task_stopped.connect(
        lambda tid, reason: stopped_events.append(reason), Qt.DirectConnection)

    task.start()
    time.sleep(0.08)  # 与子进程自然退出的时间点接近重叠
    task.stop()        # 几乎同时：用户请求停止

    finished = task.wait(3000)
    child.wait(timeout=5)

    assert finished is True
    assert len(stopped_events) == 1


def test_stop_call_returns_almost_immediately(make_task):
    """stop() 应近乎立即返回（v1.0.6 专项优化，不得因 P2-5 归位而劣化）"""
    task = make_task(interval=2.0)
    task.start()
    time.sleep(0.1)

    t0 = time.perf_counter()
    task.stop()
    stop_call_duration = time.perf_counter() - t0

    task.wait(5000)

    assert stop_call_duration < 0.05, (
        f"stop() 耗时 {stop_call_duration:.4f}s，应近乎立即返回")


def test_full_stop_latency_bounded_not_proportional_to_interval(make_task):
    """
    完全停止（stop() 调用到线程真正退出）耗时应受 100ms 睡眠分片上限约束，
    而不是与采集间隔成正比——这正是 v1.0.6 把整段 sleep 拆成 <=100ms 小片的目的。
    """
    interval = 2.0
    task = make_task(interval=interval)
    task.start()
    time.sleep(0.1)  # 让线程进入 interval 等待阶段（此时 remaining 接近 2000ms）

    t0 = time.perf_counter()
    task.stop()
    finished = task.wait(3000)
    full_stop_duration = time.perf_counter() - t0

    assert finished is True
    # 宽松上限（分片<=100ms + 收尾flush/写状态开销），核心断言是"远小于 interval"
    assert full_stop_duration < 1.0, (
        f"完全停止耗时 {full_stop_duration:.4f}s，interval={interval}s，"
        "应远小于 interval（睡眠分片<=100ms+收尾开销）"
    )
    assert full_stop_duration < interval / 2
