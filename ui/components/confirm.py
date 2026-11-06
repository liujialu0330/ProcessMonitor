"""
破坏性操作确认框：删除、清理等不可撤销操作统一使用
"""
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import MessageBox


class DestructiveConfirmBox(MessageBox):
    """不可撤销操作的确认框。

    - 按钮文案由调用方给出：项目未安装 FluentTranslator，库默认的 OK / Cancel
      在中文界面里会显示成英文。
    - 显示后焦点落在"取消"上：库在构造时把焦点给了确认按钮，顺手回车就会执行
      删除/清理；确认按钮仍保留强调色，只是不再默认获得焦点。

    用法与 MessageBox 相同：``if box.exec(): ...``，确认返回真值，取消返回假值。
    """

    def __init__(self, title: str, content: str, confirm_text: str,
                 parent: QWidget = None, cancel_text: str = "取消"):
        super().__init__(title, content, parent)
        self.yesButton.setText(confirm_text)
        self.cancelButton.setText(cancel_text)

    def showEvent(self, e):
        # 必须在父类 showEvent 之后：窗口显示/激活时会恢复到构造期记下的焦点控件
        super().showEvent(e)
        self.cancelButton.setFocus()
