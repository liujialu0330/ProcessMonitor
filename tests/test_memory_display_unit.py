"""两页共用的容量显示偏好，以及在历史快照上切换单位的数值契约。"""
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

import app_config
from app.paths import AppPaths
from core.conclusion_text import FORMAT_TEXT
from core.contracts import UnavailableReason
from core.ipc.commands import TaskAnalysisWorker, compute_verdict
from core.monitor_manager import MonitorManager
from core.report_worker import ReportWorker
from core.workers.base import CancelToken
from data.database import Database
from data.models import DataPoint, MonitorTask
from tests.factories import make_analysis_page, make_monitor_page, make_services
from tests.helpers import ManualHistoryRunner, SyncHistoryRunner, process_events_until
from ui.components.memory_unit_selector import MemoryUnitSelector
from ui.components.task_card import TaskCard
from ui.widgets.analysis.interval_table import VIEW_SAMPLES
from ui.widgets.analysis.toolbar import EXPORT_EVIDENCE
from ui.widgets.monitor.task_list_row import TaskListRow
from utils.metrics import (REGISTRY, MetricType, format_metric_delta, format_metric_value,
                           member_series_key)

MEM = MetricType.MEMORY_PRIVATE
RSS = MetricType.MEMORY_RSS
START = datetime(2026, 11, 18, 8)
BASE_KB = 2_097_152


def test_memory_units_cover_capacity_peaks_members_and_legacy_keys():
    capacity = [key for key, metric in REGISTRY.items()
                if metric.category == '内存' and metric.unit == 'KB']
    for metric in [*capacity, 'memory_vms', member_series_key(MEM, 'child.exe')]:
        assert format_metric_value(metric, BASE_KB + 32, memory_unit='KB') == '2,097,184 KB'
        assert format_metric_value(metric, BASE_KB + 32, memory_unit='MB') == '2,048.031 MB'
        assert format_metric_value(metric, BASE_KB + 32, memory_unit='GB') == '2.000031 GB'
        assert format_metric_value(metric, 1024 ** 3, memory_unit='auto') == '1 TB'
        assert format_metric_delta(metric, -32) == '-32 KB'
        assert format_metric_delta(metric, 2048) == '+2 MB'
    for metric in [MetricType.MEMORY_PERCENT, MetricType.MEMORY_NUM_PAGE_FAULTS,
                   MetricType.IO_READ_BYTES, MetricType.NUM_HANDLES, MetricType.CPU_PERCENT]:
        assert format_metric_value(metric, BASE_KB, memory_unit='KB') == format_metric_value(metric, BASE_KB)


@pytest.mark.parametrize('value, unit, text', [
    (0, 'MB', '0 MB'),
    (0.4, 'MB', '<0.001 MB'),
    (-0.4, 'MB', '>-0.001 MB'),
    (0.4, 'GB', '<0.000001 GB'),
    (1, 'GB', '0.000001 GB'),
    (0.001, 'KB', '<0.01 KB'),
    (1024 ** 3, 'MB', '1,048,576 MB'),
])
def test_fixed_memory_unit_preserves_nonzero_values_and_sign(value, unit, text):
    assert format_metric_value(MEM, value, memory_unit=unit) == text


@pytest.mark.parametrize('stored', [
    {}, {'MemoryUnit': 'TB'}, {'MemoryUnit': None}, {'MemoryUnit': 'kb'}, {'MemoryUnit': 1},
])
def test_display_preference_defaults_and_survives_reload(qapp, tmp_path, stored):
    paths = AppPaths.under(str(tmp_path))
    path = Path(paths.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'config_version': 3, 'Display': stored}), encoding='utf-8')
    app_config.load_app_config(paths)
    picker = MemoryUnitSelector()
    try:
        assert picker.unit() == 'auto'
        assert [picker.combo.itemText(i) for i in range(picker.combo.count())] == ['自动', 'KB', 'MB', 'GB']
        for key, expected in ((Qt.Key_Up, 'auto'), (Qt.Key_Down, 'KB'), (Qt.Key_Down, 'MB')):
            QTest.keyClick(picker.combo, key)
            assert picker.unit() == expected
        assert json.loads(path.read_text(encoding='utf-8'))['Display']['MemoryUnit'] == 'MB'
        app_config.cfg.memory_display_unit.value = 'auto'
        app_config.load_app_config(paths)
        assert picker.unit() == 'MB'
    finally:
        picker.close()


