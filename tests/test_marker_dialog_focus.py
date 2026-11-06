"""
打点对话框：弹出后焦点必须在标签输入框（默认标签全选），
否则用户直接打字会落空、回车会按默认标签提交。
"""
from PyQt5.QtWidgets import QWidget

from ui.components.dialogs import MarkerDialog


def test_marker_dialog_focuses_label_edit_on_show(qapp):
    parent = QWidget()
    parent.resize(800, 600)
    parent.show()
    dialog = MarkerDialog("标记 1", parent)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focusWidget() is dialog.label_edit
        assert dialog.label_edit.selectedText() == "标记 1"
    finally:
        dialog.close()
        parent.close()


def test_marker_dialog_shows_click_time(qapp):
    """对话框显示的是调用方传入的点击时刻，而不是对话框打开或确认的时刻"""
    from datetime import datetime

    parent = QWidget()
    parent.resize(800, 600)
    clicked = datetime(2026, 11, 6, 9, 8, 7)
    dialog = MarkerDialog("标记 1", parent, timestamp=clicked)
    try:
        assert dialog.timestamp == clicked
        assert dialog.time_label.text() == "时间 09:08:07（点击时刻）"
    finally:
        dialog.close()
        parent.close()


def test_marker_dialog_add_disabled_when_label_empty(qapp):
    """标签为空（或只有空白）时"添加"不可用，输入内容后恢复"""
    parent = QWidget()
    parent.resize(800, 600)
    dialog = MarkerDialog("标记 1", parent)
    try:
        assert dialog.yesButton.isEnabled()
        dialog.label_edit.clear()
        assert not dialog.yesButton.isEnabled()
        dialog.label_edit.setText("   ")
        assert not dialog.yesButton.isEnabled()
        dialog.label_edit.setText("第 3 轮")
        assert dialog.yesButton.isEnabled()
    finally:
        dialog.close()
        parent.close()
