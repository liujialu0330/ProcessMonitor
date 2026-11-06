"""
托盘控制器 TrayController：托盘不可用时全部安全空操作；可用时创建图标与菜单
（显示主界面 / 打点 / 退出），单击/双击还原，气泡按 kind 选图标。"打点"按候选任务数
变化：没有任务置灰、一个任务单项、多个任务子菜单（全部任务 + 逐个任务）。
offscreen 下系统托盘不可用，"可用"分支通过替换 isSystemTrayAvailable 模拟。
"""
import pytest
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon, QWidget

from ui.tray import (
    ALL_TASKS, MARKER_ALL_TEXT, MARKER_MENU_TEXT, MARKER_NONE_TEXT, TrayController,
)


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


def _menu_texts(tray):
    return [a.text() for a in tray.menu.menuActions()]


def test_available_tray_creates_icon_with_menu(available_tray):
    tray = available_tray
    assert tray.is_available() is True
    assert isinstance(tray.icon, QSystemTrayIcon)
    assert tray.icon.toolTip() == "进程监控助手"
    assert tray.icon.contextMenu() is tray.menu
    # 没有任务时"打点"置灰，说明原因
    assert _menu_texts(tray) == ["显示主界面", MARKER_NONE_TEXT, "退出"]
    assert not tray.menu.menuActions()[1].isEnabled()
    assert tray.marker_submenu() is None


def test_menu_actions_emit_requests(available_tray):
    tray = available_tray
    shown, quit_hits = _record(tray.show_requested), _record(tray.quit_requested)
    show_action, _marker, quit_action = tray.menu.menuActions()

    show_action.trigger()
    assert (len(shown), len(quit_hits)) == (1, 0)
    quit_action.trigger()
    assert (len(shown), len(quit_hits)) == (1, 1)


def test_single_task_marker_is_one_action(available_tray):
    tray = available_tray
    requested = []
    tray.marker_requested.connect(requested.append)
    tray.set_marker_targets(lambda: [("t1", "demo.exe · PID 1001")])

    assert _menu_texts(tray) == ["显示主界面", "打点（demo.exe · PID 1001）", "退出"]
    tray.menu.menuActions()[1].trigger()
    assert requested == ["t1"]


def test_several_tasks_marker_is_submenu(available_tray):
    tray = available_tray
    requested = []
    tray.marker_requested.connect(requested.append)
    tray.set_marker_targets(lambda: [("t1", "a.exe · PID 1"), ("t2", "b.exe · PID 2")])

    sub = tray.marker_submenu()
    assert sub is not None and sub.title() == MARKER_MENU_TEXT
    assert [a.text() for a in sub.menuActions()] == [MARKER_ALL_TEXT, "a.exe · PID 1",
                                                     "b.exe · PID 2"]
    all_action, _first, second = sub.menuActions()
    all_action.trigger()
    second.trigger()
    assert requested == [ALL_TASKS, "t2"]


def test_menu_rebuilt_before_showing(available_tray):
    """候选任务在菜单弹出前现取：任务增减后不残留旧项、旧分隔线"""
    tray = available_tray
    targets = [("t1", "a.exe · PID 1"), ("t2", "b.exe · PID 2")]
    tray.set_marker_targets(lambda: list(targets))
    assert tray.marker_submenu() is not None

    targets.pop()
    tray.menu.aboutToShow.emit()
    assert tray.marker_submenu() is None
    assert _menu_texts(tray) == ["显示主界面", "打点（a.exe · PID 1）", "退出"]
    # 只有一条分隔线（打点与退出之间），重建不累积
    assert tray.menu.view.count() == 4


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
