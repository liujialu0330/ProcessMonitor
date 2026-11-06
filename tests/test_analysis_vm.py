"""
分析页视图模型：多指标快照合并、焦点切换不查库、自定义范围、任务对比、导出命令、
打点编辑命令。查询执行器用同步/手动替身，后台线程见 test_analysis_page_async.py。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta

import pytest

from core.history_query import CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY, TaskAnalysis
from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_services
from tests.helpers import ManualHistoryRunner, SyncHistoryRunner, process_events_until
from ui.viewmodels.analysis_vm import (
    COMPARE_MISSING_METRIC, EXPORT_BUSY, EXPORT_MAINTENANCE, AnalysisViewModel,
    analysis_origin, range_text)
from ui.viewmodels.history_vm import EMPTY_LOAD_FAILED
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS


def seed(db, name="demo.exe", start=BASE, metrics=(MEM, HANDLES), samples=120,
         slope=10.0, markers=()):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=list(metrics),
        interval=1.0, start_time=start, end_time=start + timedelta(seconds=samples),
        status="stopped", sample_count=samples,
        last_sample_at=start + timedelta(seconds=samples - 1))
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1000.0 + i * slope * (k + 1), m)
        for i in range(samples) for k, m in enumerate(metrics)])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


class Recorder:
    def __init__(self, vm):
        self.analyses, self.snapshots, self.empty, self.errors = [], [], [], []
        self.custom, self.compares, self.resets = [], [], []
        vm.analysis_ready.connect(self.analyses.append)
        vm.snapshot_ready.connect(self.snapshots.append)
        vm.empty_state.connect(lambda t, h: self.empty.append((t, h)))
        vm.error.connect(lambda t, c: self.errors.append((t, c)))
        vm.custom_range_changed.connect(lambda a, b: self.custom.append((a, b)))
        vm.compare_ready.connect(self.compares.append)
        vm.range_reset.connect(self.resets.append)


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=3)))


def make_vm(db, manager, services, runner=None):
    return AnalysisViewModel(db, manager, services,
                             worker_runner=runner or SyncHistoryRunner(db),
                             defer=lambda fn: fn())


def test_one_query_per_metric_merged_into_task_analysis(qapp, db, manager, services):
    task = seed(db, markers=((20, "第 1 轮"), (60, "第 2 轮")))
    runner = SyncHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()

    assert [r.metric for r in runner.requests] == [MEM, HANDLES]
    assert all(r.analysis and r.task_id == task.task_id for r in runner.requests)
    # 执行器按请求序号区分在途请求：同一批的子请求序号互不相同
    assert len({r.request_seq for r in runner.requests}) == 2
    assert len(rec.analyses) == 1
    analysis = rec.analyses[0]
    assert isinstance(analysis, TaskAnalysis)
    assert analysis.metrics == [MEM, HANDLES] and analysis.has_data
    for snapshot in analysis.snapshots.values():
        assert snapshot.band and snapshot.intervals
    # 焦点快照随后发出，且就是分析结果里焦点指标那一份
    assert rec.snapshots[-1] is analysis.snapshot(MEM)
    assert vm.last_analysis is analysis and vm.current_metric == MEM
    assert not vm.is_busy and not vm.has_pending_query


def test_switching_focus_metric_reuses_results(qapp, db, manager, services):
    seed(db)
    runner = SyncHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    count = len(runner.requests)

    vm.select_metric(HANDLES)

    assert len(runner.requests) == count
    assert rec.snapshots[-1] is vm.last_analysis.snapshot(HANDLES)
    assert vm.current_metric == HANDLES


def test_batch_applies_only_after_all_parts_and_drops_stale(qapp, db, manager, services):
    seed(db)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    assert len(runner.pending) == 2 and vm.is_busy

    runner.complete(0)
    assert rec.analyses == [] and vm.has_pending_query
    # 新加载开始：上一批剩下的部分回来也不应用
    vm.refresh()
    runner.complete(0)
    assert rec.analyses == []
    runner.complete_all()
    assert len(rec.analyses) == 1 and not vm.is_busy


def test_any_part_failure_fails_whole_batch(qapp, db, manager, services):
    seed(db)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    runner.complete(0)
    runner.fail("disk image is malformed", 0)

    assert rec.analyses == []
    assert rec.empty[-1] == EMPTY_LOAD_FAILED
    assert rec.errors and "malformed" in rec.errors[-1][1]
    assert vm.last_analysis is None


def test_custom_range_queries_with_until_and_clears(qapp, db, manager, services):
    seed(db)
    runner = SyncHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    start, end = BASE + timedelta(seconds=30), BASE + timedelta(seconds=60)

    assert vm.set_custom_range(end, start)   # 反向拖动也按起止排序

    assert vm.range_key == CUSTOM_RANGE_KEY and vm.custom_range == (start, end)
    assert rec.custom[-1] == (start, end)
    last = runner.requests[-2:]
    assert all(r.range_key == CUSTOM_RANGE_KEY and r.since == start and r.until == end
               for r in last)
    snapshot = rec.snapshots[-1]
    assert snapshot.points[0].timestamp == start and snapshot.points[-1].timestamp == end
    assert snapshot.total_count == 31
    assert "自定义" in vm.range_description()

    # 过短的选区视为误触
    assert not vm.set_custom_range(start, start + timedelta(seconds=1))
    assert vm.custom_range == (start, end)

    vm.set_range('1h')
    assert vm.custom_range is None and rec.custom[-1] == (None, None)
    assert runner.requests[-1].range_key == '1h' and runner.requests[-1].until is None
    with pytest.raises(ValueError):
        vm.set_range(CUSTOM_RANGE_KEY)


def test_switching_task_resets_custom_range(qapp, db, manager, services):
    old = seed(db, name="old.exe", start=BASE - timedelta(days=1))
    seed(db, name="new.exe")
    vm = make_vm(db, manager, services)
    rec = Recorder(vm)
    vm.reload_tasks()
    vm.set_custom_range(BASE + timedelta(seconds=10), BASE + timedelta(seconds=50))

    vm.select_task(old.task_id)

    assert vm.range_key == DEFAULT_RANGE_KEY and vm.custom_range is None
    assert rec.resets[-1] == DEFAULT_RANGE_KEY and rec.custom[-1] == (None, None)
    assert rec.snapshots[-1].request.task_id == old.task_id


def test_view_and_time_modes(qapp, db, manager, services):
    vm = make_vm(db, manager, services)
    modes = []
    vm.view_mode_changed.connect(modes.append)
    vm.time_mode_changed.connect(modes.append)
    vm.set_view_mode('overlay')
    vm.set_view_mode('overlay')
    vm.set_time_mode('relative')
    assert modes == ['overlay', 'relative']
    with pytest.raises(ValueError):
        vm.set_view_mode('grid')
    with pytest.raises(ValueError):
        vm.set_time_mode('utc')


def test_compare_pairs_focus_metric_and_rounds(qapp, db, manager, services):
    old = seed(db, name="old.exe", start=BASE - timedelta(days=1), slope=5.0,
               markers=((10, "第 1 轮"), (40, "第 2 轮"), (80, "第 3 轮")))
    seed(db, name="new.exe", markers=((10, "第 1 轮"), (40, "第 2 轮")))
    vm = make_vm(db, manager, services)
    rec = Recorder(vm)
    vm.reload_tasks()
    assert [t.task_id for t in vm.compare_candidates()] == [old.task_id]

    vm.set_compare_task(old.task_id)

    result = rec.compares[-1]
    assert result.metric == MEM and result.other is not None
    assert result.other.request.task_id == old.task_id
    assert [r.label for r in result.other.rounds] == ["第 2 轮", "第 3 轮"]
    assert result.primary is vm.last_snapshot

    # 切焦点指标：对比随之换成该指标
    vm.select_metric(HANDLES)
    assert rec.compares[-1].metric == HANDLES

    vm.set_compare_task(None)
    assert rec.compares[-1] is None and vm.last_compare is None


def test_compare_task_without_metric_explains(qapp, db, manager, services):
    seed(db, name="other.exe", start=BASE - timedelta(days=1), metrics=(GDI,))
    seed(db, name="new.exe")
    vm = make_vm(db, manager, services)
    rec = Recorder(vm)
    vm.reload_tasks()
    vm.set_compare_task(vm.compare_candidates()[0].task_id)
    assert rec.compares[-1].other is None
    assert rec.compares[-1].message == COMPARE_MISSING_METRIC


def test_marker_commands_go_through_manager_and_requery(qapp, db, manager, services):
    task = seed(db)
    runner = SyncHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    count = len(runner.requests)

    assert vm.next_marker_label() == "基线"
    assert vm.add_marker_at(BASE + timedelta(seconds=30), "预热完成", "基线")
    markers = manager.list_markers(task.task_id)
    assert [(m.label, m.kind) for m in markers] == [("预热完成", "baseline")]
    # markers_changed → 静默重查（每个指标一个请求）
    assert len(runner.requests) == count + 2
    assert vm.last_snapshot.baseline is not None

    assert vm.add_marker_at(BASE + timedelta(seconds=60), "第 1 轮", vm.next_marker_label())
    user = [m for m in manager.list_markers(task.task_id) if m.kind == 'user'][0]
    assert vm.rename_marker(user, "第一轮")
    assert vm.set_baseline_marker(user)
    kinds = {m.label: m.kind for m in manager.list_markers(task.task_id)}
    assert kinds == {"预热完成": "user", "第一轮": "baseline"}
    assert vm.delete_marker(user)
    assert [m.label for m in manager.list_markers(task.task_id)] == ["预热完成"]


def test_export_command_writes_files_and_unregisters(qapp, db, manager, services, tmp_path):
    task = seed(db, markers=((30, "第 1 轮"), (60, "第 2 轮")))
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    busy, finished = [], []
    vm.export_busy.connect(busy.append)
    vm.export_finished.connect(finished.append)
    csv_path = str(tmp_path / "out.csv")
    summary_path = str(tmp_path / "out_摘要.csv")

    job = vm.report_job(csv_path=csv_path, summary_path=summary_path)
    assert job.metadata[0].startswith("# 进程: demo.exe")
    assert vm.start_export(job) is None
    assert manager.has_running_exports()
    assert vm.start_export(job) == EXPORT_BUSY
    assert process_events_until(lambda: services.threads.entries() == [] and finished,
                                timeout=10)

    assert busy == [True, False] and vm.export_worker is None
    assert not manager.has_running_exports()
    assert finished[0].paths == (csv_path, summary_path) and finished[0].row_count == 120
    with open(csv_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    assert lines[0].startswith("# ")
    header = next(row for row in csv.reader(lines) if row and not row[0].startswith("#"))
    assert header[:2] == ["时间", "已运行(秒)"]
    assert os.path.exists(summary_path)
    assert task.task_id   # 任务仍在


def test_export_refused_during_maintenance(qapp, db, manager, services, tmp_path):
    seed(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    assert manager.begin_maintenance()
    try:
        assert vm.start_export(vm.report_job(csv_path=str(tmp_path / "x.csv"))) == \
            EXPORT_MAINTENANCE
        assert vm.is_maintenance_running()
    finally:
        manager.end_maintenance()
    assert services.threads.entries() == []


def test_range_text_and_origin(qapp, db, manager, services):
    assert range_text('all') == "全部"
    assert range_text('1h') == "最近 1 小时"
    assert range_text('baseline', baseline_at=BASE) == "自基线（16:00:00 起）"
    assert range_text(CUSTOM_RANGE_KEY, (BASE, BASE + timedelta(minutes=5))) == \
        "自定义 07-16 16:00:00 – 07-16 16:05:00"
    seed(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    assert analysis_origin(vm.last_snapshot) == BASE
    assert analysis_origin(None) is None
