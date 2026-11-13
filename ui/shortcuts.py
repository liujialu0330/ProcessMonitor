"""窗口级快捷键；页面命令只分派给当前页面，文本编辑保留自身的撤销与复制。"""
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QShortcut
from qfluentwidgets import Flyout, FlyoutAnimationType, FluentIcon


SHORTCUT_HELP = (
    'Ctrl+1 / Ctrl+2 / Ctrl+3　监控 / 分析 / 设置\n'
    'Ctrl+M　为运行中的任务快速打点\n'
    'Ctrl+Z　撤销最近一次打点（30 秒内）\n'
    'F5　刷新当前页面\nCtrl+F　查找进程或任务\n'
    'Ctrl+E　导出分析报告\nCtrl+Shift+C　复制报告图片\n'
    'Alt+1…7　切换分析时间范围\nF1　显示本帮助'
)


class WindowShortcuts:
    def __init__(self, window):
        self.window = window
        self.items = []
        for key, callback in (
            ('Ctrl+1', window.show_monitor_page),
            ('Ctrl+2', lambda: window.switchTo(window.analysis_page)),
            ('Ctrl+3', lambda: window.switchTo(window.setting_page)),
            ('Ctrl+M', window.quick_marker),
            ('Ctrl+E', lambda: self._analysis('export_report')),
            ('Ctrl+Shift+C', lambda: self._analysis('copy_report_image')),
            ('F1', self.show_help),
        ):
            self.add(key, callback)
        # 监控页已有 F5 / Ctrl+F，按页面显隐启停；这里仅为分析页补齐。
        self.analysis_items = [
            self.add('F5', lambda: self._analysis('refresh')),
            self.add('Ctrl+F', lambda: self._analysis('focus_search')),
        ]
        self.range_items = [self.add(f'Alt+{i + 1}',
                                     lambda index=i: self._analysis('select_range_index', index))
                            for i in range(7)]
        window.stackedWidget.currentChanged.connect(self.sync)
        self.sync()

    def add(self, key, callback):
        shortcut = QShortcut(QKeySequence(key), self.window)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.activated.connect(callback)
        self.items.append(shortcut)
        return shortcut

    def sync(self, *_args):
        active = self.window._page_key() == 'analysis'
        for shortcut in [*self.analysis_items, *self.range_items]:
            shortcut.setEnabled(active)

    def _analysis(self, name, *args):
        if self.window._page_key() != 'analysis':
            return
        callback = getattr(self.window.analysis_page, name, None)
        if callback:
            callback(*args)

    def show_help(self):
        Flyout.create(icon=FluentIcon.INFO, title='键盘快捷键', content=SHORTCUT_HELP,
                      target=self.window.navigationInterface, parent=self.window,
                      isClosable=True, aniType=FlyoutAnimationType.FADE_IN)
