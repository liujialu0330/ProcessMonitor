"""
组合根：应用的启动顺序与依赖组装只在这里出现一次。

run() 的顺序是固定的，调整前先确认每一步的前置条件：
1. 解析路径（命令行 --data-dir → 环境变量 → 便携标志 → 默认）并建目录；数据目录不可用时
   只在默认日志目录记一条错误并返回 EXIT_DATA_DIR_UNAVAILABLE，绝不回退到别的目录（那会让
   数据悄悄写到别处）；打包的窗口程序没有控制台，另弹原生错误框说明路径与原因
→ 2. 日志与全局异常兜底（必须早于 ui/core/data 的导入，这些模块导入期的异常也要能落日志）
→ 3. QApplication → 4. 单实例（已有实例则通知后退出）
→ 5. 应用配置、主题、排版（页面构造时就会读配置；进度窗口也要跟随主题）
→ 6. 迁移数据库：探测（不写入）→ 待搬迁的行数或库文件超过阈值时先显示进度窗口、迁移在
   工作线程执行，否则在界面线程同步迁移 → 结果为 FAILED_KEPT（旧库没能回到原路径）时弹
   原生错误框说明原文件位置并返回 EXIT_MIGRATION_FAILED，绝不在原路径建空库继续运行
→ 7. 读库的 meta：库由更高版本写入、本版本不能安全修改时只读打开；构造 Database（不再在
   构造时迁移）→ 8. 启动校正（任何任务启动之前；只读库跳过）
→ 9. 线程登记表、界面服务 → 10. 主窗口（托盘由主窗口创建并持有；迁移结果与只读状态由主窗口
按 Database 的标志提示；数据目录在同步盘上时交给主窗口显示一次提示）→ 11. 事件循环
→ 12. 结束进程

退出收尾（停任务、等线程、关闭数据库）只在主窗口的真退出路径里经 ShutdownCoordinator
做一次，这里不再重复关闭数据库。

build_context() 是第 7~9 步（未传入迁移报告时先在这里同步迁移），不涉及 QApplication 与
窗口，测试可直接用临时目录调用。
"""
import logging
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

import config
from app.paths import (
    AppPaths, DataDirError, parse_data_dir_arg, strip_data_dir_args, sync_dir_hint,
)

if TYPE_CHECKING:
    from core.maintenance_service import MaintenanceService, ReconcileResult
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    from data.database import Database
    from data.db.migrations.runner import MigrationReport, MigrationRunner, ProbeResult
    from ui.main_window import MainWindow
    from ui.services import UiServices

logger = logging.getLogger(__name__)

# 数据目录不可用（--data-dir 缺少路径、路径非法、无权限创建）时 run() 的返回码
EXIT_DATA_DIR_UNAVAILABLE = 2
# 数据库升级失败且旧库没能回到原路径（FAILED_KEPT）时 run() 的返回码
EXIT_MIGRATION_FAILED = 3

# 迁移前先显示进度窗口的阈值：待搬迁的数据点行数超过它，或库文件（含 -wal）超过字节阈值
MIGRATION_PROGRESS_THRESHOLD_ROWS = 500_000
MIGRATION_PROGRESS_THRESHOLD_BYTES = 100 * 1024 * 1024

# 启动失败时的原生错误框（此时没有主窗口，打包的窗口程序也没有控制台）
MESSAGE_BOX_TITLE = config.APP_NAME
DATA_DIR_FAILURE_TEXT = "数据目录不可用，程序无法启动。\n\n路径：{path}\n原因：{reason}"
DATA_DIR_UNKNOWN_PATH = "（未给出）"
MIGRATION_FAILED_TEXT = (
    "数据库升级失败，原文件在：\n{path}\n\n程序将退出。请不要删除这个文件：把它改名回 "
    "{name} 后重新启动即可重试升级，详情见日志。")
_MB_ICONERROR = 0x10

# 数据目录来源在日志里的名称
_SOURCE_LOG_NAMES = {
    config.DATA_DIR_SOURCE_CLI: "命令行 " + config.DATA_DIR_ARG,
    config.DATA_DIR_SOURCE_ENV: "环境变量 " + config.DATA_DIR_ENV,
    config.DATA_DIR_SOURCE_PORTABLE: "便携模式 " + config.PORTABLE_FLAG_FILENAME,
    config.DATA_DIR_SOURCE_DEFAULT: "默认",
}


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
                  retention_days: Optional[int] = None,
                  report: Optional["MigrationReport"] = None) -> AppContext:
    """
    组装 Database / MonitorManager / 维护服务 / 界面服务（不建 QApplication 与窗口）。

    Args:
        paths: 应用路径（调用方负责 ensure_dirs）
        registry: 线程登记表；None 时新建一个
        retention_days: 启动清理用的保留天数；None 时取用户配置
        report: run() 已跑出的迁移报告（可能经过进度窗口），传入时不再迁移第二次；
            None 时在这里同步迁移
    """
    from core.maintenance_service import MaintenanceService
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    from data.database import Database
    from data.db.migrations.runner import MigrationOutcome

    # 迁移显式放在组合根：它可能备份、还原、改名库文件，必须早于任何常驻连接
    if report is None:
        report = new_migration_runner(paths).migrate()
    logger.info("数据库迁移检查完成: %s", report)
    if report.outcome is MigrationOutcome.MIGRATED and report.backup_path:
        logger.info("数据库已升级到 v%d，旧文件已保留为 %s（可用于回退到 1.4.1）",
                    report.to_version, report.backup_path)

    read_only, newer_version = _read_only_state(paths.db_path)
    db = Database(paths.db_path, migrate=False, migration_report=report,
                  read_only=read_only, newer_version=newer_version)

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


