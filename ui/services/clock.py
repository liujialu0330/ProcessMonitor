"""
当前时间（Clock）

页面里"现在几点"的取值统一经由注入的 Clock：运行时长、默认文件名里的时间戳、
打点时刻等在测试中可以用可控时钟复现，不必替换页面模块里的 datetime。
"""
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """时钟服务。"""

    def now(self) -> datetime:
        """当前本地时间（不带时区，与库里存储的时间口径一致）。"""
        ...


class SystemClock:
    """生产实现：系统本地时间。"""

    def now(self) -> datetime:
        """当前本地时间。"""
        return datetime.now()
