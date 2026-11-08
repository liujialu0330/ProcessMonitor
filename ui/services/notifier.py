"""
统一的通知出口（Notifier）

页面与主窗口不直接调用 qfluentwidgets 的 InfoBar 或托盘气泡：提示一律经由注入的
Notifier 发出，由它按"事件级别 × 主窗口状态"选择渠道，测试换成记录型替身即可断言
"弹了什么提示"。

渠道（InfoBarNotifier.route）：
- 主窗口可见且在前台：InfoBar，挂在主窗口上（不挂在某个页面上，切页不丢），统一在右下角。
  同一个 key 最多同时显示 2 条：再来一条时关掉最旧的一条，留下最近的两条，并在最新一条上
  写"另有 N 条同类提示"（N 为已被关掉的条数；上一条的后缀随之撤掉，数字只出现在最新一条上）。
- 主窗口最小化、隐藏到托盘或不在前台：托盘气泡（受设置项"窗口不在前台时用系统通知提醒"
  控制）。常驻提示（错误、不自动消失的警告）另在主窗口上保留一条 InfoBar，用户回来时仍在；
  气泡发不出去（开关关闭、没有托盘）时，所有提示都留在主窗口上。
- 没有主窗口（无窗口模式）：只写日志。

停留时长：成功 / 一般信息 2 秒；带操作按钮 6 秒；警告 5 秒；错误常驻、需手动关闭。
调用方显式给出时长时以调用方为准（小于 0 表示不自动消失）。

任务异常结束（EVENT_TASK_ABNORMAL / EVENT_TASK_ERROR）发生在主窗口不在前台时，另调用
QApplication.alert 让任务栏图标闪烁；窗口在前台时不闪。
"""
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarManager, InfoBarPosition, PushButton

logger = logging.getLogger(__name__)

# 未显式给出时长时各级提示的默认毫秒数；错误类为 -1（不自动消失、可手动关闭）：
# InfoBar.error 库默认只停 1 秒，用户来不及看清出了什么错
DEFAULT_DURATIONS = {
    "info": 2000,
    "success": 2000,
    "warning": 5000,
    "error": -1,
}
# 带操作按钮的非错误提示：留够时间点按钮
ACTION_DURATION_MS = 6000

NOTICE_LEVELS = tuple(DEFAULT_DURATIONS)

# 同一个 key 同时最多显示的提示条数，超出的合并为"另有 N 条同类提示"
MAX_BARS_PER_KEY = 2
MERGED_SUFFIX = "（另有 {count} 条同类提示）"

# 事件名（route 的第一个参数）：只用于日志与"要不要闪任务栏"之类的判断，不影响文案
EVENT_GENERIC = "generic"
EVENT_TASK_ABNORMAL = "task.abnormal_end"
EVENT_TASK_ERROR = "task.error"
EVENT_QUICK_MARKER = "marker.quick"
EVENT_MARKER_ADDED = "marker.added"
EVENT_MARKER_UNDONE = "marker.undone"
EVENT_EXPORT_BLOCKED = "export.blocked"
EVENT_EXPORT_DONE = "export.done"
EVENT_EXPORT_FAILED = "export.failed"
EVENT_COPY_DONE = "copy.done"
EVENT_COPY_FAILED = "copy.failed"
EVENT_UPDATE_AVAILABLE = "update.available"
EVENT_STORAGE_SPACE = "storage.space"
EVENT_STORAGE_INTEGRITY = "storage.integrity"
EVENT_TRAY_HIDDEN = "tray.hidden"
# 发生时主窗口不在前台就闪任务栏的事件（任务异常结束：崩溃、被测进程退出、内部错误）
ALERT_EVENTS = frozenset({EVENT_TASK_ABNORMAL, EVENT_TASK_ERROR})

# 主窗口状态
STATE_FOREGROUND = "foreground"   # 可见且是当前激活窗口
STATE_INACTIVE = "inactive"       # 可见，但别的窗口在前台
STATE_MINIMIZED = "minimized"
STATE_HIDDEN = "hidden"           # 隐藏到托盘（或尚未显示）

# 提示级别 → 托盘气泡图标种类（TrayController.show_message 的 kind）
_TRAY_KIND = {"info": "info", "success": "info", "warning": "warning", "error": "critical"}
_LOG_LEVEL = {"info": logging.INFO, "success": logging.INFO,
              "warning": logging.WARNING, "error": logging.ERROR}
