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
"""
import logging
import os
import sys

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
        import faulthandler
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
    """QApplication 未创建时跳过 UI；仅 GUI 主线程弹窗，工作线程静默"""
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
