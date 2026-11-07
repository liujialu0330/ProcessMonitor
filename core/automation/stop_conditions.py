"""
自动停止条件（纯函数）

should_stop(state, plan, now) 判断任务是否已满足方案里的停止条件，返回停止原因代码
（AUTO_STOP_*）或 None。代码是给界面映射文案用的稳定字符串，中文文案在
ui.formatting.auto_stop_text。

- 时长：从任务开始时刻起按墙钟计（暂停的时间也算），满 stop_after_seconds 即停；
- 轮数：kind='user' 的打点数（手动与自动合计，基线与暂停/恢复系统打点不算）达到
  stop_after_rounds 即停——第 N 轮的打点标志着第 N 轮结束。
两者都设置时先满足者生效；同时满足时报时长。
被测进程退出时结束任务是采集线程的现有行为，不在这里判断。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.automation.plan import AutomationPlan

AUTO_STOP_DURATION = 'duration'
AUTO_STOP_ROUNDS = 'rounds'
AUTO_STOP_REASONS = (AUTO_STOP_DURATION, AUTO_STOP_ROUNDS)


@dataclass(frozen=True)
class StopState:
    """判断停止条件所需的任务进度。

    Attributes:
        started_at: 任务开始时刻（尚未开始为 None，不判时长）
        rounds: 已完成的轮数（kind='user' 的打点数）
    """
    started_at: Optional[datetime]
    rounds: int = 0


def should_stop(state: StopState, plan: AutomationPlan, now: datetime) -> Optional[str]:
    """满足停止条件时返回原因代码（AUTO_STOP_*），否则 None。"""
    if (plan.stop_after_seconds is not None and state.started_at is not None
            and (now - state.started_at).total_seconds() >= plan.stop_after_seconds):
        return AUTO_STOP_DURATION
    if plan.stop_after_rounds is not None and state.rounds >= plan.stop_after_rounds:
        return AUTO_STOP_ROUNDS
    return None
