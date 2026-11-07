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
    # 被监控进程的创建时间（psutil.Process.create_time() 的 epoch 秒）；
    # 用于识别 PID 复用，不是查询用的时间参数，因此保留 float
    create_time: Optional[float] = None
    # 自动化方案（core.automation.AutomationPlan 的 JSON 文本）；没有方案为 None。
    # 只经 TaskRepo.save_plan 写入，save() 不碰它（UPSERT 不会把它清掉）
    plan_json: Optional[str] = None

    def is_running(self) -> bool:
        """判断任务是否正在运行"""
        return self.status == 'running'   # 与 TaskStatus.RUNNING 的值一致


@dataclass
class DataPoint:
    """数据点模型"""
    task_id: str                # 所属任务ID
    timestamp: datetime         # 时间戳
    value: float                # 指标值
    metric_type: str = ""       # 指标类型（空串兼容旧数据）


@dataclass
class Marker:
    """任务时间标记（事件打点）"""
    task_id: str                # 所属任务ID
    timestamp: datetime         # 标记时间
    label: str                  # 标记文字
    id: Optional[int] = None    # 数据库自增ID（未入库时为 None）
    kind: str = 'user'          # 打点类型，取值见 MARKER_KINDS


# 打点类型（task_markers.kind 列的合法取值）：用户手动打点 / 基线 / 暂停 / 恢复，
# 以及被测进程崩溃 / 重启 / 采集中断 / 采集恢复（由续采与退出取证写入）
MARKER_KINDS = ('user', 'baseline', 'pause', 'resume',
                'crash', 'restart', 'interrupt', 'recover')

# 阶段性改名：MonitorTask 与 core 层的同名线程类易混，新代码请用 TaskRecord
TaskRecord = MonitorTask
