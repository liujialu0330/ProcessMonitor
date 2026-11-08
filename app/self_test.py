"""
打包自检：``进程监控助手.exe --self-test --data-dir <空目录>``

发布构建与安装包冒烟用它代替“启动 5 秒后看进程还在不在”。后者只能证明引导程序没崩，
挡不住 onedir 里少一个 Qt 插件、少一个 DLL、少一份数据文件：这些问题在真正用到那一项
功能时才暴露。自检按固定顺序走一遍主要功能链，任何一步失败都立即停止，把失败的步骤与
原因写到标准输出、自检报告文件与应用日志，并以非 0 退出。

步骤（每一步都走与正式启动相同的组合根函数，不另写一套）：
1. 参数与数据目录：必须显式给 --data-dir，且目录里不能已有 monitor.db（自检会往里写
   数据，绝不允许指向真实数据）；
2. 日志、QApplication、应用配置与主题；
3. 放入 golden 旧库（v1.4.1 写出的 v1 库，打包时作为数据文件带上）并迁移到当前版本，
   核对迁移结果、旧库备份与三个任务的采样数；
4. 组装（build_context）、单实例与命令行监听；
5. 建主窗口（透明显示一下再隐藏，让原生窗口、样式表与字体真正跑一遍）；
6. 读进程列表；
7. 监控自身，收到 3 个数据点并打一个点；
8. 导出明细 CSV、摘要 CSV 与报告图片，核对文件内容；
9. 用同目录的 pmctl.exe 对本进程做 ping；
10. 走真退出路径关闭，事件循环正常结束。

golden 库选择“打包时带上的数据文件”而不是“自检时现场生成”：现场生成要把 v1 的建库
代码也打进包，那段代码只服务于造数据；带上的是已发布版本自己写出的真实库（压缩后约
64 KB），迁移走的是真实升级路径，同时顺带验证数据文件确实被收进了包里。

退出码：0 通过；EXIT_USAGE 参数不合规（没有 --data-dir、目录里已有数据库）；
EXIT_FAILED 某一步失败或总超时。进程在启动阶段就崩溃（如缺 Python DLL）时由系统给出
其他非 0 码，同样算失败。
"""
import gzip
import locale
import logging
import os
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, TextIO

import config
from app.paths import AppPaths, DataDirError, parse_data_dir_arg, strip_data_dir_args

logger = logging.getLogger(__name__)

SELF_TEST_ARG = "--self-test"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 5

# 整个自检的时间上限（秒）：任何一步卡死都不能让打包流程无限等下去
TOTAL_TIMEOUT_S = 180.0
# 收到 3 个数据点的等待上限（采集间隔 1 秒）
SAMPLES_REQUIRED = 3
SAMPLES_TIMEOUT_S = 30.0
SELF_INTERVAL_S = 1.0
SELF_METRICS = ("memory_private", "num_threads", "num_handles")
SELF_MARKER_LABEL = "自检打点"
SELF_TASK_NOTE = "打包自检"
EXPORT_TIMEOUT_S = 60.0
PMCTL_TIMEOUT_S = 30.0
QUIT_TIMEOUT_S = 40.0

# 打包时 golden 库放在 _internal\selftest\ 下（见 build\ProcessMonitor.spec 的 datas）
BUNDLED_DIRNAME = "selftest"
GOLDEN_FILENAME = "v1.4.1_v1.db.gz"
GOLDEN_SOURCE_VERSION = 1
# golden 库里三个任务迁移并经启动校正后的采样数（来自 tests/fixtures/legacy_db/manifest.json，
# tests/test_self_test.py 核对两边一致）
GOLDEN_TASK_SAMPLES = {"golden-a": 600, "golden-b": 200, "golden-c": 0}

REPORT_FILENAME = "self_test_report.txt"
PMCTL_FRAMELESS_NAME = "pmctl"

# ---------- 文案 ----------
MSG_NEED_DATA_DIR = "自检必须显式指定数据目录：--self-test --data-dir <空目录>（不会使用默认数据目录）"
MSG_DB_EXISTS = "数据目录里已有数据库 {path}：自检会写入数据，请指向一个空目录"
MSG_START = "进程监控助手 {version} 打包自检（数据目录 {path}）"
MSG_PASS = "自检通过（{count} 步，用时 {seconds:.1f} 秒）"
MSG_FAIL = "自检失败：第 {index} 步“{name}”：{reason}"
MSG_TIMEOUT = "自检超时（超过 {seconds:g} 秒），当前步骤“{name}”"


