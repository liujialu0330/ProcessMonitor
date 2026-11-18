"""
分析页视图模型：多指标快照合并、焦点切换不查库、自定义范围、任务对比、导出命令、
打点编辑命令。查询执行器用同步/手动替身，后台线程见 test_analysis_page_async.py。
"""
import csv
import os
import uuid
from datetime import datetime, timedelta

import pytest

from core.history_query import (
    CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY, HistorySnapshot, TaskAnalysis, build_request,
    run_history_query)
from core.monitor_manager import MonitorManager
from core.report_worker import ReportWorker
from core.workers.base import CancelToken
from data.errors import StorageError
from data.models import DataPoint, Marker, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_services
from tests.helpers import ManualHistoryRunner, SyncHistoryRunner, process_events_until
from ui.viewmodels.analysis_vm import (
    COMPARE_MISSING_METRIC, EXPORT_BUSY, EXPORT_MAINTENANCE, AnalysisViewModel,
    analysis_origin, range_text)
from ui.viewmodels.history_vm import EMPTY_LOAD_FAILED, ERROR_LOAD_FAILED
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
GDI = MetricType.NUM_GDI_OBJECTS


def seed(db, name="demo.exe", start=BASE, metrics=(MEM, HANDLES), samples=120,
         slope=10.0, markers=(), *, target_kind='pid'):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=list(metrics),
        interval=1.0, start_time=start, end_time=start + timedelta(seconds=samples),
        status="stopped", sample_count=samples, target_kind=target_kind,
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
    assert runner.requests[-1].range_key == '1h'
    assert runner.requests[-1].until == runner.requests[-1].anchor
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


def test_renaming_finished_comparison_refreshes_rounds_and_drops_inflight_old_label(
        qapp, db, manager, services):
    other = seed(db, name="old.exe", start=BASE - timedelta(days=1), metrics=(MEM,),
                 markers=((10, "准备"), (20, "操作")))
    task = seed(db, metrics=(MEM,))
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    primary = vm.last_analysis
    vm.set_compare_task(other.task_id)
    runner.complete_all()
    assert vm.last_compare.other.rounds[0].label == "操作"

    marker = db.markers.list(other.task_id)[1]
    assert manager.rename_marker(marker.id, "打开窗口", other.task_id)
    assert vm.last_compare is None
    request, deliver = runner.pending.pop()
    previous = run_history_query(db, request)
    assert manager.rename_marker(marker.id, "关闭窗口", other.task_id)
    deliver(previous)
    assert vm.last_compare is None
    runner.complete_all()

    assert vm.current_task_id == task.task_id and vm.last_analysis is primary
    assert vm.compare_task_ids == (other.task_id,)
    assert vm.last_compare.other.rounds[0].label == "关闭窗口"


def test_renamed_comparison_task_is_reread_when_primary_recovers(qapp, db, manager, services):
    other = seed(db, start=BASE - timedelta(days=1), metrics=(MEM,),
                 markers=((5, "准备"), (15, "旧轮次")))
    task = seed(db, metrics=(MEM,), markers=((5, "原主点"),))
    task.status, task.end_time = 'running', None
    db.save_task(task)
    services.task_status.set_running(task.task_id)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    rec = Recorder(vm)
    vm.reload_tasks()
    runner.complete_all()
    vm.set_compare_task(other.task_id)
    runner.complete_all()
    assert rec.compares[-1].other.rounds[0].label == "旧轮次"

    main_marker = vm.last_analysis.markers[0]
    other_marker = vm.last_compare.other.markers[-1]
    assert manager.rename_marker(main_marker.id, "新主点", task.task_id)
    assert vm.last_compare is None and rec.compares[-1] is None
    assert manager.rename_marker(other_marker.id, "第一次改名", other.task_id)
    runner.complete_all()
    assert vm.last_compare.primary.markers[0].label == "新主点"
    assert vm.last_compare.other.rounds[0].label == "第一次改名"

    vm.refresh()
    before = len(runner.requests)
    assert manager.rename_marker(other_marker.id, "刷新中的改名", other.task_id)
    assert len(runner.requests) == before and vm.last_compare is None
    runner.complete_all()
    assert len(runner.requests) == before + 1
    assert vm.last_compare.other.rounds[0].label == "刷新中的改名"

    vm.refresh()
    runner.fail("主任务读取失败")
    assert vm.last_analysis is None
    assert manager.rename_marker(other_marker.id, "最终轮次", other.task_id)
    assert vm.last_compare is None and rec.compares[-1] is None
    assert not runner.pending

    before = len(runner.requests)
    vm.on_tick()
    runner.complete_all()
    assert [r.task_id for r in runner.requests[before:]] == [task.task_id, other.task_id]
    assert vm.current_task_id == task.task_id and vm.compare_task_ids == (other.task_id,)
    assert vm.last_compare.other.rounds[0].label == "最终轮次"


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