def new_migration_runner(paths: AppPaths, on_progress=None) -> "MigrationRunner":
    """迁移运行器：迁移期间要改名库文件，连接工厂不能持有常驻连接"""
    from data.db.connection import ConnectionFactory
    from data.db.migrations.runner import MigrationRunner
    return MigrationRunner(ConnectionFactory(paths.db_path, keep_alive=False),
                           on_progress=on_progress)


def _read_only_state(db_path: str) -> Tuple[bool, Optional[str]]:
    """
    按库的 meta 判断本版本能否安全写入：返回 (是否只读, 写入本库的更高版本号)。

    读 meta 失败（库被锁等）不阻止启动，按可写打开并记日志：随后真有写入失败时各处会
    各自报错，比因一次读取失败把用户挡在只读模式里更可取。
    """
    import sqlite3

    from data.db import meta as meta_store
    try:
        meta = meta_store.read_meta_file(db_path)
    except sqlite3.Error:
        logger.error("读取数据库元数据失败，按可写方式打开: %s", db_path, exc_info=True)
        return False, None
    reason = meta_store.reader_blocked(meta, config.APP_VERSION)
    if reason is None:
        return False, None
    logger.warning("%s: %s", reason, db_path)
    return True, meta_store.newer_writer_version(meta)


def needs_progress_window(probe: "ProbeResult") -> bool:
    """是否先显示迁移进度窗口：需要迁移，且行数估计或库文件大小超过阈值"""
    return probe.needs_migration and (
        probe.estimated_rows > MIGRATION_PROGRESS_THRESHOLD_ROWS
        or probe.file_bytes > MIGRATION_PROGRESS_THRESHOLD_BYTES)


def _migrate_database(paths: AppPaths) -> "MigrationReport":
    """
    第 6 步：探测 → 选择同步迁移或进度窗口 → 迁移，返回迁移报告。

    探测与迁移本身的异常（库被锁、文件损坏）原样抛出，与此前在 build_context 里迁移时
    一致，交给全局异常兜底。进度窗口路径下迁移在工作线程执行，异常也原样带回这里。
    """
    probe = new_migration_runner(paths).probe()
    if not needs_progress_window(probe):
        return new_migration_runner(paths).migrate()

    from ui.windows import migration_window
    backup_path = new_migration_runner(paths).backup_path_for(probe.version)
    logger.info("数据库需要从 v%d 升级（约 %d 行，%d 字节），先显示进度窗口；旧文件将保留为 %s",
                probe.version, probe.estimated_rows, probe.file_bytes, backup_path)
    return migration_window.run_with_progress_window(
        lambda on_progress: new_migration_runner(paths, on_progress=on_progress).migrate(),
        backup_path=backup_path, estimated_rows=probe.estimated_rows)


def _kept_db_path(report: "MigrationReport", paths: AppPaths) -> str:
    """FAILED_KEPT 时旧库所在的位置（报告里没有时退回原路径）"""
    return report.broken_path or report.backup_path or paths.db_path


