"""
组合根：应用的启动顺序与依赖组装只在这里出现一次。

run() 的顺序是固定的，调整前先确认每一步的前置条件：
1. 解析路径（命令行 --data-dir → 环境变量 → 便携标志 → 默认）并建目录；数据目录不可用时
   只在默认日志目录记一条错误并返回 EXIT_DATA_DIR_UNAVAILABLE，绝不回退到别的目录（那会让
   数据悄悄写到别处）；打包的窗口程序没有控制台，另弹原生错误框说明路径与原因
→ 2. 日志与全局异常兜底（必须早于 ui/core/data 的导入，这些模块导入期的异常也要能落日志）
→ 3. QApplication → 4. 单实例（已有实例则通知后退出；--background 的从实例不通知），
   随即开始 IPC 监听（激活消息与 pmctl 命令同一条管道；组装完成前除 ping / activate 外
   的命令回 not_ready）
→ 5. 应用配置、主题、排版（页面构造时就会读配置；进度窗口也要跟随主题）
→ 6. 迁移数据库：探测（不写入）→ 待搬迁的行数或库文件超过阈值时先显示进度窗口、迁移在
   工作线程执行，否则在界面线程同步迁移 → 结果为 FAILED_KEPT（旧库没能回到原路径）时弹
   原生错误框说明原文件位置并返回 EXIT_MIGRATION_FAILED，绝不在原路径建空库继续运行
→ 7. 读库的 meta：库由更高版本写入、本版本不能安全修改时只读打开；迁移没有成功（库仍是
   旧结构：BACKUP_ABORTED / FAILED_RESTORED / FAILED_KEPT）时同样只读打开并标记
   schema_mismatch——本版本的读写都按 v5 结构写成，不能在旧结构上写；构造 Database（不再在
   构造时迁移）→ 8. 启动校正（任何任务启动之前；只读库跳过）。其中导回回退 1.4.1 期间写入的
   数据一步：待导回行数超过 MIGRATION_PROGRESS_THRESHOLD_ROWS 时经进度窗口在工作线程执行
→ 9. 线程登记表、界面服务、阻止睡眠守卫、自身监控、存储守卫（磁盘空间 / -wal 兜底 checkpoint /
后台 quick_check，窗口显示后与无窗口模式就绪时启动，退出收尾前停止）→ 10. 主窗口（托盘由主窗口创建并持有；迁移结果与只读状态由主窗口
按 Database 的标志提示；数据目录在同步盘上时交给主窗口显示一次提示；设置页的诊断信息经
diagnostics_provider 取本上下文）→ 10a. 窗口显示后在界面线程安装界面卡死看门狗（主窗口真退出
时先停掉它，收尾期间界面线程会阻塞）、自身监控每 60 秒采一次 → 10b. 把命令处理器的上下文交给
IPC（自动打点服务用监控页 Presenter 持有的那一份，全程只有一个；启动被测程序的 launcher 与
监控页共用 services.launcher）→ 11. 事件循环 → 12. 结束进程

无窗口模式另外：未捕获异常不弹框（utils.crash_handler.set_headless）；设置里关闭了"允许
命令行与脚本控制"时记日志并以 0 退出（pmctl 看到拉起的实例退出码 0 即提示用户去设置里打开）；
会话结束（注销/关机）由 HeadlessRuntime 接管收尾。

--startup-trace：各阶段末尾记一次单调时钟（core.diagnostics.StartupTrace），启动完成（窗口
显示或无窗口模式就绪）后把各阶段耗时写一条 INFO 日志。

无窗口模式（--background，pmctl 在 GUI 不在时拉起）走 _run_headless：第 1~4 步相同，之后
只加载配置（不设主题与排版、不导入 ui.main_window），同步迁移（不显示进度窗口），
build_context，HeadlessRuntime 持有自动打点服务并每秒驱动（阶段快照的报告图片用同一渲染函数
离屏绘制），自身监控每 60 秒采一次，把上下文交给 IPC，进入事件循环；不建任何顶层窗口、托盘、
全局快捷键与界面看门狗。quit 命令、Ctrl+C 或 --idle-exit-s 空闲到时经
ShutdownCoordinator 收尾后退出，退出码 0。

--self-test（打包自检，见 app.self_test）在 run() 的最开头分流，不走上面的顺序：它自己解析
必须显式给出的空数据目录，再按自己的步骤表调用本模块的组装函数。

退出收尾（停任务、等线程、关闭数据库）只在主窗口的真退出路径里经 ShutdownCoordinator
做一次，这里不再重复关闭数据库。

会话锁与续采：build_context 在启动校正之前读数据目录里的 session.lock，判断上次是否异常结束
（core.session_lock）；异常结束且库里仍有 running 任务、被测进程还活着时，这些任务不被校正，
留在 AppContext.recovery_candidates 里，由 run_recovery 在窗口显示后弹"继续监控"对话框
（--resume-tasks 不弹窗、全部续采；无窗口模式没有该参数时不续采、照常收尾并记日志）。锁在
校正之后写入，所有正常退出路径收尾完成后删除（主窗口真退出、无窗口 quit、事件循环结束兜底）。

build_context() 是第 7~9 步（未传入迁移报告时先在这里同步迁移），不涉及 QApplication 与
窗口，测试可直接用临时目录调用。
"""
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple

