"""
全局异常兜底
安装 sys.excepthook 并启用 faulthandler，避免未捕获异常/致命信号导致进程静默退出、
用户看不到任何提示且日志里也无迹可寻。

crash.log 的噪音：Windows 上 faulthandler 注册的是向量化异常处理器，会在首次机会
（first-chance）阶段看到所有 SEH 异常。读屏、UI 自动化等辅助功能工具通过 UIA/COM 访问
本进程窗口时，常触发 0x8001010d（RPC_E_CANTCALLOUT_ININPUTSYNCCALL）、
0x80040155（REGDB_E_IIDNOTREG）这类带"不可继续"标志的 COM 异常；它们随后被系统或
COM 自身处理掉，进程并未崩溃，但 faulthandler 仍会把它们记成
"Windows fatal exception" 连同全部线程堆栈写进 crash.log（真机一轮操作就能写上百条），
且 faulthandler 没有按异常码过滤的接口。

因此：
- 判断是否真的崩溃，看进程是否已退出、app.log 里是否有"未捕获异常"；crash.log 里
  0x8001010d / 0x80040155 的条目可以忽略；
- 为免 crash.log 无限增长，每次安装时若它超过 CRASH_LOG_MAX_BYTES 就先轮转为
  crash.log.1（覆盖旧的 .1）再重新打开，最多保留两份。

界面卡死看门狗（install_ui_watchdog）：界面线程用 QTimer 周期性续期
faulthandler.dump_traceback_later；界面线程被阻塞超过阈值时续期停止，faulthandler 的
后台线程把全部线程的栈写进 crash.log（"Timeout (0:00:10)!" 开头），进程不退出。
faulthandler 的定时转储全进程只有一份，因此看门狗也只能有一个；续期与停止都在界面线程。
"""
import faulthandler
import logging
import os
import sys
import time
from typing import IO, Optional

logger = logging.getLogger(__name__)

CRASH_LOG_NAME = "crash.log"
# crash.log 超过该大小时在下次启动安装钩子时轮转为 crash.log.1
CRASH_LOG_MAX_BYTES = 1024 * 1024

# crash.log 文件对象需模块级全局持有，防止被 GC 回收后自动关闭（faulthandler 要求句柄常驻）
_crash_log_file = None

# 防止 excepthook 内部处理逻辑自身再抛异常导致递归调用
_in_excepthook = False

# 安装时传入的日志目录：弹窗里提示的日志位置必须与实际写入的位置一致，
# 不能在出错时重新解析（入口可能显式传入了别的目录）
_log_dir = None

# 无窗口模式（--background）：未捕获异常只记日志，不弹 QMessageBox——没有人在看，
# 模态框还会阻塞界面线程，让 pmctl 的请求全部超时
_headless = False


def set_headless(value: bool) -> None:
    """设置是否为无窗口模式（组合根在 --background 启动时设为 True）。"""
    global _headless
    _headless = bool(value)


def is_headless() -> bool:
    return _headless


def install_excepthook(log_dir: str):
    """
    安装全局异常钩子，并启用 faulthandler 捕获段错误等致命信号写入 crash.log

    Args:
        log_dir: 日志目录（与 utils.logger.setup_logging 返回值一致）
    """
    global _crash_log_file, _log_dir

    _log_dir = log_dir
    crash_log_path = os.path.join(log_dir, CRASH_LOG_NAME)
    _rotate_crash_log(crash_log_path)
    try:
        _crash_log_file = open(crash_log_path, 'a', encoding='utf-8')
        faulthandler.enable(_crash_log_file)
    except Exception:
        logger.error("faulthandler 初始化失败", exc_info=True)

    sys.excepthook = _global_excepthook


def _rotate_crash_log(crash_log_path: str) -> None:
    """crash.log 超过 CRASH_LOG_MAX_BYTES 时改名为 crash.log.1（覆盖旧的 .1）。

    必须在 faulthandler 打开文件之前做：之后句柄常驻，Windows 上无法再改名。
    轮转失败（如另一个实例正占用文件）只记日志，继续以追加方式打开原文件。
    """
    try:
        if os.path.getsize(crash_log_path) <= CRASH_LOG_MAX_BYTES:
            return
    except OSError:
        # 文件不存在（首次运行）或无法读取大小：无需轮转
        return
    try:
        os.replace(crash_log_path, crash_log_path + ".1")
    except OSError:
        logger.warning("crash.log 轮转失败，继续追加写入: %s", crash_log_path, exc_info=True)