def test_rename_rejects_foreign_system_and_unsaved_markers(qapp, db, manager, services):
    other = seed(db, start=BASE - timedelta(days=1), markers=((15, "另一任务"),))
    task = seed(db)
    pause_id = db.markers.add(task.task_id, BASE, "暂停", 'pause')
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    snapshot = vm.export_analysis()

    assert not vm.rename_marker(db.markers.list(other.task_id)[0], "误改")
    assert not vm.rename_marker(Marker(task.task_id, BASE, "暂停", pause_id, 'pause'), "误改")
    assert not vm.rename_marker(Marker(task.task_id, BASE, "未保存"), "误改")
    assert [m.label for m in db.markers.list(other.task_id)] == ["另一任务"]
    assert [m.label for m in db.markers.list(task.task_id)] == ["暂停"]
    assert vm.export_analysis() is snapshot


@pytest.mark.parametrize('member_error, aggregate_index, expected_storage', [
    (RuntimeError("成员计算失败"), None, False),
    (StorageError("成员读取失败"), None, True),
    (StorageError("成员读取失败"), 0, False),
    (RuntimeError("成员计算失败"), 1, True),
])
def test_rename_refresh_keeps_failure_category_and_prioritizes_aggregate(
        qapp, db, manager, services, monkeypatch, member_error, aggregate_index, expected_storage):
    task = seed(db, markers=((5, "原名"),), target_kind='tree')
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    assert vm.is_group_task and vm.current_task_id == task.task_id
    rec = Recorder(vm)
    seen = []
    apply = vm._apply_snapshot

    def record_result(snapshot, forced=False):
        seen.append((snapshot.error, snapshot.storage_error))
        apply(snapshot, forced)

    def fail_members(_task_id):
        raise member_error

    monkeypatch.setattr(vm, '_apply_snapshot', record_result)
    monkeypatch.setattr(db.members, 'member_series', fail_members)
    assert vm.rename_marker(vm.last_analysis.markers[0], "新名")
    message = str(member_error)
    if aggregate_index is not None:
        message = "合计指标读取失败" if expected_storage else "合计指标计算失败"
        runner.fail(message, index=aggregate_index, storage_error=expected_storage)
    runner.complete_all()

    assert seen == [(message, expected_storage)]
    assert len(rec.errors) == 1 and message in rec.errors[0][1]
    assert vm.export_analysis() is None


@pytest.mark.parametrize(('retry_succeeds', 'refresh_in_progress'),
                         [(True, False), (False, False), (True, True)])
def test_stopped_task_rename_refresh_notifies_and_retries_only_once(
        qapp, db, manager, services, retry_succeeds, refresh_in_progress):
    task = seed(db, metrics=(MEM,), markers=((5, "原名"),))
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    rec = Recorder(vm)
    assert not services.task_status.is_running(task.task_id)
    if refresh_in_progress:
        vm.refresh()
    assert vm.rename_marker(vm.last_analysis.markers[0], "新名")
    if refresh_in_progress:
        runner.complete()  # 已被改名取代的手动刷新先返回
    runner.fail("趋势计算出错", storage_error=False)
    assert db.markers.list(task.task_id)[0].label == "新名"
    assert vm.last_analysis.markers[0].label == "原名" and vm.report_job() is None
    assert len(rec.errors) == 1
    title, detail = rec.errors[0]
    assert "刷新失败" in title and "趋势计算出错" in detail and "重试" in detail

    before = len(runner.requests)
    vm.on_tick()
    vm.on_tick()
    assert len(runner.requests) == before + 1 and len(runner.pending) == 1
    if retry_succeeds:
        runner.complete()
    else:
        runner.fail("仍然无法计算", storage_error=False)
        assert vm.report_job() is None
    vm.on_tick()
    vm.on_tick()
    assert len(runner.requests) == before + 1
    if not retry_succeeds:
        vm.refresh()
        runner.complete_all()
    assert vm.report_job().markers[0].label == "新名"


@pytest.mark.parametrize('preset', [True, False], ids=['preset', 'different-custom-bounds'])
@pytest.mark.parametrize('edit_again', [False, True], ids=['range-load', 'merged-marker-update'])
def test_range_load_failure_after_marker_edit_reports_the_new_query(
        qapp, db, manager, services, preset, edit_again):
    task = seed(db, metrics=(MEM,), markers=((5, "原名"),))
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    if not preset:
        vm.set_custom_range(BASE, BASE + timedelta(seconds=40))
        runner.complete_all()
    rec = Recorder(vm)
    marker = vm.last_analysis.markers[0]
    assert vm.rename_marker(marker, "新名")
    if preset:
        vm.set_range('10m')
    else:
        vm.set_custom_range(BASE + timedelta(seconds=10), BASE + timedelta(seconds=50))
    if edit_again:
        assert vm.rename_marker(marker, "再次改名")

    runner.fail("选区加载失败", index=-1)
    runner.complete_all()  # 先前请求的迟到结果不能覆盖失败状态。
    assert not vm.is_busy and vm.last_snapshot is None and vm.last_analysis is None
    assert rec.empty == [EMPTY_LOAD_FAILED]
    assert rec.errors == [(ERROR_LOAD_FAILED, "选区加载失败")]
    before = len(runner.requests)
    vm.on_tick()
    assert len(runner.requests) == before
    assert db.markers.list(task.task_id)[0].label == ("再次改名" if edit_again else "新名")


