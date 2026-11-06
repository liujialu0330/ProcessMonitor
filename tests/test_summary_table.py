"""
判定摘要表：每行文本（ui.formatting.build_summary_row）与控件行为（置灰、点击切焦点、
程序选中不发信号、依据行）。
"""
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import Qt

from core.history_query import build_request, run_history_query
from data.models import DataPoint, MonitorTask
from ui.formatting import SUMMARY_COLUMNS, build_summary_row, percent_change
from ui.widgets.analysis.summary_table import VERDICT_COLUMN, SummaryTable
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 16, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
GDI = MetricType.NUM_GDI_OBJECTS
IO = MetricType.IO_READ_BYTES


def _snapshot(db, metric, samples, value, markers=(), baseline=None):
    task = MonitorTask(task_id=str(uuid.uuid4()), pid=1, process_name="demo.exe",
                       metric_types=[metric], interval=1.0, start_time=BASE,
                       end_time=BASE + timedelta(seconds=samples), status="stopped")
    db.save_task(task)
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=i), value(i), metric)
                         for i in range(samples)])
    for offset, label in markers:
        db.add_marker(task.task_id, BASE + timedelta(seconds=offset), label)
    if baseline is not None:
        from core.monitor_manager import MonitorManager
        MonitorManager(db=db).add_marker(task.task_id, "基线", BASE + timedelta(seconds=baseline))
    return run_history_query(db, build_request(task.task_id, metric, 'all', analysis=True))


def test_rising_level_metric_row(db):
    snapshot = _snapshot(db, MEM, 1200, lambda i: 102400.0 + i * 10, baseline=100,
                         markers=((300, "第 1 轮"), (600, "第 2 轮"), (900, "第 3 轮")))
    row = build_summary_row(MEM, snapshot)
    assert row.text("metric") == "私有字节"
    # 基线后 5 个样本（第 101~105 秒）的中位数 = 103,430 KB
    assert row.text("baseline") == "101 MB"
    assert row.text("current") == "111.71 MB"
    assert row.text("delta") == "+10.71 MB (+10.6%)"
    # 每轮均增 = 三轮 Δ 的平均（1.95 + 2.93 + 2.93）/ 3
    assert row.text("per_round") == "+2.6 MB"
    assert row.text("rate") == "+600 ±0 KB/分 · R² 1.00"
    assert row.text("verdict") == "持续上涨"
    assert row.text("tail") == "↑ +2.92 MB"
    assert row.text("usage") == ""
    assert not row.dimmed
    assert "中位数" in row.tooltips["baseline"]
    assert "第 1 轮" in row.tooltips["per_round"]


def test_insufficient_row_is_dimmed(db):
    snapshot = _snapshot(db, MEM, 11, lambda i: 1000.0 + i)
    row = build_summary_row(MEM, snapshot)
    assert row.dimmed
    assert row.text("rate") == "数据不足（已采 10秒）"
    assert row.text("verdict") == "数据不足"
    assert "最早" in row.tooltips["baseline"]


def test_cumulative_and_object_rows(db):
    io = build_summary_row(IO, _snapshot(db, IO, 100, lambda i: 1000.0 * i))
    assert io.text("rate") == "平均 1,000 KB/秒" and io.text("verdict") == "累计量"
    assert io.text("per_round") == "--"
    gdi = build_summary_row(GDI, _snapshot(db, GDI, 1200, lambda i: 3000 + i // 100))
    assert gdi.text("usage") == "30.1% / 10000"
    assert gdi.tooltips["usage"].startswith("已用 30.1%（上限 10000）")
    assert build_summary_row(MEM, None).dimmed
    assert build_summary_row(MEM, None).text("baseline") == "--"
    assert percent_change(110, 100) == pytest.approx(10.0)
    assert percent_change(1, 0) is None


@pytest.fixture
def table(qapp, db):
    rows = [
        build_summary_row(MEM, _snapshot(db, MEM, 1200, lambda i: 102400.0 + i * 10)),
        build_summary_row(GDI, _snapshot(db, GDI, 11, lambda i: 300.0)),
    ]
    widget = SummaryTable()
    widget.set_rows(rows, "21分钟 · 1,200 点", focus=MEM)
    yield widget
    widget.close()


def test_table_rows_dimmed_and_basis(table):
    assert [r[0] for r in table.row_texts()] == ["私有字节", "GDI对象数"]
    assert len(table.row_texts()[0]) == len(SUMMARY_COLUMNS)
    assert table.dimmed_metrics() == [GDI]
    assert table.basis() == "依据：21分钟 · 1,200 点"
    assert table.selected_metric() == MEM
    # 判定列由胶囊显示，单元格不重复画字
    assert table.table.item(0, VERDICT_COLUMN).text() == ""
    assert table.table.cellWidget(0, VERDICT_COLUMN).text() == "持续上涨"
    assert not table.table.cellWidget(1, VERDICT_COLUMN).isEnabled()
    dim = table.table.item(1, 1).foreground().color()
    assert dim.alpha() < 255
    assert table.table.item(0, 1).textAlignment() & Qt.AlignRight
    assert table.table.item(0, 0).textAlignment() & Qt.AlignLeft


def test_click_row_selects_metric_and_program_select_is_silent(table):
    picked = []
    table.metric_selected.connect(picked.append)
    table.select_metric(GDI)
    assert picked == [] and table.selected_metric() == GDI
    table.table.cellClicked.emit(0, 2)
    assert picked == [MEM]
    table.select_metric("unknown")
    assert table.selected_metric() is None
    table.clear()
    assert table.row_texts() == [] and table.basis() == ""
    assert table.table.height() > 0
