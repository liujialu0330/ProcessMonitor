"""
进程选择器

一个搜索框兼管"按名搜索"和"直接输入 PID"：输入纯数字即视为 PID，不再另设 PID
快捷框（两个入口要双向同步，容易互相覆盖）。右侧是刷新进程列表按钮，下方是字段
级的校验错误文字。

resolve() 只给出"要监控谁"或"为什么不行"（错误类型），中文文案由页面/Presenter 映射。
"""
import re
from dataclasses import dataclass
from typing import Optional, Union

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, PushButton

from ui.services.process_query import ProcessQuery
from ui.widgets.searchable_combo import SearchableCombo

# 校验错误文字颜色（浅色 / 深色主题，取 Fluent 的错误色）
ERROR_LIGHT = QColor(196, 43, 28)
ERROR_DARK = QColor(255, 153, 164)

# 看起来是在输 PID、但不是合法正整数的输入（负数、小数、带正负号）
_NUMBER_LIKE = re.compile(r"[+-]?\d+(\.\d*)?|[+-]?\.\d+")

# resolve() 的错误类型
ERROR_EMPTY = 'empty'                  # 什么都没输入
ERROR_NO_MATCH = 'no_match'            # 文本与进程列表任何一项都不一致
ERROR_NOT_A_NUMBER = 'not_a_number'    # 形似数字但不是合法 PID
ERROR_NOT_FOUND = 'not_found'          # 输入的 PID 对应的进程不存在或无权访问
ERROR_KINDS = (ERROR_EMPTY, ERROR_NO_MATCH, ERROR_NOT_A_NUMBER, ERROR_NOT_FOUND)


@dataclass(frozen=True)
class Target:
    """要监控的进程。create_time 来自进程列表，用于任务启动时确认 PID 没有被别的
    进程复用；手动输入的 PID 没有可比对的列表信息，为 None。"""
    pid: int
    name: str
    create_time: Optional[float] = None


@dataclass(frozen=True)
class ResolveError:
    """无法确定目标进程的原因。text 为当时的输入文本，pid 只在 not_found 时有值。"""
    kind: str
    text: str = ""
    pid: Optional[int] = None


def item_text(target: Target) -> str:
    """下拉选项的显示文本：同名进程靠 PID 区分，文本互不相同。"""
    return f"{target.name} · PID {target.pid}"


class ProcessPicker(QWidget):
    """进程选择器（搜索框 + 刷新按钮 + 字段错误）。

    信号：
        selected(object): 用户选中了列表中的某个进程（Target）
        edited(): 输入内容发生变化（页面据此清掉表单错误等）
    """

    selected = pyqtSignal(object)
    edited = pyqtSignal()

    def __init__(self, process_query: ProcessQuery, parent=None):
        super().__init__(parent)
        self._query = process_query

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.combo = SearchableCombo(
            self, placeholder="搜索进程名或输入 PID",
            number_matcher=lambda target, pid: target.pid == pid)
        row.addWidget(self.combo, 1)

        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.setToolTip("重新读取进程列表")
        self.refresh_button.setFixedWidth(80)
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)

        # 校验错误就近显示在搜索框下方，内容一改即清除；不用页面顶部提示，
        # 避免一闪即逝或与运行期提示混在一起
        self.error_label = CaptionLabel(self)
        self.error_label.setTextColor(ERROR_LIGHT, ERROR_DARK)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.combo.selected.connect(self.selected)
        self.combo.text_changed.connect(self._on_text_changed)

    # ========== 列表 ==========

    def refresh(self) -> None:
        """重新读取进程列表：仍在的已选进程保持选中；没有选中项时保留手输文本。
        刷新后不会自动选中任何进程（避免误监控列表第一个进程）。"""
        keep = self.combo.current_data()
        typed = self.combo.text() if keep is None else ""
        targets = [Target(pid, name, create_time)
                   for pid, name, create_time in self._query.list_processes()]
        self.combo.set_items([(item_text(t), t) for t in targets], keep=keep)
        if typed and self.combo.current_data() is None:
            self.combo.set_text(typed)

    def targets(self):
        """当前进程列表（Target 列表，按显示顺序）。"""
        return [data for _text, data in self.combo.items()]

    # ========== 解析 ==========

    def resolve(self) -> Union[Target, ResolveError]:
        """确定要监控的进程：列表中精确选中的项优先；否则输入为纯数字时按 PID 查询；
        其余情况给出错误类型。"""
        target = self.combo.current_data()
        if target is not None:
            return target
        text = self.combo.text().strip()
        if not text:
            return ResolveError(ERROR_EMPTY)
        if text.isdigit():
            pid = int(text)
            name = self._query.name_of(pid)
            if not name:
                return ResolveError(ERROR_NOT_FOUND, text, pid)
            return Target(pid, name, None)
        if _NUMBER_LIKE.fullmatch(text):
            return ResolveError(ERROR_NOT_A_NUMBER, text)
        return ResolveError(ERROR_NO_MATCH, text)

    def current_pid(self) -> Optional[int]:
        """输入当前指向的 PID（选中项或纯数字输入），都不是时为 None；不查询进程。"""
        target = self.combo.current_data()
        if target is not None:
            return target.pid
        text = self.combo.text().strip()
        return int(text) if text.isdigit() else None

    def forget_if(self, pid: int) -> bool:
        """被测进程已退出时，若输入仍指向它，清掉并刷新进程列表：避免已不存在的
        PID 一直留在表单里，用户再点"开始监控"才报找不到。返回是否清掉了。"""
        if self.current_pid() != pid or self._query.is_alive(pid):
            return False
        self.combo.set_text("")
        self.refresh()
        return True

    # ========== 输入 ==========

    def set_text(self, text: str) -> None:
        self.combo.set_text(text)

    def text(self) -> str:
        return self.combo.text()

    def focus_search(self) -> None:
        self.combo.focus_input()

    # ========== 字段错误 ==========

    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()

    def clear_error(self) -> None:
        if self.error_label.isHidden() and not self.error_label.text():
            return
        self.error_label.clear()
        self.error_label.hide()

    def error_text(self) -> str:
        """当前显示的错误文字（未显示时为空）。"""
        return "" if self.error_label.isHidden() else self.error_label.text()

    def _on_text_changed(self, _text: str) -> None:
        # 输入一改，之前的校验错误就不再对应当前内容
        self.clear_error()
        self.edited.emit()