_BAR_ICON = {
    "info": InfoBarIcon.INFORMATION, "success": InfoBarIcon.SUCCESS,
    "warning": InfoBarIcon.WARNING, "error": InfoBarIcon.ERROR,
}


# Windows 气泡通知的标题 / 正文上限（系统会自行截断，这里先截好并带省略号）
BUBBLE_TITLE_MAX = 60
BUBBLE_CONTENT_MAX = 240


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def resolve_duration(level: str, duration: Optional[int], has_action: bool = False) -> int:
    """给出某级提示实际使用的显示时长（毫秒）：调用方显式给值时照用；否则错误常驻（-1），
    带操作按钮的提示 6 秒，其余取该级默认值。"""
    if level not in DEFAULT_DURATIONS:
        raise ValueError(f"未知的提示级别：{level!r}")
    if duration is not None:
        return int(duration)
    if level == "error":
        return DEFAULT_DURATIONS["error"]
    if has_action:
        return max(ACTION_DURATION_MS, DEFAULT_DURATIONS[level])
    return DEFAULT_DURATIONS[level]


@dataclass(frozen=True)
class NoticeAction:
    """提示上的一个操作按钮（如"打开文件夹"、"查看"）。"""

    text: str
    callback: Callable[[], None]


def window_state(window: QWidget) -> str:
    """主窗口当前状态（见 STATE_*）；窗口对象已销毁按隐藏处理。"""
    try:
        if not window.isVisible():
            return STATE_HIDDEN
        if window.isMinimized():
            return STATE_MINIMIZED
        if not window.isActiveWindow():
            return STATE_INACTIVE
        return STATE_FOREGROUND
    except RuntimeError:
        return STATE_HIDDEN


def _flash_taskbar(window: QWidget) -> None:
    """任务栏图标闪烁，直到用户切回窗口（duration=0）。"""
    QApplication.alert(window, 0)


@runtime_checkable
class Notifier(Protocol):
    """通知服务。

    route 是统一入口；info / success / warning / error 是它的便捷写法（event 取
    EVENT_GENERIC、parent 作为没有主窗口时的挂靠控件），行为与 route 一致。

    duration 为毫秒，None 取按级别与是否带按钮算出的默认值（见 resolve_duration），
    小于 0 表示不自动消失。返回提示对象（InfoBar）：可在其上 addWidget 追加按钮，或连接
    closedSignal（例如"同一错误仍在显示时不再叠加"的去重逻辑）；这次通知只走托盘气泡或
    只写了日志时为 None。
    """

    def route(self, event: str, level: str, title: str, content: str, *,
              action: Optional[NoticeAction] = None, key: Optional[str] = None,
              duration: Optional[int] = None, anchor: Optional[QWidget] = None) -> Any:
        """按事件级别与主窗口状态选渠道发一条通知。

        key：同类合并的键，同一 key 最多同时显示 2 条；None 不合并。
        anchor：没有主窗口（页面单独使用、测试）时提示条挂靠的控件；有主窗口时忽略。
        """
        ...

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


class NoticeBar(InfoBar):
    """在 InfoBar 上补两件事（都靠库的内部接口，失败只是位置不精确，不影响提示本身）：

    - 显示之后再追加按钮会让提示条变宽，右下角的位置要跟着重算（库只在显示那一刻按当时的
      宽度排位，按钮后加会让条的右端伸出窗口）；
    - 库给"不是第一条"的提示条另配一个"下沉"动画并放进同一个动画组，起止值要等某条提示
      关闭后才补上。关闭后 200 毫秒内新来的提示条（同类合并时关掉最旧一条紧接着再弹新的）
      拿到的是没有起止值的下沉动画，它顶掉自己的滑入动画，提示条就停在窗口右边界外。
      显示时把下沉动画的起止值补成与滑入动画一致。"""

    def showEvent(self, event):
        super().showEvent(event)
        try:
            drop, slide = self.property('dropAni'), self.property('slideAni')
            if drop is not None and slide is not None and drop.endValue() is None:
                drop.setStartValue(slide.startValue())
                drop.setEndValue(slide.endValue())
        except Exception:
            logger.debug("补提示条下沉动画的起止值失败", exc_info=True)

    def addWidget(self, widget: QWidget, stretch=0):
        super().addWidget(widget, stretch)
        self._relayout()

    def set_content(self, content: str) -> None:
        """改写正文（同类合并时撤掉上一条的"另有 N 条"）；宽度变了，位置跟着重算。"""
        self.content = content
        self._adjustText()
        self._relayout()

    def _relayout(self) -> None:
        if not self.isVisible():
            return
        self.adjustSize()
        try:
            manager = InfoBarManager.make(self.position)
            target = manager._pos(self)
            self.move(target)
            for name in ('slideAni', 'dropAni'):
                animation = self.property(name)
                if animation is not None:
                    animation.setEndValue(target)
        except Exception:
            logger.debug("重排提示条位置失败", exc_info=True)


