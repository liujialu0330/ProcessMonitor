"""
PageScaffold / EmptyStateCard（离屏）：
- 骨架：透明无边框滚动区、页边距 24、标题/副标题（空副标题隐藏）、页头命令区按序追加、
  内容区按序追加区块；导出页基于它构建；
- 空状态卡：标题/说明/图标/可选操作按钮，set_message 更换文案、空说明隐藏。
"""
import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QScrollArea, QWidget
from qfluentwidgets import FluentIcon, PushButton

from ui.widgets.empty_state import EmptyStateCard
from ui.widgets.page_scaffold import PAGE_MARGIN, SECTION_SPACING, PageScaffold


@pytest.fixture
def page(qapp):
    widget = PageScaffold("导出数据", "副标题说明")
    widget.resize(800, 600)
    yield widget
    widget.close()


def test_scroll_shell(page):
    assert isinstance(page, QScrollArea)
    assert page.widgetResizable()
    assert page.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    assert page.widget() is page.container
    assert "transparent" in page.styleSheet() and "border: none" in page.styleSheet()


def test_margins_and_spacing(page):
    margins = page.content_layout.contentsMargins()
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (
        PAGE_MARGIN,) * 4 == (24,) * 4
    assert page.content_layout.spacing() == SECTION_SPACING
    custom = PageScaffold("标题", spacing=16)
    try:
        assert custom.content_layout.spacing() == 16
    finally:
        custom.close()


def test_title_and_subtitle(page):
    assert page.title_label.text() == "导出数据"
    assert page.subtitle_label.text() == "副标题说明"
    assert page.subtitle_label.isVisibleTo(page)
    page.set_title("新标题")
    page.set_subtitle("")
    assert page.title_label.text() == "新标题"
    assert not page.subtitle_label.isVisibleTo(page)
    page.set_subtitle("又有了")
    assert page.subtitle_label.isVisibleTo(page)


def test_subtitle_hidden_when_not_given(qapp):
    bare = PageScaffold("只有标题")
    try:
        assert not bare.subtitle_label.isVisibleTo(bare)
    finally:
        bare.close()


def test_header_commands_and_sections_in_order(page):
    first = page.add_header_widget(PushButton("刷新"))
    second = page.add_header_widget(PushButton("更多"))
    layout = page.command_layout
    assert [layout.itemAt(i).widget() for i in range(layout.count())] == [first, second]

    section_a = page.add_section(QWidget())
    section_b = page.add_section(QWidget(), stretch=1)
    widgets = [page.content_layout.itemAt(i).widget()
               for i in range(page.content_layout.count())]
    # 第 0 项是页头布局，其后按添加顺序排列
    assert page.content_layout.itemAt(0).layout() is page.header_layout
    assert widgets[1:] == [section_a, section_b]
    assert page.content_layout.stretch(2) == 1

    page.show()
    # 命令区在标题右侧
    assert first.mapTo(page, first.rect().topLeft()).x() > page.title_label.x()


def test_analysis_page_is_built_on_scaffold(qapp, db):
    from core.monitor_manager import MonitorManager
    from tests.factories import make_analysis_page
    from tests.helpers import SyncHistoryRunner
    page = make_analysis_page(db, MonitorManager(db=db), worker_runner=SyncHistoryRunner(db),
                              defer=lambda fn: fn())
    try:
        assert isinstance(page, PageScaffold)
        assert page.title_label.text() == "分析"
        # 工具条是第一段内容，任务选择器嵌在工具条里
        assert page.toolbar.isAncestorOf(page.task_selector)
    finally:
        page.auto_refresh.stop()
        page.close()


# ---------------------------------------------------------------- 空状态卡


def test_empty_state_card_texts(qapp):
    card = EmptyStateCard(FluentIcon.HISTORY, "暂无历史数据", "完成一次监控后，数据会显示在这里")
    try:
        assert card.title_label.text() == "暂无历史数据"
        assert card.hint_label.text() == "完成一次监控后，数据会显示在这里"
        assert card.hint_label.isVisibleTo(card)
        assert card.action_button is None
        assert card.title_label.alignment() & Qt.AlignHCenter

        card.set_message("读取数据失败", "")
        assert card.title_label.text() == "读取数据失败"
        assert not card.hint_label.isVisibleTo(card)
        card.set_message("暂无历史数据", "说明回来了")
        assert card.hint_label.isVisibleTo(card)
        card.set_icon(FluentIcon.INFO)
    finally:
        card.close()


def test_empty_state_card_with_action_button(qapp):
    clicked = []
    button = PushButton("刷新")
    button.clicked.connect(lambda: clicked.append(1))
    card = EmptyStateCard(FluentIcon.SYNC, "未找到进程", action_button=button)
    try:
        assert card.action_button is button
        assert button.parent() is card
        assert not card.hint_label.isVisibleTo(card)
        button.click()
        assert clicked == [1]
        labels = [w.text() for w in card.findChildren(QLabel)]
        assert "未找到进程" in labels
    finally:
        card.close()
