"""
监控期间阻止系统空闲睡眠

有运行中任务（口径同 MonitorManager.running_task_count：采集中、暂停中、停止收尾中）时
调用 SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) 告诉系统"别因空闲睡眠"，
任务全部结束后用 SetThreadExecutionState(ES_CONTINUOUS) 撤销。不带 ES_DISPLAY_REQUIRED：
屏幕照常按电源计划熄灭。挡不住合上盖子、手动睡眠与电源按钮——这些不走空闲判定。

线程约束：SetThreadExecutionState 的效果属于调用它的线程（线程退出即失效），持有与撤销
必须在同一个线程。本对象只在自己所在的线程（界面线程）调用它：管理器信号经 Qt 自动连接
送达本对象所在线程；万一从别的线程调用 set_enabled 等入口，也改经排队信号回到本线程执行。

进程退出（os._exit）时系统随线程一并撤销该状态，不需要额外收尾。
"""
import logging
import sys
import threading
from typing import Callable, Optional

from PyQt5.QtCore import QObject, Qt, pyqtSignal

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# SetThreadExecutionState 的形状：参数为 EXECUTION_STATE 标志，返回先前的状态，0 表示失败
SetStateFn = Callable[[int], int]


def default_set_state() -> Optional[SetStateFn]:
    """ctypes 版 kernel32.SetThreadExecutionState；非 Windows 或加载失败返回 None。"""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        # 独立的 WinDLL 实例：自己设 argtypes/restype，不影响其他模块对 kernel32 的用法
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        fn = kernel32.SetThreadExecutionState
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = ctypes.c_uint32
        return fn
    except Exception:
        logger.warning("无法加载 SetThreadExecutionState，阻止睡眠功能不生效", exc_info=True)
        return None


class PowerGuard(QObject):
    """按"是否有运行中任务"持有或撤销"阻止空闲睡眠"。

    每次任务启停都按管理器的当前计数重新判定（而不是自己加减计数），重复或乱序的
    信号都不会让持有状态走偏。系统调用失败（抛错或返回 0）只记一次日志，此后降级为
    不生效，不影响监控本身。
    """

    # 非本线程调用入口时，经此排队信号回到本对象所在线程再同步状态
    _sync_requested = pyqtSignal()

    def __init__(self, manager, *, enabled: bool,
                 set_state: Optional[SetStateFn] = None, parent: Optional[QObject] = None):
        """
        Args:
            manager: MonitorManager（用其 task_started / task_stopped / task_removed 信号与
                running_task_count()）
            enabled: 初始开关（用户配置）
            set_state: SetThreadExecutionState 的替身；None 时用 ctypes 真实现
            parent: Qt 父对象
        """
        super().__init__(parent)
        self._manager = manager
        self._enabled = bool(enabled)
        self._holding = False
        self._owner_ident = threading.get_ident()
        self._set_state = set_state if set_state is not None else default_set_state()
        if self._set_state is None:
            logger.info("当前环境不支持阻止系统睡眠，相关设置不生效")
        self._sync_requested.connect(self._sync, Qt.QueuedConnection)
        manager.task_started.connect(self._on_task_event)
        manager.task_stopped.connect(self._on_task_event)
        # task_stopped 漏发（收尾异常等）时，线程结束后的移除再兜底一次
        manager.task_removed.connect(self._on_task_event)
        self._request_sync()

    @classmethod
    def from_config_item(cls, manager, item, **kwargs) -> "PowerGuard":
        """按配置项构造，并跟随它的变化（设置页开关一切换即生效）。

        item 是 qfluentwidgets 的 ConfigItem（取 .value 与 .valueChanged）；core 不直接依赖
        app_config，由组合根把配置项交进来。"""
        guard = cls(manager, enabled=bool(item.value), **kwargs)
        item.valueChanged.connect(guard.set_enabled)
        return guard

    # ========== 公开接口 ==========

    def set_enabled(self, enabled) -> None:
        """运行时切换：关闭时若正持有立即撤销；开启时若有运行中任务立即持有。"""
        self._enabled = bool(enabled)
        self._request_sync()

    def is_enabled(self) -> bool:
        return self._enabled

    def is_holding(self) -> bool:
        """当前是否正在阻止系统空闲睡眠"""
        return self._holding

    def is_available(self) -> bool:
        """系统调用是否可用（不可用 = 非 Windows、加载失败或调用失败后已降级）"""
        return self._set_state is not None

    # ========== 内部 ==========

    def _on_task_event(self, *_args) -> None:
        self._request_sync()

    def _request_sync(self) -> None:
        if threading.get_ident() == self._owner_ident:
            self._sync()
        else:
            self._sync_requested.emit()

    def _sync(self) -> None:
        """让持有状态与"开关开启且有运行中任务"一致；只在状态需要变化时调用系统接口。"""
        want = self._enabled and self._manager.running_task_count() > 0
        if want == self._holding or self._set_state is None:
            return
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if want else ES_CONTINUOUS
        try:
            ok = self._set_state(flags) != 0
        except Exception:
            logger.warning("SetThreadExecutionState 调用异常，阻止睡眠功能降级为不生效",
                           exc_info=True)
            ok = False
        else:
            if not ok:
                logger.warning("SetThreadExecutionState 返回失败，阻止睡眠功能降级为不生效")
        if not ok:
            # 失败后不再尝试：持有与否已无从确认，按未持有处理
            self._set_state = None
            self._holding = False
            return
        self._holding = want
        logger.info("监控期间阻止系统睡眠：%s", "已开启" if want else "已撤销")
