"""
任务下拉控件

把"清空 → 填充 → 恢复选中 → 不误触发选择"这套与 qfluentwidgets ComboBox 特殊
行为纠缠的流程收在控件内部，页面只面对一个信号：
    task_selected(task_id 或 None, explicit)
explicit 为 True 表示用户在下拉里选的，False 表示程序调用 select_by_id 选的。

选项来自 ui.models.task_options.TaskOptionsModel（文本与提示的拼装都在那里）。
"""
from typing import List, Optional, Sequence

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import ComboBox
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu

from ui.models.task_options import TaskOption


class _TooltipComboMenu(ComboBoxMenu):
    """下拉菜单：按添加顺序给每个菜单项挂上对应选项的提示。

    ComboBox 每次展开都会新建菜单并逐项 addAction；菜单项的 tooltip 取自
    QAction.toolTip()（与文本不同时才设置），所以在 addAction 时补上即可。
    """

    def __init__(self, tooltips: Sequence[str], parent=None):
        super().__init__(parent)
        self._tooltips = list(tooltips)
        self._added = 0

    def addAction(self, action):
        index = self._added
        self._added += 1
        if index < len(self._tooltips) and self._tooltips[index]:
            action.setToolTip(self._tooltips[index])
        super().addAction(action)


class _TaskCombo(ComboBox):
    """带逐项提示的 ComboBox。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.item_tooltips: List[str] = []

    def _createComboMenu(self):
        return _TooltipComboMenu(self.item_tooltips, self)


# 带逐项提示的下拉（分析页的成员下拉复用）：item_tooltips 与选项一一对应
TooltipCombo = _TaskCombo


class TaskSelector(QWidget):
    """任务下拉：set_options 填充、select_by_id 程序选中、task_selected 通知选择。

    combo 属性是内部的 ComboBox：仅供布局与"模拟用户在下拉里选择"的测试使用
    （对它 setCurrentIndex 等同用户点选，会发出 explicit=True 的信号）；
    读写选项请用本类的方法。
    """

    # (task_id 或 None, explicit：用户操作为 True、程序设置为 False)
    task_selected = pyqtSignal(object, bool)

    def __init__(self, parent=None, placeholder: str = "选择监控任务"):
        super().__init__(parent)
        self._options: List[TaskOption] = []

        self.combo = _TaskCombo(self)
        self.combo.setPlaceholderText(placeholder)
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo.currentIndexChanged.connect(self._on_index_changed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.combo)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # ---------------------------------------------------------------- 填充

    def set_options(self, options: Sequence[TaskOption],
                    keep_id: Optional[str] = None) -> None:
        """整体替换选项；keep_id 在新选项中时恢复选中它，否则不选中任何项。

        全程不发 task_selected：恢复选中后要做什么（重新载入数据、选默认项）由
        调用方决定；需要"选中并通知"时随后调用 select_by_id。
        """
        self._options = list(options)
        self.combo.item_tooltips = [option.tooltip for option in self._options]
        self.combo.blockSignals(True)
        try:
            # qfluentwidgets 的 clear() 只清空条目并把当前文本置空，不会恢复占位文字；
            # 而 addItem 在加入第一项时会自动选中它（并发 currentIndexChanged）。
            # 所以填充完先显式回到 -1（ComboBox.setCurrentIndex(-1) 会重新显示占位
            # 文字且不发信号），再按需选中要恢复的项。
            self.combo.clear()
            for option in self._options:
                self.combo.addItem(option.text, userData=option.task_id)
            self.combo.setCurrentIndex(-1)
            index = self._index_of(keep_id)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        finally:
            self.combo.blockSignals(False)
        self._sync_tooltip()

    def update_option(self, index: int, option: TaskOption) -> None:
        """原地更新某一项的文本与提示（不改变选中项、不发信号）。"""
        if not 0 <= index < len(self._options):
            return
        self._options[index] = option
        self.combo.item_tooltips[index] = option.tooltip
        # setItemText 不发信号；更新的若是当前项，按钮上的文字会一并更新
        self.combo.setItemText(index, option.text)
        if index == self.combo.currentIndex():
            self._sync_tooltip()

    # ---------------------------------------------------------------- 选择

    def select_by_id(self, task_id: Optional[str], *, silent: bool = False) -> bool:
        """程序选中某任务（None 表示清除选中）。

        找到（或 task_id 为 None）时返回 True，且除非 silent，总会发出
        task_selected(task_id, False)——即使它本来就是当前项：调用方要的是
        "选中后的效果"（载入数据等）。找不到时返回 False，选中项保持不变。
        """
        index = self._index_of(task_id) if task_id is not None else -1
        if task_id is not None and index < 0:
            return False
        self.combo.blockSignals(True)
        try:
            self.combo.setCurrentIndex(index)
        finally:
            self.combo.blockSignals(False)
        self._sync_tooltip()
        if not silent:
            self.task_selected.emit(task_id, False)
        return True

    def current_id(self) -> Optional[str]:
        """当前选中任务的 ID，未选中时为 None。"""
        index = self.combo.currentIndex()
        return self._options[index].task_id if 0 <= index < len(self._options) else None

    def current_index(self) -> int:
        return self.combo.currentIndex()

    # ---------------------------------------------------------------- 查询

    def count(self) -> int:
        return len(self._options)

    def options(self) -> List[TaskOption]:
        """当前全部选项（新列表）。"""
        return list(self._options)

    def task_id_at(self, index: int) -> Optional[str]:
        return self._options[index].task_id if 0 <= index < len(self._options) else None

    def item_text(self, index: int) -> str:
        """下拉里实际显示的第 index 项文本。"""
        return self.combo.itemText(index)

    def item_tooltip(self, index: int) -> str:
        """第 index 项的提示（展开下拉时悬停该项显示）。"""
        return self._options[index].tooltip if 0 <= index < len(self._options) else ""

    def tooltip(self) -> str:
        """下拉框自身的提示：当前项的提示，未选中时为空。"""
        return self.combo.toolTip()

    # ---------------------------------------------------------------- 内部

    def _index_of(self, task_id: Optional[str]) -> int:
        for i, option in enumerate(self._options):
            if option.task_id == task_id:
                return i
        return -1

    def _sync_tooltip(self) -> None:
        index = self.combo.currentIndex()
        self.combo.setToolTip(
            self._options[index].tooltip if 0 <= index < len(self._options) else "")

    def _on_index_changed(self, index: int) -> None:
        # 程序路径（set_options/select_by_id）都屏蔽了信号，能到这里的只有用户点选
        # （或测试对 combo 直接 setCurrentIndex，与点选等价）
        self._sync_tooltip()
        task_id = self._options[index].task_id if 0 <= index < len(self._options) else None
        self.task_selected.emit(task_id, True)
