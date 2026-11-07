"""
成员下拉（分析页工具条，进程组任务才显示）

第一项固定是"合计（默认）"，其后是各成员（按显示名排序），文字为"名称（出现过的实例数）"，
如 "chrome.exe（37）"。选项来自 AnalysisViewModel.member_options（MemberOption）。

和 TaskSelector 一样把 qfluentwidgets ComboBox 的特殊行为（addItem 自动选中第一项、
clear 不恢复占位）收在控件里：set_options / select 都不发信号，信号 member_selected
只表示用户在下拉里选了（成员键，None 为合计）。
"""
from typing import List, Optional, Sequence

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from ui.formatting import (
    MEMBER_TOTAL_OPTION, MEMBER_TOTAL_TOOLTIP, member_option_text, member_option_tooltip)
from ui.widgets.task_selector import TooltipCombo

COMBO_WIDTH = 220


class MemberSelector(QWidget):
    """成员下拉：set_options 填充、select 程序选中、member_selected 通知用户的选择。"""

    # 成员键；None 为"合计"
    member_selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keys: List[Optional[str]] = [None]
        self.combo = TooltipCombo(self)
        self.combo.setFixedWidth(COMBO_WIDTH)
        self.combo.currentIndexChanged.connect(self._on_index_changed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.combo)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.set_options([])

    # ---------------------------------------------------------------- 填充

    def set_options(self, options: Sequence, keep: Optional[str] = None) -> None:
        """整体替换选项（options 为 MemberOption）；keep 在新选项里时恢复选中它，否则回到
        "合计"。全程不发信号。"""
        self._keys = [None] + [o.key for o in options]
        texts = [MEMBER_TOTAL_OPTION] + [member_option_text(o.name, o.instances)
                                         for o in options]
        self.combo.item_tooltips = [MEMBER_TOTAL_TOOLTIP] + [
            member_option_tooltip(o.name, o.instances) for o in options]
        self.combo.blockSignals(True)
        try:
            self.combo.clear()
            for key, text in zip(self._keys, texts):
                self.combo.addItem(text, userData=key)
            self.combo.setCurrentIndex(self._index_of(keep))
        finally:
            self.combo.blockSignals(False)
        self._sync_tooltip()

    def select(self, member: Optional[str]) -> bool:
        """程序选中某成员（None 为合计），不发信号；不是选项里的成员时返回 False。"""
        index = self._index_of(member, default=-1)
        if index < 0:
            return False
        self.combo.blockSignals(True)
        try:
            self.combo.setCurrentIndex(index)
        finally:
            self.combo.blockSignals(False)
        self._sync_tooltip()
        return True

    # ---------------------------------------------------------------- 查询

    def current_member(self) -> Optional[str]:
        index = self.combo.currentIndex()
        return self._keys[index] if 0 <= index < len(self._keys) else None

    def count(self) -> int:
        """成员数（不含"合计"）。"""
        return len(self._keys) - 1

    def member_keys(self) -> List[str]:
        return [k for k in self._keys if k is not None]

    def item_texts(self) -> List[str]:
        return [self.combo.itemText(i) for i in range(self.combo.count())]

    def item_tooltip(self, index: int) -> str:
        tips = self.combo.item_tooltips
        return tips[index] if 0 <= index < len(tips) else ""

    def tooltip(self) -> str:
        return self.combo.toolTip()

    # ---------------------------------------------------------------- 内部

    def _index_of(self, member: Optional[str], default: int = 0) -> int:
        return self._keys.index(member) if member in self._keys else default

    def _sync_tooltip(self) -> None:
        self.combo.setToolTip(self.item_tooltip(self.combo.currentIndex()))

    def _on_index_changed(self, index: int) -> None:
        # 程序路径（set_options / select）都屏蔽了信号，能到这里的只有用户点选
        self._sync_tooltip()
        if 0 <= index < len(self._keys):
            self.member_selected.emit(self._keys[index])
