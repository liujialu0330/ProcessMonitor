"""
全局打点快捷键

在任何程序里按下快捷键（默认 Ctrl+Alt+M）即为当前任务打点，不必切回本窗口——
测试时手在被测程序上，切窗口会打断操作节奏，也会把打点时刻拖后。

实现：Windows 的 RegisterHotKey（绑定到界面线程，窗口句柄为空）+ Qt 原生事件过滤器
接收 WM_HOTKEY。注册动作经可替换的后端完成：测试注入假后端并用假消息驱动过滤器，
绝不在开发机桌面上注册真实热键；非 Windows 或注册失败（多半是被其他程序占用）时
静默降级，状态文案交给设置页显示"快捷键不可用"。
"""
import ctypes
import logging
import sys
from typing import Optional, Protocol, Tuple

from PyQt5.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal

logger = logging.getLogger(__name__)

try:  # 非 Windows 上 wintypes 也可导入，但保险起见不让它挡住模块加载
    from ctypes import wintypes
except (ImportError, ValueError):  # pragma: no cover - 仅极老的非 Windows Python
    wintypes = None

DEFAULT_MARKER_HOTKEY = "Ctrl+Alt+M"

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# 按住不放时不重复触发（Windows 7 起支持）
MOD_NOREPEAT = 0x4000
# 应用程序可用的热键 ID 范围是 0x0000–0xBFFF
MARKER_HOTKEY_ID = 0x7C01

# 原生事件过滤器里 Windows 消息的两种来源：线程消息走事件分发器，窗口消息走窗口过程
_WINDOWS_MSG_TYPES = (b"windows_dispatcher_MSG", b"windows_generic_MSG")

# 修饰键：书写名 → (规范名, 标志位)；规范顺序 Ctrl、Alt、Shift、Win
_MODIFIERS = {
    'ctrl': ('Ctrl', MOD_CONTROL), 'control': ('Ctrl', MOD_CONTROL),
    'alt': ('Alt', MOD_ALT),
    'shift': ('Shift', MOD_SHIFT),
    'win': ('Win', MOD_WIN), 'meta': ('Win', MOD_WIN), 'super': ('Win', MOD_WIN),
}
_MODIFIER_ORDER = ('Ctrl', 'Alt', 'Shift', 'Win')

# 状态
STATE_ACTIVE = 'active'           # 已注册，可用
STATE_OFF = 'off'                 # 未设置（空串）
STATE_INVALID = 'invalid'         # 写法不对
STATE_FAILED = 'failed'           # 注册失败（多半被其他程序占用）
STATE_UNSUPPORTED = 'unsupported'  # 当前系统不支持


