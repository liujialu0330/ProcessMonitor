"""
主窗口
使用FluentWindow创建带有Fluent UI风格的主界面
"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from qfluentwidgets import FluentWindow, NavigationItemPosition, FluentIcon

from ui.pages.monitor_page import MonitorPage
from ui.pages.history_page import HistoryPage
from core.monitor_manager import MonitorManager
import config


class MainWindow(FluentWindow):
    """主窗口类"""

    def __init__(self):
        """初始化主窗口"""
        super().__init__()

        # 初始化监控管理器
        self.monitor_manager = MonitorManager()

        # 初始化界面
        self._init_window()
        self._init_navigation()

    def _init_window(self):
        """初始化窗口属性"""
        # 设置窗口标题
        self.setWindowTitle(config.APP_NAME)

        # 设置窗口大小
        self.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
        self.setMinimumSize(config.WINDOW_MIN_WIDTH, config.WINDOW_MIN_HEIGHT)

        # 居中显示
        desktop = self.screen().geometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

    def _init_navigation(self):
        """初始化导航栏和页面"""
        # 设置导航栏宽度（减少一半）
        self.navigationInterface.setExpandWidth(150)  # 默认是250，现在设置为150

        # 创建页面实例
        self.monitor_page = MonitorPage(self)
        self.history_page = HistoryPage(self)

        # 添加子界面到导航栏
        self.addSubInterface(
            self.monitor_page,
            FluentIcon.SPEED_HIGH,
            '实时监控',
            NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.history_page,
            FluentIcon.HISTORY,
            '历史数据',
            NavigationItemPosition.TOP
        )

        # 默认显示实时监控页面
        self.switchTo(self.monitor_page)

    def closeEvent(self, event):
        """关闭窗口事件处理"""
        # 停止所有监控任务
        self.monitor_manager.stop_all_tasks()

        # 接受关闭事件
        event.accept()
