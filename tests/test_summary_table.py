"""
判定摘要表：每行文本（ui.formatting.build_summary_row）与控件行为（置灰、点击切焦点、
程序选中不发信号、依据行、列宽策略、胶囊文字色随主题切换）。
"""
import re
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QHeaderView
from qfluentwidgets import Theme, qconfig

from core.history_query import build_request, run_history_query
from data.models import DataPoint, MonitorTask
from ui.formatting import SUMMARY_COLUMNS, build_summary_row, percent_change
from ui.layout_breakpoints import WIDE
from ui.widgets.analysis.summary_table import (
    H_SCROLLBAR_SPACE, USAGE_COLUMN, VERDICT_COLUMN, SummaryTable)
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
    widget.set_tier(WIDE)   # 全部列；各档位的列取舍见 tests/test_responsive_layout.py
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
    dim = table.table.item(1, 1).foreground().color()
    assert dim.alpha() < 255
    assert table.table.item(0, 1).textAlignment() & Qt.AlignRight
    assert table.table.item(0, 0).textAlignment() & Qt.AlignLeft


def test_usage_column_hidden_when_no_metric_has_limit(table, db):
    # 夹具里有 GDI 对象数（有上限），列可见
    assert not table.table.isColumnHidden(USAGE_COLUMN)
    assert table.table.item(1, USAGE_COLUMN).text() == "3.0% / 10000"
    # 只剩没有上限的指标时整列隐藏，所需宽度随之减少
    wide = table.contents_width()
    table.set_rows([build_summary_row(MEM, _snapshot(db, MEM, 1200, lambda i: 102400.0 + i))], "x")
    assert table.table.isColumnHidden(USAGE_COLUMN)
    assert table.contents_width() < wide
    assert not table.table.isColumnHidden(VERDICT_COLUMN)


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


def _pill_color(table, row):
    """胶囊样式里显式给出的文字色。"""
    match = re.search(r"[^-]color:\s*([^;]+);", table.table.cellWidget(row, VERDICT_COLUMN).styleSheet())
    assert match, "胶囊样式应显式给出文字色"
    return match.group(1).strip()


def test_pill_text_color_not_disabled(table):
    # 置灰靠样式里的文字色表达，不再禁用控件（禁用态文字在深色胶囊底上看不清）
    assert table.table.cellWidget(0, VERDICT_COLUMN).isEnabled()
    assert table.table.cellWidget(1, VERDICT_COLUMN).isEnabled()
    assert _pill_color(table, 0) != _pill_color(table, 1)


def _show_at(table, width):
    table.resize(width, 300)
    table.show()
    QApplication.processEvents()
    return table.table.horizontalHeader()


def _assert_no_column_squeezed(table, header):
    assert header.sectionSize(0) >= table.metric_column_floor()
    assert header.sectionSize(0) >= table.table.sizeHintForColumn(0)
    for c in range(1, len(SUMMARY_COLUMNS)):
        assert header.sectionSize(c) >= table.table.sizeHintForColumn(c)


def test_columns_stretch_metric_when_wide(table):
    header = _show_at(table, 1600)
    assert not header.stretchLastSection()
    assert all(header.sectionResizeMode(c) == QHeaderView.ResizeToContents
               for c in range(1, len(SUMMARY_COLUMNS)))
    assert table.columns_fit()
    assert header.sectionResizeMode(0) == QHeaderView.Stretch
    assert not table.table.scrollDelagate.hScrollBar.isVisible()
    _assert_no_column_squeezed(table, header)


def test_columns_scroll_instead_of_truncating_when_narrow(table):
    _show_at(table, 1600)
    wide_height = table.table.height()
    header = _show_at(table, 700)
    assert not table.columns_fit()
    assert header.sectionResizeMode(0) == QHeaderView.Interactive
    _assert_no_column_squeezed(table, header)
    # qfluentwidgets 用浮在表格底边的滚动条替代原生横向滚动条（原生的恒为隐藏）
    assert table.table.scrollDelagate.hScrollBar.isVisible()
    assert table.table.height() == wide_height + H_SCROLLBAR_SPACE

    # 由窄变宽：恢复拉伸、滚动条消失、高度复原
    header = _show_at(table, 1600)
    assert table.columns_fit()
    assert header.sectionResizeMode(0) == QHeaderView.Stretch
    assert not table.table.scrollDelagate.hScrollBar.isVisible()
    assert table.table.height() == wide_height


