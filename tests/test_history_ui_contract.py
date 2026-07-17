"""历史页 v1.3.0 后续 Fluent 信息架构契约。"""

from datetime import datetime
from types import SimpleNamespace

from PyQt5.QtCore import Qt

from ui.pages.history_page import (
    DEFAULT_TIME_RANGE_KEY,
    DETAIL_TABLE_MAX_WIDTH,
    DETAIL_TABLE_ROW_HEIGHT,
    HistoryPage,
)
from ui.typography import TypeScale
from utils.metrics import MetricType


def test_history_page_uses_compact_analysis_contract(qapp, db):
    """统计、子视图和原始值列保持数据优先的默认形态。"""
    page = HistoryPage(db=db)

    assert DEFAULT_TIME_RANGE_KEY == '1h'
    assert page.current_range_key == '1h'
    assert tuple(page.stat_value_labels) == ("当前", "最小", "最大", "平均")
    assert page.view_stack.currentIndex() == 0
    assert page.data_table.isColumnHidden(2) is True

    page.view_segmented.setCurrentItem('detail')
    qapp.processEvents()
    assert page.view_stack.currentIndex() == 1

    page.raw_value_action.setChecked(True)
    qapp.processEvents()
    assert page.data_table.isColumnHidden(2) is False

    page.close()


def test_history_filters_wrap_at_narrow_width(qapp, db):
    """窄内容区时，时间范围必须换行而不是横向裁切。"""
    page = HistoryPage(db=db)
    page._update_filter_layout(600)

    index = page.select_layout.indexOf(page.range_segmented)
    row, column, row_span, column_span = page.select_layout.getItemPosition(index)
    assert (row, column, row_span, column_span) == (2, 1, 1, 3)

    page._update_filter_layout(900)
    index = page.select_layout.indexOf(page.range_segmented)
    row, column, row_span, column_span = page.select_layout.getItemPosition(index)
    assert (row, column, row_span, column_span) == (1, 3, 1, 1)

    page.close()


def test_history_detail_table_uses_centered_bounded_data_track(qapp, db):
    """宽屏明细保持居中阅读轨道，表头、正文和数据字体使用同一契约。"""
    page = HistoryPage(db=db)
    page.resize(1400, 760)
    page.content_stack.setCurrentWidget(page.analysis_page)
    page.view_segmented.setCurrentItem('detail')
    page.show()
    qapp.processEvents()

    content = page.detail_table_content
    card = content.parentWidget()
    assert content.width() == DETAIL_TABLE_MAX_WIDTH
    assert abs(content.x() - (card.width() - content.width()) / 2) <= 1
    assert page.data_table.width() == content.width()
    assert page.data_table.verticalHeader().defaultSectionSize() == (
        DETAIL_TABLE_ROW_HEIGHT)

    # 当前明细只展示单一指标、单位稳定；时间和值使用等宽数据字体并在各自列中
    # 居中，避免表头居中而正文贴向表格两端的宽屏错位。
    points = [
        SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 54, 51),
                        value=185845.76),
        SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 55, 14),
                        value=185845.92),
    ]
    page._chart_display_unit = 'MB'
    page._update_table(points, MetricType.MEMORY_RSS)
    qapp.processEvents()

    horizontal_mask = int(Qt.AlignLeft | Qt.AlignRight | Qt.AlignHCenter)
    assert (page.data_table.horizontalHeader().defaultAlignment()
            & horizontal_mask) == Qt.AlignHCenter
    assert [
        page.data_table.horizontalHeaderItem(column).text()
        for column in range(3)
    ] == ['采样时间', '指标值', '原始值']

    for column in range(3):
        item = page.data_table.item(0, column)
        assert (item.textAlignment() & horizontal_mask) == Qt.AlignHCenter
        assert item.font().pixelSize() == TypeScale.BODY
        assert item.font().fixedPitch() is True

    hidden_widths = [
        page.data_table.columnWidth(column) for column in (0, 1)
    ]
    assert max(hidden_widths) - min(hidden_widths) <= 1

    page.raw_value_action.setChecked(True)
    qapp.processEvents()
    visible_widths = [
        page.data_table.columnWidth(column) for column in range(3)
    ]
    assert max(visible_widths) - min(visible_widths) <= 1
    assert page.data_table.horizontalScrollBar().maximum() == 0

    page.close()

    # 窄窗口不保留人为留白，轨道应回到卡片的 16px 标准内边距并继续三列等分。
    narrow_page = HistoryPage(db=db)
    narrow_page.resize(800, 760)
    narrow_page.content_stack.setCurrentWidget(narrow_page.analysis_page)
    narrow_page.view_segmented.setCurrentItem('detail')
    narrow_page.raw_value_action.setChecked(True)
    narrow_page.show()
    qapp.processEvents()
    narrow_content = narrow_page.detail_table_content
    narrow_card = narrow_content.parentWidget()
    assert narrow_content.width() == narrow_card.width() - 32
    assert narrow_content.x() == 16
    narrow_widths = [
        narrow_page.data_table.columnWidth(column) for column in range(3)
    ]
    assert max(narrow_widths) - min(narrow_widths) <= 1
    assert narrow_page.data_table.horizontalScrollBar().maximum() == 0

    narrow_page.close()
