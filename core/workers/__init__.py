"""
后台短命线程：统一的基类（取消、结果/失败/进度信号、异常捕获）与各具体 worker。
"""
from core.workers.base import BackgroundTask, CancelToken, LatestRequestGate, TaskError

__all__ = ['BackgroundTask', 'CancelToken', 'LatestRequestGate', 'TaskError']
