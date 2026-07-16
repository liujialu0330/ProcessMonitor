"""实时监控页 Fluent 布局契约。"""

from ui.pages.monitor_page import MonitorPage


def test_interval_spinbox_keeps_maximum_value_visible(qapp, monkeypatch):
    """周期输入框必须为范围最大值保留完整的文本编辑区域。"""
    monkeypatch.setattr(MonitorPage, "_refresh_process_list", lambda self: None)
    page = MonitorPage()
    page.resize(800, 600)
    page.show()

    spinbox = page.interval_spinbox
    spinbox.setValue(spinbox.maximum())
    qapp.processEvents()

    value_width = spinbox.fontMetrics().horizontalAdvance(spinbox.text())
    assert spinbox.lineEdit().width() >= value_width

    page.close()
