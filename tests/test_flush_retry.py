"""
MonitorTask._flush_buffer 失败重试用例
用假 db（可控 save_data_points 返回值）驱动，不接触真实 sqlite/进程采集；
构造 MonitorTask 前须把 config.DB_PATH 指向 tmp_path，避免 __init__ 内部
`self.db = Database()` 触碰项目真实 data\\monitor.db（随后立即用假 db 顶替）
"""
from datetime import datetime

import pytest

import config
from core.monitor_task import MonitorTask, MAX_BUFFER_SIZE, CONSECUTIVE_FAILURE_NOTIFY_THRESHOLD
from data.models import DataPoint


class _FakeDB:
    """可控 save_data_points 返回值的假数据库，记录每次调用与最终落库内容"""

    def __init__(self):
        self.calls = []
        self.fail_times = 0     # 接下来还需失败几次
        self.saved_points = []

    def save_data_points(self, points):
        self.calls.append(list(points))
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        self.saved_points.extend(points)
        return True

    # MonitorTask.run() 里还会调用到的方法，测试里不会真正 run()，占位以防误用
    def save_task(self, *_args, **_kwargs):
        return True

    def update_task_status(self, *_args, **_kwargs):
        return True


@pytest.fixture
def task(tmp_path, monkeypatch, qapp):
    """构造一个不会真正启动线程的 MonitorTask，db 替换为 _FakeDB"""
    monkeypatch.setattr(config, 'DB_PATH', str(tmp_path / "flush_retry.db"))
    t = MonitorTask(pid=999999, process_name="fake.exe", metric_types=["memory_rss"], interval=1.0)
    t.db = _FakeDB()
    return t


def _make_points(task_obj, n=1, value=1.0):
    ts = datetime.now()
    return [
        DataPoint(task_id=task_obj.task_id, timestamp=ts, value=value + i, metric_type="memory_rss")
        for i in range(n)
    ]


def test_flush_failure_keeps_buffer_for_retry(task):
    """flush 失败：缓冲保留，数据不丢，不清空"""
    task.db.fail_times = 1
    task._data_buffer.extend(_make_points(task, n=3))

    task._flush_buffer()

    assert len(task._data_buffer) == 3  # 缓冲原样保留
    assert task.db.saved_points == []   # 未落库
    assert task._flush_fail_count == 1


def test_flush_retry_succeeds_next_round_no_data_loss(task):
    """失败一次后下一轮重试成功：全部数据（含重试前缓冲的）完整落库，缓冲清空"""
    task.db.fail_times = 1
    task._data_buffer.extend(_make_points(task, n=2, value=1.0))
    task._flush_buffer()  # 第一次失败
    assert len(task._data_buffer) == 2

    # 模拟下一采集周期继续追加数据后再次触发 flush（失败次数已耗尽，这次会成功）
    task._data_buffer.extend(_make_points(task, n=1, value=100.0))
    task._flush_buffer()

    assert task._data_buffer == []
    assert len(task.db.saved_points) == 3  # 两次失败缓冲的 + 新追加的，一条不丢
    assert task._flush_fail_count == 0
    assert task._notified is False


def test_flush_buffer_drops_oldest_when_exceeding_cap(task):
    """缓冲超过 MAX_BUFFER_SIZE 时丢弃最旧数据，缓冲维持在上限"""
    task.db.fail_times = 10 ** 6  # 持续失败

    # 先塞满到超过上限
    task._data_buffer.extend(_make_points(task, n=MAX_BUFFER_SIZE + 50, value=0.0))
    task._flush_buffer()

    assert len(task._data_buffer) == MAX_BUFFER_SIZE
    # 保留的应是最新的一批（丢弃的是最旧的），首条 value 应是原本第 51 条（下标50）附近
    assert task._data_buffer[0].value == 50.0


def test_consecutive_failures_notify_once_then_latched(task, qapp):
    """连续失败达到阈值弹一次 error_occurred；此后持续失败不再重复弹；成功后复位可再次弹"""
    notified = []
    task.error_occurred.connect(lambda task_id, msg: notified.append((task_id, msg)))

    task.db.fail_times = 10 ** 6
    for _ in range(CONSECUTIVE_FAILURE_NOTIFY_THRESHOLD):
        task._data_buffer.extend(_make_points(task, n=1))
        task._flush_buffer()

    assert len(notified) == 1
    assert task._notified is True

    # 继续失败，不应重复通知（锁存）
    task._data_buffer.extend(_make_points(task, n=1))
    task._flush_buffer()
    assert len(notified) == 1

    # 成功一次后复位
    task.db.fail_times = 0
    task._data_buffer.extend(_make_points(task, n=1))
    task._flush_buffer()
    assert task._notified is False
    assert task._flush_fail_count == 0

    # 再次连续失败达到阈值，应能再弹一次
    task.db.fail_times = 10 ** 6
    for _ in range(CONSECUTIVE_FAILURE_NOTIFY_THRESHOLD):
        task._data_buffer.extend(_make_points(task, n=1))
        task._flush_buffer()
    assert len(notified) == 2


def test_teardown_flush_failure_no_retry_logs_but_does_not_raise(task, caplog):
    """收尾阶段 flush 失败：无重试机会，仅记日志，不抛异常、不阻塞后续收尾"""
    task.db.fail_times = 10 ** 6
    task._data_buffer.extend(_make_points(task, n=5))

    with caplog.at_level("ERROR"):
        task._flush_buffer(is_teardown=True)

    assert len(task._data_buffer) == 5  # 数据仍在缓冲区（未落库、未丢弃，因未超上限）
    assert any("未落库" in record.message for record in caplog.records)


def test_empty_buffer_flush_is_noop(task):
    """空缓冲区调用 flush 不应调用 db.save_data_points"""
    task._flush_buffer()
    assert task.db.calls == []
