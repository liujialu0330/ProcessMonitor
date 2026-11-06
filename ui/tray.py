"""
系统托盘
托盘图标、右键菜单与气泡提示；主窗口只连 show_requested / quit_requested 两个信号，
不接触 QSystemTrayIcon 的细节。
"""
from typing import Optional, Union

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon, QWidget
from qfluentwidgets import Action, FluentIcon, SystemTrayMenu

# show_message 的 kind 取值 -> 托盘气泡图标
MESSAGE_KINDS = {
    'info': QSystemTrayIcon.Information,
    'warning': QSystemTrayIcon.Warning,
    'critical': QSystemTrayIcon.Critical,
}


class TrayController(QObject):
    """托盘控制器。

    图标常驻：构造即出现，不随"关闭时最小化到托盘"开关增删——该开关只控制
    主窗口的关闭行为。当前系统不支持托盘（offscreen/CI、部分精简系统）时不创建
    图标，is_available() 为 False，其余方法都是安全的空操作。
    """

    show_requested = pyqtSignal()   # 菜单"显示主界面"、单击或双击图标
    quit_requested = pyqtSignal()   # 菜单"退出"

    def __init__(self, parent_window: QWidget, icon: QIcon, app_name: str):
        super().__init__(parent_window)
        self.icon: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray_icon = QSystemTrayIcon(icon, parent_window)
        tray_icon.setToolTip(app_name)

        # 菜单与动作挂在窗口下：SystemTrayMenu 是 QWidget，父对象必须是控件
        menu = SystemTrayMenu(parent=parent_window)
        show_action = Action(FluentIcon.VIEW, "显示主界面", parent_window)
        show_action.triggered.connect(self.show_requested)
        menu.addAction(show_action)
        quit_action = Action(FluentIcon.CLOSE, "退出", parent_window)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(quit_action)

        tray_icon.setContextMenu(menu)
        tray_icon.activated.connect(self._on_activated)
        tray_icon.show()
        self.icon = tray_icon

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