def _key_code(name: str) -> Optional[int]:
    """主键的虚拟键码：A–Z、0–9、F1–F24；其余返回 None。"""
    upper = name.upper()
    if len(upper) == 1 and ('A' <= upper <= 'Z' or '0' <= upper <= '9'):
        return ord(upper)
    if upper.startswith('F') and upper[1:].isdigit():
        number = int(upper[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    return None


def parse_hotkey(text: str) -> Optional[Tuple[str, int, int]]:
    """解析 "Ctrl+Alt+M" 一类写法，返回 (规范写法, 修饰键标志, 虚拟键码)；不合法返回 None。

    至少要有 Ctrl、Alt、Win 之一：只用 Shift 或不带修饰键会吞掉正常打字。
    """
    parts = [part.strip() for part in (text or '').split('+')]
    if len(parts) < 2 or any(not part for part in parts):
        return None
    *modifier_names, key_name = parts
    names = set()
    flags = 0
    for raw in modifier_names:
        found = _MODIFIERS.get(raw.lower())
        if found is None or found[0] in names:
            return None
        names.add(found[0])
        flags |= found[1]
    if not flags & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        return None
    vk = _key_code(key_name)
    if vk is None:
        return None
    canonical = "+".join([n for n in _MODIFIER_ORDER if n in names] + [key_name.upper()])
    return canonical, flags, vk


def normalize_hotkey(text: str) -> Optional[str]:
    """规范写法（"ctrl + alt + m" → "Ctrl+Alt+M"）；不合法返回 None，空串返回空串。"""
    if not (text or '').strip():
        return ''
    parsed = parse_hotkey(text)
    return parsed[0] if parsed else None


class HotkeyBackend(Protocol):
    """系统热键注册后端。"""

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> bool:
        """注册热键，成功返回 True。"""
        ...

    def unregister(self, hotkey_id: int) -> None:
        """注销热键（未注册时也安全）。"""
        ...


class Win32HotkeyBackend:
    """生产实现：user32.RegisterHotKey，窗口句柄为空，热键归调用线程（界面线程）。"""

    def __init__(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        self._register = user32.RegisterHotKey
        self._register.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        self._register.restype = wintypes.BOOL
        self._unregister = user32.UnregisterHotKey
        self._unregister.argtypes = (wintypes.HWND, ctypes.c_int)
        self._unregister.restype = wintypes.BOOL

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> bool:
        ok = bool(self._register(None, hotkey_id, modifiers | MOD_NOREPEAT, vk))
        if not ok:
            logger.warning("注册全局快捷键失败（错误码 %s），可能已被其他程序占用",
                           ctypes.get_last_error())
        return ok

    def unregister(self, hotkey_id: int) -> None:
        self._unregister(None, hotkey_id)


class NullHotkeyBackend:
    """不支持全局快捷键的平台：注册总是失败。"""

    supported = False

    def register(self, hotkey_id: int, modifiers: int, vk: int) -> bool:
        return False

    def unregister(self, hotkey_id: int) -> None:
        pass


def default_backend() -> HotkeyBackend:
    """当前平台的后端：Windows 用 RegisterHotKey，其余平台不支持。"""
    if sys.platform == 'win32' and wintypes is not None:
        try:
            return Win32HotkeyBackend()
        except (OSError, AttributeError):
            logger.warning("加载 user32 失败，全局快捷键不可用", exc_info=True)
    return NullHotkeyBackend()


class _NativeFilter(QAbstractNativeEventFilter):
    """把 WM_HOTKEY 交给 MarkerHotkey。PyQt 不支持同时继承 QObject 与本类，故单独成类。"""

    def __init__(self, owner: "MarkerHotkey"):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, event_type, message):
        if wintypes is None or bytes(event_type) not in _WINDOWS_MSG_TYPES or not message:
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY and self._owner.handle_hotkey(int(msg.wParam)):
            return True, 0
        return False, 0


class MarkerHotkey(QObject):
    """全局打点快捷键。

    用法：构造（可注入后端）→ install(app) 安装原生事件过滤器 → bind("Ctrl+Alt+M")。
    按下时发出 triggered；状态变化（绑定成功/失败/改键）发出 status_changed。
    """

    triggered = pyqtSignal()
    status_changed = pyqtSignal()

    def __init__(self, backend: Optional[HotkeyBackend] = None, parent=None,
                 hotkey_id: int = MARKER_HOTKEY_ID):
        super().__init__(parent)
        self._backend = backend if backend is not None else default_backend()
        self._hotkey_id = hotkey_id
        self._filter = _NativeFilter(self)
        self._installed_on = None
        self._registered = False
        self._text = ''
        self._state = STATE_OFF

    # ========== 安装与绑定 ==========

    def install(self, app) -> None:
        """在 QApplication 上安装原生事件过滤器（只装一次）。"""
        if self._installed_on is None:
            app.installNativeEventFilter(self._filter)
            self._installed_on = app

    def native_filter(self) -> QAbstractNativeEventFilter:
        """原生事件过滤器（测试用假消息驱动它）。"""
        return self._filter

    def bind(self, text: str) -> bool:
        """改用新的快捷键（空串表示不用）；返回是否已可用。"""
        self.unbind(notify=False)
        self._text = (text or '').strip()
        if not self._text:
            self._state = STATE_OFF
        elif getattr(self._backend, 'supported', True) is False:
            self._state = STATE_UNSUPPORTED
        else:
            parsed = parse_hotkey(self._text)
            if parsed is None:
                self._state = STATE_INVALID
            else:
                self._text, modifiers, vk = parsed
                self._registered = self._backend.register(self._hotkey_id, modifiers, vk)
                self._state = STATE_ACTIVE if self._registered else STATE_FAILED
        self.status_changed.emit()
        return self._state == STATE_ACTIVE

    def unbind(self, notify: bool = True) -> None:
        """注销当前快捷键（退出前调用；进程结束时系统也会回收）。"""
        if self._registered:
            try:
                self._backend.unregister(self._hotkey_id)
            except Exception:
                logger.warning("注销全局快捷键失败", exc_info=True)
            self._registered = False
        if notify:
            self._state = STATE_OFF
            self.status_changed.emit()

    # ========== 状态 ==========

    def state(self) -> str:
        return self._state

    def is_active(self) -> bool:
        return self._state == STATE_ACTIVE

    def hotkey_text(self) -> str:
        """当前设定的快捷键（规范写法；未设置为空串）。"""
        return self._text

    def active_text(self) -> str:
        """可用时的快捷键写法，不可用时为空串（空状态引导据此决定提不提它）。"""
        return self._text if self.is_active() else ''

    def status_text(self) -> str:
        """设置页上显示的状态说明。"""
        return hotkey_status_text(self._state, self._text)

    # ========== 原生消息 ==========

    def handle_hotkey(self, hotkey_id: int) -> bool:
        """收到 WM_HOTKEY：是本热键且处于可用状态时发出 triggered 并返回 True。"""
        if hotkey_id != self._hotkey_id or not self._registered:
            return False
        self.triggered.emit()
        return True


def hotkey_status_text(state: Optional[str], text: str = '') -> str:
    """快捷键状态的说明文案（state 为 None 表示没有接上快捷键服务）。"""
    if state == STATE_ACTIVE:
        return f"已启用：在任何程序里按 {text} 即为运行中的任务打点（不弹窗）"
    if state == STATE_OFF:
        return "未设置：清空后不使用全局快捷键"
    if state == STATE_INVALID:
        return f"快捷键不可用：“{text}”写法不对，需含 Ctrl / Alt / Win，如 Ctrl+Alt+M"
    if state == STATE_FAILED:
        return f"快捷键不可用：{text} 可能已被其他程序占用，请换一个组合"
    if state == STATE_UNSUPPORTED:
        return "快捷键不可用：当前系统不支持全局快捷键"
    return "快捷键不可用"
