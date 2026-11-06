"""
qfluentwidgets 兼容封装（离屏）：
- InputDialogBase：显示后焦点交给指定输入控件并全选（MessageBoxBase 默认给"确定"）；
- CnSwitchSettingCard：构造、程序 setValue、用户切换三条路径都显示中文"开/关"；
- 模块说明里列出全部已知特殊行为（含不封装的 setMinimumHeight）。
"""
import pytest
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import FluentIcon, LineEdit, MessageBoxBase

import ui.widgets.qfw_compat as qfw_compat
from ui.widgets.qfw_compat import CnSwitchSettingCard, InputDialogBase


@pytest.fixture
def parent(qapp):
    widget = QWidget()
    widget.resize(800, 600)
    widget.show()
    yield widget
    widget.close()


class _NameDialog(InputDialogBase):
    def __init__(self, parent, *, via_ctor=False, select_all=True):
        super().__init__(parent)
        self.edit = LineEdit(self)
        self.edit.setText("默认名称")
        self.viewLayout.addWidget(self.edit)
        if via_ctor:
            return
        self.set_focus_widget(self.edit, select_all=select_all)


def test_plain_message_box_focuses_yes_button(qapp, parent):
    """对照：库的默认行为是焦点在"确定"上（这正是要封装的坑）。"""
    dialog = MessageBoxBase(parent)
    edit = LineEdit(dialog)
    dialog.viewLayout.addWidget(edit)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focusWidget() is not edit
    finally:
        dialog.close()


def test_input_dialog_focuses_and_selects_input(qapp, parent):
    dialog = _NameDialog(parent)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focus_widget() is dialog.edit
        assert dialog.focusWidget() is dialog.edit
        assert dialog.edit.selectedText() == "默认名称"
    finally:
        dialog.close()


def test_input_dialog_without_select_all(qapp, parent):
    dialog = _NameDialog(parent, select_all=False)
    dialog.edit.deselect()
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focusWidget() is dialog.edit
        assert dialog.edit.selectedText() == ""
    finally:
        dialog.close()


def test_input_dialog_focus_widget_via_constructor(qapp, parent):
    edit = LineEdit()
    dialog = InputDialogBase(parent, focus_widget=edit)
    edit.setParent(dialog)
    dialog.viewLayout.addWidget(edit)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focusWidget() is edit
    finally:
        dialog.close()


def test_input_dialog_without_focus_widget_keeps_default(qapp, parent):
    dialog = _NameDialog(parent, via_ctor=True)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focus_widget() is None
    finally:
        dialog.close()


def test_cn_switch_card_texts_on_all_paths(qapp):
    card = CnSwitchSettingCard(FluentIcon.MINIMIZE, "标题", "说明")
    try:
        # 构造时基类调用过一次 setValue（无配置项时为关）
        card.setValue(False)
        assert card.switchButton.text == qfw_compat.SWITCH_OFF_TEXT == "关"
        card.setValue(True)
        assert card.switchButton.text == qfw_compat.SWITCH_ON_TEXT == "开"
        card.switchButton.toggleChecked()          # 用户点击路径
        assert card.isChecked() is False
        assert card.switchButton.text == "关"
    finally:
        card.close()


def test_known_quirks_are_documented():
    doc = qfw_compat.__doc__
    for keyword in ("addItem", "clear()", "MessageBoxBase", "SwitchSettingCard",
                    "setMinimumHeight", "不封装"):
        assert keyword in doc, keyword
