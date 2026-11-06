"""
VisibleTimer（离屏）：宿主显示时开始计时、隐藏时停止；构造时宿主已可见则立即计时；
set_interval 生效；到时调用回调。
"""
import pytest
from PyQt5.QtWidgets import QWidget

from tests.helpers import poll_until
from ui.widgets.visible_timer import VisibleTimer


@pytest.fixture
def host(qapp):
    widget = QWidget()
    yield widget
    widget.close()


def test_follows_show_and_hide(host):
    timer = VisibleTimer(host, 30_000, lambda: None)
    assert not timer.is_active()
    host.show()
    assert timer.is_active()
    host.hide()
    assert not timer.is_active()
    host.show()
    assert timer.is_active()


def test_starts_immediately_when_host_already_visible(host):
    host.show()
    timer = VisibleTimer(host, 30_000, lambda: None)
    assert timer.is_active()


def test_manual_start_stop_until_next_visibility_change(host):
    timer = VisibleTimer(host, 30_000, lambda: None)
    timer.start()
    assert timer.is_active()
    timer.stop()
    assert not timer.is_active()
    host.show()
    assert timer.is_active()


def test_child_hidden_with_parent(qapp):
    """宿主是子控件、父窗口隐藏时（如主窗口收进托盘）同样停止。"""
    parent = QWidget()
    child = QWidget(parent)
    timer = VisibleTimer(child, 30_000, lambda: None)
    parent.show()
    try:
        assert timer.is_active()
        parent.hide()
        assert not timer.is_active()
    finally:
        parent.close()


def test_set_interval(host):
    timer = VisibleTimer(host, 30_000, lambda: None)
    timer.set_interval(5_000)
    assert timer.interval() == 5_000
    host.show()
    timer.set_interval(1_000)
    assert timer.interval() == 1_000 and timer.is_active()


def test_callback_fires_while_visible(qapp, host):
    calls = []
    VisibleTimer(host, 10, lambda: calls.append(1))
    host.show()
    from PyQt5.QtWidgets import QApplication
    assert poll_until(lambda: (QApplication.processEvents() or True) and len(calls) >= 2,
                      timeout=5)