class SelfTestError(Exception):
    """某一步的核对不通过（原因文字面向看日志的人）"""


# ---------------------------------------------------------------- 命令行

@dataclass(frozen=True)
class SelfTestArgs:
    data_dir: str


def parse_args(argv: Sequence[str]) -> SelfTestArgs:
    """解析 --self-test 的命令行：必须有 --data-dir（数据目录来源只认命令行）。

    Raises:
        SelfTestError: 缺少或不合规的 --data-dir
    """
    try:
        data_dir = parse_data_dir_arg(argv)
    except DataDirError as exc:
        raise SelfTestError(str(exc))
    if not data_dir:
        raise SelfTestError(MSG_NEED_DATA_DIR)
    return SelfTestArgs(data_dir=data_dir)


def golden_db_path() -> str:
    """golden 旧库的压缩文件：打包环境在 _internal\\selftest\\，开发环境在测试夹具目录"""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.join(os.path.dirname(sys.executable), '_internal'))
        return os.path.join(base, BUNDLED_DIRNAME, GOLDEN_FILENAME)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'tests', 'fixtures', 'legacy_db', GOLDEN_FILENAME)


def pmctl_command() -> List[str]:
    """pmctl 的启动命令：打包环境是同目录的 pmctl.exe，开发环境用当前解释器跑 pmctl.py"""
    if getattr(sys, 'frozen', False):
        return [os.path.join(os.path.dirname(sys.executable), PMCTL_FRAMELESS_NAME + ".exe")]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, os.path.join(root, 'pmctl.py')]


# ---------------------------------------------------------------- 报告与步骤执行

def _console_stream() -> Optional[TextIO]:
    """能写字的标准输出；窗口程序（console=False）没有时尝试挂接父进程的控制台。

    返回 None 表示拿不到：结果仍写入报告文件与日志。
    """
    if sys.stdout is not None:
        return sys.stdout
    if os.name != 'nt':
        return None
    try:
        import ctypes
        if not ctypes.windll.kernel32.AttachConsole(-1):    # ATTACH_PARENT_PROCESS
            return None
        encoding = f"cp{ctypes.windll.kernel32.GetConsoleOutputCP()}"
        stream = open('CONOUT$', 'w', encoding=encoding, errors='replace')
        sys.stdout = stream
        return stream
    except Exception:
        return None


