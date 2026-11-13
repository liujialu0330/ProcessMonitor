"""不依赖界面控件的运行服务；后台只组装采集与命令处理实际需要的能力。"""
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional


# 与 utils.logger.setup_logging 保持同一日志文件名。
APP_LOG_FILENAME = 'app.log'


def current_log_path() -> Optional[str]:
    """从已安装的日志处理器取实际路径，不重复推导数据目录。"""
    for handler in logging.getLogger().handlers:
        filename = getattr(handler, 'baseFilename', None)
        if filename and os.path.basename(filename) == APP_LOG_FILENAME:
            return filename
    return None


@dataclass(frozen=True)
class RuntimeServices:
    """后台命令处理需要的能力，不包含对话框、通知控件或窗口状态。"""
    clock: Any
    launcher: Any
    log_path: Callable[[], Optional[str]] = current_log_path


def production_runtime_services() -> RuntimeServices:
    from core.monitor_task import SYSTEM_CLOCK
    from core.process_launcher import ProcessLauncher
    return RuntimeServices(clock=SYSTEM_CLOCK, launcher=ProcessLauncher())