import config
from app.paths import (
    AppPaths, DataDirError, parse_data_dir_arg, strip_data_dir_args, sync_dir_hint,
)
from app.self_test import SELF_TEST_ARG

if TYPE_CHECKING:
    from core.maintenance_service import MaintenanceService, ReconcileResult
    from core.session_lock import PreviousSession, SessionLock
    from core.session_recovery import RecoveryCandidate
    from core.monitor_manager import MonitorManager
    from core.power_guard import PowerGuard
    from core.self_monitor import SelfMonitor
    from core.shutdown import ThreadRegistry
    from core.storage_guard import StorageGuard
    from data.database import Database
    from data.db.migrations.runner import MigrationReport, MigrationRunner, ProbeResult
    from ui.main_window import MainWindow
    from ui.services import UiServices

logger = logging.getLogger(__name__)

# 数据目录不可用（--data-dir 缺少路径、路径非法、无权限创建）时 run() 的返回码
EXIT_DATA_DIR_UNAVAILABLE = 2
# 数据库升级失败且旧库没能回到原路径（FAILED_KEPT）时 run() 的返回码
EXIT_MIGRATION_FAILED = 3
# 无窗口模式没能开始 IPC 监听时 run() 的返回码：没有管道就收不到任何命令（包括 quit），
# 留着只会成为一个关不掉的后台进程
EXIT_IPC_UNAVAILABLE = 4
# 无窗口模式启动时发现设置里关闭了"允许命令行与脚本控制"：正常退出（0），pmctl 据此提示
EXIT_AUTOMATION_DISABLED = 0

# 无窗口模式的命令行参数
BACKGROUND_ARG = "--background"
IDLE_EXIT_ARG = "--idle-exit-s"
# 上次异常退出时：不弹对话框，全部可续采的任务直接续采（脚本 / 无窗口模式用）
RESUME_ARG = "--resume-tasks"
# 启动阶段计时：各阶段耗时写一条 INFO 日志
STARTUP_TRACE_ARG = "--startup-trace"
# 自身监控的采样间隔（毫秒）
SELF_MONITOR_INTERVAL_MS = 60_000
# 已有实例在无窗口模式运行时，再从开始菜单启动的提示（从实例随即退出）
BACKGROUND_RUNNING_TEXT = (
    "进程监控助手正在后台（无窗口模式，由 pmctl 启动）运行同一个数据目录。\n\n"
    "请先用 “pmctl quit” 结束它，再打开主界面。")