class SelfTestReport:
    """逐步记录自检过程：同时写标准输出、应用日志，并累积成文本供写入报告文件"""

    def __init__(self, stream: Optional[TextIO] = None, clock: Callable[[], float] = time.monotonic):
        self._stream = stream
        self._clock = clock
        self._started = clock()
        self.lines: List[str] = []
        self.steps_done = 0
        self.failed_step: Optional[str] = None
        self.failure_reason: Optional[str] = None
        self.current_step: Optional[str] = None

    def emit(self, text: str, *, error: bool = False) -> None:
        self.lines.append(text)
        (logger.error if error else logger.info)("自检: %s", text)
        stream = self._stream
        if stream is None:
            return
        try:
            stream.write(text + "\n")
            stream.flush()
        except UnicodeEncodeError:
            # 控制台是 GBK 等窄编码时写不出个别字符：替换后重写，不能因输出失败判自检失败
            encoding = getattr(stream, 'encoding', None) or 'utf-8'
            try:
                stream.write(text.encode(encoding, 'replace').decode(encoding) + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass
        except (OSError, ValueError):
            pass

    @property
    def ok(self) -> bool:
        return self.failed_step is None

    def run_step(self, name: str, func: Callable[[], Optional[str]]) -> bool:
        """执行一步；func 返回详情文字（可为空），抛任何异常都记为失败。返回是否通过"""
        index = self.steps_done + 1
        self.current_step = name
        started = self._clock()
        try:
            detail = func()
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            if not isinstance(exc, SelfTestError):
                reason = f"{type(exc).__name__}: {reason}"
                logger.error("自检步骤异常: %s", name, exc_info=True)
            self.failed_step, self.failure_reason = name, reason
            self.emit("[失败] " + MSG_FAIL.format(index=index, name=name, reason=reason), error=True)
            return False
        self.steps_done = index
        suffix = f"：{detail}" if detail else ""
        self.emit(f"[通过] {index}. {name}（{self._clock() - started:.1f} 秒）{suffix}")
        return True

    def finish(self) -> int:
        """写总结行，返回退出码"""
        if self.ok:
            self.emit(MSG_PASS.format(count=self.steps_done, seconds=self._clock() - self._started))
            return EXIT_OK
        return EXIT_FAILED

    def write_file(self, path: str) -> None:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("\n".join(self.lines) + "\n")
        except OSError:
            logger.warning("写自检报告文件失败: %s", path, exc_info=True)


def _spin(app, condition: Callable[[], bool], timeout_s: float) -> bool:
    """处理 Qt 事件直到 condition() 为真或超时；返回最后一次判定结果"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(condition())


# ---------------------------------------------------------------- 各步骤

@dataclass
class _State:
    """步骤之间传递的对象"""
    argv: List[str]
    data_dir: str
    paths: Optional[AppPaths] = None
    app: object = None
    guard: object = None
    ctx: object = None
    window: object = None
    migration_report: object = None
    task_id: Optional[str] = None
    export_paths: List[str] = field(default_factory=list)


def _step_paths(state: _State) -> str:
    paths = AppPaths.resolve(argv=state.argv)
    if paths.source != config.DATA_DIR_SOURCE_CLI:
        raise SelfTestError(MSG_NEED_DATA_DIR)
    if os.path.exists(paths.db_path):
        raise SelfTestError(MSG_DB_EXISTS.format(path=paths.db_path))
    paths.ensure_dirs()
    state.paths = paths
    # 数据目录确认安全后才初始化日志：此前失败只写标准输出，不在任何目录里留文件。
    # 无人看得到弹框，未捕获异常只记日志
    from utils.crash_handler import install_excepthook, set_headless
    from utils.logger import setup_logging
    set_headless(True)
    install_excepthook(setup_logging(paths.log_dir))
    logger.info("自检开始: 数据目录 %s", paths.data_dir)
    return paths.data_dir


def _step_application(state: _State) -> str:
    from app import bootstrap
    state.app = bootstrap._create_application(
        bootstrap.strip_startup_trace_arg(strip_data_dir_args(
            [a for a in state.argv if a != SELF_TEST_ARG])))
    bootstrap._apply_preferences(state.app, state.paths)
    return f"Qt {_qt_version()}，平台 {state.app.platformName()}"


def _qt_version() -> str:
    from PyQt5.QtCore import QT_VERSION_STR
    return QT_VERSION_STR


def _read_user_version(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _step_golden_and_migrate(state: _State) -> str:
    from app import bootstrap
    from data.db.migrations.runner import MigrationOutcome
    from data.db.schema import SCHEMA_VERSION

    source = golden_db_path()
    if not os.path.isfile(source):
        raise SelfTestError(f"包里没有 golden 旧库：{source}")
    db_path = state.paths.db_path
    with gzip.open(source, 'rb') as src, open(db_path, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    version = _read_user_version(db_path)
    if version != GOLDEN_SOURCE_VERSION:
        raise SelfTestError(f"golden 库的结构版本是 v{version}，应为 v{GOLDEN_SOURCE_VERSION}")

    report = bootstrap.new_migration_runner(state.paths).migrate()
    if report.outcome is not MigrationOutcome.MIGRATED:
        raise SelfTestError(f"迁移结果是 {report.outcome.value}（{report.reason or '无原因'}），应为已迁移")
    if report.to_version != SCHEMA_VERSION or _read_user_version(db_path) != SCHEMA_VERSION:
        raise SelfTestError(f"迁移后结构版本不是 v{SCHEMA_VERSION}")
    backup = db_path + f".bak_v{GOLDEN_SOURCE_VERSION}"
    if not os.path.isfile(backup):
        raise SelfTestError(f"旧库备份不存在：{backup}")
    state.migration_report = report
    return f"v{GOLDEN_SOURCE_VERSION} → v{SCHEMA_VERSION}，备份 {os.path.basename(backup)}"


def _step_context(state: _State) -> str:
    from app import bootstrap
    ctx = bootstrap.build_context(state.paths, report=state.migration_report,
                                  legacy_importer=None)
    state.ctx = ctx
    for task_id, expected in GOLDEN_TASK_SAMPLES.items():
        task = ctx.db.get_task(task_id)
        if task is None:
            raise SelfTestError(f"迁移后找不到 golden 任务 {task_id}")
        if task.sample_count != expected:
            raise SelfTestError(f"golden 任务 {task_id} 的采样数是 {task.sample_count}，应为 {expected}")
    return f"golden 任务 {len(GOLDEN_TASK_SAMPLES)} 个，采样数一致"


def _step_instance(state: _State) -> str:
    from app.single_instance import SingleInstanceGuard
    guard = SingleInstanceGuard(data_dir=state.paths.data_dir)
    if guard.notify_existing_instance(activate=False):
        raise SelfTestError("这个数据目录已有运行中的实例")
    if not guard.listen():
        raise SelfTestError("命令行监听（命名管道）没能启动")
    state.guard = guard
    return f"管道 {guard.name}"


def _step_window(state: _State) -> str:
    from PyQt5.QtCore import Qt

    from app import bootstrap
    from ui.main_window import MainWindow

    ctx = state.ctx
    window = MainWindow(ctx.db, ctx.manager, ctx.services, hotkey=None,
                        diagnostics_provider=lambda: ctx)
    ctx.window = window
    window.attach_session_lock(ctx.session_lock)
    bootstrap._attach_ipc_to_window(state.guard, ctx, window)
    state.window = window
    # 真的显示一下（全透明、不抢焦点）再隐藏：只构造不显示发现不了原生窗口与绘制层面的缺失
    window.setAttribute(Qt.WA_ShowWithoutActivating, True)
    window.setWindowOpacity(0.0)
    window.show()
    _spin(state.app, lambda: False, 0.5)
    window.hide()
    window.setWindowOpacity(1.0)
    return "已构造并短暂显示后隐藏"


def _step_process_list(state: _State) -> str:
    from core.process_catalog import list_processes
    entries = list_processes()
    if not entries:
        raise SelfTestError("进程列表为空")
    if not any(entry.pid == os.getpid() for entry in entries):
        raise SelfTestError(f"进程列表里没有本进程（PID {os.getpid()}）")
    return f"{len(entries)} 个进程"


def _step_monitor_self(state: _State) -> str:
    from core.process_catalog import name_of

    manager = state.ctx.manager
    received: List[str] = []
    manager.data_updated.connect(lambda task_id, values: received.append(task_id))
    pid = os.getpid()
    name = name_of(pid) or os.path.basename(sys.executable)
    task_id = manager.create_task(pid, name, list(SELF_METRICS), interval=SELF_INTERVAL_S,
                                  note=SELF_TASK_NOTE)
    if not task_id:
        raise SelfTestError("创建监控任务失败")
    state.task_id = task_id
    if not manager.start_task(task_id):
        raise SelfTestError("启动监控任务失败")
    if not _spin(state.app, lambda: len(received) >= SAMPLES_REQUIRED, SAMPLES_TIMEOUT_S):
        raise SelfTestError(f"{SAMPLES_TIMEOUT_S:g} 秒内只收到 {len(received)} 个数据点，"
                            f"应为 {SAMPLES_REQUIRED} 个")
    if manager.add_marker(task_id, SELF_MARKER_LABEL) is None:
        raise SelfTestError("打点失败")
    manager.stop_task(task_id)
    if not _spin(state.app, lambda: not manager.is_task_active(task_id), 15.0):
        raise SelfTestError("监控任务没能在 15 秒内停止")
    task = state.ctx.db.get_task(task_id)
    if task is None or task.sample_count < SAMPLES_REQUIRED:
        raise SelfTestError(f"落库的采样数是 {getattr(task, 'sample_count', None)}，"
                            f"应不少于 {SAMPLES_REQUIRED}")
    return f"{task.sample_count} 个数据点，1 个打点"


def _step_export(state: _State) -> str:
    from datetime import datetime

    from PyQt5.QtGui import QImage

    from app import bootstrap
    from core.ipc.commands import TaskAnalysisWorker
    from core.report import gap_threshold_seconds, metadata_lines, summary_rows
    from core.report_worker import ReportJob, ReportWorker

    ctx = state.ctx
    task = ctx.db.get_task(state.task_id)
    if task is None:
        raise SelfTestError("读不到自检任务")
    out_dir = os.path.join(state.paths.data_dir, "self_test_export")
    os.makedirs(out_dir, exist_ok=True)

    def run_worker(worker, label: str):
        done, failed = [], []
        worker.result.connect(done.append)
        worker.failed.connect(failed.append)
        # 线程由登记表持有并在结束后释放（退出收尾也会等它），这里只等结果信号
        ctx.registry.register(worker, name=label, cancel=worker.cancel, timeout_ms=10_000)
        worker.start()
        if not _spin(state.app, lambda: bool(done or failed), EXPORT_TIMEOUT_S):
            worker.cancel()
            raise SelfTestError(f"{label}超过 {EXPORT_TIMEOUT_S:g} 秒没有结果")
        if failed:
            raise SelfTestError(f"{label}失败：{failed[0]}")
        return done[0]

    analysis = run_worker(
        TaskAnalysisWorker(ctx.db, task.task_id, task.metric_types, analysis=True), "分析")
    image = bootstrap.render_report_image_for_ipc(task, analysis, datetime.now())
    if image is None or image.isNull():
        raise SelfTestError("报告图片渲染结果为空")

    csv_path = os.path.join(out_dir, "self_test_明细.csv")
    summary_path = os.path.join(out_dir, "self_test_摘要.csv")
    image_path = os.path.join(out_dir, "self_test_报告.png")
    metadata = metadata_lines(task, exported_at=datetime.now(), range_text="全部",
                              sample_count=task.sample_count, end_time=task.end_time)
    job = ReportJob(task=task, csv_path=csv_path, summary_path=summary_path,
                    image_path=image_path, image=image, metadata=tuple(metadata),
                    summary_rows=tuple(tuple(r) for r in summary_rows(analysis.snapshots)),
                    gap_seconds=gap_threshold_seconds(task.interval))
    result = run_worker(ReportWorker(ctx.db, job), "导出")

    for path in (csv_path, summary_path, image_path):
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise SelfTestError(f"导出文件缺失或为空：{path}")
    if QImage(image_path).isNull():
        raise SelfTestError("导出的报告图片无法读回")
    with open(csv_path, encoding='utf-8-sig') as f:
        data_rows = [line for line in f.read().splitlines()[1:]
                     if line.strip() and not line.startswith('#')]
    if len(data_rows) < SAMPLES_REQUIRED:
        raise SelfTestError(f"明细 CSV 只有 {len(data_rows)} 行数据，应不少于 {SAMPLES_REQUIRED}")
    state.export_paths = list(getattr(result, 'paths', ()) or ())
    return f"明细 CSV {len(data_rows)} 行，报告图片 {QImage(image_path).width()}×{QImage(image_path).height()}"


def _decode_console(data: bytes) -> str:
    """子进程的控制台输出：先按 UTF-8，不是就按系统代码页（pmctl 在中文系统上写 GBK）"""
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), 'replace')


def _step_pmctl(state: _State) -> str:
    from PyQt5.QtCore import QProcess

    command = pmctl_command()
    if not os.path.isfile(command[0]):
        raise SelfTestError(f"找不到 pmctl：{command[0]}")
    process = QProcess()
    args = command[1:] + ["--data-dir", state.paths.data_dir, "--no-launch", "ping"]
    process.start(command[0], args)
    if not process.waitForStarted(10_000):
        raise SelfTestError(f"pmctl 无法启动：{process.errorString()}")
    # 本进程的命令行监听在界面线程，必须一边处理事件一边等 pmctl 结束
    if not _spin(state.app, lambda: process.state() == QProcess.NotRunning, PMCTL_TIMEOUT_S):
        process.kill()
        raise SelfTestError(f"pmctl ping 超过 {PMCTL_TIMEOUT_S:g} 秒没有返回")
    output = _decode_console(bytes(process.readAllStandardOutput())).strip()
    errors = _decode_console(bytes(process.readAllStandardError())).strip()
    if process.exitCode() != 0:
        raise SelfTestError(f"pmctl ping 退出码 {process.exitCode()}：{errors or output}")
    if f"PID {os.getpid()}" not in output:
        raise SelfTestError(f"pmctl ping 的回复里没有本进程：{output}")
    return output


def _step_quit(state: _State) -> str:
    """走与托盘“退出”相同的真退出路径（ShutdownCoordinator），事件循环应正常结束"""
    # 退出要在事件循环里发起：收尾结束时的 QApplication.quit() 在循环启动之前调用会被
    # Qt 丢弃，之后 exec_() 就永远不返回。循环超时没结束按失败处理
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(0, state.window.request_quit)
    timed_out = []
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: (timed_out.append(True), state.app.quit()))
    timer.start(int(QUIT_TIMEOUT_S * 1000))
    code = state.app.exec_()
    timer.stop()
    if timed_out:
        raise SelfTestError(f"真退出路径超过 {QUIT_TIMEOUT_S:g} 秒没有结束")
    if code != 0:
        raise SelfTestError(f"事件循环退出码 {code}")
    return "事件循环正常结束"


STEPS = (
    ("数据目录与参数", _step_paths),
    ("QApplication 与配置", _step_application),
    ("放入 golden 旧库并迁移到当前版本", _step_golden_and_migrate),
    ("组装应用上下文", _step_context),
    ("单实例与命令行监听", _step_instance),
    ("主窗口", _step_window),
    ("读取进程列表", _step_process_list),
    ("监控自身 3 个点", _step_monitor_self),
    ("导出 CSV 与报告图片", _step_export),
    ("pmctl ping", _step_pmctl),
    ("真退出路径关闭", _step_quit),
)


# ---------------------------------------------------------------- 入口

def run_self_test(argv: List[str], *, exit_process: Callable[[int], None] = os._exit,
                  stream: Optional[TextIO] = None, steps=STEPS,
                  timeout_s: float = TOTAL_TIMEOUT_S) -> int:
    """
    执行自检并返回退出码；结束时用 exit_process 终止进程（与正式退出路径同理：后台线程
    可能仍在收尾，经 Python 正常关闭会让 Qt 析构运行中的 QThread 而崩溃）。

    Args:
        argv: 完整命令行（含 --self-test 与 --data-dir）
        exit_process: 终止进程的函数；测试可替换
        stream: 输出流；None 时用标准输出（窗口程序尝试挂接父进程控制台）
        steps: 步骤表 [(名称, func(state) -> 详情)]；测试可替换
        timeout_s: 总超时（秒）
    """
    report = SelfTestReport(stream if stream is not None else _console_stream())
    try:
        args = parse_args(argv)
    except SelfTestError as exc:
        report.emit(f"[参数错误] {exc}", error=True)
        exit_process(EXIT_USAGE)
        return EXIT_USAGE

    state = _State(argv=list(argv), data_dir=args.data_dir)
    report_path = os.path.join(os.path.abspath(args.data_dir), REPORT_FILENAME)
    done = threading.Event()

    def on_timeout() -> None:
        if done.is_set():
            return
        report.emit("[失败] " + MSG_TIMEOUT.format(seconds=timeout_s, name=report.current_step),
                    error=True)
        report.failed_step = report.failed_step or report.current_step or "?"
        report.write_file(report_path)
        exit_process(EXIT_FAILED)

    watchdog = threading.Timer(timeout_s, on_timeout)
    watchdog.daemon = True
    watchdog.start()

    report.emit(MSG_START.format(version=config.APP_VERSION, path=os.path.abspath(args.data_dir)))
    for name, func in steps:
        if not report.run_step(name, lambda func=func: func(state)):
            break

    code = report.finish()
    done.set()
    watchdog.cancel()
    if not report.ok:
        _cleanup_after_failure(state)
    report.write_file(report_path)
    exit_process(code)
    return code


def _cleanup_after_failure(state: _State) -> None:
    """失败后尽力收尾：停任务、关库，让数据目录里没有残留的会话锁。收尾本身出错只记日志"""
    ctx = state.ctx
    if ctx is None:
        return
    try:
        ctx.manager.stop_all_tasks()
        ctx.manager.join_all(timeout_ms=3000)
    except Exception:
        logger.warning("自检失败后的收尾：停止任务出错", exc_info=True)
    try:
        from app.bootstrap import release_session_lock
        release_session_lock(ctx)
        ctx.db.close()
    except Exception:
        logger.warning("自检失败后的收尾：关闭数据库出错", exc_info=True)
