"""
自动化：自动打点与停止条件

- plan：AutomationPlan（定时打点、日志触发、停止条件、停止后导出），JSON 往返与校验；
  随任务存进 tasks.plan_json；
- log_watcher：日志文件增量读尾与正则匹配（处理截断与轮转）；
- stop_conditions：停止条件判断（纯函数）；
- auto_marker：AutoMarkerService，按方案经 MonitorManager 的公开命令打点与停止任务。
"""
from core.automation.plan import (
    PLAN_SCHEMA_VERSION,
    AutomationPlan,
    PlanFormatError,
    PlanVersionError,
    plan_from_json_or_none,
)
from core.automation.stop_conditions import (
    AUTO_STOP_DURATION,
    AUTO_STOP_REASONS,
    AUTO_STOP_ROUNDS,
    StopState,
    should_stop,
)

__all__ = [
    'PLAN_SCHEMA_VERSION', 'AutomationPlan', 'PlanFormatError', 'PlanVersionError',
    'plan_from_json_or_none',
    'AUTO_STOP_DURATION', 'AUTO_STOP_ROUNDS', 'AUTO_STOP_REASONS', 'StopState', 'should_stop',
]
