"""
历史页明细表控件 SampleTable：每格文本与提示文案（与历史页安全网/流程测试逐字一致）、
倒序、原始值列开关、清空、居中阅读轨道与数据字体契约。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt

from data.models import DataPoint
from tests.helpers import fetch_history_render_inputs
from tests.test_safety_net_history import (
    BASE, MAX_BUCKETS, QUERY_CASES, TABLE_LIMIT, _seed_query_dataset)
from ui.typography import TypeScale
from ui.widgets.history.chart_view import chart_display_unit
from ui.widgets.history.sample_table import (
    DETAIL_TABLE_MAX_WIDTH, DETAIL_TABLE_ROW_HEIGHT, SampleTable)
from utils.metrics import MetricType

# 页面左右边距 24：原页面里明细卡片宽度 = 视口 - 48
PAGE_MARGINS = 48


@pytest.fixture
def table(qapp):
    widget = SampleTable()
    yield widget
    widget.close()


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, stat_texts",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_rows_and_hint_match_history_safety_net(
        table, db, metric, range_key, since_minutes, rows, count, buckets,
        markers, stat_texts):
    task = _seed_query_dataset(db)
    since = None if since_minutes is None else BASE + timedelta(minutes=since_minutes)
    table_points, stats, chart_points, _markers = fetch_history_render_inputs(
        db, task.task_id, metric, since, TABLE_LIMIT, MAX_BUCKETS)
    unit = chart_display_unit(metric, [p.value for p in chart_points])

    table.set_points(table_points, metric, stats['count'], unit)

    texts = table.row_texts()
    assert len(texts) == rows
    assert table.table_hint_label.text() == (
        f"最新数据优先 · 显示最近 {rows:,} 条 / 共 {count:,} 条")
    # 倒序：第一行是范围内最新一条，最后一行是最早一条
    newest, oldest = table_points[-1], table_points[0]
    assert texts[0][0] == newest.timestamp.strftime('%m-%d %H:%M:%S')
    assert texts[-1][0] == oldest.timestamp.strftime('%m-%d %H:%M:%S')
    assert texts[0][2] == f"{newest.value:.4f}"
    # 当前值列与统计条"当前"同单位、同格式
    assert texts[0][1] == stat_texts["当前"]


def test_hint_shown_over_total_and_thousands_separator(table):
    base = datetime(2026, 7, 16, 20, 0, 0)
    points = [DataPoint("t", base + timedelta(seconds=i), 1024.0 * (100 + i),
                        MetricType.MEMORY_PRIVATE) for i in range(4)]
    table.set_points(points, MetricType.MEMORY_PRIVATE, total_count=10)
    assert table.table_hint_label.text() == "最新数据优先 · 显示最近 4 条 / 共 10 条"
    assert table.data_table.rowCount() == 4

    threads = [DataPoint("t", base + timedelta(seconds=i), 1.0, MetricType.NUM_THREADS)
               for i in range(3)]
    table.set_points(threads, MetricType.NUM_THREADS, total_count=12345)
    assert "共 12,345 条" in table.table_hint_label.text()

    # 总数缺省或小于已显示条数时按已显示条数
    table.set_points(threads, MetricType.NUM_THREADS)
    assert table.table_hint_label.text() == "最新数据优先 · 显示最近 3 条 / 共 3 条"
    table.set_points(threads, MetricType.NUM_THREADS, total_count=1)
    assert table.table_hint_label.text().endswith("共 3 条")


def test_display_unit_is_applied_to_value_column(table):
    points = [SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 54, 51), value=185845.76)]
    table.set_points(points, MetricType.MEMORY_RSS, display_unit='MB')
    assert table.row_texts() == [('07-16 20:54:51', '181.49 MB', '185845.7600')]


def test_clear_restores_default_hint(table):
    points = [SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 0, 0), value=1.0)]
    table.set_points(points, MetricType.NUM_THREADS, total_count=5)
    table.clear()
    assert table.data_table.rowCount() == 0
    assert table.table_hint_label.text() == "最新数据优先"


def test_raw_value_column_hidden_by_default_and_toggles(table):
    assert table.data_table.isColumnHidden(2) is True
    assert table.raw_values_visible() is False
    table.set_raw_values(True)
    assert table.data_table.isColumnHidden(2) is False
    table.set_raw_values(False)
    assert table.data_table.isColumnHidden(2) is True


def test_wide_track_is_centered_and_bounded(qapp, table):
    """宽屏：阅读轨道最宽 960 并在卡片内居中；表头、正文与数据字体契约不变。"""
    table.resize(1400 - PAGE_MARGINS, 700)
    table.show()
    qapp.processEvents()

    content = table.detail_table_content
    assert content.parentWidget() is table
    assert content.width() == DETAIL_TABLE_MAX_WIDTH
    assert abs(content.x() - (table.width() - content.width()) / 2) <= 1
    assert table.data_table.width() == content.width()
    assert table.data_table.verticalHeader().defaultSectionSize() == DETAIL_TABLE_ROW_HEIGHT

    points = [
        SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 54, 51), value=185845.76),
        SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 55, 14), value=185845.92),
    ]
    table.set_points(points, MetricType.MEMORY_RSS, display_unit='MB')
    qapp.processEvents()

    horizontal_mask = int(Qt.AlignLeft | Qt.AlignRight | Qt.AlignHCenter)
    data_table = table.data_table
    assert (data_table.horizontalHeader().defaultAlignment()
            & horizontal_mask) == Qt.AlignHCenter
    assert [data_table.horizontalHeaderItem(c).text() for c in range(3)] == [
        '采样时间', '指标值', '原始值']
    for column in range(3):
        item = data_table.item(0, column)
        assert (item.textAlignment() & horizontal_mask) == Qt.AlignHCenter
        assert item.font().pixelSize() == TypeScale.BODY
        assert item.font().fixedPitch() is True

    hidden_widths = [data_table.columnWidth(c) for c in (0, 1)]
    assert max(hidden_widths) - min(hidden_widths) <= 1
    table.set_raw_values(True)
    qapp.processEvents()
    visible_widths = [data_table.columnWidth(c) for c in range(3)]
    assert max(visible_widths) - min(visible_widths) <= 1
    assert data_table.horizontalScrollBar().maximum() == 0


def test_narrow_track_fills_card_padding(qapp, table):
    """窄窗口：轨道回到卡片 16px 标准内边距，三列等分且无横向滚动。"""
    table.resize(800 - PAGE_MARGINS, 700)
    table.set_raw_values(True)
    table.show()
    qapp.processEvents()

    content = table.detail_table_content
    assert content.width() == table.width() - 32
    assert content.x() == 16
    widths = [table.data_table.columnWidth(c) for c in range(3)]
    assert max(widths) - min(widths) <= 1
    assert table.data_table.horizontalScrollBar().maximum() == 0