def test_focus_load_failure_after_a_marker_edit_reports_the_new_query(
        qapp, db, manager, services):
    task = seed(db, markers=((5, "原名"),))
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    assert vm.last_snapshot.request.metric == MEM

    rec = Recorder(vm)
    vm.refresh()
    vm.select_metric(HANDLES)
    assert vm.rename_marker(vm.last_analysis.markers[0], "新名")
    runner.fail("新指标读取失败", index=-1)
    runner.complete_all()

    assert not vm.is_busy and vm.current_metric == HANDLES
    assert vm.last_snapshot is None and vm.last_analysis is None
    assert rec.empty == [EMPTY_LOAD_FAILED]
    assert rec.errors == [(ERROR_LOAD_FAILED, "新指标读取失败")]
    before = len(runner.requests)
    vm.on_tick()
    assert len(runner.requests) == before
    assert db.markers.list(task.task_id)[0].label == "新名"


def test_switching_tasks_discards_the_previous_marker_retry(qapp, db, manager, services):
    other = seed(db, start=BASE - timedelta(days=1), metrics=(MEM,))
    task = seed(db, metrics=(MEM,), markers=((5, "原名"),))
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    assert vm.rename_marker(vm.last_analysis.markers[0], "新名")
    runner.fail("打点刷新失败")

    vm.select_task(other.task_id)
    vm.select_task(task.task_id)
    rec = Recorder(vm)
    runner.fail("任务加载失败", index=-1)
    runner.complete_all()
    assert rec.empty == [EMPTY_LOAD_FAILED]
    assert rec.errors == [(ERROR_LOAD_FAILED, "任务加载失败")]
    before = len(runner.requests)
    vm.on_tick()
    assert len(runner.requests) == before


def test_task_analysis_uses_markers_from_first_successful_snapshot(db):
    task = seed(db, markers=((5, "真实打点"),))
    marker = db.markers.list(task.task_id)[0]
    vm_snapshots = {metric: run_history_query(db, build_request(task.task_id, metric, 'all'))
                    for metric in task.metric_types}
    failed = HistorySnapshot(vm_snapshots[MEM].request, error="读库失败")
    assert TaskAnalysis(task.task_id, 'all', {MEM: failed}).markers == []
    assert TaskAnalysis(task.task_id, 'all', {
        MEM: failed, HANDLES: vm_snapshots[HANDLES],
    }).markers == [marker]


def test_rename_freezes_started_export_and_waits_for_successful_refresh(
        qapp, db, manager, services, tmp_path):
    task = seed(db, metrics=(MEM,), markers=((15, "旧名称"),))
    task.status, task.end_time = 'running', None
    db.save_task(task)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    previous = vm.report_job(csv_path=str(tmp_path / "before.csv"),
                             summary_path=str(tmp_path / "before-summary.csv"))

    marker = vm.export_analysis().markers[0]
    # 监控卡也走管理器改名；分析页应同时失效旧快照。
    assert manager.rename_marker(marker.id, "新名称", task.task_id)
    assert vm.export_analysis() is None and vm.report_job() is None
    runner.fail("读取失败")
    assert vm.report_job() is None
    vm.refresh()
    runner.complete_all()
    current = vm.report_job(csv_path=str(tmp_path / "after.csv"),
                            summary_path=str(tmp_path / "after-summary.csv"))

    for job, label in ((previous, "旧名称"), (current, "新名称")):
        assert ReportWorker(db, job).execute(CancelToken()).row_count == 120
        with open(job.csv_path, encoding='utf-8-sig') as detail:
            rows = [row for row in csv.reader(detail) if row and row[0][:4] == "2026"]
        assert [row[-1] for row in rows if row[-1]] == [label]
        with open(job.summary_path, encoding='utf-8-sig') as summary:
            rows = [row for row in csv.reader(summary) if row and row[0] == "私有字节"]
        assert [row[2] for row in rows] == [f"开始 → {label}", f"{label} → 结束"]


