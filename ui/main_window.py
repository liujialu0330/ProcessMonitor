"""
主窗口
使用FluentWindow创建带有Fluent UI风格的主界面
"""
import logging

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon
from qfluentwidgets import FluentWindow, NavigationItemPosition, FluentIcon, InfoBar, InfoBarPosition

from ui.pages.monitor_page import MonitorPage
from ui.pages.history_page import HistoryPage
from ui.pages.export_page import ExportPage
from ui.pages.about_page import AboutPage
from core.monitor_manager import MonitorManager
from data.database import Database
from utils.thread_utils import shutdown_thread
import config

logger = logging.getLogger(__name__)


class MainWindow(FluentWindow):
    """主窗口类"""

    def __init__(self, db=None):
        """初始化主窗口

        Args:
            db: 数据库实例（可选，默认回退新建 Database()；测试可注入隔离的临时库）。
                必须在构造 MonitorManager 之前赋值，确保全局唯一数据库向下分发。
        """
        super().__init__()

        # 唯一数据库实例：向下分发给 MonitorManager 与各页面
        self.db = db if db is not None else Database()

        # 初始化监控管理器（单例：仅首次构造时传入的 db 生效）
        self.monitor_manager = MonitorManager(db=self.db)

        # 孤儿任务校正：上次运行未正常退出遗留的 running 状态任务本次启动时统一校正为
        # stopped。必须在任何任务启动前、每进程只调用一次；不能放进 Database.__init__。
        self.db.reconcile_orphan_tasks()

        # 启动自动清理：默认禁用（DATA_RETENTION_DAYS=0）。执行顺序固定在孤儿校正
        # 之后，避免把本次启动才校正为 stopped 的"刚崩溃"任务当作过期任务误删。
        self.db.cleanup_old_tasks(config.DATA_RETENTION_DAYS)

        # 初始化界面
        self._init_window()
        self._init_navigation()

        # 启动3秒后静默检查更新（对齐参考实现，避免阻塞启动）
        QTimer.singleShot(3000, lambda: self.about_page.check_update(silent=True))

        # 数据库迁移三态提示（延迟到窗口显示后弹出，三态互斥，按严重程度顺序判断）
        db = self.db
        if db.backup_aborted:
            QTimer.singleShot(500, self._show_backup_aborted_tip)
        elif db.data_reset:
            QTimer.singleShot(500, self._show_data_reset_tip)
        elif db.migration_failed:
            QTimer.singleShot(500, self._show_migration_failed_tip)

    def _show_migration_failed_tip(self):
        """迁移两次尝试均失败，已还原旧数据：本次运行新数据无法保存"""
        InfoBar.warning(
            title="数据库升级失败",
            content="数据库升级失败，已还原旧数据，本次运行新数据无法保存，请重启重试。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=-1,
            parent=self
        )

    def _show_data_reset_tip(self):
        """还原备份也失败：损坏库已改名保留，应用以新空库运行"""
        InfoBar.warning(
            title="数据库已重置",
            content="历史数据已移至 monitor.db.broken_*，应用以新空库正常运行。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=-1,
            parent=self
        )

    def _show_backup_aborted_tip(self):
        """迁移前备份失败：中止迁移，旧库原样保留未做任何改动"""
        InfoBar.warning(
            title="数据库升级已中止",
            content="未能升级数据库，历史数据完好保存在磁盘，请释放磁盘空间后重启。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=-1,
            parent=self
        )

    def _init_window(self):
        """初始化窗口属性"""
        # 设置窗口标题（包含版本号）
        self.setWindowTitle(f"{config.APP_NAME} v{config.APP_VERSION}")

        # 设置窗口大小
        self.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
        self.setMinimumSize(config.WINDOW_MIN_WIDTH, config.WINDOW_MIN_HEIGHT)

        # 居中显示
        desktop = self.screen().geometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        # 设置窗口背景为透明，让Fluent Design的背景效果生效
        self.setStyleSheet("MainWindow{background: transparent}")

    def _init_navigation(self):
        """初始化导航栏和页面"""
        # 设置导航栏宽度（减少一半）
        self.navigationInterface.setExpandWidth(150)  # 默认是250，现在设置为150

        # 创建页面实例（注入统一数据库实例；about_page 不涉及数据库，不传）
        self.monitor_page = MonitorPage(self, db=self.db)
        self.history_page = HistoryPage(self, db=self.db)
        self.export_page = ExportPage(self, db=self.db)
        self.about_page = AboutPage(self)

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

        self.addSubInterface(
            self.export_page,
            FluentIcon.DOWNLOAD,
            '导出数据',
            NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.about_page,
            FluentIcon.INFO,
            '关于',
            NavigationItemPosition.BOTTOM
        )

        # 默认显示实时监控页面
        self.switchTo(self.monitor_page)

    def closeEvent(self, event):
        """关闭窗口事件处理：依次清理监控任务与关于页的后台线程，避免遗留线程导致
        进程退出时崩溃或挂起"""
        # 1. 停止所有监控任务（MonitorManager.stop_task 内部按 task.is_running() 逻辑标志
        #    决定是否 stop()+wait()）
        self.monitor_manager.stop_all_tasks()

        # 1.1 兜底 join：进程自然消亡的任务在 run() 主循环退出后、_teardown() 完成前存在一个
        #     窗口——此时 _running（is_running()）已为 False，但对应 QThread 的 isRunning()
        #     仍为 True（收尾中的 flush/写状态/emit 还没跑完）。stop_all_tasks 内部按
        #     is_running() 判断是否 wait，会跳过这类任务，不会等待其收尾完成。main.py 退出
        #     时用 os._exit() 直接终止进程（规避下载线程残留导致的 0xC0000409），若不在此
        #     兜底等待，可能在收尾写库的中途就被掐死，丢失最后一批数据。
        for task in self.monitor_manager.get_all_tasks():
            if task.isRunning():
                shutdown_thread(task, timeout_ms=3000)

        # 2. 关于页的下载线程：先置取消标志，再等待结束（超时只记日志，不阻塞更久）
        downloader = getattr(self.about_page, '_downloader', None)
        shutdown_thread(downloader, cancel_fn=getattr(downloader, 'cancel', None), timeout_ms=2000)

        # 3. 关于页的更新检查线程：无取消机制，仅等待
        checker = getattr(self.about_page, '_checker', None)
        shutdown_thread(checker, timeout_ms=1000)

        # 4. 导出页的导出线程：先置取消标志（取消后会自行删除写了一半的CSV），再等待结束
        export_worker = getattr(self.export_page, '_export_worker', None)
        shutdown_thread(
            export_worker, cancel_fn=getattr(export_worker, 'cancel', None), timeout_ms=2000)

        # 接受关闭事件
        event.accept()
