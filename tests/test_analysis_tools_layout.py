"""分析页低频工具按需展开，任务/成员工具条按实际宽度换行。"""
import pytest
from PyQt5.QtCore import QPoint, QRect, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import Theme

from tests.helpers import dispose_widget
from tests.visual.snapshot import theme_applied
from ui.models.task_options import TaskOption
from ui.widgets.analysis.member_selector import MemberSelector
from ui.widgets.analysis.toolbar import AnalysisToolbar
from ui.widgets.page_scaffold import PageScaffold
from ui.widgets.task_selector import TaskSelector


@pytest.fixture(autouse=True)
def concrete_theme():
    # 正常入口会解析 AUTO；纯控件测试也需要真实样式，而不是不存在的 auto/*.qss。
    with theme_applied(Theme.LIGHT):
        yield


def _settle():
    for _ in range(4):
        QApplication.processEvents()


def test_member_selector_wraps_without_horizontal_feedback(qapp):
    page = PageScaffold('分析')
    toolbar = AnalysisToolbar()
    selector = TaskSelector()
    selector.set_options([TaskOption(
        'task', '长进程名.exe · PID 1234 · 备注' * 8, '完整任务信息', False)], 'task')
    toolbar.set_task_selector(selector)
    toolbar.set_member_selector(MemberSelector())
    toolbar.set_members_visible(True)
    page.add_section(toolbar)
    page.breakpoints.tier_changed.connect(toolbar.set_tier)
    page.show()
    try:
        for width, inline in ((1280, True), (950, False), (800, False),
                              (1280, True), (950, False)):
            page.resize(width, 700)
            _settle()
            assert page.horizontalScrollBar().maximum() == 0
            assert (toolbar.top.indexOf(toolbar.member_box) >= 0) is inline
            # 搜索与下拉不能伸进右侧操作区；长文本应在现有控件里省略。
            right = selector.mapTo(toolbar, QPoint(selector.width(), 0)).x()
            assert right <= toolbar.compare_button.x()
            for widget in (selector, toolbar.compare_button, toolbar.export_button,
                           toolbar.refresh_button, toolbar.more_button, toolbar.member_box):
                rectangle = QRect(widget.mapTo(toolbar, QPoint()), widget.size())
                assert toolbar.rect().contains(rectangle)
    finally:
        dispose_widget(page)


def test_analysis_header_toggles_keep_optional_tools_accessible(qapp, db):
    from core.monitor_manager import MonitorManager
    from tests.factories import make_services
    from tests.helpers import SyncHistoryRunner
    from ui.pages.analysis_page import AnalysisPage

    services = make_services()
    page = AnalysisPage(db, MonitorManager(db), services,
                        worker_runner=SyncHistoryRunner(db), defer=lambda fn: fn())
    page.resize(800, 820)
    page.show()
    try:
        _settle()
        assert page.reference_controls.isHidden() and page.task_library.isHidden()
        assert page.reference_toggle.isVisible()
        assert page.task_library.toggle_button.isVisible()
        assert page.horizontalScrollBar().maximum() == 0
        for button in (page.reference_toggle, page.task_library.toggle_button):
            rectangle = QRect(button.mapTo(page.viewport(), QPoint()), button.size())
            assert page.viewport().rect().contains(rectangle)

        page.reference_toggle.setFocus()
        QTest.keyClick(page.reference_toggle, Qt.Key_Space)
        assert page.reference_controls.isVisible()
        page.reference_controls.scenario_edit.setText('窗口操作')
        page.reference_toggle.click()
        assert page.reference_controls.isHidden()
        page.reference_toggle.click()
        assert page.reference_controls.scenario_edit.text() == '窗口操作'

        page.task_library.toggle_button.click()
        assert page.task_library.isVisible() and page.task_library.body.isVisible()
        page.task_library.search_edit.setText('worker')
        page.task_library.toggle_button.click()
        assert page.task_library.isHidden()
        assert page.task_library.toggle_button.isVisible()
        page.task_library.toggle_button.click()
        assert page.task_library.isVisible()
        assert page.task_library.search_edit.text() == 'worker'
        _settle()
        assert page.horizontalScrollBar().maximum() == 0
    finally:
        services.threads.cancel_all()
        services.threads.shutdown_all()
        dispose_widget(page)
