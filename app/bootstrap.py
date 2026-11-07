"""
组合根：应用的启动顺序与依赖组装只在这里出现一次。

run() 的顺序是固定的，调整前先确认每一步的前置条件：
1. 解析路径并建目录 → 2. 日志与全局异常兜底（必须早于 ui/core/data 的导入，
   这些模块导入期的异常也要能落日志）→ 3. QApplication → 4. 单实例（已有实例则通知后退出）
→ 5. 应用配置、主题、排版（页面构造时就会读配置）→ 6. 显式迁移数据库
→ 7. 构造 Database（不再在构造时迁移）→ 8. 启动校正（任何任务启动之前）
→ 9. 线程登记表、界面服务 → 10. 主窗口（托盘由主窗口创建并持有）→ 11. 事件循环
→ 12. 结束进程

退出收尾（停任务、等线程、关闭数据库）只在主窗口的真退出路径里经 ShutdownCoordinator
做一次，这里不再重复关闭数据库。

build_context() 是第 6~9 步，不涉及 QApplication 与窗口，测试可直接用临时目录调用。
"""
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, List, Optional

from app.paths import AppPaths

if TYPE_CHECKING:
    from core.maintenance_service import MaintenanceService, ReconcileResult
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    from data.database import Database
    from ui.main_window import MainWindow
    from ui.services import UiServices

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """组合根组装出的全部长生命周期对象"""
    paths: AppPaths
    db: "Database"
    manager: "MonitorManager"
    services: "UiServices"
    registry: "ThreadRegistry"
    maintenance: "MaintenanceService"
    reconcile: "ReconcileResult"
    window: Optional["MainWindow"] = None


def build_context(paths: AppPaths, *, registry=None,
                  retention_days: Optional[int] = None) -> AppContext:
    """
    迁移数据库并组装 Database / MonitorManager / 维护服务 / 界面服务（不建 QApplication 与窗口）。

    Args:
        paths: 应用路径（调用方负责 ensure_dirs）
        registry: 线程登记表；None 时新建一个
        retention_days: 启动清理用的保留天数；None 时取用户配置
    """
    from core.maintenance_service import MaintenanceService
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    from data.database import Database
    from data.db.connection import ConnectionFactory
    from data.db.migrations.runner import MigrationRunner

    # 迁移显式放在组合根：它可能备份、还原、改名库文件，必须早于任何常驻连接
    report = MigrationRunner(ConnectionFactory(paths.db_path, keep_alive=False)).migrate()
    logger.info("数据库迁移检查完成: %s", report)
    db = Database(paths.db_path, migrate=False, migration_report=report)

    manager = MonitorManager(db=db)
    maintenance = MaintenanceService(db, manager)
    if retention_days is None:
        from app_config import cfg
        retention_days = cfg.get(cfg.retention_days)
    # 必须在任何监控任务启动之前；此时 manager 刚构造，还没有任务
    reconcile = maintenance.startup_reconcile(retention_days)

    if registry is None:
        registry = ThreadRegistry()
    from ui.services import production_services
    services = production_services(manager, registry)
    return AppContext(paths=paths, db=db, manager=manager, services=services,
                      registry=registry, maintenance=maintenance, reconcile=reconcile)


def _create_application(argv: List[str]):
    """创建 QApplication：高 DPI、应用信息、图标，关闭"最后一个窗口关闭即退出" """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import QApplication

    import config

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    app = QApplication(argv)

    # 托盘驻留要求"隐藏到托盘"时应用继续运行：关掉 Qt 默认的"最后一个可见窗口关闭
    # 即 quit()"，唯一真退出路径是主窗口收尾完成后显式调用的 QApplication.quit()
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationVersion(config.APP_VERSION)

    icon_path = config.get_icon_path()
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    return app


def _apply_preferences(app, paths: AppPaths) -> None:
    """
    加载应用配置并应用主题、排版。

    必须在 QApplication 之后（ConfigItem 依赖 Qt 信号机制）、主窗口之前（页面构造时
    就会读配置，已创建的 Fluent 标签也不会再跟随后改的字体）。
    """
    from qfluentwidgets import setTheme

    from app_config import cfg, load_app_config
    from ui.typography import configure_application_typography

    load_app_config(paths)
    configure_application_typography(app)
    # 必须传原始配置值（可能是 Theme.AUTO），不能用 qconfig.theme（那是跟随系统
    # 解析后的具体浅/深色）——否则每次启动都会把"跟随系统"静默覆写成当时的系统主题
    setTheme(cfg.get(cfg.themeMode))


def _create_marker_hotkey(app, services):
    """全局打点快捷键：装到 QApplication 的原生事件过滤器上，按用户偏好注册。

    注册失败（被其他程序占用、非 Windows）只记日志，设置页显示"快捷键不可用"，
    不影响启动。
    """
    from ui.hotkey import MarkerHotkey

    hotkey = MarkerHotkey()
    hotkey.install(app)
    hotkey.bind(services.preferences.marker_hotkey())
    if not hotkey.is_active():
        logger.info("全局打点快捷键未启用：%s", hotkey.status_text())
    return hotkey


def run(argv: List[str], *, exit_process: Callable[[int], None] = os._exit) -> int:
    """
    应用入口：按模块说明的固定顺序启动，返回进程退出码。

    Args:
        argv: 命令行参数（传给 QApplication）
        exit_process: 事件循环结束后终止进程的函数，默认 os._exit（理由见函数末尾）；
            测试可替换
    """
    paths = AppPaths.resolve()
    paths.ensure_dirs()

    # 日志与全局异常兜底最先初始化，之后任何初始化异常（含 ui/core/data 导入期）都能落日志
    from utils.crash_handler import install_excepthook
    from utils.logger import setup_logging
    log_dir = setup_logging(paths.log_dir)
    install_excepthook(log_dir)

    app = _create_application(argv)

    # 单实例：已有实例时通知其激活窗口后直接返回；此时尚未创建任何线程与窗口，
    # 正常结束解释器即可。实例按"用户 + 数据目录"区分，不同数据目录可以并存
    from app.single_instance import SingleInstanceGuard, activate_main_window
    guard = SingleInstanceGuard(data_dir=paths.data_dir)
    if guard.notify_existing_instance():
        logger.info("检测到已运行的实例，已通知其激活窗口，本进程退出")
        return 0
    guard.listen()

    _apply_preferences(app, paths)

    ctx = build_context(paths)

    from ui.main_window import MainWindow
    hotkey = _create_marker_hotkey(app, ctx.services)
    window = MainWindow(ctx.db, ctx.manager, ctx.services, hotkey=hotkey)
    ctx.window = window
    window.show()
    guard.activation_requested.connect(lambda: activate_main_window(window))

    exit_code = app.exec_()

    # 主窗口收尾时已尽力停止后台线程，但仍可能有线程没能在期限内停下（如下载线程
    # 阻塞在 urlopen() 的 connect() 阶段，取消标志只在读取循环里检查）。走 Python
    # 正常关闭流程时，Qt 析构仍在运行的 QThread 会致命中止（Windows 上表现为
    # 0xC0000409），因此用 os._exit 直接结束进程：不经过 Python/Qt 对象析构，由操作
    # 系统回收线程与句柄。日志按行 flush，不受影响；本应用不依赖 atexit 清理逻辑。
    logger.info("应用退出，退出码=%d", exit_code)
    exit_process(exit_code)
    return exit_code
