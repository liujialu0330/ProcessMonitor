"""采集失败不再记 0：指标留空并记录原因；MonitorTask 只通知一次。"""
import os
import sys
import time

import psutil
import pytest
from PyQt5.QtCore import Qt

from core.contracts import UnavailableReason
from core.monitor_task import MonitorTask
from core.process_collector import MetricNotSupported, ProcessCollector
from utils.metrics import MetricType

win_only = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows")


@win_only
def test_gui_resources_of_current_process():
    c = ProcessCollector(os.getpid())
    values = c.collect_metrics(
        [MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS])
    assert values is not None
    assert values[MetricType.NUM_GDI_OBJECTS] >= 0
    assert values[MetricType.NUM_USER_OBJECTS] >= 0
    assert c.last_unavailable == {}


def test_nonexistent_pid_returns_none():
    c = ProcessCollector(99999999)
    assert c.collect_metrics([MetricType.MEMORY_RSS]) is None


@pytest.mark.parametrize("exc, reason", [
    (psutil.AccessDenied(1), UnavailableReason.ACCESS_DENIED),
    (psutil.ZombieProcess(1), UnavailableReason.PROCESS_EXITING),
    (RuntimeError("boom"), UnavailableReason.ERROR),
    (MetricNotSupported(UnavailableReason.NOT_IN_SESSION), UnavailableReason.NOT_IN_SESSION),
    (MetricNotSupported(), UnavailableReason.NOT_SUPPORTED),
])
def test_failed_metric_is_skipped_not_zero(monkeypatch, exc, reason):
    c = ProcessCollector(os.getpid())
    original = c._collect_one

    def fake(metric_type):
        if metric_type == MetricType.MEMORY_USS:
            raise exc
        return original(metric_type)

    monkeypatch.setattr(c, "_collect_one", fake)
    values = c.collect_metrics([MetricType.MEMORY_RSS, MetricType.MEMORY_USS,
                                MetricType.NUM_THREADS])
    assert values is not None
    assert MetricType.MEMORY_USS not in values
    assert values[MetricType.MEMORY_RSS] > 0
    assert values[MetricType.NUM_THREADS] > 0
    assert c.last_unavailable == {MetricType.MEMORY_USS: reason}

    # 每次调用开头清空：指标恢复后不再残留
    monkeypatch.setattr(c, "_collect_one", original)
    values = c.collect_metrics([MetricType.MEMORY_USS])
    assert c.last_unavailable == {}


def test_no_such_process_in_metric_returns_none(monkeypatch):
    c = ProcessCollector(os.getpid())

    def fake(metric_type):
        raise psutil.NoSuchProcess(1)

    monkeypatch.setattr(c, "_collect_one", fake)
    assert c.collect_metrics([MetricType.MEMORY_RSS]) is None


class _FakeCollector:
    """每周期都返回同一个不可用指标的假采集器"""
    last_unavailable = {}

    def is_process_running(self):
        return True

    def prime_cpu(self):
        pass

    def collect_metrics(self, metric_types):
        self.last_unavailable = {MetricType.MEMORY_USS: UnavailableReason.ACCESS_DENIED}
        return {MetricType.MEMORY_RSS: 1.0}


def test_monitor_task_notifies_unavailable_only_once(db, qapp):
    task = MonitorTask(
        pid=os.getpid(), process_name="pytest-target",
        metric_types=[MetricType.MEMORY_RSS, MetricType.MEMORY_USS],
        interval=0.05, db=db)
    task.collector = _FakeCollector()
    cycles, notices = [], []
    task.data_updated.connect(lambda tid, v: cycles.append(v), Qt.DirectConnection)
    task.metrics_unavailable.connect(
        lambda tid, r: notices.append((tid, dict(r))), Qt.DirectConnection)

    task.start()
    deadline = time.time() + 3
    while len(cycles) < 5 and time.time() < deadline:
        time.sleep(0.05)
    task.stop()
    task.wait(3000)

    assert len(cycles) >= 5
    assert notices == [(task.task_id, {MetricType.MEMORY_USS: UnavailableReason.ACCESS_DENIED})]



