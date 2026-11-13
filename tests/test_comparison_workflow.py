"""多任务对比：轮次口径、完整批次、过期保护以及导出原子替换。"""
import csv
import math
from dataclasses import replace
from datetime import timedelta

import pytest

from core.comparison import (ALIGN_ABSOLUTE, ALIGN_RELATIVE, ALIGN_ROUNDS, CompareEntry,
                             curve_for, matrix_cell)
from core.history_query import build_request, run_history_query
from core.workers.base import CancelToken
from core.workers.comparison_worker import ComparisonExportJob, ComparisonExportWorker
from tests.helpers import ManualHistoryRunner, process_events_until
from tests.test_analysis_vm import BASE, GDI, HANDLES, MEM, make_vm, manager, seed, services
from ui.widgets.analysis.compare_panel import ComparePanel, render_comparison_image


def seed_four(db):
    return [seed(db, name=f'app-{i}.exe', start=BASE + timedelta(days=i),
                 slope=i + 1, markers=((10, '基线'), (40, '第 1 轮'), (80, '第 2 轮')))
            for i in range(4)]


def test_four_task_batch_contains_every_metric(qapp, db, manager, services):
    tasks = seed_four(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    result = vm.last_compare
    assert len(result.entries) == 4
    assert set(result.metrics) == {MEM, HANDLES}
    assert all(set(entry.snapshots) == {MEM, HANDLES} for entry in result.entries)
    vm.select_metric(HANDLES)
    assert vm.last_compare.metric == HANDLES
    with pytest.raises(ValueError):
        vm.set_compare_tasks(['1', '2', '3', '4'])


def test_inflight_old_selection_never_publishes(qapp, db, manager, services):
    tasks = seed_four(db)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    emitted = []
    vm.compare_ready.connect(emitted.append)
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    old_count = len(runner.pending)
    runner.complete(-1)
    assert vm.last_compare is None  # 一项完成不能发布半批结果。
    vm.set_compare_tasks([tasks[0].task_id])
    for _ in range(old_count - 1):
        runner.complete(0)
    assert vm.last_compare is None
    runner.complete_all()
    assert [e.task.task_id for e in vm.last_compare.entries] == [tasks[3].task_id, tasks[0].task_id]
    assert len([e for e in emitted if e is not None]) == 1


def test_round_curve_uses_existing_round_deltas_and_keeps_missing_round_gap(db):
    task = seed_four(db)[0]
    snapshot = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    entry = CompareEntry(task, {MEM: snapshot})
    curve = curve_for(entry, MEM, ALIGN_ROUNDS)
    assert curve.xs == (0.0, 1.0, 2.0)
    spans = {(r.start, r.end) for r in snapshot.rounds}
    intervals = [row for row in snapshot.intervals if (row.start, row.end) in spans]
    assert curve.ys[-1] == sum(row.delta for row in intervals)
    assert curve.ys[-1] != sum(r.delta for r in snapshot.rounds)
    broken = replace(snapshot, intervals=[replace(row, delta=None) if row is intervals[0] else row
                                         for row in snapshot.intervals])
    broken_curve = curve_for(CompareEntry(task, {MEM: broken}), MEM, ALIGN_ROUNDS)
    assert math.isnan(broken_curve.ys[1]) and math.isnan(broken_curve.ys[2])


def test_no_rounds_is_explained_not_silently_time_aligned(db):
    task = seed(db)
    snapshot = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    entry = CompareEntry(task, {MEM: snapshot})
    assert not curve_for(entry, MEM, ALIGN_ROUNDS).xs
    assert '相对时间' in curve_for(entry, MEM, ALIGN_ROUNDS).message
    relative = curve_for(entry, MEM, ALIGN_RELATIVE)
    absolute = curve_for(entry, MEM, ALIGN_ABSOLUTE)
    assert relative.ys == absolute.ys
    assert relative.xs[0] < 1 and absolute.xs[0] > 1_000_000_000


def test_matrix_missing_metric_never_becomes_zero(db):
    task = seed(db, metrics=(MEM,))
    cell = matrix_cell(CompareEntry(task), HANDLES)
    assert cell.mean_delta is None and cell.message == '未采集该指标'


def test_export_failure_and_cancel_leave_existing_file(qapp, db, tmp_path, monkeypatch):
    task = seed_four(db)[0]
    snapshot = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    entry = CompareEntry(task, {MEM: snapshot})
    path = tmp_path / 'compare.csv'
    path.write_text('old', encoding='utf-8')
    job = ComparisonExportJob(str(path), (entry,), (MEM,), MEM, ALIGN_ROUNDS)
    token = CancelToken()
    token.cancel()
    assert ComparisonExportWorker(db, job).execute(token) is None
    assert path.read_text(encoding='utf-8') == 'old'
    monkeypatch.setattr('core.workers.comparison_worker.os.replace',
                        lambda *_: (_ for _ in ()).throw(OSError('禁止替换')))
    with pytest.raises(OSError):
        ComparisonExportWorker(db, job).execute(CancelToken())
    assert path.read_text(encoding='utf-8') == 'old'
    assert list(tmp_path.glob('.comparison-*')) == []


def test_export_csv_and_image_cover_all_tasks(qapp, db, manager, services, tmp_path):
    tasks = seed_four(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    result = vm.last_compare
    path = tmp_path / 'compare.csv'
    job = ComparisonExportJob(str(path), result.entries, result.metrics, MEM, ALIGN_ROUNDS)
    assert vm.start_export(job, worker_factory=ComparisonExportWorker) is None
    assert process_events_until(lambda: vm.export_worker is None, timeout=10)
    with path.open(encoding='utf-8-sig', newline='') as source:
        rows = list(csv.reader(source))
    assert {r[0] for r in rows[1:9]} == {t.task_id for t in tasks}
    image = render_comparison_image(result.entries, result.metrics, MEM, ALIGN_ROUNDS)
    assert not image.isNull() and image.width() == 1600
    image_path = tmp_path / 'comparison.png'
    job = replace(job, path=str(image_path), image=image)
    ComparisonExportWorker(db, job).execute(CancelToken())
    assert image_path.read_bytes().startswith(b'\x89PNG')


def test_panel_default_rounds_and_four_rows(qapp, db, manager, services):
    tasks = seed_four(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    panel = ComparePanel()
    try:
        panel.set_result(vm.last_compare)
        assert panel.alignment == ALIGN_ROUNDS
        assert panel.matrix.rowCount() == 4
        assert len(panel.chart.series) == 4
        panel.alignment_combo.setCurrentIndex(1)
        assert panel.alignment == ALIGN_RELATIVE
        assert panel.chart.time_axis.relative_origin == 0.0
    finally:
        panel.close()
        panel.deleteLater()


def test_different_metrics_keep_missing_cells_and_other_tasks(qapp, db, manager, services):
    tasks = seed_four(db)
    extra = seed(db, name='only-gdi.exe', start=BASE - timedelta(days=1), metrics=(GDI,))
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_tasks([tasks[0].task_id, tasks[1].task_id, extra.task_id])
    result = vm.last_compare
    assert len(result.entries) == 4
    missing = result.entries[-1]
    assert matrix_cell(missing, MEM).mean_delta is None
    assert matrix_cell(missing, GDI).message == ''
    assert GDI in result.metrics
    assert matrix_cell(result.entries[0], GDI).mean_delta is None
    assert len([e for e in result.entries if e.snapshots.get(MEM)]) == 3


def test_failed_part_finishes_batch_and_preserves_successful_tasks(qapp, db, manager, services):
    tasks = seed_four(db)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    failed = runner.pending[0][0]
    runner.fail('数据库暂不可读')
    runner.complete_all()
    assert vm.last_compare is not None and not vm._compare_parts
    entry = next(e for e in vm.last_compare.entries if e.task.task_id == failed.task_id)
    assert matrix_cell(entry, failed.metric).message == '数据库暂不可读'
    assert len(vm.last_compare.entries) == 4


def test_range_change_invalidates_inflight_comparison(qapp, db, manager, services):
    tasks = seed_four(db)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    old_count = len(runner.pending)
    vm.set_range('10m')
    for _ in range(old_count):
        runner.complete(0)
    assert vm.last_compare is None
    runner.complete_all()
    assert vm.last_compare.primary.request.range_key == '10m'
    assert all(s.request.range_key == '10m' for e in vm.last_compare.entries for s in e.snapshots.values())


def test_deleted_selected_comparison_is_removed_after_refresh(qapp, db, manager, services):
    tasks = seed_four(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    db.tasks.delete(tasks[0].task_id)
    vm.reload_tasks()
    vm.refresh()
    assert tasks[0].task_id not in vm.compare_task_ids
    assert len(vm.last_compare.entries) == 3


def test_switch_to_comparison_task_excludes_it_from_other_selection(qapp, db, manager, services):
    tasks = seed_four(db)
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_tasks([t.task_id for t in tasks[:3]])
    vm.select_task(tasks[0].task_id, explicit=True)
    assert tasks[0].task_id not in vm.compare_task_ids
    assert vm.last_compare.primary.request.task_id == tasks[0].task_id
    assert len({e.task.task_id for e in vm.last_compare.entries}) == 3


def test_clearing_selection_discards_late_comparison(qapp, db, manager, services):
    tasks = seed_four(db)
    runner = ManualHistoryRunner(db)
    vm = make_vm(db, manager, services, runner)
    vm.reload_tasks()
    runner.complete_all()
    vm.set_compare_tasks([tasks[0].task_id])
    vm.set_compare_tasks([])
    runner.complete_all()
    assert vm.last_compare is None and not vm.compare_task_ids


def test_comparison_selection_cannot_add_fifth_task(qapp, db, manager, services):
    from ui.models.task_options import TaskOptionsModel
    tasks = seed_four(db)
    panel = ComparePanel()
    try:
        options = TaskOptionsModel().rebuild(tasks, set(), BASE)
        panel.set_options(options)
        for task in tasks[:3]:
            panel._actions[task.task_id].trigger()
        assert len(panel.selected_ids()) == 3
        assert not panel._actions[tasks[3].task_id].isEnabled()
        panel._actions[tasks[0].task_id].trigger()
        assert panel._actions[tasks[3].task_id].isEnabled()
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.parametrize('width', [800, 1280])
def test_four_task_page_never_overflows_horizontally(qapp, db, manager, services, width):
    from ui.pages.analysis_page import AnalysisPage
    from tests.helpers import SyncHistoryRunner
    tasks = seed_four(db)
    page = AnalysisPage(db, manager, services, worker_runner=SyncHistoryRunner(db),
                        defer=lambda fn: fn())
    try:
        page.resize(width, 820)
        page.show()
        page.vm.reload_tasks()
        page._set_compare_open(True)
        page.vm.set_compare_tasks([t.task_id for t in tasks[:3]])
        qapp.processEvents()
        assert page.horizontalScrollBar().maximum() == 0
        assert page.widget().width() <= page.viewport().width()
    finally:
        page.close()
        page.deleteLater()


def test_reference_query_updates_summary_column(qapp, db, manager, services):
    from ui.pages.analysis_page import AnalysisPage
    from tests.helpers import SyncHistoryRunner
    from ui.formatting import SUMMARY_COLUMNS
    markers = ((10, '基线'), (40, '第 1 轮'), (70, '第 2 轮'), (100, '第 3 轮'))
    reference = seed(db, name='reference.exe', start=BASE - timedelta(days=1), slope=1, markers=markers)
    current = seed(db, name='current.exe', slope=10, markers=markers)
    for task in (reference, current):
        manager.update_task_identity(task.task_id, '循环开关', '1.0')
        manager.set_baseline(task.task_id, db.markers.list(task.task_id)[0].id)
    manager.set_task_reference(reference.task_id)
    page = AnalysisPage(db, manager, services, worker_runner=SyncHistoryRunner(db),
                        defer=lambda fn: fn())
    try:
        page.vm.reload_tasks()
        assert page.vm.last_reference.reference_id == reference.task_id
        assert page.vm.last_reference.status == 'regression'
        column = len(SUMMARY_COLUMNS)
        assert not page.summary_table.table.isColumnHidden(column)
        assert '回归' in page.summary_table.table.item(0, column).text()
        page._compare_reference(reference.task_id)
        assert page.vm.compare_task_id == reference.task_id
    finally:
        page.close()
        page.deleteLater()


def test_long_export_labels_are_preserved_in_png_metadata(qapp, db):
    import json
    task = seed(db, name='很长的被测程序名称' * 20,
                markers=((10, '基线'), (40, '第1轮'), (80, '第2轮')))
    task.build = '版本描述' * 30
    snapshot = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    entry = CompareEntry(task, {MEM: snapshot})
    image = render_comparison_image((entry,), (MEM,), MEM, ALIGN_ABSOLUTE)
    labels = json.loads(image.text('任务信息'))
    assert task.process_name in labels[0]['label'] and task.build in labels[0]['label']
    assert image.width() == 1600


def test_summary_keyboard_moves_focus_without_requery(qapp, db, manager, services):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    from tests.helpers import SyncHistoryRunner
    from ui.pages.analysis_page import AnalysisPage
    seed(db)
    runner = SyncHistoryRunner(db)
    page = AnalysisPage(db, manager, services, worker_runner=runner, defer=lambda fn: fn())
    try:
        page.resize(1000, 820)
        page.show()
        page.vm.reload_tasks()
        table = page.summary_table.table
        table.setCurrentCell(0, 0)
        table.setFocus()
        before = len(runner.requests)
        QTest.keyClick(table, Qt.Key_Down)
        assert page.current_metric == page.summary_table.metrics()[1]
        assert len(runner.requests) == before
        assert table.horizontalHeaderItem(0).textAlignment() & Qt.AlignLeft
        assert table.horizontalHeaderItem(1).textAlignment() & Qt.AlignRight
    finally:
        page.close()
        page.deleteLater()


def test_no_round_task_is_dimmed_and_explained(qapp, db, manager, services):
    from ui import theme_tokens as tokens
    other = seed(db, name='no-rounds.exe', start=BASE - timedelta(days=1))
    seed(db, markers=((10, '基线'), (40, '第1轮')))
    vm = make_vm(db, manager, services)
    vm.reload_tasks()
    vm.set_compare_task(other.task_id)
    panel = ComparePanel()
    try:
        panel.set_result(vm.last_compare)
        assert panel.matrix.item(1, 0).foreground().color() == tokens.qcolor(tokens.DIMMED_TEXT)
        assert '相对时间' in panel.message_label.text()
        assert '打点边界参考值' in panel.matrix.item(0, 1).toolTip()
    finally:
        panel.close()
        panel.deleteLater()
