"""
界面层服务：页面对外部环境（提示、对话框、时间、进程查询、任务状态、用户偏好、
后台线程登记）的全部依赖都经由这里注入。

生产环境由 production_services(manager, registry) 一次性组装，主窗口向下分发给各页面；
测试用 tests/doubles.py 中的替身组装（见 tests/factories.py 的 make_services）。
"""
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ui.services.clock import Clock, SystemClock
from ui.services.dialogs import (
    CLOSE_ACTION_CANCEL, CLOSE_ACTION_QUIT, CLOSE_ACTION_TRAY, CLOSE_ACTIONS,
    Dialogs, QtDialogs,
)
from ui.services.notifier import (
    DEFAULT_DURATIONS, NOTICE_LEVELS, InfoBarNotifier, Notifier, resolve_duration,
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


def production_services(manager: "MonitorManager",
                        registry: "ThreadRegistry") -> UiServices:
    """组装生产环境的服务集合；任务状态查询绑定到给定的 MonitorManager，
    线程登记表由组合根创建并同时交给退出协调器。"""
    return UiServices(
        notifier=InfoBarNotifier(),
        dialogs=QtDialogs(),
        clock=SystemClock(),
        process_query=PsutilProcessQuery(),
        task_status=ManagerTaskStatus(manager),
        preferences=ConfigPreferences(),
        threads=registry,
    )


__all__ = [
    'UiServices', 'production_services',
    'Notifier', 'InfoBarNotifier', 'DEFAULT_DURATIONS', 'NOTICE_LEVELS', 'resolve_duration',
    'Dialogs', 'QtDialogs',
    'CLOSE_ACTION_QUIT', 'CLOSE_ACTION_TRAY', 'CLOSE_ACTION_CANCEL', 'CLOSE_ACTIONS',
    'Clock', 'SystemClock',
    'ProcessQuery', 'PsutilProcessQuery', 'ProcessEntry',
    'TaskStatusProvider', 'ManagerTaskStatus',
    'MonitorPreferences', 'ConfigPreferences',
]
