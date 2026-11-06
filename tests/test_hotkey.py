"""
全局打点快捷键：写法解析与规范化、绑定状态（可用/未设置/写法不对/被占用/不支持）、
假原生消息驱动过滤器、注册失败静默降级、设置页快捷键卡片、主窗口接线。

绝不注册真实热键：全部经 FakeBackend；Win32 后端只在测试里检查它存在，不调用 register。
"""
import ctypes
from ctypes import wintypes

import pytest
from PyQt5.QtCore import QAbstractNativeEventFilter, QEvent, Qt
from PyQt5.QtGui import QKeyEvent

from tests.doubles import FakePreferences
from tests.factories import make_services
from ui.hotkey import (
    DEFAULT_MARKER_HOTKEY, MARKER_HOTKEY_ID, MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN,
    STATE_ACTIVE, STATE_FAILED, STATE_INVALID, STATE_OFF, STATE_UNSUPPORTED, WM_HOTKEY,
    MarkerHotkey, NullHotkeyBackend, hotkey_status_text, normalize_hotkey, parse_hotkey,
)


class FakeBackend:
    """记录注册/注销；ok=False 模拟被其他程序占用。"""

    def __init__(self, ok=True):
        self.ok = ok
        self.registered = {}
        self.calls = []

    def register(self, hotkey_id, modifiers, vk):
        self.calls.append(("register", hotkey_id, modifiers, vk))
        if self.ok:
            self.registered[hotkey_id] = (modifiers, vk)
        return self.ok

    def unregister(self, hotkey_id):
        self.calls.append(("unregister", hotkey_id))
        self.registered.pop(hotkey_id, None)


def _native_message(message, wparam):
    """构造一条 Windows MSG，返回 (结构体, 地址)；结构体要活到过滤器读完。"""
    msg = wintypes.MSG()
    msg.message = message
    msg.wParam = wparam
    return msg, ctypes.addressof(msg)


# ---------------------------------------------------------------- 解析


@pytest.mark.parametrize("text, expected", [
    ("Ctrl+Alt+M", ("Ctrl+Alt+M", MOD_CONTROL | MOD_ALT, ord("M"))),
    ("alt + ctrl + m", ("Ctrl+Alt+M", MOD_CONTROL | MOD_ALT, ord("M"))),
    ("Ctrl+Shift+F9", ("Ctrl+Shift+F9", MOD_CONTROL | MOD_SHIFT, 0x78)),
    ("Win+Alt+1", ("Alt+Win+1", MOD_ALT | MOD_WIN, ord("1"))),
    ("Control+F24", ("Ctrl+F24", MOD_CONTROL, 0x87)),
])
def test_parse_valid(text, expected):
    assert parse_hotkey(text) == expected


@pytest.mark.parametrize("text", [
    "", "M", "Shift+M", "Ctrl+", "Ctrl+Ctrl+M", "Ctrl+Alt+Space", "Ctrl+F25", "Hyper+M",
    "Ctrl++M",
])
def test_parse_invalid(text):
    assert parse_hotkey(text) is None


def test_normalize():
    assert normalize_hotkey("  ") == ""
    assert normalize_hotkey("ctrl+alt+m") == "Ctrl+Alt+M"
    assert normalize_hotkey("Shift+M") is None


# ---------------------------------------------------------------- 绑定状态


def test_bind_registers_and_reports_active(qapp):
    backend = FakeBackend()
    hotkey = MarkerHotkey(backend)
    changes = []
    hotkey.status_changed.connect(lambda: changes.append(hotkey.state()))

    assert hotkey.bind("ctrl+alt+m") is True
    assert backend.registered == {MARKER_HOTKEY_ID: (MOD_CONTROL | MOD_ALT, ord("M"))}
    assert (hotkey.state(), hotkey.hotkey_text(), hotkey.active_text()) == (
        STATE_ACTIVE, "Ctrl+Alt+M", "Ctrl+Alt+M")
    assert "Ctrl+Alt+M" in hotkey.status_text() and hotkey.status_text().startswith("已启用")
    assert changes == [STATE_ACTIVE]


def test_rebind_unregisters_previous(qapp):
    backend = FakeBackend()
    hotkey = MarkerHotkey(backend)
    hotkey.bind("Ctrl+Alt+M")
    hotkey.bind("Ctrl+Alt+K")
    assert ("unregister", MARKER_HOTKEY_ID) in backend.calls
    assert backend.registered == {MARKER_HOTKEY_ID: (MOD_CONTROL | MOD_ALT, ord("K"))}


def test_registration_failure_degrades_silently(qapp):
    """被其他程序占用：不抛异常、不触发，状态文案"快捷键不可用" """
    hotkey = MarkerHotkey(FakeBackend(ok=False))
    hits = []
    hotkey.triggered.connect(lambda: hits.append(1))

    assert hotkey.bind(DEFAULT_MARKER_HOTKEY) is False
    assert hotkey.state() == STATE_FAILED and hotkey.active_text() == ""
    assert hotkey.status_text().startswith("快捷键不可用")
    assert hotkey.handle_hotkey(MARKER_HOTKEY_ID) is False
    assert hits == []


def test_unsupported_platform(qapp):
    hotkey = MarkerHotkey(NullHotkeyBackend())
    assert hotkey.bind(DEFAULT_MARKER_HOTKEY) is False
    assert hotkey.state() == STATE_UNSUPPORTED
    assert hotkey.status_text() == "快捷键不可用：当前系统不支持全局快捷键"


def test_invalid_and_empty(qapp):
    backend = FakeBackend()
    hotkey = MarkerHotkey(backend)
    assert hotkey.bind("Shift+M") is False and hotkey.state() == STATE_INVALID
    assert hotkey.status_text().startswith("快捷键不可用")
    assert hotkey.bind("") is False and hotkey.state() == STATE_OFF
    assert backend.registered == {}


