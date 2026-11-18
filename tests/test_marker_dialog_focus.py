"""
打点对话框：弹出后焦点必须在名称输入框（默认名称全选），否则用户直接打字会落空、
回车会按默认名称提交；文案为"打点"/"进程名 · PID"/"已在 HH:MM:SS 记录，可修改名称"，
按钮"保存"/"取消"，名称为空时不能保存。
"""
from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import SubtitleLabel

from ui.components.dialogs import MARKER_LABEL_MAX_LENGTH, MarkerDialog, marker_target_text
from ui.services.dialogs import QtDialogs


def _parent():
    parent = QWidget()
    parent.resize(800, 600)
    return parent


def test_marker_dialog_focuses_label_edit_on_show(qapp):
    parent = _parent()
    parent.show()
    dialog = MarkerDialog("基线", parent, process_name="demo.exe", pid=1234)
    dialog.show()
    qapp.processEvents()
    try:
        assert dialog.focusWidget() is dialog.label_edit
        assert dialog.label_edit.selectedText() == "基线"
    finally:
        dialog.close()
        parent.close()


def test_rename_dialog_preserves_the_full_existing_name(qapp):
    parent = _parent()
    parent.show()
    old_name = '满幅扫描完成🧪' * 12
    recorded_at = datetime(2026, 11, 18, 9, 15, 32)

    def inspect_and_save(dialog):
        dialog.show()
        qapp.processEvents()
        assert [w.text() for w in dialog.findChildren(SubtitleLabel)] == ['重命名打点']
        assert dialog.target_label.text() == 'demo.exe · PID 1234'
        assert '09:15:32' in dialog.time_label.text()
        assert dialog.focusWidget() is dialog.label_edit
        assert dialog.label_edit.selectedText() == old_name
        return True

    try:
        actual = QtDialogs(run_dialog=inspect_and_save).ask_marker_label(
            parent, old_name, recorded_at, process_name='demo.exe', pid=1234, renaming=True)
        assert actual == old_name
    finally:
        parent.close()


def test_rename_input_at_length_limit_keeps_complete_characters(qapp):
    parent = _parent()
    dialog = MarkerDialog('短名称', parent, renaming=True)
    try:
        prefix = '验' * (MARKER_LABEL_MAX_LENGTH - 1)
        dialog.label_edit.setText(prefix + '🧪')
        assert dialog.label_edit.text() == prefix
        assert dialog.get_label() == prefix
    finally:
        dialog.close()
        parent.close()


def test_rename_filters_isolated_surrogates_in_old_and_edited_names(qapp):
    parent = _parent()
    dialog = MarkerDialog('旧' + chr(0xd800) + '🧪', parent, renaming=True)
    try:
        assert dialog.get_label() == '旧🧪'
        dialog.label_edit.setText(chr(0xdc00))
        assert dialog.label_edit.text() == '' and not dialog.yesButton.isEnabled()
        dialog.label_edit.setText('新名称')
        assert dialog.yesButton.isEnabled()
    finally:
        dialog.close()
        parent.close()


def test_marker_dialog_texts_show_target_and_click_time(qapp):
    """对话框显示的是调用方传入的点击时刻，而不是对话框打开或确认的时刻"""
    parent = _parent()
    clicked = datetime(2026, 11, 6, 9, 8, 7)
    dialog = MarkerDialog("第 2 轮", parent, timestamp=clicked,
                          process_name="MPrintExp.exe", pid=4321)
    try:
        titles = [w.text() for w in dialog.findChildren(SubtitleLabel)]
        assert titles == ["打点"]
        assert dialog.timestamp == clicked
        assert dialog.target_label.text() == "MPrintExp.exe · PID 4321"
        assert not dialog.target_label.isHidden()
        assert dialog.time_label.text() == "已在 09:08:07 记录，可修改名称"
        assert dialog.label_edit.text() == "第 2 轮"
        assert (dialog.yesButton.text(), dialog.cancelButton.text()) == ("保存", "取消")
    finally:
        dialog.close()
        parent.close()


def test_marker_dialog_without_target_hides_info_line(qapp):
    """旧调用方式（不传进程名与 PID）仍可用：信息行隐藏"""
    parent = _parent()
    dialog = MarkerDialog("基线", parent)
    try:
        assert dialog.target_label.isHidden()
    finally:
        dialog.close()
        parent.close()


def test_marker_target_text_skips_missing_parts():
    assert marker_target_text("a.exe", 1) == "a.exe · PID 1"
    assert marker_target_text("", 1) == "PID 1"
    assert marker_target_text("a.exe") == "a.exe"
    assert marker_target_text() == ""


def test_marker_dialog_save_disabled_when_label_empty(qapp):
    """名称为空（或只有空白）时"保存"不可用，输入内容后恢复"""
    parent = _parent()
    dialog = MarkerDialog("基线", parent)
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


def test_enter_in_label_edit_saves(qapp):
    parent = _parent()
    parent.show()
    dialog = MarkerDialog("第 1 轮", parent)
    accepted = []
    # accept() 带淡出动画、accepted 信号要等动画结束；这里只验证回车触发了保存
    dialog.accept = lambda: accepted.append(dialog.get_label())
    dialog.show()
    qapp.processEvents()
    try:
        dialog.label_edit.setText("  建模 200 次  ")
        QTest.keyClick(dialog.label_edit, Qt.Key_Return)
        assert accepted == ["建模 200 次"]
    finally:
        dialog.close()
        parent.close()
