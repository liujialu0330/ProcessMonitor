"""
打点区间表：行文本（ui.formatting.interval_row_texts）、点击行发区间起止、
无打点时的提示，以及 [打点区间 | 采集明细] 子视图切换；相对时间格式。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from core.history_query import build_request, run_history_query
from data.models import DataPoint, MonitorTask
from ui.formatting import INTERVAL_HEADERS, format_relative
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
    table.clear()
    assert table.row_texts() == [] and table.title_label.text() == "打点区间"


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
