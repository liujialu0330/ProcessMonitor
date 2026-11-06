"""
主窗口
使用FluentWindow创建带有Fluent UI风格的主界面
"""
import logging
import os
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon, PushButton
)

from core.shutdown import ShutdownCoordinator
from ui.pages.monitor_page import MonitorPage
from ui.pages.history_page import HistoryPage
from ui.pages.export_page import ExportPage
from ui.pages.about_page import AboutPage
from ui.pages.setting_page import SettingPage
from ui.services import CLOSE_ACTION_CANCEL, CLOSE_ACTION_TRAY, UiServices
from ui.tray import TrayController
from core.monitor_manager import MonitorManager
from data.database import Database
from app_config import cfg
import config

logger = logging.getLogger(__name__)


class MainWindow(FluentWindow):
    """主窗口类"""

    def __init__(self, db: Database, manager: MonitorManager, services: UiServices):
        """初始化主窗口

        三项依赖都由组合根（app.bootstrap）构造后注入，主窗口不再自建；测试注入隔离的
        临时库与替身服务。同一份 services 向下分发给全部页面。

        Args:
            db: 唯一数据库实例，向下分发给各页面（manager 也应持有同一实例）
            manager: 监控管理器
            services: 界面服务（提示、对话框、时钟、进程查询、任务状态、用户偏好）
        """
        super().__init__()

        self.db = db
        self.monitor_manager = manager
        self.services = services

        # 托盘驻留相关状态：必须在 _init_tray()/closeEvent 用到之前初始化完毕。
        # - _really_quit：托盘菜单"退出"、关于页"立即安装"与会话结束会置 True，
        #   closeEvent 据此跳过隐藏分支与关窗确认、直接走真退出路径。
        # - _session_ending：本次 _really_quit 是否只由会话结束（注销/关机）置位。
        #   会话结束可能被系统或其他程序取消，窗口随后重新显示时据此复位，避免之后
        #   用户点 X 跳过托盘与确认直接全停；托盘退出/立即安装置的标志不复位。
        # - _shutdown_started：真退出收尾已开始，之后再来的关闭事件不重复收尾。
        # - tray：托盘控制器，由 _init_tray() 创建；offscreen/CI 或精简系统下
        #   is_available() 为 False，closeEvent 的托盘隐藏分支与各类补充气泡逻辑都
        #   因此天然短路，退化为无托盘现状（不变式⑤）。
        # - _tray_tip_shown：首次隐藏到托盘时的提示气泡只弹一次。
        # 启动校正（孤儿任务、采样计数、过期清理）由组合根在构造本窗口之前完成。
        self._really_quit = False
        self._session_ending = False
        self._shutdown_started = False
        self.tray: Optional[TrayController] = None
        self._tray_tip_shown = False
        # 真退出收尾期间报告过的进度文案（按顺序；供日志与测试核对）
        self.shutdown_progress: List[str] = []

        # 初始化界面
        self._init_window()
        self._init_navigation()
        self._init_tray()

        # 注销/关机：系统会话结束时 closeEvent 的三选确认对话框会阻塞关机，
        # 这里在会话结束时置真退出标志并直接 close()，跳过确认走真退出流程。
        # DirectConnection：必须在 Qt 随后关闭各窗口之前同步执行。
        app = QApplication.instance()
        if app is not None:
            app.commitDataRequest.connect(
                self._on_commit_data_request, Qt.DirectConnection)

        # 非模态更新提醒：静默检查发现新版本时不由 about_page 直接弹模态对话框，
        # 而是发信号，这里统一处理为非模态提示（必须在 self.about_page 创建
        # 之后连接，故放在 _init_navigation 之后）
        self.about_page.update_available_silent.connect(self._on_update_available_silent)
        # 关于页"立即安装"：安装向导已启动，主程序必须真退出释放文件占用
        self.about_page.install_requested.connect(self.quit_for_install)

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
        """迁移失败且旧数据仍在原库（已从备份还原，或还原与改名都失败而原样保留）：
        本次运行新数据无法保存，下次启动会重新迁移"""
        self.services.notifier.warning(
            self, "数据库升级失败",
            "数据库升级失败，本次运行新数据无法保存；旧数据已保留，请重启重试。",
            duration=-1)

    def _show_data_reset_tip(self):
        """还原备份也失败：损坏库已改名保留，应用以新空库运行"""
        self.services.notifier.warning(
            self, "数据库已重置",
            "历史数据已移至 monitor.db.broken_*，应用以新空库正常运行。",
            duration=-1)

    def _show_backup_aborted_tip(self):
        """迁移前备份失败：中止迁移，旧库原样保留未做任何改动"""
        self.services.notifier.warning(
            self, "数据库升级已中止",
            "未能升级数据库，历史数据完好保存在磁盘，请释放磁盘空间后重启。",
            duration=-1)

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

        # 创建页面实例：统一注入同一数据库、监控管理器与服务集合
        # （about_page 不涉及数据库与任务，只注入服务）
        self.monitor_page = MonitorPage(
            self.db, self.monitor_manager, self.services, parent=self)
        self.history_page = HistoryPage(
            self.db, self.monitor_manager, self.services, parent=self)
        # 导出页需要 manager 登记导出，与设置页的清理/压缩数据库互斥
        self.export_page = ExportPage(
            self.db, self.monitor_manager, self.services, parent=self)
        # 设置页数据管理卡片需要 db（查询占用/清理压缩）与 manager（维护锁、
        # 是否有运行中任务决定清理按钮是否可用）
        self.setting_page = SettingPage(
            self.db, self.monitor_manager, self.services, parent=self)
        self.about_page = AboutPage(self.services, parent=self)

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
        """创建托盘控制器（图标常驻；托盘不可用时 is_available() 为 False）"""
        icon_path = config.get_icon_path()
        icon = QIcon(icon_path) if os.path.exists(icon_path) else self.windowIcon()
        self.tray = TrayController(self, icon, config.APP_NAME)
        self.tray.show_requested.connect(self._show_from_tray)
        self.tray.quit_requested.connect(self._quit_from_tray)

    @property
    def tray_icon(self) -> Optional[QSystemTrayIcon]:
        """兼容属性：托盘图标对象，托盘不可用时为 None。新代码请用 self.tray。"""
        return self.tray.icon if self.tray is not None else None

    def _tray_available(self) -> bool:
        return self.tray is not None and self.tray.is_available()

    def _show_from_tray(self) -> None:
        """托盘菜单"显示主界面"/单击或双击图标：从隐藏态还原窗口"""
        self._clear_session_end_flag()
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def showEvent(self, event):
        """窗口重新显示：此前的会话结束显然已被取消，复位它置的真退出标志"""
        self._clear_session_end_flag()
        super().showEvent(event)

    def _clear_session_end_flag(self) -> None:
        """只复位由会话结束置的 _really_quit；托盘退出/立即安装置的标志保持不变"""
        if self._session_ending:
            self._session_ending = False
            self._really_quit = False

    def _quit_from_tray(self) -> None:
        """托盘菜单"退出"：置真退出标志后走正常关闭流程（与 quit_for_install
        走同一条真退出路径）"""
        self._really_quit = True
        self.close()

    def quit_for_install(self) -> None:
        """更新安装流程（关于页 install_requested）：无论"关闭时最小化到托盘"是否
        开启，都必须走真退出路径——否则该开关开启时，安装向导已经启动，主程序却被
        closeEvent 的隐藏分支拦截、常驻托盘继续占用文件，导致覆盖安装因文件占用而失败。"""
        self._really_quit = True
        self.close()

    def _on_commit_data_request(self, session_manager=None) -> None:
        """系统会话即将结束（注销/关机）：置真退出标志并直接 close()。

        不能等 Qt 随后的关闭流程：它只关闭可见窗口，主窗口隐藏在托盘时收不到关闭
        事件，收尾协调器就被绕过了（任务未 join、数据库未关）。这里主动 close()，
        隐藏态也会进 closeEvent，跳过"隐藏到托盘"与三选确认，走真退出路径；收尾后
        窗口已隐藏，Qt 后续的关闭流程不会重复（closeEvent 自身也只收尾一次）。"""
        if not self._really_quit:
            # 已由托盘退出/立即安装置位时不标记为会话结束，免得窗口重新显示时被复位
            self._really_quit = True
            self._session_ending = True
        self.close()

    def _on_update_available_silent(self, info: dict) -> None:
        """静默检查更新发现新版本：不由 about_page 直接弹出模态对话框打断用户，
        改为顶部非模态提示+「查看」按钮，点击后切到关于页并弹出与
        手动检查一致的确认对话框。

        若此时主窗口处于托盘隐藏态，提示条挂在隐藏窗口上不会被用户看到，
        需额外补一条托盘气泡。
        """
        info_bar = self.services.notifier.info(
            self, f"发现新版本 {info['version']}", "点击查看详情", duration=10000)
        view_button = PushButton("查看")
        view_button.clicked.connect(lambda: self._show_update_detail(info))
        info_bar.addWidget(view_button)

        if self.isHidden() and self._tray_available():
            self.tray.show_message(
                f"发现新版本 {info['version']}", "打开主界面查看", 'info')

    def _show_update_detail(self, info: dict) -> None:
        """更新提示条「查看」按钮点击：切到关于页并弹出更新确认对话框"""
        self.switchTo(self.about_page)
        self.about_page.show_update_dialog_for(info)

    def _show_history_for_task(self, task_id: str) -> None:
        """监控页已结束卡片的"查看历史"：切到历史页并选中该任务"""
        self.switchTo(self.history_page)
        self.history_page.select_task(task_id)

    def _on_task_ended_abnormally(self, title: str, content: str) -> None:
        """被测进程自行结束（不是用户点停止）：主窗口隐藏或不在前台时补一条托盘气泡
        （窗口在前台时监控页自身的提示条已足够显眼）。托盘不可用时短路。"""
        if not self._tray_available():
            return
        if self.isHidden() or not self.isActiveWindow():
            self.tray.show_message(title, content, 'warning')

    def _ask_close_action(self) -> str:
        """仍有任务在监控时关闭窗口：询问用户的选择。

        Returns:
            'quit'（停止监控并退出）/ 'tray'（最小化到托盘）/ 'cancel'（取消关闭）。
            对话框经由注入的 services.dialogs；保留为薄方法，测试也可直接替换它。
        """
        return self.services.dialogs.ask_close_action(
            self, self.monitor_manager.running_task_count(),
            self._tray_available())

    def _hide_to_tray(self, event) -> None:
        """隐藏到托盘：不停止任务、不做任何清理，忽略本次关闭事件。"""
        self.hide()
        if not self._tray_tip_shown:  # 每次运行只提示一次，避免反复打扰
            self.tray.show_message(
                config.APP_NAME, "已最小化到系统托盘，监控仍在后台运行", 'info')
            self._tray_tip_shown = True
        event.ignore()

    def _on_shutdown_progress(self, text: str) -> None:
        """退出收尾的进度：窗口此时已隐藏，进度写日志并显示在托盘图标的悬停文字上"""
        self.shutdown_progress.append(text)
        logger.info("退出收尾: %s", text)
        if self.tray is not None:
            try:
                self.tray.set_tooltip(f"{config.APP_NAME}：{text}")
            except Exception:
                logger.warning("更新托盘提示失败", exc_info=True)

    def _run_shutdown(self) -> bool:
        """真退出收尾：先隐藏窗口给出即时反馈，再交给 ShutdownCoordinator 按固定顺序
        停任务、带超时 join、收尾登记表里的后台线程、关闭数据库。协调器有 30 秒
        总期限，超时只记日志；入口随后用 os._exit() 结束进程，所以期限内没收完的
        线程会被直接终止——"不丢数据"由期限加日志兜底，而不是无限等待。"""
        self.hide()
        coordinator = ShutdownCoordinator(
            self.monitor_manager, self.services.threads, db=self.db)
        ok = coordinator.run(on_progress=self._on_shutdown_progress)
        if not ok:
            logger.error("退出收尾未在期限内全部完成，详见上方日志")
        return ok

    def closeEvent(self, event):
        """关闭窗口事件处理。

        默认（未开启"关闭时最小化到托盘"，或托盘不可用）：没有运行中任务时走
        真退出流程 -> QApplication.quit()（唯一真退出路径，入口用 os._exit
        结束进程）；仍有任务在监控时先询问"停止监控并退出 / 最小化到托盘 / 取消"。
        开启该设置且托盘可用时直接隐藏到托盘，不停止任务、不做任何清理，等待
        用户从托盘菜单选择"退出"。

        退出路径不变式（绝不破坏）：
        ① 默认配置（close_to_tray=False）且无运行中任务时直接走真退出；
        ② 托盘"退出"、关于页"立即安装"与会话结束都置 _really_quit=True，绕开
           隐藏分支与关窗确认，走同一条真退出路径（会话结束直接 close()，托盘隐藏
           时也进协调器；被取消后窗口重新显示即复位它置的标志）；
        ③ 隐藏到托盘不停止任务、不做任何清理；
        ④ 收尾中途抛异常，QApplication.quit() 仍必须执行（try/finally）；
        ⑤ offscreen/无托盘环境下托盘不可用，隐藏分支天然短路；
        ⑥ 真退出的收尾只经 ShutdownCoordinator，总期限 30 秒，超时记日志；
           收尾只做一次，之后再来的关闭事件直接接受。
        """
        if self._shutdown_started:
            event.accept()
            return

        if (not self._really_quit and self._tray_available()
                and self.services.preferences.close_to_tray()):
            self._hide_to_tray(event)
            return

        # 真退出前的确认：仍有任务在监控时让用户选择，避免误关窗口丢失长时间监控
        if not self._really_quit and self.monitor_manager.has_running_tasks():
            action = self._ask_close_action()
            if action == CLOSE_ACTION_CANCEL:
                event.ignore()
                return
            if action == CLOSE_ACTION_TRAY and self._tray_available():
                self._hide_to_tray(event)
                return
            # 'quit'：继续走下面的真退出流程

        # 真退出路径：QApplication.quit() 放 finally——setQuitOnLastWindowClosed(False)
        # 拆掉了"closeEvent 异常也能退出"的隐性保险丝，退出全靠这里显式调用 quit()；
        # 收尾中途抛异常又不放 finally，事件循环不会退出而窗口已隐藏，用户无从恢复。
        self._shutdown_started = True
        try:
            self._run_shutdown()
            # 收尾完成才显式 accept；异常路径下 QCloseEvent 默认已是 accepted，
            # 且 finally 的 quit() 与入口的 os._exit() 兜底退出，不依赖这一行
            event.accept()
        finally:
            # 真退出前主动隐藏托盘图标（理由见 TrayController.hide）。托盘对象出错
            # 不得妨碍随后必须执行的 quit()
            if self.tray is not None:
                try:
                    self.tray.hide()
                except Exception:
                    logger.warning("隐藏托盘图标失败", exc_info=True)
            QApplication.quit()
