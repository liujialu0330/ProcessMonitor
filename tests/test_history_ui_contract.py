"""历史页 Fluent 信息架构契约：数据优先的默认形态与明细表阅读轨道。"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt

from core.history_query import DEFAULT_RANGE_KEY
from core.monitor_manager import MonitorManager
from tests.factories import make_history_page, make_services
from tests.helpers import SyncHistoryRunner
from ui.typography import TypeScale
from ui.widgets.history.sample_table import (
    DETAIL_TABLE_MAX_WIDTH, DETAIL_TABLE_ROW_HEIGHT)
from utils.metrics import MetricType


@pytest.fixture
def new_page(qapp, db):
    """按需构造历史页（全替身服务），返回构造函数供同一用例多次调用。"""
    manager = MonitorManager(db=db)
    return lambda: make_history_page(db, manager, make_services(),
                                     worker_runner=SyncHistoryRunner(db), defer=lambda f: f())


def _show_detail(page):
    page.content_stack.setCurrentWidget(page.analysis_page)
    page.filter_bar.view_segmented.setCurrentItem('detail')


def test_history_page_uses_compact_analysis_contract(qapp, new_page):
    """统计、子视图和原始值列保持数据优先的默认形态。"""
    page = new_page()

    assert DEFAULT_RANGE_KEY == 'all'
    assert page.vm.range_key == page.filter_bar.current_range() == 'all'
    assert tuple(page.stats_strip.texts()) == (
        "开始值", "当前值", "变化量", "每小时增长", "最小", "最大")
    assert page.view_stack.currentWidget() is page.chart_view
    assert page.sample_table.data_table.isColumnHidden(4) is True

    page.filter_bar.view_segmented.setCurrentItem('detail')
    qapp.processEvents()
    assert page.view_stack.currentWidget() is page.sample_table

    page.filter_bar.raw_value_action.setChecked(True)
    qapp.processEvents()
    assert page.sample_table.data_table.isColumnHidden(4) is False

    page.close()


def test_history_detail_table_uses_centered_bounded_data_track(qapp, new_page):
    """宽屏明细保持居中阅读轨道，表头、正文和数据字体使用同一契约。"""
    page = new_page()
    page.resize(1400, 760)
    _show_detail(page)
    page.show()
    qapp.processEvents()

    table = page.sample_table.data_table
    content = page.sample_table.detail_table_content
    card = content.parentWidget()
    assert content.width() == DETAIL_TABLE_MAX_WIDTH
    assert abs(content.x() - (card.width() - content.width()) / 2) <= 1
    assert table.width() == content.width()
    assert table.verticalHeader().defaultSectionSize() == DETAIL_TABLE_ROW_HEIGHT

    # 明细只展示单一指标、单位稳定；时间和值使用等宽数据字体并在各自列中居中
    points = [
        SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 54, 51), value=185845.76),
        SimpleNamespace(timestamp=datetime(2026, 7, 16, 20, 55, 14), value=185845.92),
    ]
    page.sample_table.set_points(points, MetricType.MEMORY_RSS, display_unit='MB')
    qapp.processEvents()

    horizontal_mask = int(Qt.AlignLeft | Qt.AlignRight | Qt.AlignHCenter)
    assert (table.horizontalHeader().defaultAlignment() & horizontal_mask) == Qt.AlignHCenter
    assert [table.horizontalHeaderItem(c).text() for c in range(5)] == [
        '时间', '数值', '较上次', '较基线', '原始值']
    for column in range(5):
        item = table.item(0, column)
        assert (item.textAlignment() & horizontal_mask) == Qt.AlignHCenter
        assert item.font().pixelSize() == TypeScale.BODY
        assert item.font().fixedPitch() is True

    hidden_widths = [table.columnWidth(c) for c in range(4)]
    assert max(hidden_widths) - min(hidden_widths) <= 1

    page.filter_bar.raw_value_action.setChecked(True)
    qapp.processEvents()
    visible_widths = [table.columnWidth(c) for c in range(5)]
    assert max(visible_widths) - min(visible_widths) <= 1
    assert table.horizontalScrollBar().maximum() == 0
    page.close()

    # 窄窗口不保留人为留白，轨道回到卡片的 16px 标准内边距并继续五列等分
    narrow = new_page()
    narrow.resize(800, 760)
    _show_detail(narrow)
    narrow.filter_bar.raw_value_action.setChecked(True)
    narrow.show()
    qapp.processEvents()
    narrow_content = narrow.sample_table.detail_table_content
    assert narrow_content.width() == narrow_content.parentWidget().width() - 32
    assert narrow_content.x() == 16
    narrow_widths = [narrow.sample_table.data_table.columnWidth(c) for c in range(5)]
    assert max(narrow_widths) - min(narrow_widths) <= 1
    assert narrow.sample_table.data_table.horizontalScrollBar().maximum() == 0
    narrow.close()
