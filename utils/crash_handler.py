"""
全局异常兜底
安装 sys.excepthook 并启用 faulthandler，避免未捕获异常/致命信号导致进程静默退出、
用户看不到任何提示且日志里也无迹可寻。
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

# crash.log 文件对象需模块级全局持有，防止被 GC 回收后自动关闭（faulthandler 要求句柄常驻）
_crash_log_file = None

# 防止 excepthook 内部处理逻辑自身再抛异常导致递归调用
_in_excepthook = False


def install_excepthook(log_dir: str):
    """
    安装全局异常钩子，并启用 faulthandler 捕获段错误等致命信号写入 crash.log

    Args:
        log_dir: 日志目录（与 utils.logger.setup_logging 返回值一致）
    """
    global _crash_log_file

    crash_log_path = os.path.join(log_dir, "crash.log")
    try:
        _crash_log_file = open(crash_log_path, 'a', encoding='utf-8')
        import faulthandler
        faulthandler.enable(_crash_log_file)
    except Exception:
        logger.error("faulthandler 初始化失败", exc_info=True)

    sys.excepthook = _global_excepthook


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

        from utils.logger import get_log_dir
        log_path = os.path.join(get_log_dir(), "app.log")
        QMessageBox.critical(
            None,
            "程序发生错误",
            f"程序遇到未预期的错误，详细信息已记录到日志文件：\n{log_path}",
        )
    except Exception:
        logger.error("弹出异常提示框失败", exc_info=True)
