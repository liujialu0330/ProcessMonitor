"""
StageSnapshotWriter：真实库 + 同步执行的线程替身（不依赖跨线程信号投递）。
- 在目录里写出报告图片与摘要 CSV 两个文件（不写明细 CSV），命名
  <进程名>_<task_id 前 8 位>_阶段<序号两位>_<yyyyMMdd-HHmm>，目录不存在时先建；
- 写失败只经 failed 提示一次，之后只记日志；导出登记始终配对注销；
- 同一任务上一次还没写完时跳过本次，别的任务不受影响；
- 线程登记到线程登记表。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtGui import QColor, QImage

from core.automation.stage_snapshot import (
    ERR_MAINTENANCE, ERR_NO_DATABASE, ERR_NO_TASK, THREAD_NAME, StageAnalysisWorker,
    StageSnapshotWriter, range_text, snapshot_basename,
)
from core.report import METADATA_PREFIX, SUMMARY_HEADER
from core.report_worker import ReportWorker
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from utils.metrics import MetricType

BASE = datetime(2026, 11, 7, 9, 0, 0)
NOW = datetime(2026, 11, 7, 15, 30, 42)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES


class _Manager:
    """只有导出登记与维护状态的 manager 替身。"""

    def __init__(self):
        self.exports = 0
        self.maintenance = False

    def is_maintenance_running(self):
        return self.maintenance

    def begin_export(self):
        if self.maintenance:
            return False
        self.exports += 1
        return True

    def end_export(self):
        self.exports -= 1


class _InlineAnalysis(StageAnalysisWorker):
    """真实读库逻辑，但在调用线程里同步执行。"""

    def start(self):
        self.run()
        self.finished.emit()


class _InlineReport(ReportWorker):
    def start(self):
        self.run()
        self.finished.emit()


class _HeldAnalysis(StageAnalysisWorker):
    """start() 后挂起，release() 才执行：模拟"上一次还没写完"。"""
    held = []

    def start(self):
        _HeldAnalysis.held.append(self)

    def release(self):
        self.run()
        self.finished.emit()


class _Threads:
    def __init__(self):
        self.names = []

    def register(self, thread, *, name, cancel=None, timeout_ms=0):
        self.names.append(name)


def _image():
    image = QImage(40, 20, QImage.Format_ARGB32)
    image.fill(QColor("#336699"))
    return image


def _seed(db, name="demo.exe"):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=4321, process_name=name,
                       metric_types=[MEM, HANDLES], interval=1.0, start_time=BASE,
                       end_time=None, status="running", note="浸泡")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=s), 100.0 + s, m)
                         for s in range(60) for m in task.metric_types])
    db.add_marker(task.task_id, BASE + timedelta(seconds=10), "基线")
    db.add_marker(task.task_id, BASE + timedelta(seconds=30), "第 1 轮")
    return task


@pytest.fixture
def out(tmp_path):
    """快照目录（与测试库所在的 tmp_path 分开；不预先创建，由写入器建）"""
    return tmp_path / "out"


@pytest.fixture
def manager():
    return _Manager()


@pytest.fixture
def renders():
    return []


def _writer(db, manager, renders, *, renderer=..., threads=None,
            analysis=_InlineAnalysis):
    if renderer is ...:
        def renderer(task, analysis_result, at, range_desc):
            renders.append((task.task_id, sorted(analysis_result.snapshots), at, range_desc))
            return _image()
    writer = StageSnapshotWriter(db, manager, threads, FakeClock(NOW), renderer=renderer,
                                 analysis_worker_factory=analysis,
                                 report_worker_factory=_InlineReport)
    writer.done, writer.errors = [], []
    writer.written.connect(lambda tid, paths: writer.done.append((tid, paths)))
    writer.failed.connect(lambda tid, message: writer.errors.append((tid, message)))
    return writer


def test_writes_image_and_summary_with_stage_names(qapp, db, manager, renders, out):
    task = _seed(db)
    target = out / "快照" / "子目录"            # 不存在：先建目录
    writer = _writer(db, manager, renders)

    assert writer.write(task.task_id, str(target), 3) is True

    base = f"demo_{task.task_id[:8]}_阶段03_20261107-1530"
    assert sorted(os.listdir(target)) == [base + ".csv", base + ".png"]
    image, summary = str(target / (base + ".png")), str(target / (base + ".csv"))
    assert writer.done == [(task.task_id, (image, summary))]
    assert writer.errors == [] and manager.exports == 0 and not writer.is_busy(task.task_id)
    assert QImage(image).size() == _image().size()
    assert renders == [(task.task_id, sorted([MEM, HANDLES]), NOW, range_text(3))]

    with open(summary, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    metadata = [line for line in lines if line.startswith(METADATA_PREFIX)]
    assert f"{METADATA_PREFIX}范围: {range_text(3)}" in metadata
    assert f"{METADATA_PREFIX}结束: 进行中" in metadata
    rows = list(csv.reader(lines[len(metadata):]))
    assert rows[0] == list(SUMMARY_HEADER)
    assert {row[0] for row in rows[1:]} == {"私有字节", "句柄数"}
    assert any(row[2] == "基线 → 第 1 轮" for row in rows[1:])


def test_basename_strips_exe_and_invalid_chars():
    task = MonitorTask(task_id="0123456789abcdef", pid=1, process_name='a:b?.EXE',
                       metric_types=[], interval=1.0, start_time=BASE, end_time=None,
                       status="running")
    assert snapshot_basename(task, 12, NOW) == "a_b__01234567_阶段12_20261107-1530"


def test_failure_is_reported_only_once(qapp, db, manager, renders, out):
    task = _seed(db)

    def broken(*_args):
        raise RuntimeError("显卡不可用")

    writer = _writer(db, manager, renders, renderer=broken)
    writer.write(task.task_id, str(out), 1)
    writer.write(task.task_id, str(out), 2)

    assert len(writer.errors) == 1
    assert writer.errors[0][0] == task.task_id and "显卡不可用" in writer.errors[0][1]
    assert writer.has_reported_failure(task.task_id)
    assert writer.done == [] and os.listdir(out) == []
    assert manager.exports == 0 and not writer.is_busy(task.task_id)


def test_failures_of_different_kinds_still_reported_once(qapp, db, manager, renders, out):
    task = _seed(db)
    out.mkdir()
    blocker = out / "是文件"
    blocker.write_text("x", encoding="utf-8")
    writer = _writer(db, manager, renders)

    writer.write(task.task_id, str(blocker / "快照"), 1)     # 目录建不出来
    manager.maintenance = True
    writer.write(task.task_id, str(out), 2)             # 正在清理数据库
    manager.maintenance = False
    writer.write("no-such-task", str(out), 1)           # 另一任务：照常提示

    assert [tid for tid, _m in writer.errors] == [task.task_id, "no-such-task"]
    assert "无法创建快照目录" in writer.errors[0][1]
    assert writer.errors[1][1] == ERR_NO_TASK
    assert manager.exports == 0


def test_success_after_reported_failure_still_writes(qapp, db, manager, renders, out):
    task = _seed(db)
    manager.maintenance = True
    writer = _writer(db, manager, renders)
    writer.write(task.task_id, str(out), 1)
    assert writer.errors == [(task.task_id, ERR_MAINTENANCE)]
    manager.maintenance = False
    assert writer.write(task.task_id, str(out), 2) is True
    assert len(writer.done) == 1 and len(os.listdir(out)) == 2


def test_busy_task_skips_new_request(qapp, db, manager, renders, out):
    _HeldAnalysis.held = []
    first, second = _seed(db), _seed(db, "other.exe")
    writer = _writer(db, manager, renders, analysis=_HeldAnalysis)

    assert writer.write(first.task_id, str(out), 1) is True
    assert writer.is_busy(first.task_id)
    assert writer.write(first.task_id, str(out), 2) is False   # 上一次还在写：跳过
    assert writer.write(second.task_id, str(out), 1) is True   # 别的任务不受影响
    assert len(_HeldAnalysis.held) == 2 and manager.exports == 2
    assert writer.errors == []                                      # 跳过不算失败

    for worker in list(_HeldAnalysis.held):
        worker.release()
    assert not writer.is_busy(first.task_id) and manager.exports == 0
    assert len(writer.done) == 2
    assert writer.write(first.task_id, str(out), 3) is True    # 写完后可以再写


def test_without_database_fails_without_thread(qapp, manager, renders, out):
    writer = _writer(None, manager, renders)
    assert writer.write("t", str(out), 1) is False
    assert writer.errors == [("t", ERR_NO_DATABASE)] and manager.exports == 0


def test_without_renderer_writes_summary_only(qapp, db, manager, renders, out):
    task = _seed(db)
    writer = _writer(db, manager, renders, renderer=None)
    writer.write(task.task_id, str(out), 1)
    assert [os.path.splitext(name)[1] for name in os.listdir(out)] == [".csv"]
    assert writer.errors == [] and manager.exports == 0


def test_threads_are_registered(qapp, db, manager, renders, out):
    task = _seed(db)
    threads = _Threads()
    writer = _writer(db, manager, renders, threads=threads)
    writer.write(task.task_id, str(out), 1)
    assert threads.names == [THREAD_NAME, THREAD_NAME]     # 读库段 + 写文件段
    assert len(writer.done) == 1