def _global_excepthook(exc_type, exc_value, exc_tb):
    """
    未捕获异常统一处理：
    - 始终记日志（含堆栈）
    - 仅当异常发生在 GUI 主线程且 QApplication 已创建时才弹窗提示；
      工作线程（如 MonitorTask）里的未捕获异常只记日志，不弹窗打断采集
    """
    global _in_excepthook
    if _in_excepthook:
        # hook 内部再次抛异常，避免递归死循环，直接走默认处理
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    _in_excepthook = True
    try:
        try:
            logger.error("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            # 连日志都记录失败（如磁盘已满/句柄异常）：退回解释器默认处理，
            # 不再让新异常从这里逃逸（那样会掩盖原始异常，且可能导致更深层问题）
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        _maybe_show_message_box()
    finally:
        _in_excepthook = False


def _maybe_show_message_box():
    """QApplication 未创建时跳过 UI；仅 GUI 主线程弹窗，工作线程静默；无窗口模式不弹窗"""
    if _headless:
        return
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox
        from PyQt5.QtCore import QThread

        app = QApplication.instance()
        if app is None:
            return
        if QThread.currentThread() is not app.thread():
            return

        log_dir = _log_dir
        if log_dir is None:
            from utils.logger import get_log_dir
            log_dir = get_log_dir()
        log_path = os.path.join(log_dir, "app.log")
        QMessageBox.critical(
            None,
            "程序发生错误",
            f"程序遇到未预期的错误，详细信息已记录到日志文件：\n{log_path}",
        )
    except Exception:
        logger.error("弹出异常提示框失败", exc_info=True)


# ---------------------------------------------------------------- 界面卡死看门狗

# 续期间隔上限（毫秒）；阈值很短时按阈值的三分之一续期，保证正常运行时不会误触发
WATCHDOG_BEAT_MS = 1000
WATCHDOG_DEFAULT_TIMEOUT_S = 10.0

# 当前生效的看门狗（faulthandler 的定时转储全进程只有一份）
_active_watchdog: Optional["UiWatchdog"] = None


class UiWatchdog:
    """
    界面线程卡死看门狗，由 install_ui_watchdog 创建。

    界面线程每 beat_ms 续期一次 faulthandler.dump_traceback_later(timeout_s,
    repeat=False, exit=False)：先 cancel 再重设。界面线程阻塞超过 timeout_s 时转储一次
    全部线程栈（repeat=False，同一次卡死只写一次），恢复后的下一次续期重新布防。

    Args:
        timeout_s: 卡死阈值（秒）
        file: 转储写入的文件对象（须有 fileno，且在 stop() 之前保持打开）
        clock: 提供 monotonic() 的时钟，只用于记录最近一次续期时刻（诊断信息用），
            None 用 time
        beat_ms: 续期间隔（毫秒），None 按阈值推算（不超过 1 秒、不超过阈值的三分之一）；
            给定值同样不得低于 50 ms、不得超过阈值的三分之一
    """

    def __init__(self, timeout_s: float, file: IO, clock=None, beat_ms: Optional[int] = None):
        from PyQt5.QtCore import QTimer

        self.timeout_s = float(timeout_s)
        limit = int(self.timeout_s * 1000 / 3)
        wanted = WATCHDOG_BEAT_MS if beat_ms is None else int(beat_ms)
        self.beat_ms = max(50, min(wanted, limit))
        self._file = file
        self._clock = clock if clock is not None else time
        self.last_beat: Optional[float] = None
        self.beats = 0
        self._timer = QTimer()
        self._timer.setInterval(self.beat_ms)
        self._timer.timeout.connect(self.beat)
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True
        self.beat()
        self._timer.start()

    def beat(self) -> None:
        """续期一次（界面线程的 QTimer 调用；测试也可直接调）"""
        if not self._running:
            return
        try:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(self.timeout_s, repeat=False,
                                              file=self._file, exit=False)
        except Exception:
            # 文件已关闭等：停用看门狗，不能让续期异常每秒刷一次
            logger.error("界面看门狗续期失败，已停用", exc_info=True)
            self.stop()
            return
        self.beats += 1
        self.last_beat = self._clock.monotonic()

    def seconds_since_beat(self) -> Optional[float]:
        """距最近一次续期的秒数（诊断信息用）；尚未续期为 None"""
        if self.last_beat is None:
            return None
        return self._clock.monotonic() - self.last_beat

    def stop(self) -> None:
        """停止续期并取消已布防的转储；可重复调用"""
        global _active_watchdog
        self._running = False
        self._timer.stop()
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            logger.warning("取消界面看门狗失败", exc_info=True)
        if _active_watchdog is self:
            _active_watchdog = None


def install_ui_watchdog(app=None, timeout_s: float = WATCHDOG_DEFAULT_TIMEOUT_S,
                        clock=None, *, file: Optional[IO] = None,
                        beat_ms: Optional[int] = None) -> Optional[UiWatchdog]:
    """
    安装界面卡死看门狗（须在界面线程、QApplication 创建之后调用）。

    Args:
        app: QApplication（只用于确认在界面线程调用；None 取当前实例）
        timeout_s: 界面线程无响应多少秒后转储全部线程栈
        clock: 见 UiWatchdog
        file: 转储写入的文件，None 用 install_excepthook 打开的 crash.log
        beat_ms: 续期间隔，见 UiWatchdog

    Returns:
        UiWatchdog；没有可写的 crash.log、或不在界面线程时为 None（只记日志）。
        再次安装会先停掉旧的看门狗。
    """
    global _active_watchdog
    from PyQt5.QtCore import QThread
    from PyQt5.QtWidgets import QApplication

    app = app if app is not None else QApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        logger.warning("界面看门狗必须在界面线程、QApplication 创建之后安装，已跳过")
        return None
    target = file if file is not None else _crash_log_file
    if target is None or getattr(target, 'closed', False):
        logger.warning("crash.log 未打开（install_excepthook 未调用或失败），界面看门狗未安装")
        return None
    if _active_watchdog is not None:
        _active_watchdog.stop()
    watchdog = UiWatchdog(timeout_s, target, clock, beat_ms=beat_ms)
    watchdog.start()
    _active_watchdog = watchdog
    logger.info("界面看门狗已安装: 阈值 %.1f 秒，续期间隔 %d ms",
                watchdog.timeout_s, watchdog.beat_ms)
    return watchdog


def active_ui_watchdog() -> Optional[UiWatchdog]:
    """当前生效的界面看门狗（诊断信息用）"""
    return _active_watchdog
