"""
界面层服务：页面对外部环境（提示、对话框、时间、进程查询、任务状态、用户偏好、
后台线程登记、启动被测程序）的全部依赖都经由这里注入。

生产环境由 production_services(manager, registry) 一次性组装，主窗口向下分发给各页面；
测试用 tests/doubles.py 中的替身组装（见 tests/factories.py 的 make_services）。
"""
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from ui.services.clock import Clock, SystemClock
from ui.services.dialogs import (
    CLOSE_ACTION_CANCEL, CLOSE_ACTION_QUIT, CLOSE_ACTION_TRAY, CLOSE_ACTIONS,
    Dialogs, QtDialogs,
)
from ui.services.notifier import (
    ACTION_DURATION_MS, ALERT_EVENTS, DEFAULT_DURATIONS, EVENT_COPY_DONE, EVENT_COPY_FAILED,
    EVENT_EXPORT_BLOCKED, EVENT_EXPORT_DONE, EVENT_EXPORT_FAILED, EVENT_GENERIC,
    EVENT_MARKER_ADDED, EVENT_MARKER_UNDONE, EVENT_QUICK_MARKER,
    EVENT_STORAGE_INTEGRITY, EVENT_STORAGE_SPACE, EVENT_TASK_ABNORMAL, EVENT_TASK_ERROR,
    EVENT_TRAY_HIDDEN, EVENT_UPDATE_AVAILABLE, NOTICE_LEVELS, InfoBarNotifier, NoticeAction,
    Notifier, resolve_duration, window_state,
)
from ui.services.preferences import ConfigPreferences, MonitorPreferences
from ui.services.process_query import ProcessEntry, ProcessQuery, PsutilProcessQuery
from ui.services.task_status import ManagerTaskStatus, TaskStatusProvider

if TYPE_CHECKING:
    # 只做类型标注：登记表与管理器实例都由组合根创建后注入
    from core.shutdown import ThreadRegistry
    from core.monitor_manager import MonitorManager


@dataclass(frozen=True)
class UiServices:
    """页面所需服务的集合：构造后不可替换单项，测试需要不同组合时重新构造一份。"""

    notifier: Notifier
    dialogs: Dialogs
    clock: Clock
    process_query: ProcessQuery
    task_status: TaskStatusProvider
    preferences: MonitorPreferences
    # 短命后台线程登记表：页面启动线程时登记，退出时由 ShutdownCoordinator 统一收尾
    threads: "ThreadRegistry"
    # 启动 / 关闭被测程序（core.process_launcher.ProcessLauncher 或同接口的替身）；
    # None 时用到的地方现建生产实现
    launcher: Any = None
    # 读主屏显示缩放倍数（1.5 表示 150%）的函数，写进被测目标元数据；None 表示不记录
    dpi_scale: Optional[Callable[[], Optional[float]]] = None
    # 取应用日志 app.log 路径的函数，证据包据此摘录日志；None 或返回 None 时不收录日志
    log_path: Optional[Callable[[], Optional[str]]] = None
    # 窗口与工作状态记忆（ui.window_state.WindowMemoryStore 或同接口的替身）；None 表示
    # 不记忆：主窗口按默认大小居中、停在监控页，也不写配置
    window_memory: Any = None


# 应用日志文件名（与 utils.logger.setup_logging 一致）
APP_LOG_FILENAME = 'app.log'


def primary_screen_dpi_scale() -> Optional[float]:
    """主屏的设备像素比（启用了高 DPI 缩放，PassThrough 舍入下即系统缩放倍数）；
    没有 QGuiApplication 或主屏时为 None。只能在界面线程调用。"""
    from PyQt5.QtGui import QGuiApplication
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return None
    return float(screen.devicePixelRatio()) or None


def current_log_path() -> Optional[str]:
    """root logger 上 app.log 文件处理器的实际路径：日志目录由组合根按数据目录推导，
    从处理器上取避免在这里重复那套规则。没有配置文件日志时为 None。"""
    for handler in logging.getLogger().handlers:
        filename = getattr(handler, 'baseFilename', None)
        if filename and os.path.basename(filename) == APP_LOG_FILENAME:
            return filename
    return None


def production_services(manager: "MonitorManager",
                        registry: "ThreadRegistry") -> UiServices:
    """组装生产环境的服务集合；任务状态查询绑定到给定的 MonitorManager，
    线程登记表由组合根创建并同时交给退出协调器。启动被测程序的 launcher 与命令处理器
    共用这一份（组合根从这里取）。"""
    from core.process_launcher import ProcessLauncher
    from ui.window_state import ConfigWindowMemory
    return UiServices(
        notifier=InfoBarNotifier(),
        dialogs=QtDialogs(),
        clock=SystemClock(),
        process_query=PsutilProcessQuery(),
        task_status=ManagerTaskStatus(manager),
        preferences=ConfigPreferences(),
        threads=registry,
        launcher=ProcessLauncher(),
        dpi_scale=primary_screen_dpi_scale,
        log_path=current_log_path,
        window_memory=ConfigWindowMemory(),
    )


__all__ = [
    'UiServices', 'production_services', 'primary_screen_dpi_scale', 'current_log_path',
    'Notifier', 'InfoBarNotifier', 'NoticeAction', 'DEFAULT_DURATIONS', 'ACTION_DURATION_MS',
    'NOTICE_LEVELS', 'resolve_duration', 'window_state', 'ALERT_EVENTS', 'EVENT_GENERIC',
    'EVENT_TASK_ABNORMAL', 'EVENT_TASK_ERROR', 'EVENT_QUICK_MARKER', 'EVENT_UPDATE_AVAILABLE',
    'EVENT_STORAGE_SPACE', 'EVENT_STORAGE_INTEGRITY', 'EVENT_TRAY_HIDDEN',
    'EVENT_MARKER_ADDED', 'EVENT_MARKER_UNDONE', 'EVENT_EXPORT_BLOCKED', 'EVENT_EXPORT_DONE',
    'EVENT_EXPORT_FAILED', 'EVENT_COPY_DONE', 'EVENT_COPY_FAILED',
    'Dialogs', 'QtDialogs',
    'CLOSE_ACTION_QUIT', 'CLOSE_ACTION_TRAY', 'CLOSE_ACTION_CANCEL', 'CLOSE_ACTIONS',
    'Clock', 'SystemClock',
    'ProcessQuery', 'PsutilProcessQuery', 'ProcessEntry',
    'TaskStatusProvider', 'ManagerTaskStatus',
    'MonitorPreferences', 'ConfigPreferences',
]