class DetachedBar(QObject):
    """没有提示条（只发了托盘气泡）时便捷写法返回的占位对象：提供与 InfoBar 相同的
    addWidget / close / closedSignal，调用方照旧追加按钮、连接关闭信号也不会出错，
    只是这些按钮没有地方显示。"""

    closedSignal = pyqtSignal()

    def addWidget(self, widget: QWidget, stretch: int = 0) -> None:
        pass

    def close(self) -> bool:
        self.closedSignal.emit()
        return True


class _KeyGroup:
    """同一个 key 下正在显示的提示条。"""

    def __init__(self) -> None:
        self.bars: List[InfoBar] = []
        self.overflow = 0                 # 已合并掉的条数
        self.summary: Optional[NoticeBar] = None   # 带"另有 N 条"的那一条
        self.summary_content = ""         # 它不带后缀的原文


class InfoBarNotifier:
    """生产实现：按主窗口状态在 InfoBar 与托盘气泡之间选渠道。

    主窗口创建后调用 attach；没有 attach（无窗口模式、页面单独使用）时 route 只写日志，
    若调用方给了 anchor 则把提示条挂在 anchor 上。
    """

    def __init__(self, *, alert: Optional[Callable[[QWidget], None]] = None,
                 state_of: Optional[Callable[[QWidget], str]] = None):
        """alert / state_of 供测试替换（闪任务栏、判断窗口状态）。"""
        self._alert = alert or _flash_taskbar
        self._state_of = state_of or window_state
        self._window: Optional[QWidget] = None
        self._tray: Any = None
        self._background_enabled: Callable[[], bool] = lambda: True
        self._groups: Dict[str, _KeyGroup] = {}

    # ---------- 主窗口接入 ----------

    def attach(self, window: QWidget, *, tray: Any = None,
               background_enabled: Optional[Callable[[], bool]] = None) -> None:
        """接入主窗口与托盘；background_enabled 读设置项"窗口不在前台时用系统通知提醒"。"""
        self._window = window
        self._tray = tray
        if background_enabled is not None:
            self._background_enabled = background_enabled
        try:
            window.destroyed.connect(lambda *_: self._on_window_destroyed(window))
        except (AttributeError, RuntimeError, TypeError):
            pass

    def detach(self) -> None:
        self._window = None
        self._tray = None
        self._groups.clear()

    def _on_window_destroyed(self, window: QWidget) -> None:
        if self._window is window:
            self.detach()

    # ---------- 统一入口 ----------

    def route(self, event: str, level: str, title: str, content: str, *,
              action: Optional[NoticeAction] = None, key: Optional[str] = None,
              duration: Optional[int] = None, anchor: Optional[QWidget] = None
              ) -> Optional[InfoBar]:
        shown = resolve_duration(level, duration, action is not None)
        window = self._window
        if window is None:
            if anchor is None:
                logger.log(_LOG_LEVEL[level], "[通知 %s] %s：%s", event, title, content)
                return None
            return self._show_bar(anchor, level, title, content, shown, action, key)

        state = self._state_of(window)
        if state == STATE_FOREGROUND:
            return self._show_bar(window, level, title, content, shown, action, key)

        if event in ALERT_EVENTS and state in (STATE_INACTIVE, STATE_MINIMIZED):
            self._flash(window)
        bubbled = self._bubble(level, title, content)
        if bubbled and shown >= 0:
            return None
        # 气泡发不出去，或提示需要常驻：留在主窗口上，用户回来时还在
        return self._show_bar(window, level, title, content, shown, action, key)

    def _convenience(self, level: str, parent: QWidget, title: str, content: str,
                     duration: Optional[int]):
        """便捷写法：与 route 一致，但总返回带 addWidget / closedSignal 的对象（旧调用方
        直接在返回值上追加按钮）；这次只发了托盘气泡时返回 DetachedBar。"""
        bar = self.route(EVENT_GENERIC, level, title, content,
                         duration=duration, anchor=parent)
        return bar if bar is not None else DetachedBar()

    def info(self, parent: QWidget, title: str, content: str,
             duration: Optional[int] = None):
        return self._convenience("info", parent, title, content, duration)

    def success(self, parent: QWidget, title: str, content: str,
                duration: Optional[int] = None):
        return self._convenience("success", parent, title, content, duration)

    def warning(self, parent: QWidget, title: str, content: str,
                duration: Optional[int] = None):
        return self._convenience("warning", parent, title, content, duration)

    def error(self, parent: QWidget, title: str, content: str,
              duration: Optional[int] = None):
        return self._convenience("error", parent, title, content, duration)

    # ---------- 渠道 ----------

    def _flash(self, window: QWidget) -> None:
        try:
            self._alert(window)
        except Exception:
            logger.warning("闪烁任务栏失败", exc_info=True)

    def _bubble(self, level: str, title: str, content: str) -> bool:
        """发托盘气泡；开关关闭、没有托盘或发送失败返回 False。"""
        tray = self._tray
        if tray is None or not tray.is_available() or not self._background_enabled():
            return False
        try:
            tray.show_message(_clip(title, BUBBLE_TITLE_MAX), _clip(content, BUBBLE_CONTENT_MAX),
                              _TRAY_KIND[level])
        except Exception:
            logger.warning("发送托盘气泡失败", exc_info=True)
            return False
        return True

    def _make_bar(self, parent: QWidget, level: str, title: str, content: str,
                  duration: int, action: Optional[NoticeAction]) -> InfoBar:
        bar = NoticeBar(
            _BAR_ICON[level], title, content, Qt.Horizontal, True, duration,
            InfoBarPosition.BOTTOM_RIGHT, parent)
        if action is not None:
            button = PushButton(action.text)
            button.clicked.connect(lambda _=False, cb=action.callback: cb())
            bar.addWidget(button)
        bar.show()
        return bar

    def _show_bar(self, parent: QWidget, level: str, title: str, content: str,
                  duration: int, action: Optional[NoticeAction],
                  key: Optional[str]) -> InfoBar:
        if key is None:
            return self._make_bar(parent, level, title, content, duration, action)

        group = self._groups.setdefault(key, _KeyGroup())
        if len(group.bars) < MAX_BARS_PER_KEY:
            bar = self._make_bar(parent, level, title, content, duration, action)
            self._track(key, group, bar)
            return bar

        # 已满：关掉最旧的一条，保留最近的两条，最新一条带"另有 N 条"
        group.overflow += 1
        oldest = group.bars[0]
        previous_summary, previous_content = group.summary, group.summary_content
        self._forget(group, oldest)   # 先摘掉再关：关闭信号不能把计数清零
        try:
            oldest.close()
        except RuntimeError:
            pass   # 已随窗口销毁
        if previous_summary is not None and previous_summary in group.bars:
            # "另有 N 条"只写在最新一条上：上一条的后缀撤掉，免得两条各写一个数字
            try:
                previous_summary.set_content(previous_content)
            except RuntimeError:
                pass
        merged = content + MERGED_SUFFIX.format(count=group.overflow)
        bar = self._make_bar(parent, level, title, merged, duration, action)
        group.summary = bar
        group.summary_content = content
        self._track(key, group, bar)
        return bar

    def _track(self, key: str, group: _KeyGroup, bar: InfoBar) -> None:
        group.bars.append(bar)
        bar.closedSignal.connect(lambda: self._on_bar_closed(key, group, bar))

    def _forget(self, group: _KeyGroup, bar: InfoBar) -> None:
        if bar in group.bars:
            group.bars.remove(bar)
        if group.summary is bar:
            group.summary = None

    def _on_bar_closed(self, key: str, group: _KeyGroup, bar: InfoBar) -> None:
        if group.summary is bar:
            group.overflow = 0
        self._forget(group, bar)
        if not group.bars and self._groups.get(key) is group:
            del self._groups[key]
