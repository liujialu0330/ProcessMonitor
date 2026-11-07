"""
主窗口

FluentWindow 外壳：侧栏 监控 / 分析 / 设置（底部），窗口宽度 ≥ 1200 时侧栏展开显示文字。
主窗口只做页面之间、页面与托盘/全局快捷键之间的中转，不承担业务流程：
- 监控卡片"分析"→ 切到分析页并选中该任务；
- 托盘菜单"打点"、全局快捷键 → 监控页的快速打点，结果用托盘气泡确认；
- 静默检查到新版本 → 非模态提示，"查看"跳到设置页"关于与更新"。
启动提示也在这里：数据库迁移结果（三态失败提示、升级成功时旧文件的备份位置）、只读库的
常驻横幅（同时让监控页禁止开始监控与打点）、数据目录位于同步盘的警告。
"""
import logging
import os
from typing import Any, Callable, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon, PushButton
)

from core.shutdown import ShutdownCoordinator
from ui.hotkey import MarkerHotkey
from ui.pages.analysis_page import AnalysisPage
from ui.pages.monitor_page import MonitorPage
from ui.pages.setting_page import SettingPage
from ui.presenters.monitor_presenter import QUICK_MARKER_OK
from ui.services import CLOSE_ACTION_CANCEL, CLOSE_ACTION_TRAY, UiServices
from ui.storage_notices import StorageNotices
from ui.theme_follow import SystemThemeWatcher
from ui.tray import TrayController
from core.monitor_manager import MonitorManager
from data.database import Database
from data.db.schema import SCHEMA_VERSION
from ui.formatting import (
    MIGRATED_TIP_DURATION_MS, MIGRATED_TIP_TITLE, READ_ONLY_START_BLOCKED, READ_ONLY_TITLE,
    SCHEMA_STALE_START_BLOCKED, migration_failed_text, schema_stale_banner_text,
    backup_aborted_text, migrated_tip_text, read_only_banner_text,
)
from app_config import cfg
import config

logger = logging.getLogger(__name__)

# 窗口宽度达到该值时侧栏展开显示文字，窄于它时收成图标栏
NAV_EXPAND_MIN_WIDTH = 1200
NAV_EXPAND_WIDTH = 150

# 启动提示延迟到窗口显示之后再弹出
STARTUP_TIP_DELAY_MS = 500
# 数据目录位于同步盘的提示标题（内容由组合根给出）
SYNC_DIR_TIP_TITLE = "存储位置风险"


