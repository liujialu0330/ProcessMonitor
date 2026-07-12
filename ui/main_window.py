"""
主窗口
使用FluentWindow创建带有Fluent UI风格的主界面
"""
import logging
import os
from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon, InfoBar, InfoBarPosition,
    PushButton, SystemTrayMenu, Action
)

from ui.pages.monitor_page import MonitorPage
from ui.pages.history_page import HistoryPage
from ui.pages.export_page import ExportPage
from ui.pages.about_page import AboutPage
from ui.pages.setting_page import SettingPage
from core.monitor_manager import MonitorManager
from data.database import Database
from utils.thread_utils import shutdown_thread
from app_config import cfg
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

        # 托盘驻留相关状态（v1.3.0 批4，D）：必须在 _init_tray()/closeEvent 用到
        # 之前初始化完毕。
        # - _really_quit：托盘菜单"退出"与 quit_for_install() 会置 True，
        #   closeEvent 据此跳过隐藏分支、直接走真退出路径（评审修订 B3）。
        # - tray_icon：仅当 QSystemTrayIcon.isSystemTrayAvailable() 时由
        #   _init_tray() 赋值；offscreen/CI 或精简系统下恒为 None，closeEvent
        #   的托盘隐藏分支与更新提醒的补充气泡逻辑都因此天然短路，退化为无
        #   托盘的 v1.2.0 现状（不变式⑤）。
        # - _tray_tip_shown：首次隐藏到托盘时的提示气泡只弹一次（评审修订 N1）。
        self._really_quit = False
        self.tray_icon: Optional[QSystemTrayIcon] = None
        self._tray_tip_shown = False

        # 孤儿任务校正：上次运行未正常退出遗留的 running 状态任务本次启动时统一校正为
        # stopped。必须在任何任务启动前、每进程只调用一次；不能放进 Database.__init__。
        self.db.reconcile_orphan_tasks()

        # 启动自动清理：保留天数取自设置页（app_config.cfg），默认禁用
        # （0=永久保留，config.DATA_RETENTION_DAYS 缺省值同此）。执行顺序固定在
        # 孤儿校正之后，避免把本次启动才校正为 stopped 的"刚崩溃"任务当作过期
        # 任务误删。
        self.db.cleanup_old_tasks(cfg.get(cfg.retention_days))

        # 初始化界面
        self._init_window()
        self._init_navigation()
        self._init_tray()

        # 非模态更新提醒（v1.3.0 批4，C5）：静默检查发现新版本时不再由
        # about_page 直接弹模态对话框，改为发信号，这里统一处理为 InfoBar
        # 提示（必须在 self.about_page 创建之后连接，故放在 _init_navigation
        # 之后）
        self.about_page.update_available_silent.connect(self._on_update_available_silent)

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
        # 设置页数据管理卡片需要 db（查询占用/清理压缩）与 manager（只读查询
        # 是否有运行中任务，决定清理按钮是否可用）两个依赖，均为可选注入
        self.setting_page = SettingPage(self, db=self.db, manager=self.monitor_manager)
        self.about_page = AboutPage(self)

        # 默认采集周期联动：设置页改动"默认采集周期"后，实时同步到监控页
        # 采集周期输入框的默认显示值（不影响已创建任务，只影响下一次新建）
        cfg.default_interval.valueChanged.connect(self.monitor_page.set_default_interval)

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

        # 设置与关于同处底部区域；NavigationPanel 按 addSubInterface 调用顺序
        # 依次向下追加（insertWidget(-1, ...)，先调用者排在更上方），故设置页
        # 的 addSubInterface 必须写在关于页之前，才能实现"设置在上、关于在下"
        self.addSubInterface(
            self.setting_page,
            FluentIcon.SETTING,
            '设置',
            NavigationItemPosition.BOTTOM
        )

        self.addSubInterface(
            self.about_page,
            FluentIcon.INFO,
            '关于',
            NavigationItemPosition.BOTTOM
        )

        # 默认显示实时监控页面
        self.switchTo(self.monitor_page)

    def _init_tray(self) -> None:
        """初始化系统托盘图标（v1.3.0 批4，D 托盘驻留）。

        图标常驻：应用启动即出现，不随"关闭时最小化到托盘"开关增删——该开关
        只控制 closeEvent 的关闭行为，不控制图标本身的显隐。

        仅当当前系统支持系统托盘（QSystemTrayIcon.isSystemTrayAvailable()）时
        才创建；offscreen/CI 或部分精简系统该判断为 False，self.tray_icon
        保持 None，closeEvent 的托盘隐藏分支与更新提醒的补充气泡逻辑都因此
        天然短路，退化为无托盘的 v1.2.0 现状（不变式⑤）。
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon_path = config.get_icon_path()
        icon = QIcon(icon_path) if os.path.exists(icon_path) else self.windowIcon()

        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip(config.APP_NAME)

        menu = SystemTrayMenu(parent=self)

        show_action = Action(FluentIcon.VIEW, "显示主界面", self)
        show_action.triggered.connect(self._show_from_tray)
        menu.addAction(show_action)

        quit_action = Action(FluentIcon.CLOSE, "退出", self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _show_from_tray(self) -> None:
        """托盘菜单"显示主界面"/双击图标：从隐藏态还原窗口"""
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _quit_from_tray(self) -> None:
        """托盘菜单"退出"：置真退出标志后走正常关闭流程（与 quit_for_install
        走同一条真退出路径，评审修订 B3 场景之一）"""
        self._really_quit = True
        self.close()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """托盘图标激活事件：单击（Trigger）与双击（DoubleClick）均还原主界面；
        右键（Context）由 Qt 自动弹出 setContextMenu 设置的菜单，无需在此处理"""
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_from_tray()

    def quit_for_install(self) -> None:
        """供更新安装流程调用的公开方法（评审修订 B3）：无论"关闭时最小化到
        托盘"是否开启，都必须走真退出路径——否则该开关开启时，安装向导已经
        启动，主程序却被 closeEvent 的隐藏分支拦截、常驻托盘继续占用文件，
        导致覆盖安装因文件占用而失败。"""
        self._really_quit = True
        self.close()

    def _on_update_available_silent(self, info: dict) -> None:
        """静默检查更新发现新版本（v1.3.0 批4，C5，修复遗留 P2-2）：不再由
        about_page 直接弹出模态对话框打断用户，改为右上角 InfoBar 非模态提示
        +「查看」按钮，点击后切到关于页并弹出与手动检查一致的确认对话框。

        【评审修订 N2】若此时主窗口处于托盘隐藏态，InfoBar 挂在隐藏窗口上
        不会被用户看到，需额外补一条托盘气泡。
        """
        info_bar = InfoBar.info(
            title=f"发现新版本 {info['version']}",
            content="点击查看详情",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=10000,
            parent=self
        )
        view_button = PushButton("查看")
        view_button.clicked.connect(lambda: self._show_update_detail(info))
        info_bar.addWidget(view_button)

        if self.isHidden() and self.tray_icon is not None:
            self.tray_icon.showMessage(
                f"发现新版本 {info['version']}", "打开主界面查看",
                QSystemTrayIcon.Information)

    def _show_update_detail(self, info: dict) -> None:
        """InfoBar「查看」按钮点击：切到关于页并弹出更新确认对话框"""
        self.switchTo(self.about_page)
        self.about_page.show_update_dialog_for(info)

    def closeEvent(self, event):
        """关闭窗口事件处理。

        默认（未开启"关闭时最小化到托盘"，或托盘不可用）走既有完整清理流程
        -> QApplication.quit()（v1.2.0 起唯一真退出路径，main.py 用 os._exit
        兜底）；开启该设置且托盘可用时改为隐藏到托盘，不停止任务、不做任何
        清理，等待用户从托盘菜单选择"退出"。

        退出路径不变式（v1.3.0 批4，UI体验升级方案_v1.3.0.md §4.1，绝不破坏）：
        ① 默认配置（close_to_tray=False）关闭行为与现状完全一致；
        ② 托盘"退出"与 About 页"立即安装"都置 _really_quit=True，绕开隐藏分支
           走同一条真退出路径；
        ③ 隐藏到托盘不停止任务、不做任何清理；
        ④ 清理步骤中途抛异常，QApplication.quit() 仍必须执行（try/finally，
           评审修订 B4）；
        ⑤ offscreen/无托盘环境（isSystemTrayAvailable() 为 False）下
           tray_icon 为 None，隐藏分支天然短路，行为与现状一致。
        """
        if (not self._really_quit and self.tray_icon is not None
                and cfg.get(cfg.close_to_tray)):
            self.hide()
            if not self._tray_tip_shown:  # 每次运行只提示一次，避免反复打扰
                self.tray_icon.showMessage(
                    config.APP_NAME, "已最小化到系统托盘，监控仍在后台运行",
                    QSystemTrayIcon.Information)
                self._tray_tip_shown = True
            event.ignore()
            return

        # 【评审修订 B4】真退出路径：既有全部清理步骤整体包进 try/finally，
        # QApplication.quit() 放 finally——setQuitOnLastWindowClosed(False)
        # 拆掉了"closeEvent 异常也能退出"的隐性保险丝，改造后退出全靠这里
        # 显式调用 quit()；若清理中途抛异常又不放 finally，会导致事件循环
        # 不退出且窗口已被隐藏，用户无法通过任何界面操作恢复。
        try:
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

            # 5. 设置页的数据库清理线程（v1.3.0 批4新增）：仅 join 兜底，无取消
            #    机制——清理动作本身应尽快跑完，不强行中断以免 VACUUM 中途被
            #    打断导致数据库处于中间态
            cleanup_worker = getattr(self.setting_page, '_cleanup_worker', None)
            shutdown_thread(cleanup_worker, timeout_ms=5000)

            # 接受关闭事件（放 try 尾部：清理全部成功才显式 accept；异常路径下
            # QCloseEvent 默认已 accepted，且 finally 的 quit() 与 main.py 的
            # os._exit() 双重兜底退出，不依赖这一行）
            event.accept()
        finally:
            # 真退出前主动隐藏托盘图标——main.py 用 os._exit() 结束进程，不经
            # Qt 析构，不显式 hide 会在通知区残留幽灵图标直到鼠标划过。防御
            # try/except：托盘对象异常不得妨碍 finally 里必须执行的 quit()
            if self.tray_icon is not None:
                try:
                    self.tray_icon.hide()
                except Exception:
                    pass
            QApplication.quit()
