"""
页面顶部的非模态提示（Notifier）

页面不再直接调用 qfluentwidgets 的 InfoBar：提示统一经由注入的 Notifier 发出，
测试换成记录型替身即可断言"弹了什么提示"，不必替换页面模块里的 InfoBar 名字。
"""
from typing import Any, Optional, Protocol, runtime_checkable

from PyQt5.QtWidgets import QWidget
from qfluentwidgets import InfoBar, InfoBarPosition

# 各级提示未显式给出时长时的默认值（毫秒）。错误类为 -1（不自动消失、可手动关闭）：
# InfoBar.error 库默认只停 1 秒，用户来不及看清出了什么错
DEFAULT_DURATIONS = {
    "info": 2000,
    "success": 2000,
    "warning": 3000,
    "error": -1,
}

NOTICE_LEVELS = tuple(DEFAULT_DURATIONS)


def resolve_duration(level: str, duration: Optional[int]) -> int:
    """给出某级提示实际使用的显示时长：调用方显式给值时照用，否则取该级默认值。"""
    if level not in DEFAULT_DURATIONS:
        raise ValueError(f"未知的提示级别：{level!r}")
    return DEFAULT_DURATIONS[level] if duration is None else int(duration)


@runtime_checkable
class Notifier(Protocol):
    """页面提示服务。

    parent 是提示要挂靠的控件（页面传 self）：InfoBar 显示在它的顶部，并随它销毁。
    duration 为毫秒，None 取该级默认值（见 DEFAULT_DURATIONS），小于 0 表示不自动消失。
    返回提示对象：调用方可在其上 addWidget 追加按钮，或连接 closedSignal
    （例如"同一错误仍在显示时不再叠加"的去重逻辑）。
    """

    def info(self, parent: QWidget, title: str, content: str,
             duration: Optional[int] = None) -> Any:
        """一般信息提示。"""
        ...

    def success(self, parent: QWidget, title: str, content: str,
                duration: Optional[int] = None) -> Any:
        """操作成功提示。"""
        ...

    def warning(self, parent: QWidget, title: str, content: str,
                duration: Optional[int] = None) -> Any:
        """警告提示。"""
        ...

    def error(self, parent: QWidget, title: str, content: str,
              duration: Optional[int] = None) -> Any:
        """错误提示：默认不自动消失。"""
        ...


class InfoBarNotifier:
    """生产实现：在 parent 顶部弹出 qfluentwidgets 的 InfoBar（可手动关闭）。"""

    def _show(self, level: str, parent: QWidget, title: str, content: str,
              duration: Optional[int]) -> InfoBar:
        factory = getattr(InfoBar, level)
        return factory(
            title=title,
            content=content,
            isClosable=True,
            duration=resolve_duration(level, duration),
            position=InfoBarPosition.TOP,
            parent=parent,
        )

    def info(self, parent: QWidget, title: str, content: str,
             duration: Optional[int] = None) -> InfoBar:
        """一般信息提示，默认 2 秒后消失。"""
        return self._show("info", parent, title, content, duration)

    def success(self, parent: QWidget, title: str, content: str,
                duration: Optional[int] = None) -> InfoBar:
        """操作成功提示，默认 2 秒后消失。"""
        return self._show("success", parent, title, content, duration)

    def warning(self, parent: QWidget, title: str, content: str,
                duration: Optional[int] = None) -> InfoBar:
        """警告提示，默认 3 秒后消失。"""
        return self._show("warning", parent, title, content, duration)

    def error(self, parent: QWidget, title: str, content: str,
              duration: Optional[int] = None) -> InfoBar:
        """错误提示，默认不自动消失（需用户手动关闭）。"""
        return self._show("error", parent, title, content, duration)
