"""全部指标本周期都不可用（collect_metrics 返回空字典）时不 emit data_updated。"""
import os
import time

from PyQt5.QtCore import Qt

from core.monitor_task import MonitorTask
from utils.metrics import MetricType


class _AllUnavailableCollector:
    """全部指标都不可用：collect_metrics 返回空字典（进程仍存活）"""
    last_unavailable = {}

    def is_process_running(self):
        return True

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        self.last_unavailable = {m: "权限不足" for m in metric_types}
        return {}


def test_monitor_task_does_not_emit_data_updated_for_empty_values(db, qapp):
    """values 为空字典：没有数据落库，不 emit data_updated（卡片采集次数不虚增），
    metrics_unavailable 通知照常"""
    task = MonitorTask(
        pid=os.getpid(), process_name="pytest-target",
        metric_types=[MetricType.MEMORY_USS], interval=0.05, db=db)
    task.collector = _AllUnavailableCollector()
    cycles, notices = [], []
    task.data_updated.connect(lambda tid, v: cycles.append(v), Qt.DirectConnection)
    task.metrics_unavailable.connect(
        lambda tid, r: notices.append((tid, dict(r))), Qt.DirectConnection)

    task.start()
    deadline = time.time() + 3
    while not notices and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)   # 再多跑几个采集周期
    task.stop()
    task.wait(3000)

    assert cycles == []
    assert notices == [(task.task_id, {MetricType.MEMORY_USS: "权限不足"})]
    assert db.get_data_point_count(task.task_id) == 0
