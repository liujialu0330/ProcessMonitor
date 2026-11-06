"""
视图模型：页面的业务状态与命令（不持有控件），对外只发信号，便于无控件单元测试。
"""
from ui.viewmodels.history_auto_refresh import (
    AUTO_REFRESH_INTERVAL_MID_MS, AUTO_REFRESH_INTERVAL_MS,
    AUTO_REFRESH_INTERVAL_SLOW_MS, HistoryAutoRefresh, auto_refresh_interval_for,
)
from ui.viewmodels.history_vm import HistoryViewModel, HistoryWorkerRunner

__all__ = [
    'HistoryViewModel', 'HistoryWorkerRunner', 'HistoryAutoRefresh',
    'auto_refresh_interval_for', 'AUTO_REFRESH_INTERVAL_MS',
    'AUTO_REFRESH_INTERVAL_MID_MS', 'AUTO_REFRESH_INTERVAL_SLOW_MS',
]