def show_native_message_box(title: str, text: str) -> None:
    """原生错误框（ctypes MessageBoxW）：不依赖 Qt，打包的窗口程序没有控制台时也能告知用户。
    非 Windows 或调用失败时只记日志"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, _MB_ICONERROR)
    except Exception:
        logger.warning("无法显示原生错误框: %s", text, exc_info=True)


def _create_application(argv: List[str]):
    """创建 QApplication：高 DPI、应用信息、图标，关闭"最后一个窗口关闭即退出" """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import QApplication

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


def _data_dir_failure_path(exc: Exception, argv: List[str]) -> str:
    """数据目录失败时显示给用户的路径：异常带的文件名 → 命令行参数 → 未给出"""
    path = getattr(exc, 'filename', None)
    if path:
        return str(path)
    try:
        path = parse_data_dir_arg(argv)
    except DataDirError:
        path = None
    return path or DATA_DIR_UNKNOWN_PATH


def _report_data_dir_failure(exc: Exception) -> None:
    """
    数据目录不可用时记录启动失败原因。

    此时日志尚未初始化：失败原因写到默认位置的日志目录（AppPaths.default()），
    那里也写不了就只输出到标准错误（打包的窗口程序没有控制台时 stderr 为 None，
    由 run() 另弹原生错误框）。
    不回退到默认数据目录继续运行——那会让用户的数据悄悄写到别处。
    """
    message = f"数据目录不可用，启动中止：{exc}"
    try:
        from utils.logger import setup_logging
        setup_logging(AppPaths.default().log_dir)
        logger.error("数据目录不可用，启动中止：%s", exc)
    except Exception:
        pass
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass


def run(argv: List[str], *, exit_process: Callable[[int], None] = os._exit,
        message_box: Callable[[str, str], None] = show_native_message_box) -> int:
    """
    应用入口：按模块说明的固定顺序启动，返回进程退出码。

    Args:
        argv: 命令行参数（解析 --data-dir；剥掉它之后传给 QApplication）
        exit_process: 事件循环结束后终止进程的函数，默认 os._exit（理由见函数末尾）；
            测试可替换
        message_box: 启动失败时的原生错误框 (标题, 正文)；测试可替换

    Returns:
        事件循环的退出码；已有实例时为 0；数据目录不可用时为 EXIT_DATA_DIR_UNAVAILABLE；
        数据库升级失败且旧库没能回到原路径时为 EXIT_MIGRATION_FAILED
    """
    try:
        paths = AppPaths.resolve(argv=argv)
        paths.ensure_dirs()
    except (DataDirError, OSError) as exc:
        _report_data_dir_failure(exc)
        if getattr(sys, 'frozen', False):
            # 打包的窗口程序没有控制台，stderr 看不到：用原生错误框告诉用户
            message_box(MESSAGE_BOX_TITLE, DATA_DIR_FAILURE_TEXT.format(
                path=_data_dir_failure_path(exc, argv), reason=exc))
        return EXIT_DATA_DIR_UNAVAILABLE

    # 日志与全局异常兜底最先初始化，之后任何初始化异常（含 ui/core/data 导入期）都能落日志
    from utils.crash_handler import install_excepthook
    from utils.logger import setup_logging
    log_dir = setup_logging(paths.log_dir)
    install_excepthook(log_dir)
    logger.info("数据目录: %s（来源: %s）", paths.data_dir,
                _SOURCE_LOG_NAMES.get(paths.source, paths.source))

    app = _create_application(strip_data_dir_args(argv))

    # 单实例：已有实例时通知其激活窗口后直接返回；此时尚未创建任何线程与窗口，
    # 正常结束解释器即可。实例按"用户 + 数据目录"区分，不同数据目录可以并存
    from app.single_instance import SingleInstanceGuard, activate_main_window
    guard = SingleInstanceGuard(data_dir=paths.data_dir)
    if guard.notify_existing_instance():
        logger.info("检测到已运行的实例，已通知其激活窗口，本进程退出")
        return 0
    guard.listen()

    _apply_preferences(app, paths)

    from data.db.migrations.runner import MigrationOutcome
    report = _migrate_database(paths)
    if report.outcome is MigrationOutcome.FAILED_KEPT:
        # 旧库没能回到原路径：继续启动会让连接工厂在原路径建出空库，用户的数据看上去
        # "没了"。只能告知原文件位置后退出（此时还没有任何窗口与后台线程）
        kept = _kept_db_path(report, paths)
        logger.error("数据库升级失败，原文件在 %s，程序退出", kept)
        message_box(MESSAGE_BOX_TITLE, MIGRATION_FAILED_TEXT.format(
            path=kept, name=os.path.basename(paths.db_path)))
        return EXIT_MIGRATION_FAILED

    ctx = build_context(paths, report=report)

    from ui.main_window import MainWindow
    hotkey = _create_marker_hotkey(app, ctx.services)
    window = MainWindow(ctx.db, ctx.manager, ctx.services, hotkey=hotkey)
    ctx.window = window
    window.show()
    guard.activation_requested.connect(lambda: activate_main_window(window))
    hint = sync_dir_hint(paths.data_dir)
    if hint:
        logger.warning("%s: %s", hint, paths.data_dir)
        window.show_sync_dir_warning(hint)

    exit_code = app.exec_()

    # 主窗口收尾时已尽力停止后台线程，但仍可能有线程没能在期限内停下（如下载线程
    # 阻塞在 urlopen() 的 connect() 阶段，取消标志只在读取循环里检查）。走 Python
    # 正常关闭流程时，Qt 析构仍在运行的 QThread 会致命中止（Windows 上表现为
    # 0xC0000409），因此用 os._exit 直接结束进程：不经过 Python/Qt 对象析构，由操作
    # 系统回收线程与句柄。日志按行 flush，不受影响；本应用不依赖 atexit 清理逻辑。
    logger.info("应用退出，退出码=%d", exit_code)
    exit_process(exit_code)
    return exit_code
