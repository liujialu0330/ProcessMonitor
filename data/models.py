"""
数据模型
定义监控任务和数据点的数据结构
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class MonitorTask:
    """监控任务模型"""
    task_id: str                    # 任务唯一ID
    pid: int                        # 进程ID
    process_name: str               # 进程名称
    metric_type: str                # 监控指标类型
    interval: float                 # 采集间隔（秒）
    start_time: datetime            # 开始时间
    end_time: Optional[datetime]    # 结束时间（None表示正在运行）
    status: str                     # 状态：running/stopped

    def is_running(self) -> bool:
        """判断任务是否正在运行"""
        return self.status == 'running'

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'pid': self.pid,
            'process_name': self.process_name,
            'metric_type': self.metric_type,
            'interval': self.interval,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'status': self.status,
        }

    @staticmethod
    def from_dict(data: dict) -> 'MonitorTask':
        """从字典创建"""
        return MonitorTask(
            task_id=data['task_id'],
            pid=data['pid'],
            process_name=data['process_name'],
            metric_type=data['metric_type'],
            interval=data['interval'],
            start_time=datetime.fromisoformat(data['start_time']) if data['start_time'] else None,
            end_time=datetime.fromisoformat(data['end_time']) if data['end_time'] else None,
            status=data['status'],
        )


@dataclass
class DataPoint:
    """数据点模型"""
    task_id: str                # 所属任务ID
    timestamp: datetime         # 时间戳
    value: float                # 指标值

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'value': self.value,
        }

    @staticmethod
    def from_dict(data: dict) -> 'DataPoint':
        """从字典创建"""
        return DataPoint(
            task_id=data['task_id'],
            timestamp=datetime.fromisoformat(data['timestamp']) if data['timestamp'] else None,
            value=data['value'],
        )
