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
from ui.components.dialogs import CloseActionDialog
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

        # 托盘驻留相关状态：必须在 _init_tray()/closeEvent 用到之前初始化完毕。
        # - _really_quit：托盘菜单"退出"与 quit_for_install() 会置 True，
        #   closeEvent 据此跳过隐藏分支与关窗确认、直接走真退出路径。
        # - tray_icon：仅当 QSystemTrayIcon.isSystemTrayAvailable() 时由
        #   _init_tray() 赋值；offscreen/CI 或精简系统下恒为 None，closeEvent
        #   的托盘隐藏分支与各类补充气泡逻辑都因此天然短路，退化为无托盘现状
        #   （不变式⑤）。
        # - _tray_tip_shown：首次隐藏到托盘时的提示气泡只弹一次。
        self._really_quit = False
        self.tray_icon: Optional[QSystemTrayIcon] = None
        self._tray_tip_shown = False

        # 孤儿任务校正：上次运行未正常退出遗留的 running 状态任务本次启动时统一校正为
        # stopped。必须在任何任务启动前、每进程只调用一次；不能放进 Database.__init__。
        self.db.reconcile_orphan_tasks()

        # 采样计数校正：用户回退旧版本运行后再升级回来，旧版写入的任务 sample_count=0
        # 会在历史页/导出页消失，这里按数据点重算一次（同样只在启动时、任务启动前调用）
        self.db.reconcile_sample_counts()

        # 启动自动清理：保留天数取自设置页（app_config.cfg），默认禁用
        # （0=永久保留，config.DATA_RETENTION_DAYS 缺省值同此）。执行顺序固定在
        # 孤儿校正之后，避免把本次启动才校正为 stopped 的"刚崩溃"任务当作过期
        # 任务误删。
        self.db.cleanup_old_tasks(cfg.get(cfg.retention_days))

        # 初始化界面
        self._init_window()
        self._init_navigation()
        self._init_tray()

        # 注销/关机：系统会话结束时 closeEvent 的三选确认对话框会阻塞关机，
        # 这里在会话结束前置真退出标志，跳过确认直接走真退出流程。
        # DirectConnection：必须在 Qt 随后向窗口发送 QCloseEvent 之前同步执行。
        app = QApplication.instance()
        if app is not None:
            app.commitDataRequest.connect(
                self._on_commit_data_request, Qt.DirectConnection)

        # 非模态更新提醒：静默检查发现新版本时不由 about_page 直接弹模态对话框，
        # 而是发信号，这里统一处理为 InfoBar 提示（必须在 self.about_page 创建
        # 之后连接，故放在 _init_navigation 之后）
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

        # 创建页面实例（注入统一数据库实例与监控管理器；about_page 不涉及数据库，不传）
        self.monitor_page = MonitorPage(
            self, db=self.db, manager=self.monitor_manager)
        self.history_page = HistoryPage(
            self, db=self.db, manager=self.monitor_manager)
        self.export_page = ExportPage(self, db=self.db)
        # 设置页数据管理卡片需要 db（查询占用/清理压缩）与 manager（只读查询
        # 是否有运行中任务，决定清理按钮是否可用）两个依赖，均为可选注入
        self.setting_page = SettingPage(self, db=self.db, manager=self.monitor_manager)
        self.about_page = AboutPage(self)

        # 默认采集周期联动：设置页改动"默认采集周期"后，实时同步到监控页
        # 采集周期输入框的默认显示值（不影响已创建任务，只影响下一次新建）
        cfg.default_interval.valueChanged.connect(self.monitor_page.set_default_interval)

        # 监控页的页面间请求由主窗口中转：查看历史 / 被测进程异常结束时的托盘提醒
        self.monitor_page.history_requested.connect(self._show_history_for_task)
        self.monitor_page.task_ended_abnormally.connect(
            self._on_task_ended_abnormally)

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
        """初始化系统托盘图标。

        图标常驻：应用启动即出现，不随"关闭时最小化到托盘"开关增删——该开关
        只控制 closeEvent 的关闭行为，不控制图标本身的显隐。

        仅当当前系统支持系统托盘（QSystemTrayIcon.isSystemTrayAvailable()）时
        才创建；offscreen/CI 或部分精简系统该判断为 False，self.tray_icon
        保持 None，closeEvent 的托盘隐藏分支与各类补充气泡逻辑都因此天然短路，
        退化为无托盘现状（不变式⑤）。
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
        走同一条真退出路径）"""
        self._really_quit = True
        self.close()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """托盘图标激活事件：单击（Trigger）与双击（DoubleClick）均还原主界面；
        右键（Context）由 Qt 自动弹出 setContextMenu 设置的菜单，无需在此处理"""
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_from_tray()

    def quit_for_install(self) -> None:
        """供更新安装流程调用的公开方法：无论"关闭时最小化到
        托盘"是否开启，都必须走真退出路径——否则该开关开启时，安装向导已经
        启动，主程序却被 closeEvent 的隐藏分支拦截、常驻托盘继续占用文件，
        导致覆盖安装因文件占用而失败。"""
        self._really_quit = True
        self.close()

    def _on_commit_data_request(self, session_manager=None) -> None:
        """系统会话即将结束（注销/关机）：置真退出标志。

        随后 Qt 会向窗口发送关闭事件，closeEvent 据此跳过"隐藏到托盘"与
        三选确认对话框，直接走清理并退出的真退出路径（退出不变式保持不变）。"""
        self._really_quit = True

    def _on_update_available_silent(self, info: dict) -> None:
        """静默检查更新发现新版本：不由 about_page 直接弹出模态对话框打断用户，
        改为右上角 InfoBar 非模态提示+「查看」按钮，点击后切到关于页并弹出与
        手动检查一致的确认对话框。

        若此时主窗口处于托盘隐藏态，InfoBar 挂在隐藏窗口上不会被用户看到，
        需额外补一条托盘气泡。
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

    def _show_history_for_task(self, task_id: str) -> None:
        """监控页已结束卡片的"查看历史"：切到历史页并选中该任务"""
        self.switchTo(self.history_page)
        self.history_page.select_task(task_id)

    def _on_task_ended_abnormally(self, title: str, content: str) -> None:
        """被测进程非用户停止而结束：主窗口隐藏或不在前台时补一条托盘气泡
        （窗口在前台时监控页自身的 InfoBar 已足够显眼）。tray_icon 为 None
        （无托盘环境）时短路。"""
        if self.tray_icon is None:
            return
        if self.isHidden() or not self.isActiveWindow():
            self.tray_icon.showMessage(title, content, QSystemTrayIcon.Warning)

    def _ask_close_action(self) -> str:
        """仍有任务在监控时关闭窗口：询问用户的选择。

        Returns:
            'quit'（停止监控并退出）/ 'tray'（最小化到托盘）/ 'cancel'（取消关闭）。
            抽成独立方法，测试中可 monkeypatch，避免弹出模态对话框阻塞。
        """
        dialog = CloseActionDialog(
            self.monitor_manager.running_task_count(),
            can_tray=self.tray_icon is not None, parent=self)
        if dialog.exec():
            return dialog.choice
        return 'cancel'

    def _hide_to_tray(self, event) -> None:
        """隐藏到托盘：不停止任务、不做任何清理，忽略本次关闭事件。"""
        self.hide()
        if not self._tray_tip_shown:  # 每次运行只提示一次，避免反复打扰
            self.tray_icon.showMessage(
                config.APP_NAME, "已最小化到系统托盘，监控仍在后台运行",
                QSystemTrayIcon.Information)
            self._tray_tip_shown = True
        event.ignore()

    def closeEvent(self, event):
        """关闭窗口事件处理。

        默认（未开启"关闭时最小化到托盘"，或托盘不可用）：没有运行中任务时走
        完整清理流程 -> QApplication.quit()（唯一真退出路径，main.py 用 os._exit
        兜底）；仍有任务在监控时先询问"停止监控并退出 / 最小化到托盘 / 取消"。
        开启该设置且托盘可用时直接隐藏到托盘，不停止任务、不做任何清理，等待
        用户从托盘菜单选择"退出"。

        退出路径不变式（绝不破坏）：
        ① 默认配置（close_to_tray=False）且无运行中任务时，关闭行为与现状完全一致；
        ② 托盘"退出"与 About 页"立即安装"都置 _really_quit=True，绕开隐藏分支与
           关窗确认，走同一条真退出路径；
        ③ 隐藏到托盘不停止任务、不做任何清理；
        ④ 清理步骤中途抛异常，QApplication.quit() 仍必须执行（try/finally）；
        ⑤ offscreen/无托盘环境（isSystemTrayAvailable() 为 False）下
           tray_icon 为 None，隐藏分支天然短路，行为与现状一致。
        """
        if (not self._really_quit and self.tray_icon is not None
                and cfg.get(cfg.close_to_tray)):
            self._hide_to_tray(event)
            return

        # 真退出前的确认：仍有任务在监控时让用户选择，避免误关窗口丢失长时间监控
        if not self._really_quit and self.monitor_manager.has_running_tasks():
            action = self._ask_close_action()
            if action == 'cancel':
                event.ignore()
                return
            if action == 'tray' and self.tray_icon is not None:
                self._hide_to_tray(event)
                return
            # 'quit'：继续走下面的真退出流程

        # 真退出路径：既有全部清理步骤整体包进 try/finally，QApplication.quit() 放
        # finally——setQuitOnLastWindowClosed(False) 拆掉了"closeEvent 异常也能退出"
        # 的隐性保险丝，退出全靠这里显式调用 quit()；若清理中途抛异常又不放 finally，
        # 会导致事件循环不退出且窗口已被隐藏，用户无法通过任何界面操作恢复。
        try:
            # 1. 停止所有监控任务：stop_all_tasks 先给全部运行中线程置停止标志，再逐个
            #    无超时 wait，返回时各任务的收尾 flush 与状态落库都已完成
            self.monitor_manager.stop_all_tasks()

            # 1.1 兜底 join：stop_all_tasks 只处理调用时 isRunning 的线程；对仍在
            #     收尾的线程补一个带超时的 join。main.py 退出时用 os._exit() 直接
            #     终止进程（规避下载线程残留导致的 0xC0000409），若不在此兜底等待，
            #     可能在收尾写库的中途就被掐死，丢失最后一批数据。
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

            # 5. 设置页的数据库清理线程：仅 join 兜底，无取消机制——清理动作本身
            #    应尽快跑完，不强行中断以免 VACUUM 中途被打断导致数据库处于中间态
            cleanup_worker = getattr(self.setting_page, '_cleanup_worker', None)
            shutdown_thread(cleanup_worker, timeout_ms=5000)

            # 6. 历史页的自动刷新查询线程：只读查询、无取消机制，仅带超时 join 兜底，
            #    避免退出时线程仍在读库（超时只记日志，不阻塞更久）
            refresh_worker = getattr(self.history_page, '_refresh_worker', None)
            shutdown_thread(refresh_worker, timeout_ms=3000)

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
