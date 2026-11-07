"""
跨层契约
core 与 ui 之间传递的枚举与只读快照。界面文案不放这里：枚举到中文的映射
统一在 ui/formatting.py，core 只表达"发生了什么"，不关心怎么显示。
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from data.models import QualityStats


class TaskStatus(str, Enum):
    """任务状态。继承 str：落库值仍是原字符串，与数据库读出的 str 可直接比较。"""
    PENDING = 'pending'    # 已创建、线程尚未启动
    RUNNING = 'running'    # 线程在运行（含暂停中与停止收尾中）
    STOPPED = 'stopped'    # 线程已结束，或库中已写入停止状态

    def __str__(self) -> str:
        # 日志与字符串拼接里显示原始值，而不是 "TaskStatus.RUNNING"
        return self.value


class StopReason(Enum):
    """任务停止原因（经 task_stopped 信号传给界面）。"""
    USER = 'user'                            # 用户主动停止或退出程序
    PROCESS_EXITED = 'process_exited'        # 采集中发现进程已退出（含运行中 PID 被复用）
    PROCESS_NOT_FOUND = 'process_not_found'  # 启动时进程就不存在或无法访问
    PID_REUSED = 'pid_reused'                # 启动时发现所选 PID 已被另一个进程占用
    INTERNAL_ERROR = 'internal_error'        # 监控线程内部未预期异常


class UnavailableReason(Enum):
    """单个指标无法采集的原因（经 metrics_unavailable 信号传给界面）。"""
    ACCESS_DENIED = 'access_denied'          # 无权访问，以管理员运行可采集
    PROCESS_EXITING = 'process_exiting'      # 进程正在退出（僵尸态）
    NOT_IN_SESSION = 'not_in_session'        # 进程不在当前桌面会话，提权也无法采集
    NOT_SUPPORTED = 'not_supported'          # 当前平台不支持该指标
    ERROR = 'error'                          # 其他采集异常


@dataclass(frozen=True)
class TaskInfo:
    """manager 内存中某个任务的只读快照（界面只拿它，不接触任务线程对象）。

    status 取自线程实际状态：RUNNING 覆盖暂停中与停止收尾中，is_stopping 为 True
    表示已请求停止但收尾尚未完成。quality 为采集数据质量快照（MonitorWorker.
    quality_snapshot()，可选；尚未开始调度或调用方未提供时为 None）。
    """
    task_id: str
    pid: int
    process_name: str
    metric_types: Tuple[str, ...]
    interval: float
    note: str
    status: TaskStatus
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    create_time: Optional[float]     # 所选进程的创建时间（epoch 秒），手动输入 PID 时为 None
    is_paused: bool
    is_stopping: bool
    quality: Optional['QualityStats'] = None
