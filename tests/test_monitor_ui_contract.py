"""实时监控页 Fluent 布局契约。"""

from core.contracts import UnavailableReason
from core.monitor_manager import MonitorManager
from tests.factories import make_monitor_page


def test_interval_spinbox_keeps_maximum_value_visible(qapp, db):
    """周期输入框必须为范围最大值保留完整的文本编辑区域。"""
    # 替身进程表为空，构造时不触达本机进程
    page = make_monitor_page(db, MonitorManager(db=db))
    page.resize(800, 600)
    page.show()

    spinbox = page.interval_spinbox
    spinbox.setValue(spinbox.maximum())
    qapp.processEvents()

    value_width = spinbox.fontMetrics().horizontalAdvance(spinbox.text())
    assert spinbox.lineEdit().width() >= value_width

    page.close()


def test_task_card_mark_unavailable_shows_text_and_tooltip(qapp):
    """不可用指标显示"不可用"并给出原因；之后采到值会正常覆盖"""
    from ui.pages.monitor_page import TaskCard
    card = TaskCard("t1", "demo.exe", 1234, ["memory_rss", "memory_uss"])

    card.mark_unavailable({"memory_uss": UnavailableReason.ACCESS_DENIED})
    label = card.value_labels["memory_uss"]
    assert label.text() == "不可用"
    assert "权限不足" in label.toolTip()
    assert "不会记为 0" in label.toolTip()
    assert card.value_labels["memory_rss"].text() == "--"

    card.update_values({"memory_uss": 2048.0})
    assert label.text() == "2 MB"


def test_monitor_page_and_task_card_take_time_from_injected_clock():
    """卡片结束时间、打点时刻都由页面经注入的 Clock 取得：两处源码都不再直接读系统时间"""
    import inspect

    import ui.components.task_card as task_card_module
    import ui.pages.monitor_page as monitor_module
    for module in (monitor_module, task_card_module):
        assert "datetime.now" not in inspect.getsource(module), module.__name__
