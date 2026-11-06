"""
托盘控制器 TrayController：托盘不可用时全部安全空操作；可用时创建图标、菜单两个
动作分别发 show_requested / quit_requested，单击/双击还原，气泡按 kind 选图标。
offscreen 下系统托盘不可用，"可用"分支通过替换 isSystemTrayAvailable 模拟。
"""
import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon, QWidget

from ui.tray import TrayController


@pytest.fixture
def host(qapp):
    widget = QWidget()
    yield widget
    widget.deleteLater()


@pytest.fixture
def available_tray(host, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', staticmethod(lambda: True))
    tray = TrayController(host, QIcon(), "进程监控助手")
    yield tray
    tray.hide()


def _record(signal) -> list:
    hits = []
    signal.connect(lambda: hits.append(1))
    return hits


def test_unavailable_tray_is_safe_noop(host, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, 'isSystemTrayAvailable', staticmethod(lambda: False))
    tray = TrayController(host, QIcon(), "进程监控助手")

    assert tray.is_available() is False
    assert tray.icon is None
    tray.show_message("标题", "内容", 'warning')
    tray.set_tooltip("x")
    tray.hide()


def test_available_tray_creates_icon_with_menu(available_tray):
    tray = available_tray
    assert tray.is_available() is True
    assert isinstance(tray.icon, QSystemTrayIcon)
    assert tray.icon.toolTip() == "进程监控助手"
    actions = tray.icon.contextMenu().actions()
    assert [a.text() for a in actions] == ["显示主界面", "退出"]


def test_menu_actions_emit_requests(available_tray):
    tray = available_tray
    shown, quit_hits = _record(tray.show_requested), _record(tray.quit_requested)
    show_action, quit_action = tray.icon.contextMenu().actions()

    show_action.trigger()
    assert (len(shown), len(quit_hits)) == (1, 0)
    quit_action.trigger()
    assert (len(shown), len(quit_hits)) == (1, 1)


@pytest.mark.parametrize("reason, expected", [
    (QSystemTrayIcon.Trigger, 1),
    (QSystemTrayIcon.DoubleClick, 1),
    (QSystemTrayIcon.Context, 0),
])
def test_activation_restores_only_on_click(available_tray, reason, expected):
    shown = _record(available_tray.show_requested)
    available_tray.icon.activated.emit(reason)
    assert len(shown) == expected


@pytest.mark.parametrize("kind, icon", [
    ('info', QSystemTrayIcon.Information),
    ('warning', QSystemTrayIcon.Warning),
    ('critical', QSystemTrayIcon.Critical),
    ('unknown', QSystemTrayIcon.Information),
    (QSystemTrayIcon.Warning, QSystemTrayIcon.Warning),
])
def test_show_message_maps_kind(available_tray, kind, icon):
    sent = []
    available_tray.icon.showMessage = lambda *args: sent.append(args)

    available_tray.show_message("标题", "内容", kind)

    assert sent == [("标题", "内容", icon)]


def test_tooltip_and_hide(available_tray):
    available_tray.set_tooltip("进程监控助手：正在保存…")
    assert available_tray.icon.toolTip() == "进程监控助手：正在保存…"
    available_tray.hide()
    assert available_tray.icon.isVisible() is False