def test_unbind_releases(qapp):
    backend = FakeBackend()
    hotkey = MarkerHotkey(backend)
    hotkey.bind(DEFAULT_MARKER_HOTKEY)
    hotkey.unbind()
    assert backend.registered == {} and hotkey.state() == STATE_OFF
    assert hotkey.handle_hotkey(MARKER_HOTKEY_ID) is False


def test_status_text_without_service():
    assert hotkey_status_text(None) == "快捷键不可用"


# ---------------------------------------------------------------- 原生消息


def test_native_filter_triggers_on_own_wm_hotkey(qapp):
    hotkey = MarkerHotkey(FakeBackend())
    hotkey.bind(DEFAULT_MARKER_HOTKEY)
    hits = []
    hotkey.triggered.connect(lambda: hits.append(1))
    native = hotkey.native_filter()
    assert isinstance(native, QAbstractNativeEventFilter)

    msg, address = _native_message(WM_HOTKEY, MARKER_HOTKEY_ID)
    assert native.nativeEventFilter(b"windows_dispatcher_MSG", address) == (True, 0)
    assert hits == [1]

    # 别的热键 ID、别的消息、别的平台事件都放行
    other, other_address = _native_message(WM_HOTKEY, MARKER_HOTKEY_ID + 1)
    assert native.nativeEventFilter(b"windows_generic_MSG", other_address) == (False, 0)
    paint, paint_address = _native_message(0x000F, MARKER_HOTKEY_ID)
    assert native.nativeEventFilter(b"windows_generic_MSG", paint_address) == (False, 0)
    assert native.nativeEventFilter(b"xcb_generic_event_t", address) == (False, 0)
    assert hits == [1]


def test_install_once(qapp, monkeypatch):
    installed = []
    monkeypatch.setattr(qapp, 'installNativeEventFilter', installed.append)
    hotkey = MarkerHotkey(FakeBackend())
    hotkey.install(qapp)
    hotkey.install(qapp)
    assert installed == [hotkey.native_filter()]


# ---------------------------------------------------------------- 设置页卡片


@pytest.fixture
def setting_env(qapp, tmp_path):
    from core.monitor_manager import MonitorManager
    from data.database import Database
    from tests.factories import _services_for
    from ui.pages.setting_page import SettingPage

    db = Database(str(tmp_path / "hotkey.db"))
    manager = MonitorManager(db=db)
    prefs = FakePreferences()
    services = _services_for(manager, make_services(preferences=prefs))
    pages = []

    def build(hotkey=None):
        page = SettingPage(db, manager, services, hotkey=hotkey)
        pages.append(page)
        return page

    yield build, prefs
    for page in pages:
        page.close()
    manager.stop_all_tasks()


def _key(widget, key, modifiers=Qt.NoModifier):
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers))


def test_setting_card_without_service_says_unavailable(setting_env):
    build, prefs = setting_env
    page = build()
    assert page.hotkey_status() == "快捷键不可用"
    assert page.hotkey_edit.text() == "Ctrl+Alt+M"


def test_capturing_keys_saves_and_rebinds(setting_env):
    build, prefs = setting_env
    backend = FakeBackend()
    hotkey = MarkerHotkey(backend)
    hotkey.bind(prefs.marker_hotkey())
    page = build(hotkey)
    assert page.hotkey_status().startswith("已启用")

    _key(page.hotkey_edit, Qt.Key_Control, Qt.ControlModifier)     # 只按修饰键不算
    assert prefs.__dict__.get('hotkeys_set') is None
    _key(page.hotkey_edit, Qt.Key_K, Qt.ControlModifier | Qt.AltModifier)

    assert prefs.hotkeys_set == ["Ctrl+Alt+K"]
    assert backend.registered == {MARKER_HOTKEY_ID: (MOD_CONTROL | MOD_ALT, ord("K"))}
    assert page.hotkey_edit.text() == "Ctrl+Alt+K"
    assert "Ctrl+Alt+K" in page.hotkey_status()


def test_invalid_capture_explains_and_keeps_setting(setting_env):
    build, prefs = setting_env
    hotkey = MarkerHotkey(FakeBackend())
    hotkey.bind(prefs.marker_hotkey())
    page = build(hotkey)

    _key(page.hotkey_edit, Qt.Key_M, Qt.ShiftModifier)
    assert prefs.__dict__.get('hotkeys_set') is None
    assert hotkey.is_active() and hotkey.hotkey_text() == "Ctrl+Alt+M"
    assert page.hotkey_status().startswith("快捷键不可用")


def test_occupied_hotkey_shows_unavailable(setting_env):
    build, prefs = setting_env
    hotkey = MarkerHotkey(FakeBackend(ok=False))
    hotkey.bind(prefs.marker_hotkey())
    page = build(hotkey)
    assert page.hotkey_status().startswith("快捷键不可用")
    # 恢复默认仍可点（换个组合或稍后再试）
    page.hotkey_reset_button.click()
    assert prefs.hotkeys_set == [DEFAULT_MARKER_HOTKEY]


def test_backspace_clears_hotkey(setting_env):
    build, prefs = setting_env
    backend = FakeBackend()
    hotkey = MarkerHotkey(backend)
    hotkey.bind(prefs.marker_hotkey())
    page = build(hotkey)

    _key(page.hotkey_edit, Qt.Key_Backspace)
    assert prefs.hotkeys_set == [""]
    assert backend.registered == {} and hotkey.state() == STATE_OFF
    assert page.hotkey_edit.text() == ""
