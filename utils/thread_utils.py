"""
线程收尾清理工具
用于窗口关闭等场景下统一处理后台 QThread 的取消与等待，避免遗留线程导致
进程退出时崩溃（如 0xC0000409）或无限期挂起。
"""
import logging

logger = logging.getLogger(__name__)


def shutdown_thread(thread, cancel_fn=None, timeout_ms=2000) -> bool:
    """
    关闭一个后台 QThread：先调用取消回调（如果提供），再等待线程结束，超时只记日志不再等待。

    Args:
        thread: QThread 实例；None 或未运行时直接跳过
        cancel_fn: 取消回调（如 downloader.cancel），可选
        timeout_ms: 等待超时时间（毫秒）

    Returns:
        bool: 线程是否在超时前正常结束（thread 为 None 或本就未运行时视为 True）
    """
    if thread is None or not thread.isRunning():
        return True

    if cancel_fn is not None:
        try:
            cancel_fn()
        except Exception:
            logger.error("线程取消回调执行失败", exc_info=True)

    finished = thread.wait(timeout_ms)
    if not finished:
        logger.error("线程在 %dms 内未能结束: %r", timeout_ms, thread)
    return finished
