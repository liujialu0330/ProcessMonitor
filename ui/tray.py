"""
系统托盘
托盘图标、右键菜单与气泡提示；主窗口只连 show_requested / quit_requested /
marker_requested 三个信号，不接触 QSystemTrayIcon 的细节。

右键菜单："显示主界面" / "打点" / "退出"。"打点"不弹窗，直接用默认名称记下当前时刻：
只有一个任务时是一项"打点（demo.exe · PID 1234）"；多个任务时是子菜单（全部任务 +
逐个任务）；没有任务时置灰。候选任务在菜单弹出前经 marker_targets 现取。
"""
from typing import Callable, List, Optional, Sequence, Tuple, Union

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon, QWidget
from qfluentwidgets import Action, FluentIcon, RoundMenu, SystemTrayMenu

# show_message 的 kind 取值 -> 托盘气泡图标
MESSAGE_KINDS = {
    'info': QSystemTrayIcon.Information,
    'warning': QSystemTrayIcon.Warning,
    'critical': QSystemTrayIcon.Critical,
}

SHOW_TEXT = "显示主界面"
QUIT_TEXT = "退出"
MARKER_MENU_TEXT = "打点（当前任务）"
MARKER_NONE_TEXT = "打点（没有正在监控的任务）"
MARKER_ALL_TEXT = "全部任务"
# marker_requested 的参数：空串表示"全部运行中的任务"
ALL_TASKS = ""

# (task_id, 显示名)
MarkerTarget = Tuple[str, str]


def marker_single_text(label: str) -> str:
    return f"打点（{label}）"


class TrayController(QObject):
    """托盘控制器。

    图标常驻：构造即出现，不随"关闭时最小化到托盘"开关增删——该开关只控制
    主窗口的关闭行为。当前系统不支持托盘（offscreen/CI、部分精简系统）时不创建
    图标，is_available() 为 False，其余方法都是安全的空操作。
    """

    show_requested = pyqtSignal()   # 菜单"显示主界面"、单击或双击图标
    quit_requested = pyqtSignal()   # 菜单"退出"
    marker_requested = pyqtSignal(str)   # 菜单"打点"：task_id，空串表示全部任务

    def __init__(self, parent_window: QWidget, icon: QIcon, app_name: str, *,
                 marker_targets: Optional[Callable[[], Sequence[MarkerTarget]]] = None):
        super().__init__(parent_window)
        self.icon: Optional[QSystemTrayIcon] = None
        self.menu: Optional[SystemTrayMenu] = None
        self._marker_menu: Optional[RoundMenu] = None
        self._marker_targets = marker_targets or (lambda: [])
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_icon = QSystemTrayIcon(icon, parent_window)
        tray_icon.setToolTip(app_name)

        # 菜单与动作挂在窗口下：SystemTrayMenu 是 QWidget，父对象必须是控件
        self.menu = SystemTrayMenu(parent=parent_window)
        self.menu.aboutToShow.connect(self.rebuild_menu)
        self.rebuild_menu()

        tray_icon.setContextMenu(self.menu)
        tray_icon.activated.connect(self._on_activated)
        tray_icon.show()
        self.icon = tray_icon

    def set_marker_targets(self, provider: Callable[[], Sequence[MarkerTarget]]) -> None:
        """设置"打点"候选任务的来源（菜单弹出前调用）。"""
        self._marker_targets = provider
        self.rebuild_menu()

    def rebuild_menu(self) -> None:
        """按当前候选任务重建菜单（弹出前自动调用；托盘不可用时不做任何事）。"""
        menu = self.menu
        if menu is None:
            return
        stale = list(menu.menuActions()) + [
            m for m in menu.findChildren(RoundMenu) if m.parent() is menu]
        menu.clear()
        # clear() 不清分隔线（分隔线只是列表里的空项），连同列表项一起清掉
        menu.view.clear()
        for obj in stale:
            obj.deleteLater()

        self._marker_menu = None
        show_action = Action(FluentIcon.VIEW, SHOW_TEXT, menu)
        show_action.triggered.connect(self.show_requested)
        menu.addAction(show_action)

        targets: List[MarkerTarget] = list(self._marker_targets())
        if not targets:
            none_action = Action(FluentIcon.TAG, MARKER_NONE_TEXT, menu)
            none_action.setEnabled(False)
            menu.addAction(none_action)
        elif len(targets) == 1:
            task_id, label = targets[0]
            action = Action(FluentIcon.TAG, marker_single_text(label), menu)
            action.triggered.connect(lambda _=False, t=task_id: self.marker_requested.emit(t))
            menu.addAction(action)
        else:
            sub = RoundMenu(MARKER_MENU_TEXT, menu)
            sub.setIcon(FluentIcon.TAG)
            all_action = Action(MARKER_ALL_TEXT, sub)
            all_action.triggered.connect(lambda _=False: self.marker_requested.emit(ALL_TASKS))
            sub.addAction(all_action)
            sub.addSeparator()
            for task_id, label in targets:
                action = Action(label, sub)
                action.triggered.connect(
                    lambda _=False, t=task_id: self.marker_requested.emit(t))
                sub.addAction(action)
            menu.addMenu(sub)
            self._marker_menu = sub

        menu.addSeparator()
        quit_action = Action(FluentIcon.CLOSE, QUIT_TEXT, menu)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(quit_action)

    def marker_submenu(self) -> Optional[RoundMenu]:
        """多任务时的"打点"子菜单（测试与截图用）；其余情况为 None。"""
        return self._marker_menu

    def is_available(self) -> bool:
        """托盘图标是否存在（决定能否隐藏到托盘、能否弹气泡）"""
        return self.icon is not None

    def show_message(self, title: str, content: str,
                     kind: Union[str, QSystemTrayIcon.MessageIcon] = 'info') -> None:
        """弹托盘气泡；kind 为 'info' / 'warning' / 'critical' 或 Qt 图标枚举。
        托盘不可用时不做任何事。"""
        if self.icon is None:
            return
        icon_kind = MESSAGE_KINDS.get(kind, QSystemTrayIcon.Information) \
            if isinstance(kind, str) else kind
        self.icon.showMessage(title, content, icon_kind)

    def set_tooltip(self, text: str) -> None:
        """更新图标悬停文字（退出收尾期间显示进度）"""
        if self.icon is not None:
            self.icon.setToolTip(text)

    def hide(self) -> None:
        """隐藏图标。真退出前必须调用：入口用 os._exit() 结束进程、不经 Qt 析构，
        不隐藏会在通知区残留幽灵图标，直到鼠标划过才消失。"""
        if self.icon is not None:
            self.icon.hide()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """单击（Trigger）与双击（DoubleClick）还原主界面；右键（Context）由 Qt
        自动弹出 setContextMenu 设置的菜单，无需处理"""
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_requested.emit()
