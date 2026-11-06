"""
数据模型
定义监控任务和数据点的数据结构
"""
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Union

if TYPE_CHECKING:
    # 仅用于类型标注：数据层在运行时不依赖 core（分层单向）
    from core.contracts import TaskStatus


@dataclass
class MonitorTask:
    """监控任务模型"""
    task_id: str                    # 任务唯一ID
    pid: int                        # 进程ID
    process_name: str               # 进程名称
    metric_types: List[str]         # 监控指标类型列表（支持多指标）
    interval: float                 # 采集间隔（秒）
    start_time: Optional[datetime]  # 开始时间（任务线程启动前为 None）
    end_time: Optional[datetime]    # 结束时间（None表示正在运行）
    # 状态：pending（已创建未启动，仅内存）/running/stopped。内存中写 TaskStatus，
    # 从数据库读出的是原字符串；TaskStatus 继承 str，两者可直接比较
    status: Union['TaskStatus', str]
    note: str = ''                          # 任务备注
    sample_count: int = 0                   # 已采集次数（同一周期多指标算一次）
    last_sample_at: Optional[datetime] = None  # 最后一次采样时间

    def is_running(self) -> bool:
        """判断任务是否正在运行"""
        return self.status == 'running'   # 与 TaskStatus.RUNNING 的值一致

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'pid': self.pid,
            'process_name': self.process_name,
            'metric_types': self.metric_types,
            'interval': self.interval,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'status': self.status,
            'note': self.note,
            'sample_count': self.sample_count,
            'last_sample_at': self.last_sample_at.isoformat() if self.last_sample_at else None,
        }

    @staticmethod
    def from_dict(data: dict) -> 'MonitorTask':
        """从字典创建（兼容旧版单指标 metric_type 字段）"""
        return MonitorTask(
            task_id=data['task_id'],
            pid=data['pid'],
            process_name=data['process_name'],
            metric_types=data.get('metric_types') or [data['metric_type']],
            interval=data['interval'],
            start_time=datetime.fromisoformat(data['start_time']) if data['start_time'] else None,
            end_time=datetime.fromisoformat(data['end_time']) if data['end_time'] else None,
            status=data['status'],
            note=data.get('note') or '',
            sample_count=data.get('sample_count') or 0,
            last_sample_at=(datetime.fromisoformat(data['last_sample_at'])
                            if data.get('last_sample_at') else None),
        )


@dataclass
class DataPoint:
    """数据点模型"""
    task_id: str                # 所属任务ID
    timestamp: datetime         # 时间戳
    value: float                # 指标值
    metric_type: str = ""       # 指标类型（空串兼容旧数据）

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'value': self.value,
            'metric_type': self.metric_type,
        }

    @staticmethod
    def from_dict(data: dict) -> 'DataPoint':
        """从字典创建"""
        return DataPoint(
            task_id=data['task_id'],
            timestamp=datetime.fromisoformat(data['timestamp']) if data['timestamp'] else None,
            value=data['value'],
            metric_type=data.get('metric_type', ''),
        )


@dataclass
class Marker:
    """任务时间标记（事件打点）"""
    task_id: str                # 所属任务ID
    timestamp: datetime         # 标记时间
    label: str                  # 标记文字
    id: Optional[int] = None    # 数据库自增ID（未入库时为 None）
