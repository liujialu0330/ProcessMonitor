"""
打点区间表：行文本（ui.formatting.interval_row_texts）、点击行发区间起止、
无打点时的提示，以及 [打点区间 | 采集明细] 子视图切换；相对时间格式。
"""
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from core.history_query import build_request, run_history_query
from data.models import DataPoint, MonitorTask
from tests.helpers import process_events_until
from ui.formatting import INTERVAL_HEADERS, format_relative
from ui.layout_breakpoints import COMPACT
from ui.widgets.analysis.interval_table import (EMPTY_HINT, VIEW_INTERVALS, VIEW_SAMPLES,
                                                IntervalTable, LowerViews)
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
HANDLES = MetricType.NUM_HANDLES


def _snapshot(db, markers=((10, "基线"), (40, "第 1 轮"), (70, "第 2 轮"))):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name="demo.exe",
                       metric_types=[HANDLES], interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=100), status="stopped")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=i), 100.0 + i, HANDLES)
                         for i in range(100)])
    for offset, label in markers:
        db.add_marker(task.task_id, BASE + timedelta(seconds=offset), label)
    snapshot = run_history_query(db, build_request(task.task_id, HANDLES, 'all', analysis=True))
    return snapshot, db.markers.list(task.task_id)


@pytest.fixture
def table(qapp):
    widget = IntervalTable()
    yield widget
    widget.close()


def test_rows_follow_markers(table, db):
    snapshot, markers = _snapshot(db)
    table.set_intervals(HANDLES, snapshot.intervals, markers)
    rows = table.row_texts()
    assert len(rows[0]) == len(INTERVAL_HEADERS)
    assert [r[0] for r in rows] == ["开始 → 基线", "基线 → 第 1 轮", "第 1 轮 → 第 2 轮",
                                    "第 2 轮 → 结束"]
    # 第 1 轮：[40, 70) 秒，起值 140、终值 169、Δ +29，与上轮持平
    assert rows[2][1] == "30秒"
    assert rows[2][2:5] == ("140 个", "169 个", "+29 个")
    assert rows[2][6] == "169 个"
    assert rows[2][7] == "±0"
    assert rows[0][7] == "--"                       # 第一段没有上一轮
    assert "句柄数" in table.title_label.text()
    assert table.hint_label.text().startswith("4 段")


def test_activate_row_emits_interval_bounds(table, db):
    snapshot, markers = _snapshot(db)
    table.set_intervals(HANDLES, snapshot.intervals, markers)
    got = []
    table.interval_activated.connect(lambda s, e: got.append((s, e)))
    table.activate_row(2)
    assert got == [(BASE + timedelta(seconds=40), BASE + timedelta(seconds=70))]
    table.table.setCurrentCell(0, 3)
    assert got[-1][0] == BASE
    table.activate_row(99)                          # 越界忽略
    assert len(got) == 2
    assert table.interval_at(99) is None


def test_no_markers_single_interval_with_hint(table, db):
    snapshot, markers = _snapshot(db, markers=())
    table.set_intervals(HANDLES, snapshot.intervals, markers)
    assert [r[0] for r in table.row_texts()] == ["开始 → 结束"]
    assert table.hint_label.text() == EMPTY_HINT
    table.table.setCurrentCell(0, 0)
    assert not table.rename_button.isEnabled()
    table.clear()
    assert table.row_texts() == [] and table.title_label.text() == "打点区间"
    assert not table.rename_button.isEnabled()


def test_interval_with_system_marker_ids_has_no_editable_endpoint(table, db):
    snapshot, _markers = _snapshot(db, markers=())
    task_id = snapshot.request.task_id
    pause = db.markers.add(task_id, BASE, '开始', kind='pause')
    resume = db.markers.add(task_id, BASE + timedelta(seconds=99), '结束', kind='resume')
    snapshot = run_history_query(db, build_request(task_id, HANDLES, 'all', analysis=True))
    # 查询本身会滤掉系统边界；控件即使收到系统记录的真实 ID，也应拒绝改名。
    interval, = snapshot.intervals
    table.set_intervals(HANDLES, [replace(interval, start_marker_id=pause, end_marker_id=resume)],
                        snapshot.markers)
    table.table.setCurrentCell(0, 0)
    assert [row[0] for row in table.row_texts()] == ['开始 → 结束']
    assert not table.rename_button.isEnabled()