def test_switch_task_while_rename_refresh_is_pending(qapp, db, manager, services):
    other = seed(db, start=BASE - timedelta(days=1), metrics=(MEM,))
    task = seed(db, metrics=(MEM,), markers=((15, "旧名称"),))
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    assert vm.rename_marker(vm.export_analysis().markers[0], "新名称")
    assert vm.select_task(other.task_id) and vm.report_job() is None
    runner.complete_all()
    assert vm.report_job().task.task_id == other.task_id
    assert vm.select_task(task.task_id) and vm.report_job() is None
    runner.complete_all()
    assert [m.label for m in vm.report_job().markers] == ["新名称"]


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


def test_report_job_uses_current_analysis_snapshot_not_stale_running_task_count(
        qapp, db, manager, services):
    task = seed(db, samples=120)
    task.status, task.end_time = 'running', None
    db.save_task(task)
    manager.add_marker(task.task_id, "基线", BASE + timedelta(seconds=20), kind='baseline')
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_range('baseline')

    # 当前任务列表仍是120次采样；分析完成后数据库增加新点，不重新加载分析快照。
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=120), 3000.0, MEM),
        DataPoint(task.task_id, BASE + timedelta(seconds=120), 4000.0, HANDLES),
    ])
    job = vm.report_job(csv_path='ignored.csv')

    assert job.data_since == BASE + timedelta(seconds=20)
    assert job.data_until == BASE + timedelta(seconds=119)
    assert "# 采集次数: 100" in job.metadata
    assert "# 结束: 2026-07-16 16:01:59" in job.metadata


def test_analysis_batch_retries_at_common_cutoff_when_running_data_changes(
        qapp, db, manager, services):
    task = seed(db, samples=20)

    class RacingRunner:
        def __init__(self):
            self.requests = []

        def __call__(self, request, on_done):
            index = len(self.requests)
            self.requests.append(request)
            on_done(run_history_query(db, request))
            if index == 0:
                db.save_data_points([
                    DataPoint(task.task_id, BASE + timedelta(seconds=20), 2000.0, MEM),
                    DataPoint(task.task_id, BASE + timedelta(seconds=20), 3000.0, HANDLES),
                ])

    runner = RacingRunner()
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()

    # 初次两指标读到不同任务末尾时，视图模型以较早末尾重读整批；不会发布混合快照。
    assert len(runner.requests) == 4
    assert all(snapshot.request.until == BASE + timedelta(seconds=19)
               for snapshot in vm.last_analysis.snapshots.values())
    assert all(snapshot.total_count == 20 for snapshot in vm.last_analysis.snapshots.values())


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


# ---------------------------------------------------------------- 排序与折叠口径（纯函数）


def _judged(metric, verdict=None, slope_per_hour=0.0, first=100.0, stats=True):
    """合成快照：verdict 为 None 时不做趋势判定（累计量 / 峰值类）。"""
    from core.analysis.trend import Judgement, LinearFit
    from core.history_query import HistorySnapshot, build_request
    judgement = None
    if verdict is not None:
        judgement = Judgement(verdict, LinearFit(slope_per_hour / 3600.0, first, 0.9, 0.0),
                              'x', 0.5)
    stat = {'count': 100, 'first': first, 'last': first, 'span_seconds': 1200.0} if stats else None
    return HistorySnapshot(request=build_request("t", metric, 'all'), stats=stat,
                           judgement=judgement)


def test_ordered_metrics_by_verdict_then_relative_rate():
    from core.analysis.trend import Verdict
    from ui.viewmodels.history_vm import is_anomalous, ordered_metrics, relative_rate
    io = MetricType.IO_READ_BYTES
    threads = MetricType.NUM_THREADS
    snapshots = {
        GDI: _judged(GDI, Verdict.STABLE),
        io: _judged(io),                                              # 不判定：排最后
        threads: _judged(threads, stats=False),                       # 没有数据：按数据不足
        HANDLES: _judged(HANDLES, Verdict.RISING, slope_per_hour=120, first=800),   # 15%/时
        MEM: _judged(MEM, Verdict.RISING, slope_per_hour=36000, first=512000),      # 7%/时
        MetricType.NUM_USER_OBJECTS: _judged(MetricType.NUM_USER_OBJECTS, Verdict.FLUCTUATING),
    }
    analysis = TaskAnalysis("t", 'all', snapshots)
    # 同为持续上涨时比相对增速：句柄 120/800 > 私有字节 36000/512000，不比绝对斜率
    assert ordered_metrics(analysis) == [
        HANDLES, MEM, MetricType.NUM_USER_OBJECTS, GDI, threads, io]
    assert relative_rate(snapshots[HANDLES]) == pytest.approx(0.15)
    assert [m for m in snapshots if is_anomalous(snapshots[m])] == [
        HANDLES, MEM, MetricType.NUM_USER_OBJECTS]
    # 起点为 0 的上涨排在同组最前
    zero = _judged(GDI, Verdict.RISING, slope_per_hour=1, first=0.0)
    assert relative_rate(zero) == float('inf')