def test_switch_between_pages_reformats_history_without_sampling_or_querying(qapp, db):
    manager = MonitorManager(db=db)
    services = make_services()
    runner = SyncHistoryRunner(db)
    values = [BASE_KB] * 5 + [BASE_KB + 32, BASE_KB + 64]
    stamps = [START + timedelta(minutes=i) for i in range(len(values))]
    task = MonitorTask('history', 120, '复盘.exe', metric_types=[MEM, RSS], interval=60,
                       start_time=START, end_time=stamps[-1], status='stopped')
    assert db.save_task(task)
    assert db.save_data_points([DataPoint(task.task_id, t, v, m)
                                for t, v in zip(stamps, values) for m in task.metric_types])
    db.markers.add(task.task_id, START, '基线', 'baseline')
    monitor = make_monitor_page(db, manager, services)
    analysis = make_analysis_page(db, manager, services, worker_runner=runner, defer=lambda fn: fn())
    try:
        card = TaskCard('live', '采集中.exe', 121, task.metric_types)
        monitor.board.add_card(card)
        card.set_baseline(START, {MEM: BASE_KB})
        for t, value in zip(stamps[-3:], values[-3:]):
            card.update_values({MEM: value, RSS: value}, t)
        monitor.board.set_view_mode('list')
        list_row = next(row for row in monitor.board.findChildren(TaskListRow) if row.card is card)
        analysis.select_task(task.task_id)
        panel = analysis.trend_panel
        panel.expand_all()
        chart = panel.focus_chart
        chart.show_hover_at(stamps[-1].timestamp())
        panel.zoom_to(stamps[-3].timestamp(), stamps[-1].timestamp())
        before_range = chart.chart_widget.getPlotItem().vb.viewRange()
        analysis.lower_views.set_view(VIEW_SAMPLES)
        analysis.sample_table.data_table.setCurrentCell(1, 1)
        original = analysis.vm.last_analysis
        request_count = len(runner.requests)
        assert original.snapshot(MEM).baseline.value == BASE_KB
        assert card.value_labels[MEM].text() == '2 GB'

        for picker, unit, expected in (
                (monitor.memory_unit_selector, 'KB', ['2,097,152 KB', '2,097,184 KB', '2,097,216 KB']),
                (analysis.memory_unit_selector, 'MB', ['2,048 MB', '2,048.031 MB', '2,048.062 MB']),
                (monitor.memory_unit_selector, 'GB', ['2 GB', '2.000031 GB', '2.000061 GB']),
                (analysis.memory_unit_selector, 'auto', ['2 GB'] * 3)):
            picker.combo.setCurrentIndex(picker.combo.findData(unit))
            assert monitor.memory_unit_selector.unit() == analysis.memory_unit_selector.unit() == unit
            assert card.value_labels[MEM].text() == expected[-1]
            assert list_row.value.text().startswith(expected[-1])
            assert card.delta_text(MEM) == '较基线 +64 KB'
            rows = analysis.sample_table.row_texts()
            assert [row[1] for row in rows[:3]][::-1] == expected
            assert rows[0][2:5] == ('+32 KB', '+64 KB', '2097216.0000')
            actual_unit = 'GB' if unit == 'auto' else unit
            assert chart.axis_title().endswith(f'（{actual_unit}）')
            assert panel.small_charts()[RSS].title_label.text().endswith(f'（{actual_unit}）')
            assert chart.value_axis.tickStrings([BASE_KB], 1, BASE_KB) == [expected[0].split()[0]]
            assert expected[-1] in chart.hover_label.text()
            summary = analysis.summary_table.rows()[analysis.summary_table.row_of(MEM)]
            assert summary.text('current') == expected[-1]
            assert summary.text('baseline') == expected[0]
            assert summary.text('delta') == '+64 KB (+0.0%)'
            interval = analysis.interval_table.row_texts()[-1]
            assert interval[2:5] == (expected[0], expected[-1], '+64 KB')
            assert interval[6] == expected[-1]
            if unit in ('auto', 'GB'):
                axis = chart.value_axis
                low, high = BASE_KB + 0.125, BASE_KB + 1.625
                levels = axis.tickValues(low, high, 400)
                labels = []
                for step, ticks in levels:
                    visible = [tick for tick in ticks if low <= tick <= high]
                    labels.extend(map(float, axis.tickStrings(visible, 1, step)))
                assert len(labels) >= 3 and len(labels) == len(set(labels))
            assert analysis.sample_table.data_table.currentRow() == 1
            assert chart.chart_widget.getPlotItem().vb.viewRange() == before_range
            assert analysis.vm.last_analysis is original and len(runner.requests) == request_count
            assert card.sparkline.series() == values[-3:]

        panel.set_expanded(False)
        assert panel.folded_rows()[RSS].value_label.text() == '2 GB'
        card.mark_unavailable({MEM: UnavailableReason.ACCESS_DENIED})
        monitor.memory_unit_selector.combo.setCurrentIndex(1)
        assert card.value_labels[MEM].text() == '不可用'
        assert list_row.value.text().startswith('不可用')
        assert panel.folded_rows()[RSS].value_label.text() == '2,097,216 KB'
        card.update_values({MEM: BASE_KB + 96})
        list_row.refresh()
        assert list_row.value.text().startswith('2,097,248 KB')
        newer = TaskCard('new', '新任务.exe', 122, [MEM])
        monitor.board.add_card(newer)
        newer.update_values({MEM: BASE_KB + 96})
        assert newer.value_labels[MEM].text() == '2,097,248 KB'
    finally:
        analysis.auto_refresh.stop()
        analysis.close()
        monitor.close()