# 迁移前先显示进度窗口的阈值：待搬迁的数据点行数超过它，或库文件（含 -wal）超过字节阈值。
# 启动校正导回降级数据时同样按行数阈值决定是否显示进度窗口
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
    # 监控期间阻止系统空闲睡眠（跟随 cfg.keep_awake_while_monitoring）
    power_guard: Optional["PowerGuard"] = None
    # 本程序自身的资源占用（每 60 秒一次，24 小时环形缓冲；诊断信息用）与驱动它的定时器
    self_monitor: Optional["SelfMonitor"] = None
    self_monitor_timer: Any = None
    # 界面卡死看门狗（只在有窗口模式、窗口显示后安装；诊断信息用）
    watchdog: Any = None
    # 存储守卫：磁盘空间、-wal 兜底 checkpoint、后台 quick_check（build_context 创建，
    # start_storage_guard 启动；有窗口与无窗口模式都用）
    storage_guard: Optional["StorageGuard"] = None
    # 会话锁（数据目录下的 session.lock）：build_context 写入，正常退出路径收尾后删除
    session_lock: Optional["SessionLock"] = None
    # 启动时对上次运行的判断（None 表示没做判断：只读库等）
    previous_session: Optional["PreviousSession"] = None
    # 上次异常退出时仍可续采的任务（被测进程还活着）；run_recovery 处理后清空
    recovery_candidates: Tuple["RecoveryCandidate", ...] = ()

    @property
    def hub(self):
        """全系统快照中心（诊断信息用）"""
        return getattr(self.manager, 'snapshot_hub', None)


