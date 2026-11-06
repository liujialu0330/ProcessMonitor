"""
可搜索下拉框

qfluentwidgets 的 EditableComboBox + 子串补全（不区分大小写）。把这个库控件的几处
特殊行为收在本类里处理，调用方只面对"选项 (文本, 数据) 列表 + 当前数据"：

- EditableComboBox.clear() 只在有选中项时清文本：无选中项时用户手输的文本会残留；
- addItem 加入第一项时会自动选中它（setCurrentIndex(0) 并把文本改成该项）；
- setCurrentIndex(-1) 在当前已是 -1 时直接返回，不会清掉输入框文本；
- 回车时库的默认行为是把不匹配的手输文本加成一个没有数据的新选项；
- 选中状态由"文本与某项完全相同"推出：任何文本变化都会先把当前下标置为 -1，
  再按精确文本找回下标（见 combo_box.py 的 _onComboTextChanged）。
"""
from typing import Any, Callable, List, Optional, Sequence, Tuple

from PyQt5.QtCore import QStringListModel, Qt, pyqtSignal
from PyQt5.QtWidgets import QCompleter, QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import EditableComboBox

# 选项：(显示文本, 数据)。显示文本应互不相同，选中状态靠文本精确匹配推出
ComboItem = Tuple[str, Any]
# 纯数字输入的匹配规则：(某项数据, 输入的数字) -> 是否对应
NumberMatcher = Callable[[Any, int], bool]


class _ComboEdit(EditableComboBox):
    """拦下库的回车处理：不让不匹配的手输文本变成新选项，改由外层决定选哪一项。"""

    enter_pressed = pyqtSignal()

    def _onReturnPressed(self):  # noqa: N802 - 覆盖库方法（库在构造时按名连接 returnPressed）
        self.enter_pressed.emit()

    def completer_menu(self):
        """库内部的补全菜单（未弹出过时为 None）。"""
        return getattr(self, '_completerMenu', None)


class SearchableCombo(QWidget):
    """可搜索下拉框。

    信号：
        selected(object): 用户选中了某一项（从下拉/补全菜单选、输入了完整项文本、
            回车选中），参数为该项数据；set_items 恢复选中时不发出
        text_edited(str): 用户编辑了输入框文本（程序设置文本不发出）
        text_changed(str): 输入框文本发生任何变化（含程序设置）
    """

    selected = pyqtSignal(object)
    text_edited = pyqtSignal(str)
    text_changed = pyqtSignal(str)

    def __init__(self, parent=None, *, placeholder: str = "",
                 number_matcher: Optional[NumberMatcher] = None):
        """
        Args:
            parent: 父控件
            placeholder: 输入框占位文字
            number_matcher: 回车时输入为纯数字的匹配规则；给出时选中第一个匹配项，
                没有匹配项（或未给出规则）时保留数字原样，不套用补全首项
        """
        super().__init__(parent)
        self._number_matcher = number_matcher
        # set_items 期间的选中变化不算用户选择
        self._silent = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.line_edit = _ComboEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setMinimumWidth(0)
        self.line_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.line_edit)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # 补全模型与选项使用同一份文本：从补全菜单选中后回填的文本才能在选项里
        # 精确找到对应项，从而带出数据
        self._completer_model = QStringListModel(self)
        completer = QCompleter(self._completer_model, self)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.line_edit.setCompleter(completer)

        self.line_edit.currentIndexChanged.connect(self._on_index_changed)
        self.line_edit.textEdited.connect(self.text_edited)
        self.line_edit.textChanged.connect(self.text_changed)
        self.line_edit.enter_pressed.connect(self._on_enter)

    # ========== 选项 ==========

    def set_items(self, pairs: Sequence[ComboItem], keep: Any = None) -> None:
        """一次完成清空、填充、不选中任何项；keep 不为 None 且仍在新选项中时恢复选中它。

        结束后输入框文本为空（或为恢复选中项的文本）；需要保留手输文本的调用方
        自行在之后 set_text。本方法不发出 selected。
        """
        edit = self.line_edit
        self._silent = True
        try:
            # clear() 只在有选中项时清文本，无选中项时手输文本会残留，单独清一次
            edit.clear()
            edit.setText("")
            for text, data in pairs:
                edit.addItem(text, userData=data)
            # addItem 加第一项时会自动选中它并改写文本；setCurrentIndex(-1) 在下标
            # 已是 -1 时不做任何事，所以直接清文本：文本变化会把下标重置为 -1
            edit.setText("")
            self._completer_model.setStringList([text for text, _data in pairs])
            if keep is not None:
                index = self._index_of_data(keep)
                if index >= 0:
                    edit.setCurrentIndex(index)
        finally:
            self._silent = False

    def items(self) -> List[ComboItem]:
        """当前全部选项。"""
        edit = self.line_edit
        return [(edit.itemText(i), edit.itemData(i)) for i in range(edit.count())]

    def count(self) -> int:
        return self.line_edit.count()

    def _index_of_data(self, data: Any) -> int:
        edit = self.line_edit
        for i in range(edit.count()):
            if edit.itemData(i) == data:
                return i
        return -1

    # ========== 当前状态 ==========

    def current_data(self) -> Any:
        """当前选中项的数据；输入框文本与所选项文本不完全一致（用户改过）时返回 None。"""
        edit = self.line_edit
        index = edit.currentIndex()
        if 0 <= index < edit.count() and edit.itemText(index) == edit.text():
            return edit.itemData(index)
        return None

    def text(self) -> str:
        return self.line_edit.text()

    def set_text(self, text: str) -> None:
        """设置输入框文本；文本正好是某项时随之选中该项（发出 selected）。"""
        self.line_edit.setText(text)

    def select_data(self, data: Any) -> bool:
        """选中数据等于 data 的项（发出 selected），找不到返回 False。"""
        index = self._index_of_data(data)
        if index < 0:
            return False
        self._select_index(index)
        return True

    def focus_input(self) -> None:
        self.line_edit.setFocus()

    # ========== 内部 ==========

    def _select_index(self, index: int) -> None:
        if index == self.line_edit.currentIndex() and self.current_data() is not None:
            # 已选中且文本一致：库的 setCurrentIndex 会直接返回，这里补发一次
            self.selected.emit(self.line_edit.itemData(index))
            return
        self.line_edit.setCurrentIndex(index)

    def _on_index_changed(self, index: int) -> None:
        if self._silent or index < 0:
            return
        self.selected.emit(self.line_edit.itemData(index))

    def _on_enter(self) -> None:
        """回车：文本正好是某项时选中它；纯数字按 number_matcher 选中或原样保留；
        其余取补全列表的第一项（子串、不区分大小写）。"""
        edit = self.line_edit
        text = edit.text().strip()
        if not text:
            return
        menu = edit.completer_menu()
        if menu is not None and menu.isVisible() and menu.view.currentRow() >= 0:
            # 用户在补全菜单里用方向键挑了一项：由菜单自己回填该项
            return

        index = edit.findText(edit.text())
        if index < 0 and text.isdigit():
            if self._number_matcher is not None:
                number = int(text)
                index = next((i for i in range(edit.count())
                              if self._number_matcher(edit.itemData(i), number)), -1)
        elif index < 0:
            needle = text.casefold()
            index = next((i for i in range(edit.count())
                          if needle in edit.itemText(i).casefold()), -1)

        if menu is not None and menu.isVisible():
            menu.close()
        if index >= 0:
            self._select_index(index)
