"""
跟随系统主题：主题设为"跟随系统"（Theme.AUTO）时，定时读系统深浅色，变化后重新解析主题并
发 qconfig.themeChanged，让已打开的图表、胶囊、卡片重新取色。

qfluentwidgets 只在 setTheme(AUTO) 那一刻读一次系统主题，之后系统切换深浅色时界面不跟随；
其自带的 SystemThemeListener 在 Windows 上阻塞在 darkdetect.listener 里，没有可靠的退出
方式，会卡住退出路径。这里改用主线程 QTimer 轮询（读一次注册表，开销可忽略），
与 SystemThemeListener 的处理一致：只在 AUTO 模式且解析结果变化时，改写内存中的主题
（不写配置文件，配置仍是"跟随系统"）并发 themeChanged(Theme.AUTO)。样式表的刷新与 Mica
重设由已连接 themeChanged 的设置页（setTheme → updateStyleSheet / themeChangedFinished）完成。

主窗口接入（一行）：self.theme_watcher = SystemThemeWatcher(self).start()
"""
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from qfluentwidgets import Theme, qconfig

# 轮询间隔（毫秒）：系统切换深浅色后最多这么久界面跟上
POLL_INTERVAL_MS = 5000


def _system_is_dark() -> bool:
    """读系统当前是否为深色；读不到（非 Windows、注册表缺项）时按浅色。"""
    import darkdetect
    try:
        return bool(darkdetect.isDark())
    except Exception:
        return False


class SystemThemeWatcher(QObject):
    """定时检查系统深浅色，"跟随系统"模式下变化时重新解析主题并发 themeChanged。"""

    # 本对象实际触发了一次主题切换（参数为解析后的 Theme.LIGHT / Theme.DARK）
    system_theme_changed = pyqtSignal(object)

    def __init__(self, parent: Optional[QObject] = None, *,
                 is_dark: Optional[Callable[[], bool]] = None,
                 interval_ms: int = POLL_INTERVAL_MS):
        """
        Args:
            parent: 父对象（通常是主窗口，随之销毁）
            is_dark: 读系统是否深色的函数，测试注入；默认用 darkdetect
            interval_ms: 轮询间隔（毫秒）
        """
        super().__init__(parent)
        self._is_dark = is_dark or _system_is_dark
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll)

    def start(self) -> "SystemThemeWatcher":
        """开始轮询；返回自身，便于一行接入。"""
        self._timer.start()
        return self

    def stop(self) -> None:
        self._timer.stop()

    def is_active(self) -> bool:
        return self._timer.isActive()

    def interval(self) -> int:
        return self._timer.interval()

    def poll(self) -> bool:
        """检查一次；发生了主题切换返回 True。非"跟随系统"模式或系统主题未变时什么也不做。"""
        if qconfig.themeMode.value != Theme.AUTO:
            return False
        resolved = Theme.DARK if self._is_dark() else Theme.LIGHT
        if resolved == qconfig.theme:
            return False
        # 与 qconfig.theme 的 setter 解析 AUTO 的结果一致，只是用注入的读法；不写配置文件。
        # 经 qconfig._cfg 发信号（与 qfluentwidgets 的 SystemThemeListener 相同）：
        # 载入应用配置后 _cfg 是 AppConfig 实例，它的 themeChanged 会转发到 qconfig.themeChanged
        qconfig.theme = resolved
        qconfig._cfg.themeChanged.emit(Theme.AUTO)
        self.system_theme_changed.emit(resolved)
        return True