def build_context(paths: AppPaths, *, registry=None,
                  retention_days: Optional[int] = None,
                  report: Optional["MigrationReport"] = None,
                  legacy_importer=None) -> AppContext:
    """
    组装 Database / MonitorManager / 维护服务 / 界面服务 / 阻止睡眠守卫（不建 QApplication 与窗口）。

    Args:
        paths: 应用路径（调用方负责 ensure_dirs）
        registry: 线程登记表；None 时新建一个
        retention_days: 启动清理用的保留天数；None 时取用户配置
        report: run() 已跑出的迁移报告（可能经过进度窗口），传入时不再迁移第二次；
            None 时在这里同步迁移
        legacy_importer: 启动校正里导回降级数据的执行方式（见
            MaintenanceService.startup_reconcile）；run() 传 import_legacy_with_progress，
            None 时同步导回
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

    schema_mismatch = schema_not_upgraded(report)
    if schema_mismatch:
        # 旧结构上不能跑 v5 的写入与校正：只读打开，主窗口横幅说明原因
        logger.warning("数据库尚未升级（%s：%s），本次以只读方式打开: %s",
                       report.outcome.value, report.reason or "原因见上文", paths.db_path)
        read_only, newer_version = True, None
    else:
        read_only, newer_version = _read_only_state(paths.db_path)
    db = Database(paths.db_path, migrate=False, migration_report=report,
                  read_only=read_only, newer_version=newer_version,
                  schema_mismatch=schema_mismatch)

    manager = MonitorManager(db=db)
    maintenance = MaintenanceService(db, manager)
    if retention_days is None:
        from app_config import cfg
        retention_days = cfg.get(cfg.retention_days)

    # 会话锁：残留且记录的进程已不在 = 上次异常退出，此时被测进程还活着的 running 任务先
    # 留着、等用户决定是否续采；只读库不写锁也不续采
    from core.session_lock import PreviousSession, SessionLock
    from core.session_recovery import find_recoverable
    session_lock = SessionLock(paths.data_dir)
    previous: Optional[PreviousSession] = None
    candidates: Tuple["RecoveryCandidate", ...] = ()
    if not read_only:
        previous = session_lock.check_previous()
        if previous is PreviousSession.ABNORMAL:
            candidates = tuple(find_recoverable(db))
    # 必须在任何监控任务启动之前；此时 manager 刚构造，还没有任务
    if previous in (PreviousSession.OTHER_INSTANCE, PreviousSession.UNKNOWN):
        # 另一个实例仍在运行（或探测权限不足、无法排除它在运行，如上一个实例以管理员身份运行）：
        # 库里"运行中"的任务可能是它正在跑的，校正会把它们收尾成已停止（还会清理它在写的数据），
        # 整个启动校正跳过
        if previous is PreviousSession.OTHER_INSTANCE:
            logger.warning("数据目录的会话锁属于仍在运行的另一个实例，跳过启动校正")
        else:
            logger.warning("数据目录的会话锁属于一个无法确认是否仍在运行的进程（探测权限不足），"
                           "跳过启动校正")
        from core.maintenance_service import ReconcileResult
        reconcile = ReconcileResult(orphans=0, recounted=0, cleaned=0)
    else:
        reconcile = maintenance.startup_reconcile(
            retention_days, legacy_importer=legacy_importer,
            protect_task_ids=[c.task_id for c in candidates])
    if not read_only:
        session_lock.acquire()

    if registry is None:
        registry = ThreadRegistry()
    from ui.services import production_services
    services = production_services(manager, registry)
    from app_config import cfg
    from core.power_guard import PowerGuard
    power_guard = PowerGuard.from_config_item(manager, cfg.keep_awake_while_monitoring)
    from core.self_monitor import SelfMonitor
    from core.storage_guard import StorageGuard
    storage_guard = StorageGuard(manager, maintenance, registry, paths.db_path,
                                 read_only=bool(getattr(db, 'read_only', False)))
    return AppContext(paths=paths, db=db, manager=manager, services=services,
                      registry=registry, maintenance=maintenance, reconcile=reconcile,
                      power_guard=power_guard, self_monitor=SelfMonitor(),
                      storage_guard=storage_guard, session_lock=session_lock,
                      previous_session=previous, recovery_candidates=candidates)


def schema_not_upgraded(report: "MigrationReport") -> bool:
    """
    迁移报告是否意味着原路径的库仍是旧结构：备份/预检中止、迁移失败已复原、或旧库原样保留。
    UP_TO_DATE / MIGRATED 是当前结构；RESET 是新建的空库，可正常使用。
    """
    from data.db.migrations.runner import MigrationOutcome
    return report.outcome in (MigrationOutcome.BACKUP_ABORTED, MigrationOutcome.FAILED_RESTORED,
                              MigrationOutcome.FAILED_KEPT)


def import_legacy_with_progress(pending: int, do_import) -> int:
    """
    启动校正的导回一步（MaintenanceService.startup_reconcile 的 legacy_importer）：
    待导回行数超过阈值时显示进度窗口、在工作线程导回，否则在界面线程同步导回。
    必须在主窗口创建之前调用（与迁移进度窗口相同）。
    """
    if pending <= MIGRATION_PROGRESS_THRESHOLD_ROWS:
        return do_import(None)
    from ui.windows import migration_window
    logger.info("回退旧版本期间写入的数据约 %d 行，先显示进度窗口再导回", pending)
    return migration_window.run_with_progress_window(
        do_import, backup_path=None, estimated_rows=pending,
        title=migration_window.LEGACY_IMPORT_WINDOW_TITLE,
        hint=migration_window.LEGACY_IMPORT_HINT_TEXT)


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


def _parse_idle_exit(argv: List[str]) -> float:
    """--idle-exit-s N / --idle-exit-s=N（秒，缺省或非法为 0 = 不因空闲退出）"""
    value = None
    args = list(argv[1:])
    for i, arg in enumerate(args):
        if arg == IDLE_EXIT_ARG and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(IDLE_EXIT_ARG + "="):
            value = arg[len(IDLE_EXIT_ARG) + 1:]
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except ValueError:
        logger.warning("忽略非法的 %s 取值: %r", IDLE_EXIT_ARG, value)
        return 0.0


def strip_background_args(argv: List[str]) -> List[str]:
    """去掉 --background、--resume-tasks 与 --idle-exit-s（及其值）后的命令行参数（交给 QApplication）"""
    result: List[str] = []
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if i > 0 and arg in (BACKGROUND_ARG, RESUME_ARG):
            continue
        if i > 0 and arg == IDLE_EXIT_ARG:
            skip_next = True
            continue
        if i > 0 and arg.startswith(IDLE_EXIT_ARG + "="):
            continue
        result.append(arg)
    return result


def command_settings():
    """IPC 命令处理器读取的用户设置（自动化开关、默认指标与采集间隔），都在用时现读"""
    from app_config import cfg, interval_range
    from core.ipc.commands import CommandSettings
    return CommandSettings(
        automation_enabled=lambda: bool(cfg.get(cfg.automation_enabled)),
        default_metrics=lambda: list(cfg.get(cfg.last_metrics)),
        default_interval=lambda: float(cfg.get(cfg.default_interval)),
        interval_range=lambda: tuple(float(v) for v in interval_range()),
        task_limit=config.MAX_MONITOR_TASKS)


def render_report_image_for_ipc(task, analysis, generated_at):
    """命令行导出的报告图片：与分析页"导出报告"同一条离屏渲染管线（范围为"全部"，
    时间轴为绝对时间）。在界面线程调用；只用 QImage/QPainter 与不显示的控件，
    无窗口模式下也不出现可见窗口。"""
    from ui.viewmodels.analysis_vm import range_text
    from ui.widgets.analysis.report_sheet import render_report_for
    return render_report_for(task, analysis, generated_at, range_text(analysis.range_key))


def render_stage_snapshot_image(task, analysis, generated_at, range_desc):
    """阶段快照的报告图片（StageSnapshotWriter 的 renderer）：同一条离屏渲染管线，范围文案
    由写入器给出（"全部（运行中第 N 次阶段快照）"）。在界面线程调用，无窗口模式也可用。"""
    from ui.widgets.analysis.report_sheet import render_report_for
    return render_report_for(task, analysis, generated_at, range_desc)


def start_self_monitor(ctx: AppContext, interval_ms: int = SELF_MONITOR_INTERVAL_MS):
    """自身监控：马上采一次（诊断信息里立刻有值），之后每 interval_ms 采一次。定时器在
    界面线程，挂在 ctx 上（持有引用）；没有自身监控对象时什么都不做。"""
    from PyQt5.QtCore import QTimer
    monitor = ctx.self_monitor
    if monitor is None:
        return None
    timer = QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(monitor.tick)
    timer.start()
    QTimer.singleShot(0, monitor.tick)
    ctx.self_monitor_timer = timer
    return timer


def run_recovery(ctx: AppContext, *, auto_marker=None, prompt=None,
                 resume_all: bool = False):
    """处理上次异常退出遗留的可续采任务（只处理一次）。

    Args:
        ctx: 组合根上下文（recovery_candidates 为空时什么都不做）
        auto_marker: 自动打点服务：续采前按库里的方案重新挂接
        prompt: 询问用户的函数 candidates -> 选中的任务 ID 列表（有窗口模式传对话框）；
            None 表示没有界面可问（无窗口模式）
        resume_all: 不询问，全部续采（--resume-tasks）

    Returns:
        RecoveryOutcome；没有候选任务返回 None
    """
    from core.session_recovery import apply_decision, close_all
    candidates = ctx.recovery_candidates
    if not candidates:
        return None
    ctx.recovery_candidates = ()
    if resume_all:
        selected = [c.task_id for c in candidates]
    elif prompt is not None:
        try:
            selected = list(prompt(candidates))
        except Exception:
            logger.error("续采确认出错，按放弃处理", exc_info=True)
            selected = []
    else:
        # 没有界面可问：不擅自续采，照常收尾；想续采的脚本用 --resume-tasks
        outcome = close_all(ctx.db, candidates)
        logger.info("上次异常退出，有 %d 个任务的被测进程仍在运行，但当前无法询问，已按结束处理"
                    "（需要续采请使用 %s）: %s", len(candidates), RESUME_ARG,
                    ", ".join(t[:8] for t in outcome.abandoned))
        return outcome
    outcome = apply_decision(ctx.manager, ctx.db, candidates, selected, auto_marker=auto_marker)
    logger.info("续采结果: 续采 %d 个，放弃 %d 个，失败 %d 个", len(outcome.resumed),
                len(outcome.abandoned), len(outcome.failed))
    return outcome


def release_session_lock(ctx: AppContext) -> None:
    """删除会话锁（正常退出路径的收尾；可重复调用，出错只记日志）。"""
    lock = ctx.session_lock
    if lock is None:
        return
    try:
        lock.release()
    except Exception:
        logger.warning("删除会话锁失败", exc_info=True)


def start_storage_guard(ctx: AppContext, window=None) -> None:
    """启动存储守卫的定时检查。有窗口模式先让主窗口接上守卫的信号（提示与卡片标注），再启动，
    首次检查才不会错过任何提示；无窗口模式不连界面，守卫只记日志。没有守卫时什么都不做。"""
    guard = ctx.storage_guard
    if guard is None:
        return
    attach = getattr(window, 'attach_storage_guard', None)
    if attach is not None:
        attach(guard)
    guard.start()


def install_watchdog(app, ctx: AppContext, window) -> None:
    """界面卡死看门狗：窗口显示后在界面线程安装，交给主窗口（真退出时先停掉它）。
    没有 crash.log 等原因装不上时只记日志。"""
    from utils.crash_handler import install_ui_watchdog
    watchdog = install_ui_watchdog(app)
    ctx.watchdog = watchdog
    if watchdog is not None:
        window.attach_watchdog(watchdog)


def _parse_startup_trace(argv: List[str]) -> bool:
    return STARTUP_TRACE_ARG in argv[1:]


def _new_trace(origin: float):
    from core.diagnostics import StartupTrace
    return StartupTrace(origin=origin)


def _report_trace(trace) -> None:
    if trace is not None:
        logger.info("%s", trace.report().rstrip())


def _schedule_recovery(ctx: AppContext, window, *, resume_all: bool = False) -> None:
    """窗口显示后（事件循环里）处理可续采的任务：先让主窗口画出来再弹对话框。
    没有候选任务时什么都不做。"""
    if not ctx.recovery_candidates:
        return
    from PyQt5.QtCore import QTimer
    from ui.dialogs.recover_dialog import default_prompt

    def _go() -> None:
        try:
            outcome = run_recovery(
                ctx, auto_marker=window.monitor_page.presenter.auto_marker,
                prompt=default_prompt(window), resume_all=resume_all)
            if outcome is not None and outcome.resumed:
                # 续采之后用户第一眼应看到正在跑的任务，不管上次停在哪一页
                window.show_monitor_page()
        except Exception:
            logger.error("处理续采任务失败", exc_info=True)

    QTimer.singleShot(0, _go)


def _attach_ipc_to_window(guard, ctx: AppContext, window) -> None:
    """有窗口模式：把命令处理器的上下文交给 IPC。自动打点服务用监控页 Presenter 持有的
    那一份（全程只有一个），quit 等价于托盘"退出"。守卫没有处理器（监听失败、测试替身）
    时跳过。"""
    dispatcher = getattr(guard, 'dispatcher', None)
    if dispatcher is None:
        return
    from core.ipc.commands import CommandContext
    presenter = window.monitor_page.presenter
    dispatcher.set_settings(command_settings())
    dispatcher.set_context(CommandContext(
        manager=ctx.manager, db=ctx.db, auto_marker=presenter.auto_marker,
        registry=ctx.registry, clock=ctx.services.clock, quit=window.request_quit,
        report_renderer=render_report_image_for_ipc,
        launcher=getattr(ctx.services, "launcher", None),
        log_path=getattr(ctx.services, "log_path", None)))
    logger.info("命令行控制已就绪（有窗口模式）")


def _run_headless(app, guard, paths: AppPaths, idle_exit_s: float, *,
                  exit_process: Callable[[int], None], trace=None,
                  argv: Optional[List[str]] = None) -> int:
    """
    无窗口模式（--background）：加载配置 → 同步迁移 → build_context → HeadlessRuntime
    → 把上下文交给 IPC → 事件循环。不导入 ui.main_window，不建窗口、托盘与全局快捷键；
    迁移不显示进度窗口（没有窗口可依附，pmctl 会一直等到就绪或超时）。
    """
    argv = argv if argv is not None else []
    from app_config import load_app_config
    from core.headless import HeadlessRuntime
    from core.ipc.commands import CommandContext
    from data.db.migrations.runner import MigrationOutcome

    load_app_config(paths)
    _mark(trace, "配置")
    if not _automation_enabled():
        # 无窗口实例只能经命令行控制：开关关着时留下来既收不到命令、也没有界面可用
        logger.warning("设置里已关闭“允许命令行与脚本控制”，无窗口模式不启动，直接退出")
        return EXIT_AUTOMATION_DISABLED
    report = new_migration_runner(paths).migrate()
    if report.outcome is MigrationOutcome.FAILED_KEPT:
        logger.error("数据库升级失败，原文件在 %s，无窗口模式退出", _kept_db_path(report, paths))
        return EXIT_MIGRATION_FAILED
    _mark(trace, "迁移")
    ctx = build_context(paths, report=report)
    _mark(trace, "组装")
    dispatcher = getattr(guard, 'dispatcher', None)
    runtime = HeadlessRuntime(
        ctx.manager, ctx.registry, ctx.db, idle_exit_s=idle_exit_s,
        busy=(dispatcher.has_pending if dispatcher is not None else None),
        stage_renderer=render_stage_snapshot_image,
        quit_hooks=([ctx.storage_guard.stop] if ctx.storage_guard is not None else []),
        exit_hooks=[lambda: release_session_lock(ctx)])
    if dispatcher is not None:
        dispatcher.set_settings(command_settings())
        dispatcher.set_context(CommandContext(
            manager=ctx.manager, db=ctx.db, auto_marker=runtime.auto_marker,
            registry=ctx.registry, clock=ctx.services.clock, quit=runtime.quit,
            report_renderer=render_report_image_for_ipc, handle_auto_export=True,
            launcher=getattr(ctx.services, "launcher", None),
            log_path=getattr(ctx.services, "log_path", None)))
    runtime.start()
    run_recovery(ctx, auto_marker=runtime.auto_marker, prompt=None,
                 resume_all=RESUME_ARG in argv[1:])
    start_self_monitor(ctx)
    start_storage_guard(ctx)
    logger.info("无窗口模式已就绪（空闲退出：%s）",
                f"{idle_exit_s:g} 秒" if idle_exit_s > 0 else "不退出")
    _mark(trace, "就绪")
    _report_trace(trace)

    exit_code = app.exec_()
    logger.info("无窗口模式退出，退出码=%d", exit_code)
    release_session_lock(ctx)
    exit_process(exit_code)
    return exit_code


def _automation_enabled() -> bool:
    """设置里的"允许命令行与脚本控制"（读失败按开启处理：不能因读配置出错让 pmctl 无从诊断）"""
    try:
        return bool(command_settings().automation_enabled())
    except Exception:
        logger.error("读取命令行控制开关失败，按开启处理", exc_info=True)
        return True


def _mark(trace, stage: str) -> None:
    if trace is not None:
        trace.mark(stage)


def strip_startup_trace_arg(argv: List[str]) -> List[str]:
    """去掉 --startup-trace 后的命令行参数（交给 QApplication）"""
    return [arg for i, arg in enumerate(argv) if i == 0 or arg != STARTUP_TRACE_ARG]


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
        数据库升级失败且旧库没能回到原路径时为 EXIT_MIGRATION_FAILED；无窗口模式没能开始
        IPC 监听时为 EXIT_IPC_UNAVAILABLE
    """
    if SELF_TEST_ARG in argv[1:]:
        # 打包自检自己解析数据目录（必须显式给出且是空目录），不能先走下面会建默认目录的路径解析
        from app.self_test import run_self_test
        return run_self_test(argv, exit_process=exit_process)

    # 启动计时的起点尽量早；StartupTrace 要等日志初始化之后才导入（见下）
    started_at = time.monotonic()
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
    trace = _new_trace(started_at) if _parse_startup_trace(argv) else None
    _mark(trace, "路径与日志")

    background = BACKGROUND_ARG in argv[1:]
    idle_exit_s = _parse_idle_exit(argv)
    if background:
        # 无窗口模式没有人看得到弹框，模态框还会卡住界面线程：未捕获异常只记日志
        from utils.crash_handler import set_headless
        set_headless(True)
    app = _create_application(
        strip_startup_trace_arg(strip_background_args(strip_data_dir_args(argv))))
    _mark(trace, "QApplication")

    # 单实例：已有实例时通知其激活窗口后直接返回；此时尚未创建任何线程与窗口，
    # 正常结束解释器即可。实例按"用户 + 数据目录"区分，不同数据目录可以并存。
    # 无窗口模式的从实例不发激活消息（不能把用户的窗口拉到前台）
    from app.single_instance import SingleInstanceGuard, activate_main_window
    if background:
        guard = SingleInstanceGuard(data_dir=paths.data_dir, background=True)
        if guard.notify_existing_instance(activate=False):
            logger.info("检测到已运行的实例，无窗口模式的本进程直接退出")
            return 0
    else:
        guard = SingleInstanceGuard(data_dir=paths.data_dir)
        if guard.notify_existing_instance():
            logger.info("检测到已运行的实例，已通知其激活窗口，本进程退出")
            if getattr(guard, 'activation_error', None) == 'invalid_state':
                # 已运行的是无窗口模式：没有窗口可激活，不提示的话用户会以为"打不开"
                message_box(MESSAGE_BOX_TITLE, BACKGROUND_RUNNING_TEXT)
            return 0
    # 尽早开始 IPC 监听：pmctl 拉起后台实例后马上就会连，组装完成前回 not_ready
    listening = guard.listen()
    _mark(trace, "单实例与命令行监听")

    if background:
        if not listening:
            logger.error("无窗口模式无法开始 IPC 监听，退出（否则没有办法控制或结束它）")
            return EXIT_IPC_UNAVAILABLE
        return _run_headless(app, guard, paths, idle_exit_s, exit_process=exit_process,
                             trace=trace, argv=argv)

    _apply_preferences(app, paths)
    _mark(trace, "配置与主题")

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
    _mark(trace, "迁移")

    ctx = build_context(paths, report=report, legacy_importer=import_legacy_with_progress)
    _mark(trace, "组装")

    from ui.main_window import MainWindow
    hotkey = _create_marker_hotkey(app, ctx.services)
    window = MainWindow(ctx.db, ctx.manager, ctx.services, hotkey=hotkey,
                        diagnostics_provider=lambda: ctx)
    ctx.window = window
    window.attach_session_lock(ctx.session_lock)
    _mark(trace, "主窗口")
    window.show()
    _mark(trace, "显示")
    install_watchdog(app, ctx, window)
    start_self_monitor(ctx)
    start_storage_guard(ctx, window)
    guard.activation_requested.connect(lambda: activate_main_window(window))
    _attach_ipc_to_window(guard, ctx, window)
    _schedule_recovery(ctx, window, resume_all=RESUME_ARG in argv[1:])
    hint = sync_dir_hint(paths.data_dir)
    if hint:
        logger.warning("%s: %s", hint, paths.data_dir)
        window.show_sync_dir_warning(hint)
    _mark(trace, "就绪")
    _report_trace(trace)

    exit_code = app.exec_()

    # 真退出路径里主窗口已删过会话锁；事件循环因其他原因正常结束时在这里兜底
    release_session_lock(ctx)

    # 主窗口收尾时已尽力停止后台线程，但仍可能有线程没能在期限内停下（如下载线程
    # 阻塞在 urlopen() 的 connect() 阶段，取消标志只在读取循环里检查）。走 Python
    # 正常关闭流程时，Qt 析构仍在运行的 QThread 会致命中止（Windows 上表现为
    # 0xC0000409），因此用 os._exit 直接结束进程：不经过 Python/Qt 对象析构，由操作
    # 系统回收线程与句柄。日志按行 flush，不受影响；本应用不依赖 atexit 清理逻辑。
    logger.info("应用退出，退出码=%d", exit_code)
    exit_process(exit_code)
    return exit_code