def test_theme_switch_restyles_pills_and_keeps_selection(table):
    picked = []
    table.metric_selected.connect(picked.append)
    table.select_metric(GDI)
    light = (_pill_color(table, 0), _pill_color(table, 1))
    original = qconfig._theme
    try:
        qconfig._theme = Theme.DARK
        qconfig.themeChanged.emit(Theme.DARK)
        dark = (_pill_color(table, 0), _pill_color(table, 1))
        assert dark != light and dark[0] != dark[1]
        assert dark[1].startswith("rgba(255, 255, 255")
        assert table.table.item(1, 1).foreground().color().red() == 255
        # 切换主题只换颜色：不重建行、不改选中、不发 metric_selected
        assert table.selected_metric() == GDI and picked == []
    finally:
        qconfig._theme = original
        qconfig.themeChanged.emit(original)
    assert (_pill_color(table, 0), _pill_color(table, 1)) == light


def test_pill_and_dimmed_colors_come_from_theme_tokens(table):
    """胶囊底色按判定语气取自 ui.theme_tokens（与任务卡片状态胶囊同源），文字色也在那里；
    置灰行文字对卡片底仍 ≥ 4.5:1。"""
    from ui import theme_tokens as tokens
    from ui.color_contrast import TEXT_MIN_CONTRAST, contrast_ratio

    pill = table.table.cellWidget(0, VERDICT_COLUMN)
    style = pill.styleSheet()
    assert tokens.pill_background(tokens.verdict_tone(pill.text())) in style
    assert _pill_color(table, 0) == tokens.pick(tokens.PILL_TEXT)
    assert _pill_color(table, 1) == tokens.pick(tokens.PILL_TEXT_DIMMED)
    dim = table.table.item(1, 1).foreground().color()
    assert dim == tokens.qcolor(tokens.DIMMED_TEXT)
    for i in (0, 1):
        assert contrast_ratio(tokens.DIMMED_TEXT[i], tokens.SURFACE[i]) >= TEXT_MIN_CONTRAST


# ---------- 分析页的显示规则（build_table_row）：平稳写"—"、上限占比只写百分比、样本不足 ⓘ ----------


def test_table_row_stable_dash_and_usage_percent(db):
    from ui.formatting import STABLE_DASH, STABLE_TIP_PREFIX, build_table_row
    snapshot = _snapshot(db, GDI, 1200, lambda i: 300.0, baseline=100,
                         markers=((400, "第 1 轮"), (700, "第 2 轮")))
    original = build_summary_row(GDI, snapshot)
    row = build_table_row(GDI, snapshot)
    assert row.text("verdict") == "平稳"
    assert (row.text("rate"), row.text("per_round")) == (STABLE_DASH, STABLE_DASH)
    assert row.tooltips["rate"].startswith(STABLE_TIP_PREFIX + original.text("rate"))
    assert row.tooltips["per_round"].startswith(STABLE_TIP_PREFIX + original.text("per_round"))
    # 上限占比：单元格只写百分比，原文（占比 / 上限）与说明进 tooltip
    assert original.text("usage") == "3.0% / 10000"
    assert row.text("usage") == "3%"
    assert "3.0% / 10000" in row.tooltips["usage"] and "上限" in row.tooltips["usage"]
    assert not row.dimmed and row.note == ""


def test_table_row_rising_keeps_rate(db):
    from ui.formatting import build_table_row
    snapshot = _snapshot(db, MEM, 1200, lambda i: 102400.0 + i * 10)
    assert build_table_row(MEM, snapshot).texts == build_summary_row(MEM, snapshot).texts


def test_insufficient_row_gets_info_mark_with_reason(qapp, db):
    from ui.formatting import INFO_MARK, NO_DATA_NOTE, build_table_row
    row = build_table_row(GDI, _snapshot(db, GDI, 11, lambda i: 300.0))
    assert row.dimmed
    assert row.note.startswith("数据不足（已采 10秒）；至少采集 10分钟")
    assert build_table_row(MEM, None).note == NO_DATA_NOTE
    widget = SummaryTable()
    try:
        widget.set_rows([build_table_row(MEM, _snapshot(db, MEM, 1200, lambda i: 102400.0 + i)),
                         row])
        assert widget.pill_text(0) == "持续上涨"
        assert widget.pill_text(1) == f"数据不足 {INFO_MARK}"
        assert row.note in widget.table.cellWidget(1, VERDICT_COLUMN).toolTip()
        # 胶囊配色仍按判定取（不受 ⓘ 影响）
        assert _pill_color(widget, 1) != _pill_color(widget, 0)
    finally:
        widget.close()


def test_header_hidden_keeps_basis_text(qapp, db):
    widget = SummaryTable(show_header=False)
    try:
        widget.set_rows([build_summary_row(MEM, None)], "21分钟 · 1,200 点")
        widget.show()
        assert not widget.title_label.isVisible() and not widget.basis_label.isVisible()
        assert widget.basis() == "依据：21分钟 · 1,200 点"
    finally:
        widget.close()
