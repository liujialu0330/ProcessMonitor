"""
空状态卡

"没有数据 / 读取失败 / 尚未选择"等状态作为内容区的一种状态显示（图标 + 标题 +
说明 + 可选操作按钮），不用浮层提示反复打断操作。三页通用。
"""
from typing import Optional, Union

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, IconWidget, StrongBodyLabel
from qfluentwidgets.common.icon import FluentIconBase

EMPTY_ICON_SIZE = 32


class EmptyStateCard(CardWidget):
    """居中的空状态卡片。

    属性 title_label / hint_label / icon_widget / action_button 供页面与测试读取。
    """

    def __init__(self, icon: Union[FluentIconBase, QIcon, str], title: str,
                 hint: str = "", action_button: Optional[QWidget] = None,
                 parent=None):
        """
        Args:
            icon: 图标（FluentIcon 等）
            title: 标题（一句话说明当前状态）
            hint: 说明（下一步怎么做）；为空时隐藏
            action_button: 可选的操作按钮，居中放在说明下方
            parent: 父控件
        """
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 48, 24, 48)
        layout.setSpacing(8)
        layout.addStretch()

        self.icon_widget = IconWidget(icon, self)
        self.icon_widget.setFixedSize(EMPTY_ICON_SIZE, EMPTY_ICON_SIZE)
        layout.addWidget(self.icon_widget, 0, Qt.AlignHCenter)

        self.title_label = StrongBodyLabel(title, self)
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)

        self.hint_label = CaptionLabel(hint, self)
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(bool(hint))
        layout.addWidget(self.hint_label)

        self.action_button = action_button
        if action_button is not None:
            layout.addSpacing(8)
            layout.addWidget(action_button, 0, Qt.AlignHCenter)

        layout.addStretch()

    def set_message(self, title: str, hint: str = "") -> None:
        """更换标题与说明（说明为空时隐藏）。"""
        self.title_label.setText(title)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))

    def set_icon(self, icon: Union[FluentIconBase, QIcon, str]) -> None:
        self.icon_widget.setIcon(icon)