class MainWindow(FluentWindow):
    """主窗口类"""

    def __init__(self, db: Database, manager: MonitorManager, services: UiServices, *,
                 hotkey: Optional[MarkerHotkey] = None,
                 diagnostics_provider: Optional[Callable[[], Any]] = None):
        """初始化主窗口

        三项依赖都由组合根（app.bootstrap）构造后注入，主窗口不再自建；测试注入隔离的
        临时库与替身服务。同一份 services 向下分发给全部页面。

        Args:
            db: 唯一数据库实例，向下分发给各页面（manager 也应持有同一实例）
            manager: 监控管理器
            services: 界面服务（提示、对话框、时钟、进程查询、任务状态、用户偏好）
            hotkey: 全局打点快捷键（组合根创建、安装并绑定）；为 None 时不接全局快捷键，
                设置页显示"快捷键不可用"——测试默认如此，绝不在桌面上注册真实热键
            diagnostics_provider: 设置页"复制诊断信息 / 导出诊断包"取诊断部件的函数（组合根
                传返回 AppContext 的函数）；None 时设置页只用 db 与 manager
        """
        super().__init__()

        self.db = db
        self.monitor_manager = manager
        self.services = services
        self.hotkey = hotkey
        self.diagnostics_provider = diagnostics_provider
        # 界面卡死看门狗（组合根在窗口显示后安装并交给这里）：真退出收尾会阻塞界面线程，
        # 收尾前必须先停掉它，否则会把正常的收尾当成卡死转储
        self.watchdog = None
        # 存储守卫（组合根在窗口显示后经 attach_storage_guard 交给这里）与把它的信号翻译成
        # 界面提示的 StorageNotices：真退出收尾前同样要先停守卫
        self.storage_guard = None
        self.storage_notices: Optional[StorageNotices] = None

        # 托盘驻留相关状态：必须在 _init_tray()/closeEvent 用到之前初始化完毕。
        # - _really_quit：托盘菜单"退出"、设置页"立即安装"与会话结束会置 True，
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
        # 只读库的常驻横幅（窗口显示后创建；可写库为 None）
        self.read_only_banner = None

        # 被测目标元数据里的显示缩放只有界面层读得到：交给 manager，任务启动时读一次
        dpi_scale = getattr(services, 'dpi_scale', None)
        set_dpi_provider = getattr(manager, 'set_dpi_scale_provider', None)
        if dpi_scale is not None and set_dpi_provider is not None:
            set_dpi_provider(dpi_scale)

        # 初始化界面
        self._init_window()
        self._init_navigation()
        self._init_tray()
        self._init_hotkey()

        # "跟随系统"主题：系统切换深浅色后界面跟上（只在 AUTO 模式下生效，见 ui.theme_follow）
        self.theme_watcher = SystemThemeWatcher(self).start()

        # 注销/关机：系统会话结束时 closeEvent 的三选确认对话框会阻塞关机，
        # 这里在会话结束时置真退出标志并直接 close()，跳过确认走真退出流程。
        # DirectConnection：必须在 Qt 随后关闭各窗口之前同步执行。
        app = QApplication.instance()
        if app is not None:
            app.commitDataRequest.connect(
                self._on_commit_data_request, Qt.DirectConnection)

        # 非模态更新提醒：静默检查发现新版本时不由"关于与更新"分组直接弹模态对话框，
        # 而是发信号，这里统一处理为非模态提示（必须在 self.about_group 创建
        # 之后连接，故放在 _init_navigation 之后）
        self.about_group.update_available_silent.connect(self._on_update_available_silent)
        # "立即安装"：安装向导已启动，主程序必须真退出释放文件占用
        self.about_group.install_requested.connect(self.quit_for_install)

        # 启动3秒后静默检查更新（对齐参考实现，避免阻塞启动）
        QTimer.singleShot(3000, lambda: self.about_group.check_update(silent=True))

        # 数据库迁移三态提示（延迟到窗口显示后弹出，三态互斥，按严重程度顺序判断）；
        # 都不是而本次刚升级成功时，提示旧文件的备份位置。升级失败、库仍是旧结构时
        # 只挂下面的只读横幅（它已写明失败原因与处理办法），不再重复弹失败提示
        db = self.db
        schema_mismatch = getattr(db, 'schema_mismatch', False)
        if not schema_mismatch:
            if db.backup_aborted:
                QTimer.singleShot(STARTUP_TIP_DELAY_MS, self._show_backup_aborted_tip)
            elif db.data_reset:
                QTimer.singleShot(STARTUP_TIP_DELAY_MS, self._show_data_reset_tip)
            elif db.migration_failed:
                QTimer.singleShot(STARTUP_TIP_DELAY_MS, self._show_migration_failed_tip)
            elif (getattr(db, 'backup_path', None)
                  and getattr(db, 'migrated_from', None) is not None):
                QTimer.singleShot(STARTUP_TIP_DELAY_MS, self._show_migrated_tip)

        # 只读库（由更高版本创建，或升级失败仍是旧结构）：立即禁止开始监控与打点，
        # 窗口显示后挂常驻横幅
        if getattr(db, 'read_only', False):
            self.monitor_page.presenter.set_read_only(
                SCHEMA_STALE_START_BLOCKED if schema_mismatch else READ_ONLY_START_BLOCKED)
            QTimer.singleShot(STARTUP_TIP_DELAY_MS, self._show_read_only_banner)

    def show_sync_dir_warning(self, text: str, delay_ms: int = STARTUP_TIP_DELAY_MS) -> None:
        """数据目录位于同步盘：窗口显示后给一条警告提示（每次启动都提示，但不弹对话框）。

        由组合根在建窗后按 app.paths.sync_dir_hint 的结果调用；本窗口不判断路径。
        """
        QTimer.singleShot(delay_ms, lambda: self._show_sync_dir_tip(text))

    def _show_sync_dir_tip(self, text: str) -> None:
        self.services.notifier.warning(self, SYNC_DIR_TIP_TITLE, text, duration=-1)

    def _show_migration_failed_tip(self):
        """迁移失败且旧数据仍在原库（已从备份还原，或还原与改名都失败而原样保留）：
        本次运行新数据无法保存，下次启动会重新迁移"""
        report = getattr(self.db, 'migration_report', None)
        self.services.notifier.warning(
            self, "数据库升级失败", migration_failed_text(getattr(report, 'reason', None)),
            duration=-1)

    def _show_data_reset_tip(self):
        """还原备份也失败：损坏库已改名保留，应用以新空库运行"""
        self.services.notifier.warning(
            self, "数据库已重置",
            "历史数据已移至 monitor.db.broken_*，应用以新空库正常运行。",
            duration=-1)

    def _show_backup_aborted_tip(self):
        """迁移前备份失败或磁盘预检不足：中止迁移，旧库原样保留未做任何改动"""
        report = getattr(self.db, 'migration_report', None)
        self.services.notifier.warning(
            self, "数据库升级已中止",
            backup_aborted_text(getattr(report, 'required_bytes', None),
                                getattr(report, 'free_bytes', None)),
            duration=-1)

    def _show_migrated_tip(self):
        """本次启动成功升级了库：告诉用户旧文件保留在哪里（回退到 1.4.1 时要用）"""
        report = self.db.migration_report
        version = report.to_version if report is not None else SCHEMA_VERSION
        self.services.notifier.success(
            self, MIGRATED_TIP_TITLE, migrated_tip_text(version, self.db.backup_path),
            duration=MIGRATED_TIP_DURATION_MS)

    def read_only_banner_content(self) -> str:
        """只读横幅的文案：升级失败（旧结构）时写明失败原因，否则说明库由更高版本创建"""
        if getattr(self.db, 'schema_mismatch', False):
            report = getattr(self.db, 'migration_report', None)
            return schema_stale_banner_text(
                getattr(report, 'reason', None), bool(getattr(report, 'disk_shortage', False)))
        return read_only_banner_text(getattr(self.db, 'newer_version', None))

    def _show_read_only_banner(self):
        """只读库：顶部常驻警告（不自动消失）"""
        self.read_only_banner = self.services.notifier.warning(
            self, READ_ONLY_TITLE, self.read_only_banner_content(), duration=-1)

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_navigation_expand(event.oldSize().width(), event.size().width())

    def _sync_navigation_expand(self, old_width: int, new_width: int) -> None:
        """宽度跨过 1200 时展开侧栏（窄于阈值时 qfluentwidgets 自己会收起）。

        只在"跨过"时动作：用户在宽窗口里手动收起侧栏后，单纯拖动窗口不会又被展开。
        """
        nav = getattr(self, 'navigationInterface', None)
        if nav is None:
            return
        panel = nav.panel
        if new_width >= NAV_EXPAND_MIN_WIDTH > old_width and panel.isCollapsed():
            panel.expand(useAni=False)

    def _init_navigation(self):
        """初始化导航栏和页面"""
        nav = self.navigationInterface
        nav.setExpandWidth(NAV_EXPAND_WIDTH)
        # 只有三个一级页面，没有可返回的层级
        nav.setReturnButtonVisible(False)
        nav.setMinimumExpandWidth(NAV_EXPAND_MIN_WIDTH)

        hotkey_text = self.hotkey.active_text() if self.hotkey is not None else ''
        # 创建页面实例：统一注入同一数据库、监控管理器与服务集合
        self.monitor_page = MonitorPage(
            self.db, self.monitor_manager, self.services, parent=self,
            hotkey_text=hotkey_text)
        # 分析页（历史数据 + 导出合并）：只读库，打点编辑与导出登记经 manager
        self.analysis_page = AnalysisPage(
            self.db, self.monitor_manager, self.services, parent=self)
        # 设置页数据管理卡片需要 db（查询占用/清理压缩）与 manager（维护锁、
        # 是否有运行中任务决定清理按钮是否可用）；快捷键卡片需要快捷键服务
        self.setting_page = SettingPage(
            self.db, self.monitor_manager, self.services, parent=self,
            hotkey=self.hotkey, diagnostics_provider=self.diagnostics_provider)
        # 原"关于"页已并入设置页；about_group 指向该分组（更新流程的入口都在它上面）
        self.about_group = self.setting_page.about_group

        # 默认采集间隔联动：设置页改动后实时同步到新建表单的默认值
        # （不影响已创建任务，只影响下一次新建）
        cfg.default_interval.valueChanged.connect(self.monitor_page.set_default_interval)

        # 监控页的页面间请求由主窗口中转：去分析 / 被测进程异常结束时的托盘提醒 /
        # 快速打点的结果反馈
        self.monitor_page.history_requested.connect(self._show_history_for_task)
        self.monitor_page.task_ended_abnormally.connect(
            self._on_task_ended_abnormally)
        self.monitor_page.presenter.quick_marker_feedback.connect(
            self._on_quick_marker_feedback)

        self.addSubInterface(
            self.monitor_page, FluentIcon.SPEED_HIGH, '监控', NavigationItemPosition.TOP)
        self.addSubInterface(
            self.analysis_page, FluentIcon.MARKET, '分析', NavigationItemPosition.TOP)
        self.addSubInterface(
            self.setting_page, FluentIcon.SETTING, '设置', NavigationItemPosition.BOTTOM)

        # 默认显示监控页面
        self.switchTo(self.monitor_page)

    def attach_watchdog(self, watchdog) -> None:
        """组合根装好界面看门狗后交给主窗口：真退出时先停掉它。"""
        self.watchdog = watchdog

    def _stop_watchdog(self) -> None:
        if self.watchdog is None:
            return
        try:
            self.watchdog.stop()
        except Exception:
            logger.warning("停止界面看门狗失败", exc_info=True)

    def attach_storage_guard(self, guard) -> None:
        """组合根创建存储守卫后交给主窗口（在 guard.start() 之前调用，首次检查才不会错过
        提示）：磁盘空间提示、暂停原因标注、完整性警告都经 StorageNotices 显示；真退出时先停它。"""
        self.storage_guard = guard
        self.storage_notices = StorageNotices(
            guard, self.services.notifier, self, db_path=getattr(guard, 'db_path', None),
            set_pause_marks=self.monitor_page.presenter.set_space_paused,
            show_integrity=self.setting_page.show_integrity_warning,
            tray_message=self._storage_tray_message, parent=self)

    def _storage_tray_message(self, title: str, content: str) -> None:
        """窗口隐藏在托盘时，补发存储提示的托盘气泡（提示条挂在隐藏的窗口上看不到）"""
        if self.isHidden() and self._tray_available():
            self.tray.show_message(title, content, 'warning')

    def _stop_storage_guard(self) -> None:
        if self.storage_guard is None:
            return
        try:
            self.storage_guard.stop()
        except Exception:
            logger.warning("停止存储守卫失败", exc_info=True)

    def _init_tray(self) -> None:
        """创建托盘控制器（图标常驻；托盘不可用时 is_available() 为 False）"""
        icon_path = config.get_icon_path()
        icon = QIcon(icon_path) if os.path.exists(icon_path) else self.windowIcon()
        self.tray = TrayController(
            self, icon, config.APP_NAME,
            marker_targets=self.monitor_page.presenter.marker_targets)
        self.tray.show_requested.connect(self._show_from_tray)
        self.tray.quit_requested.connect(self._quit_from_tray)
        self.tray.marker_requested.connect(self._on_tray_marker_requested)

    def _init_hotkey(self) -> None:
        """全局打点快捷键：按下即为全部运行中的任务打点；改键/失效时同步监控页引导文案。"""
        if self.hotkey is None:
            return
        self.hotkey.triggered.connect(self.quick_marker)
        self.hotkey.status_changed.connect(self._on_hotkey_status_changed)

    def _on_hotkey_status_changed(self) -> None:
        self.monitor_page.set_hotkey_text(self.hotkey.active_text())

    # ========== 快速打点（托盘 / 全局快捷键） ==========

    def quick_marker(self, task_id: Optional[str] = None) -> None:
        """不弹窗打点：task_id 为空表示全部运行中的任务。"""
        self.monitor_page.presenter.quick_marker(task_id or None)

    def _on_tray_marker_requested(self, task_id: str) -> None:
        self.quick_marker(task_id or None)

    def _on_quick_marker_feedback(self, kind: str, title: str, content: str) -> None:
        """快速打点的结果：用户此时多半在被测程序里，托盘气泡是不抢焦点的确认方式；
        没有托盘时退回窗口内提示。"""
        if self._tray_available():
            self.tray.show_message(
                title, content, 'info' if kind == QUICK_MARKER_OK else 'warning')
            return
        notifier = self.services.notifier
        if kind == QUICK_MARKER_OK:
            notifier.success(self, title, content, duration=2000)
        else:
            notifier.warning(self, title, content, duration=3000)

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

    def request_quit(self) -> None:
        """命令行 quit（IPC）：与托盘菜单"退出"走同一条真退出路径。"""
        self._quit_from_tray()

    def quit_for_install(self) -> None:
        """更新安装流程（install_requested）：无论"关闭时最小化到托盘"是否
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
        """静默检查更新发现新版本：不直接弹出模态对话框打断用户，改为顶部非模态
        提示+「查看」按钮，点击后切到设置页"关于与更新"并弹出与手动检查一致的
        确认对话框。

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
        """更新提示条「查看」按钮点击：切到设置页"关于与更新"并弹出更新确认对话框"""
        self.switchTo(self.setting_page)
        self.setting_page.scroll_to_about()
        self.about_group.show_update_dialog_for(info)

    def _show_history_for_task(self, task_id: str) -> None:
        """监控卡片的"分析"/"查看分析"：切到分析页并选中该任务"""
        self.switchTo(self.analysis_page)
        self.analysis_page.select_task(task_id)

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
        ② 托盘"退出"、设置页"立即安装"与会话结束都置 _really_quit=True，绕开
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
        # 收尾会阻塞界面线程（最长 30 秒），看门狗不能把它当成卡死
        self._stop_watchdog()
        # 守卫的定时器与在途后台作业也先停（作业的等待交给登记表）
        self._stop_storage_guard()
        # 收尾期间不再切主题（窗口已隐藏，重刷样式表只会拖慢退出）
        self.theme_watcher.stop()
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
            # 进程结束时系统也会回收热键；主动注销让收尾顺序可预期
            if self.hotkey is not None:
                try:
                    self.hotkey.unbind()
                except Exception:
                    logger.warning("注销全局快捷键失败", exc_info=True)
            QApplication.quit()