def test_read_only_group_uses_latest_preference_and_exports_original_kb(qapp, db, tmp_path):
    io = MetricType.IO_READ_BYTES
    child = 'worker.exe'
    child_mem = member_series_key(MEM, child)
    child_io = member_series_key(io, child)
    end = START + timedelta(minutes=2)
    task = MonitorTask('tree', 132, '树.exe', [MEM, io], 60, START, end,
                       status='stopped', target_kind='tree')
    assert db.save_task(task)
    points = []
    for i in range(3):
        ts = START + timedelta(minutes=i)
        points.extend(DataPoint(task.task_id, ts, value, metric) for metric, value in (
            (MEM, BASE_KB + i * 32), (child_mem, 2048 + i * 32),
            (io, BASE_KB + i * 120), (child_io, BASE_KB + i * 60)))
    assert db.save_data_points(points)
    reader = Database(db.db_path, read_only=True)
    runner = ManualHistoryRunner(reader)
    page = make_analysis_page(reader, MonitorManager(db=reader), worker_runner=runner,
                              defer=lambda fn: fn())
    try:
        page.select_task(task.task_id)
        picker = page.memory_unit_selector.combo
        picker.setCurrentIndex(picker.findData('KB'))
        runner.complete_all()
        assert page.vm.select_member(child)
        assert page.trend_panel.focus_chart.metric_type == child_mem
        assert page.sample_table.row_texts()[0][1] == '2,112 KB'
        picker.setCurrentIndex(picker.findData('MB'))
        assert page.sample_table.row_texts()[0][1:3] == ('2.062 MB', '+32 KB')
        assert page.trend_panel.focus_chart.axis_title().endswith('（MB）')

        page.vm.set_view_mode('overlay')
        overlay = page.trend_panel.overlay_chart
        assert '%' in overlay.axis_title() and '%' in overlay.hover_text(0)
        page.vm.select_metric(io)
        raw_io = page.sample_table.row_texts()[0]
        assert raw_io[1] == '2 GB' and page.trend_panel.focus_chart.is_rate
        picker.setCurrentIndex(picker.findData('GB'))
        assert page.sample_table.row_texts()[0] == raw_io

        path = tmp_path / 'detail.csv'
        job = page.vm.report_job(csv_path=str(path))
        assert ReportWorker(reader, job).execute(CancelToken()).row_count == 3
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = csv.DictReader(line for line in stream if not line.startswith('# '))
            assert [r['私有字节(KB)'] for r in rows] == [
                '2097152.0000', '2097184.0000', '2097216.0000']
    finally:
        page.auto_refresh.stop()
        page.close()
        reader.close()


def test_memory_unit_updates_copied_conclusion_and_preserves_report_outputs(qapp, db, tmp_path):
    end = START + timedelta(minutes=6)
    task = MonitorTask('reports', 120, '复盘.exe', [MEM], 60, START, end, status='stopped')
    assert db.save_task(task)
    assert db.save_data_points([
        DataPoint(task.task_id, START + timedelta(minutes=i), value, MEM)
        for i, value in enumerate([BASE_KB] * 5 + [BASE_KB + 32, BASE_KB + 64])
    ])
    db.markers.add(task.task_id, START, '基线', 'baseline')
    services = make_services()
    page = make_analysis_page(db, MonitorManager(db=db), services,
                              worker_runner=SyncHistoryRunner(db), defer=lambda fn: fn())
    try:
        page.select_task(task.task_id)

        def export(unit):
            combo = page.memory_unit_selector.combo
            combo.setCurrentIndex(combo.findData(unit))
            output = tmp_path / f'{unit}.zip'
            services.dialogs.push('ask_save_path', str(output))
            assert page.export_flow.run(EXPORT_EVIDENCE)
            assert process_events_until(lambda: not services.threads.entries(), timeout=10)
            assert page.export_flow.copy_conclusion(FORMAT_TEXT)
            copied = services.dialogs.last_call('copy_text')['text']
            with ZipFile(output) as archive:
                artifacts = {name: archive.read(name) for name in archive.namelist()}
            worker = TaskAnalysisWorker(db, task.task_id, task.metric_types, analysis=True)
            return artifacts, compute_verdict(worker.execute(CancelToken())), copied

        auto_files, auto_verdict, auto_text = export('auto')
        kb_files, kb_verdict, kb_text = export('KB')
        assert {'task.json', 'README.txt'} <= auto_files.keys()
        assert sum(name.endswith('.csv') for name in auto_files) == 2
        assert any(name.endswith('.png') for name in auto_files)
        assert auto_files == kb_files
        assert auto_verdict == kb_verdict
        assert '当前 2 GB' in auto_text and '当前 2,097,216 KB' in kb_text
        assert '较基线 +64 KB' in auto_text and '较基线 +64 KB' in kb_text
    finally:
        page.auto_refresh.stop()
        process_events_until(lambda: not services.threads.entries(), timeout=10)
        page.close()
