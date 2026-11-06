"""
只在控件可见时运行的定时器

页面切走（被 QStackedWidget 隐藏）或主窗口最小化/收进托盘时，后台页面没必要
继续定时查库：定时器随宿主控件的显示/隐藏自动启停，页面无需再重写
showEvent/hideEvent 去管它。
"""
from typing import Callable

from PyQt5.QtCore import QEvent, QObject, QTimer
from PyQt5.QtWidgets import QWidget


class VisibleTimer(QObject):
    """宿主控件可见时按间隔调用 callback，隐藏时停止。

    通过事件过滤器监听宿主的 Show/Hide 事件（不占用宿主的 showEvent/hideEvent，
    也不要求宿主转调）；构造时宿主已可见则立即开始计时。显示时只开始计时、
    不立即回调：显示时要做的整体刷新由页面自己负责。
    """

    def __init__(self, widget: QWidget, interval_ms: int,
                 callback: Callable[[], None]):
        """
        Args:
            widget: 宿主控件（同时作为父对象，随宿主一起销毁）
            interval_ms: 回调间隔（毫秒）
            callback: 每次到时调用的无参函数
        """
        super().__init__(widget)
        self._widget = widget
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(callback)
        widget.installEventFilter(self)
        if widget.isVisible():
            self._timer.start()

    def eventFilter(self, obj, event):
        if obj is self._widget:
            if event.type() == QEvent.Show:
                self._timer.start()
            elif event.type() == QEvent.Hide:
                self._timer.stop()
        return super().eventFilter(obj, event)

    def set_interval(self, interval_ms: int) -> None:
        """修改间隔；正在计时时从现在起按新间隔重新计时。"""
        if self._timer.interval() == interval_ms:
            return
        self._timer.setInterval(interval_ms)
        if self._timer.isActive():
            self._timer.start()

    def interval(self) -> int:
        return self._timer.interval()

    def is_active(self) -> bool:
        """当前是否在计时（宿主可见且未被 stop）。"""
        return self._timer.isActive()

    def start(self) -> None:
        """手动开始计时（宿主下次隐藏时仍会自动停止）。"""
        self._timer.start()

    def stop(self) -> None:
        """手动停止计时（宿主下次显示时仍会自动开始）。"""
        self._timer.stop()
