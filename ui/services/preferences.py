"""
用户偏好读取（MonitorPreferences）

页面需要的用户偏好（上次选择的指标、默认采集间隔、保留天数、关窗到托盘、打点
快捷键）及其取值范围（采集间隔范围、保留天数可选值）统一
经由注入的 MonitorPreferences 读取：测试可以给出固定偏好，不必改动全局 cfg 单例。

设置页上直接绑定 ConfigItem 的设置卡片不经过本服务：那是 qfluentwidgets 设置卡
的既有用法，卡片本身负责读写与落盘。
"""
from typing import Iterable, List, Protocol, Tuple, runtime_checkable

import app_config
from app_config import cfg


@runtime_checkable
class MonitorPreferences(Protocol):
    """用户偏好服务。"""

    def last_metrics(self) -> List[str]:
        """上次在监控页选择的指标（新建任务时沿用）；返回副本，修改不影响存储。"""
        ...

    def remember_last_metrics(self, metrics: Iterable[str]) -> None:
        """记住本次的指标选择。"""
        ...

    def default_interval(self) -> int:
        """新建任务时采集周期输入框的初始值（秒）。"""
        ...

    def retention_days(self) -> int:
        """历史数据保留天数，0 表示永久保留。"""
        ...

    def close_to_tray(self) -> bool:
        """关闭主窗口时是否最小化到系统托盘。"""
        ...

    def interval_range(self) -> Tuple[int, int]:
        """采集周期（秒）的合法范围 (最小, 最大)：取值范围只在 app_config 定义一份。"""
        ...

    def retention_options(self) -> List[int]:
        """历史数据保留天数的可选值（0 = 永久保留），顺序即设置页选项顺序。"""
        ...

    def marker_hotkey(self) -> str:
        """全局打点快捷键（如 "Ctrl+Alt+M"；空串表示不使用）。"""
        ...

    def set_marker_hotkey(self, text: str) -> None:
        """记住新的全局打点快捷键。"""
        ...


class ConfigPreferences:
    """生产实现：读写 app_config 的全局 cfg 单例。"""

    def last_metrics(self) -> List[str]:
        """上次选择的指标（副本）。"""
        return list(cfg.get(cfg.last_metrics))

    def remember_last_metrics(self, metrics: Iterable[str]) -> None:
        """记住指标选择：委托 app_config.remember_last_metrics（归一化、按需落盘）。"""
        app_config.remember_last_metrics(metrics)

    def default_interval(self) -> int:
        """默认采集周期（秒）。"""
        return int(cfg.get(cfg.default_interval))

    def retention_days(self) -> int:
        """历史数据保留天数。"""
        return int(cfg.get(cfg.retention_days))

    def close_to_tray(self) -> bool:
        """关窗时是否最小化到托盘。"""
        return bool(cfg.get(cfg.close_to_tray))

    def interval_range(self) -> Tuple[int, int]:
        """采集周期范围：委托 app_config.interval_range（来自 ConfigItem 校验器）。"""
        return app_config.interval_range()

    def retention_options(self) -> List[int]:
        """保留天数可选值：委托 app_config.retention_options（来自 ConfigItem 校验器）。"""
        return app_config.retention_options()

    def marker_hotkey(self) -> str:
        """全局打点快捷键：委托 app_config.get_marker_hotkey。"""
        return app_config.get_marker_hotkey()

    def set_marker_hotkey(self, text: str) -> None:
        """记住全局打点快捷键：委托 app_config.set_marker_hotkey（按需落盘）。"""
        app_config.set_marker_hotkey(text)
