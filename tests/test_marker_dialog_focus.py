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
