"""
破坏性确认框：按钮文案为中文、显示后焦点在"取消"上，回车不会触发确认。
用真实对话框离屏 show()（不调 exec()，避免阻塞）。
"""
import pytest
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from qfluentwidgets import PrimaryPushButton

from ui.components.confirm import DestructiveConfirmBox


@pytest.fixture
def host(qapp):
    # MaskDialogBase 是宿主窗口内的遮罩子控件（无独立窗口标志），宿主不可见时它也不会显示
    widget = QWidget()
    widget.resize(800, 600)
    widget.show()
    QApplication.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _show(box):
    # 等淡入动画（200ms）跑完再操作，避免动画回调落到后续用例里
    box.show()
    QTest.qWait(300)


def _wait_hidden(box):
    # 关闭有 100ms 淡出动画，等它真正隐藏后再结束用例
    for _ in range(50):
        if not box.isVisible():
            return
        QTest.qWait(20)


def test_buttons_use_given_chinese_text(host):
    box = DestructiveConfirmBox("确认删除", "此操作不可恢复。", "删除", host)
    assert box.yesButton.text() == "删除"
    assert box.cancelButton.text() == "取消"

    custom = DestructiveConfirmBox("标题", "正文", "清理", host, cancel_text="保留")
    assert custom.yesButton.text() == "清理"
    assert custom.cancelButton.text() == "保留"


def test_focus_goes_to_cancel_after_show_and_enter_cancels(host):
    box = DestructiveConfirmBox("确认删除", "此操作不可恢复。", "删除", host)
    yes, cancel = [], []
    box.yesSignal.connect(lambda: yes.append(True))
    box.cancelSignal.connect(lambda: cancel.append(True))

    _show(box)

    assert box.focusWidget() is box.cancelButton
    # 确认按钮仍是强调色主按钮，只是不持有焦点
    assert isinstance(box.yesButton, PrimaryPushButton)

    # 键盘事件送达焦点控件：回车只会触发"取消"
    QTest.keyClick(box.focusWidget(), Qt.Key_Return)
    _wait_hidden(box)
    assert cancel == [True]
    assert yes == []
