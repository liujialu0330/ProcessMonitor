"""历史页 v1.3.0 后续 Fluent 信息架构契约。"""

from ui.pages.history_page import HistoryPage, DEFAULT_TIME_RANGE_KEY


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