@win_only
@pytest.mark.parametrize("last_error, expected", [
    (87, UnavailableReason.NOT_IN_SESSION),  # session 0 系统服务：提权也无用，不能提示权限不足
    (5, UnavailableReason.ACCESS_DENIED),
])
def test_gui_resource_zero_with_error_maps_to_reason(monkeypatch, last_error, expected):
    import core.process_collector as pc

    class FakeKernel32:
        def OpenProcess(self, *_):
            return 1234

        def CloseHandle(self, _):
            return True

    class FakeUser32:
        def GetGuiResources(self, *_):
            return 0

    monkeypatch.setattr(pc, "_init_win32", lambda: (FakeKernel32(), FakeUser32()))
    monkeypatch.setattr(pc.ctypes, "get_last_error", lambda: last_error)
    c = ProcessCollector(os.getpid())
    values = c.collect_metrics([MetricType.NUM_GDI_OBJECTS, MetricType.MEMORY_RSS])
    assert MetricType.NUM_GDI_OBJECTS not in values
    assert values[MetricType.MEMORY_RSS] > 0
    assert c.last_unavailable == {MetricType.NUM_GDI_OBJECTS: expected}


# ========== 快照路径：失败语义不变 ==========

@pytest.mark.parametrize("exc, reason", [
    (psutil.AccessDenied(1), UnavailableReason.ACCESS_DENIED),
    (psutil.ZombieProcess(1), UnavailableReason.PROCESS_EXITING),
    (RuntimeError("boom"), UnavailableReason.ERROR),
    (MetricNotSupported(UnavailableReason.NOT_IN_SESSION), UnavailableReason.NOT_IN_SESSION),
])
def test_snapshot_path_failed_psutil_metric_is_skipped_not_zero(monkeypatch, exc, reason):
    """注入 hub 时快照取的指标照常有值，仍走 psutil 的指标失败只跳过该项并记原因"""
    from core.snapshot_hub import SnapshotHub
    c = ProcessCollector(os.getpid(), hub=SnapshotHub(), max_age_s=0.0)
    original = c._collect_one

    def fake(metric_type):
        if metric_type == MetricType.MEMORY_USS:
            raise exc
        return original(metric_type)

    monkeypatch.setattr(c, "_collect_one", fake)
    values = c.collect_metrics([MetricType.MEMORY_PRIVATE, MetricType.MEMORY_USS,
                                MetricType.NUM_THREADS])
    assert values is not None
    assert MetricType.MEMORY_USS not in values
    assert values[MetricType.MEMORY_PRIVATE] > 0 and values[MetricType.NUM_THREADS] > 0
    assert c.last_unavailable == {MetricType.MEMORY_USS: reason}
    if sys.platform == "win32":
        assert c.last_source == 'snapshot'


def test_snapshot_path_nonexistent_pid_returns_none():
    from core.snapshot_hub import SnapshotHub
    c = ProcessCollector(99999999, hub=SnapshotHub(), max_age_s=0.0)
    assert c.collect_metrics([MetricType.MEMORY_PRIVATE]) is None


@win_only
def test_snapshot_path_real_process_collects_all_keys():
    """真实进程、真实快照：全部键要么采到、要么记了原因，且快照指标确实来自快照"""
    from core.process_collector import SAMPLERS
    from core.snapshot_hub import SnapshotHub
    c = ProcessCollector(os.getpid(), hub=SnapshotHub(), max_age_s=0.0)
    values = c.collect_metrics(list(SAMPLERS))
    assert values is not None and c.last_source == 'snapshot'
    assert set(values) | set(c.last_unavailable) == set(SAMPLERS)
    assert values[MetricType.MEMORY_PRIVATE] > 0
    assert values[MetricType.NUM_THREADS] >= 1