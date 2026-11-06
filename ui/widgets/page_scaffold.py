"""
页面骨架

主导航各页共用的外壳：透明无边框的纵向滚动区 + 页头（标题、可选副标题、
右侧命令区）+ 内容布局。页面继承本类后只需往 content_layout 里放区块、
往页头命令区放按钮，边距与间距统一在这里定义。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel

from ui.typography import PageTitleLabel

# 页边距与区块间距（4px 网格）
PAGE_MARGIN = 24
SECTION_SPACING = 24
# 页头：标题与副标题之间、命令区按钮之间
HEADER_TITLE_SPACING = 2
HEADER_COMMAND_SPACING = 8


class PageScaffold(QScrollArea):
    """透明滚动外壳 + 页头 + 内容布局。

    属性：
        container: 滚动区内的容器控件
        title_label / subtitle_label: 页标题与副标题（副标题为空时隐藏）
        header_layout: 页头整行（标题组 | 伸缩 | 命令区）
        command_layout: 页头右侧命令区，按添加顺序从左到右排列
        content_layout: 页头之下的内容区（纵向，区块间距 SECTION_SPACING）
    """

    def __init__(self, title: str, subtitle: str = "", parent=None, *,
                 spacing: int = SECTION_SPACING):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 去掉边框与底色，透出主窗口的 Fluent 背景
        self.setStyleSheet("QScrollArea{background: transparent; border: none}")
        self.viewport().setStyleSheet("background: transparent")

        self.container = QWidget()
        self.container.setStyleSheet("background: transparent")
        self.setWidget(self.container)

        self.content_layout = QVBoxLayout(self.container)
        self.content_layout.setContentsMargins(
            PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        self.content_layout.setSpacing(spacing)

        self.header_layout = QHBoxLayout()
        self.header_layout.setSpacing(HEADER_COMMAND_SPACING)

        title_group = QVBoxLayout()
        title_group.setSpacing(HEADER_TITLE_SPACING)
        self.title_label = PageTitleLabel(title)
        title_group.addWidget(self.title_label)
        self.subtitle_label = CaptionLabel(subtitle)
        self.subtitle_label.setVisible(bool(subtitle))
        title_group.addWidget(self.subtitle_label)
        self.header_layout.addLayout(title_group)
        self.header_layout.addStretch(1)

        self.command_layout = QHBoxLayout()
        self.command_layout.setSpacing(HEADER_COMMAND_SPACING)
        # 命令区贴顶与标题行对齐：有副标题时不被挪到整个页头的垂直中间
        self.header_layout.addLayout(self.command_layout)
        self.header_layout.setAlignment(self.command_layout, Qt.AlignTop)
        self.content_layout.addLayout(self.header_layout)

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)

    def set_subtitle(self, text: str) -> None:
        """设置副标题；空文本时隐藏副标题行。"""
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))

    def add_header_widget(self, widget: QWidget) -> QWidget:
        """在页头命令区末尾追加一个控件（通常是按钮），返回该控件。"""
        self.command_layout.addWidget(widget)
        return widget

    def add_section(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """在内容区末尾追加一个区块，返回该控件。"""
        self.content_layout.addWidget(widget, stretch)
        return widget
