"""
v1.4.1 `data/models.py` 的冻结副本 —— 禁止修改

来源：tag v1.4.1（commit ea1dd90e7991502c7ef2f390b6337b0093809c39）的 `data/models.py`，用 `git show v1.4.1:data/models.py` 取得。
用途：反向兼容测试（tests/test_reverse_compat_v141.py）以"用户回退到 1.4.1"的真实
代码打开当前版本建的库。它代表已发布的行为，与仓库代码一同演进就失去了意义，
所以除下列为独立导入所做的最小改动外，正文与原文件逐字一致：
- 无（仅替换了文件头说明）。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class MonitorTask:
    """监控任务模型"""
    task_id: str                    # 任务唯一ID
    pid: int                        # 进程ID
    process_name: str               # 进程名称
    metric_types: List[str]         # 监控指标类型列表（支持多指标）
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
            'metric_types': self.metric_types,
            'interval': self.interval,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'status': self.status,
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