def test_refresh_tracks_selected_marker_ids_without_zooming_again(table, db):
    snapshot, markers = _snapshot(db, markers=((25, '采样'),) * 4)
    table.set_intervals(HANDLES, snapshot.intervals, markers)
    task_id = snapshot.request.task_id
    zoomed = []
    table.interval_activated.connect(lambda start, end: zoomed.append((start, end)))
    table.table.setCurrentCell(1, 0)
    assert zoomed == [(markers[0].timestamp, markers[1].timestamp)]

    # 多段同名且同一时刻，插入更早的打点后，原来的第 2 行变成第 3 行。
    db.markers.add(task_id, BASE + timedelta(seconds=5), '预热')
    refreshed = run_history_query(db, snapshot.request)
    table.set_intervals(HANDLES, refreshed.intervals, refreshed.markers)
    current = table.table.currentRow()
    assert current == 2
    selected = table.interval_at(current)
    assert (selected.start_marker_id, selected.end_marker_id) == (markers[0].id, markers[1].id)
    assert table.rename_button.isEnabled() and len(zoomed) == 1

    assert db.markers.delete(markers[1].id)
    refreshed = run_history_query(db, snapshot.request)
    table.set_intervals(HANDLES, refreshed.intervals, refreshed.markers)
    assert table.table.currentRow() == -1 and not table.table.selectionModel().hasSelection()
    assert not table.rename_button.isEnabled() and len(zoomed) == 1


def test_f2_directly_edits_the_only_real_endpoint(table, db):
    snapshot, markers = _snapshot(db, markers=((25, '开始'),))
    table.set_intervals(HANDLES, snapshot.intervals, markers)
    table.show()
    table.activateWindow()
    table.table.setCurrentCell(0, 0)
    table.table.setFocus()
    assert process_events_until(table.table.hasFocus)
    renamed = []
    table.rename_requested.connect(renamed.append)
    QTest.keyClick(table.table, Qt.Key_F2)
    assert renamed == markers and table.rename_menu is None


@pytest.mark.parametrize('endpoint', [0, 1], ids=['start', 'end'])
def test_keyboard_rename_selects_the_right_marker_at_identical_time_and_name(
        table, db, endpoint):
    snapshot, markers = _snapshot(db, markers=((25, '结束'), (25, '结束')))
    table.set_intervals(HANDLES, snapshot.intervals, markers)
    table.set_tier(COMPACT)
    table.resize(680, 280)
    table.show()
    table.activateWindow()
    table.table.setFocus()
    assert process_events_until(table.table.hasFocus)
    assert table.row_texts()[1][0] == '结束 → 结束'
    assert table.rename_button.isVisible()

    renamed, zoomed = [], []
    table.rename_requested.connect(renamed.append)
    table.interval_activated.connect(lambda start, end: zoomed.append((start, end)))
    cell = table.table.visualItemRect(table.table.item(1, 0))
    QTest.mouseClick(table.table.viewport(), Qt.LeftButton, pos=cell.center())
    assert zoomed == [(markers[0].timestamp, markers[1].timestamp)]
    assert table.rename_button.isEnabled()

    QTest.keyClick(table.table, Qt.Key_F2)
    menu = table.rename_menu
    assert menu is not None and menu.isVisible()
    try:
        actions = menu.actions()
        assert len(actions) == 2
        assert actions[0].text().startswith('起点') and actions[1].text().startswith('终点')
        assert all('结束' in action.text() for action in actions)
        if endpoint == 1:
            QTest.keyClick(menu.view, Qt.Key_Down)
        QTest.keyClick(menu.view, Qt.Key_Return)
        assert [marker.id for marker in renamed] == [markers[endpoint].id]
        assert len(zoomed) == 1
    finally:
        menu.close()


def test_lower_views_switch(qapp):
    views = LowerViews()
    seen = []
    views.view_changed.connect(seen.append)
    assert views.current_view() == VIEW_INTERVALS
    assert views.stack.currentWidget() is views.interval_table
    views.set_view(VIEW_SAMPLES)
    assert views.current_view() == VIEW_SAMPLES
    assert views.stack.currentWidget() is views.sample_table
    assert seen[-1] == VIEW_SAMPLES
    views.clear()
    views.close()


def test_interval_page_sized_to_rows_not_to_sample_table(qapp, db):
    """焦点区里区间表高度贴合行数：堆叠不按更高的采集明细页留空白，切到明细再按明细。"""
    from ui.widgets.analysis.interval_table import HEADER_HEIGHT, ROW_HEIGHT
    views = LowerViews()
    try:
        snapshot, markers = _snapshot(db)
        views.interval_table.set_intervals(HANDLES, snapshot.intervals, markers)
        assert views.interval_table.table.height() == HEADER_HEIGHT + 4 * ROW_HEIGHT + 4
        short = views.stack.sizeHint().height()
        assert short < views.sample_table.sizeHint().height()
        views.set_view(VIEW_SAMPLES)
        assert views.stack.sizeHint().height() >= views.sample_table.sizeHint().height()
        views.set_view(VIEW_INTERVALS)
        assert views.stack.sizeHint().height() == short
    finally:
        views.close()


def test_format_relative():
    assert format_relative(0) == "T+00:00"
    assert format_relative(75.4) == "T+01:15"
    assert format_relative(3725) == "T+1:02:05"
    assert format_relative(-30) == "T-00:30"
