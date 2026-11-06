"""
页面后台线程的退出登记结构

各页面通过公开方法 background_threads() 返回 BackgroundThread 列表，主窗口退出时
统一遍历收尾，不再按私有属性名逐个 getattr——私有属性改名时旧写法会静默跳过，
随后 main.py 的 os._exit 会把仍在写库/写文件的线程直接掐断。
"""
from dataclasses import dataclass
from typing import Callable, Optional

from PyQt5.QtCore import QThread


@dataclass(frozen=True)
class BackgroundThread:
    """一个需要在退出时收尾的后台线程。

    Attributes:
        name: 线程用途（仅用于日志定位）
        thread: 线程对象；None 表示当前没有该线程，收尾时直接跳过
        cancel_fn: 取消回调，等待前先调用；None 表示无取消机制、只等待
        timeout_ms: 等待超时（毫秒），超时只记日志不再等待
    """
    name: str
    thread: Optional[QThread]
    cancel_fn: Optional[Callable[[], None]] = None
    timeout_ms: int = 2000
