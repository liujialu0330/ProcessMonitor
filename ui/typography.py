"""应用级 Fluent 中文排版系统。

QFluentWidgets 会为自身控件设置字体，但原生 Qt 控件和 pyqtgraph 默认继承
QApplication 字体。Windows 中文环境中两条继承链可能分别落到雅黑与宋体，造成
同一页面里的正文、表格和图表字形明显不一致。本模块集中定义字体角色与字号，
让所有页面共享同一套排版基线。
"""
from typing import Sequence

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import BodyLabel, CaptionLabel, setFontFamilies


# Fluent 的拉丁字符优先使用 Windows 11 的 Segoe UI Variable；中文自动回退到
# Microsoft YaHei UI。后续字体兼容旧版 Windows 与精简系统，不随包分发字体文件。
UI_FONT_FAMILIES = (
    "Segoe UI Variable Text",
    "Segoe UI Variable",
    "Segoe UI",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
)

# 时间、PID、表格与图表刻度使用等宽字体，强化密集数据的纵向比较能力；核心
# 实时值仍用 Fluent UI 字体，避免大号等宽字产生过强的“终端”观感。
DATA_FONT_FAMILIES = (
    "Cascadia Mono",
    "Consolas",
    "Microsoft YaHei UI",
)


class TypeScale:
    """应用排版层级，单位为 Qt/QFluentWidgets 使用的逻辑像素。"""

    PAGE_TITLE = 24
    SECTION_TITLE = 16
    BODY = 14
    CAPTION = 12
    STAT_VALUE = 18
    CHART_TICK = 12


def _font(
        families: Sequence[str], pixel_size: int,
        weight: int = QFont.Normal) -> QFont:
    """按字体角色创建 QFont，不依赖当前控件的隐式继承链。"""
    font = QFont()
    font.setFamilies(list(families))
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    font.setKerning(True)
    return font


def ui_font(pixel_size: int = TypeScale.BODY,
            weight: int = QFont.Normal) -> QFont:
    """创建界面文字字体。"""
    return _font(UI_FONT_FAMILIES, pixel_size, weight)


def data_font(pixel_size: int = TypeScale.BODY,
              weight: int = QFont.Normal) -> QFont:
    """创建遥测数字与时间字体。"""
    font = _font(DATA_FONT_FAMILIES, pixel_size, weight)
    font.setStyleHint(QFont.Monospace)
    font.setFixedPitch(True)
    return font


def configure_application_typography(app: QApplication) -> None:
    """统一 QFluentWidgets 与原生 Qt/pyqtgraph 的全局字体基线。"""
    setFontFamilies(list(UI_FONT_FAMILIES), save=False)
    app.setFont(ui_font())


class PageTitleLabel(BodyLabel):
    """主导航页面标题：比库默认 28px 更适合高 DPI 桌面工具。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFont(ui_font(TypeScale.PAGE_TITLE, QFont.DemiBold))


class SectionTitleLabel(BodyLabel):
    """页面内区域标题。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFont(ui_font(TypeScale.SECTION_TITLE, QFont.DemiBold))


class DataLabel(BodyLabel):
    """主要遥测数值：沿用 Fluent UI 字体并提高字重。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFont(ui_font(TypeScale.BODY, QFont.DemiBold))


class DataCaptionLabel(CaptionLabel):
    """时间、PID、版本号等次要结构化数据。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFont(data_font(TypeScale.CAPTION))


class StatValueLabel(DataLabel):
    """历史统计等需要优先扫读的核心数值。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFont(ui_font(TypeScale.STAT_VALUE, QFont.DemiBold))
