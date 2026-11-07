"""
WCAG 2.x 颜色对比度纯函数

供主题配色（ui/theme_tokens.py、ui/chart_theme.py）与测试校验前景色对底色的可读性：
文字要求对比度 ≥ 4.5:1，图形元素（曲线、轴线、竖线、色条）≥ 3:1。

颜色可传 QColor 或 Qt 能解析的字符串（"#rrggbb"、"#aarrggbb"、颜色名），
另支持样式表常用的 "rgba(r, g, b, a)"（a 为 0–1 小数或 0–255 整数）。
带透明度的前景色先按 alpha 合成到底色上再算对比度（屏幕上看到的就是合成后的颜色）；
底色本身须不透明，半透明的底先用 composite() 合成到它下面那一层。
"""
import re
from typing import Union

from PyQt5.QtGui import QColor

ColorLike = Union[QColor, str]

# WCAG 门槛：正文文字 / 图形元素与大号文字
TEXT_MIN_CONTRAST = 4.5
GRAPHIC_MIN_CONTRAST = 3.0

_RGBA_PATTERN = re.compile(
    r"^\s*rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)\s*$", re.I)


def to_qcolor(color: ColorLike) -> QColor:
    """把 QColor / 颜色字符串（含 rgba(...)）转成 QColor；无法解析时抛 ValueError。"""
    if isinstance(color, QColor):
        return QColor(color)
    match = _RGBA_PATTERN.match(color)
    if match:
        r, g, b = (int(match.group(i)) for i in (1, 2, 3))
        alpha_text = match.group(4)
        if alpha_text is None:
            alpha = 255
        elif "." in alpha_text or float(alpha_text) <= 1:
            alpha = round(float(alpha_text) * 255)
        else:
            alpha = int(alpha_text)
        return QColor(r, g, b, max(0, min(255, alpha)))
    result = QColor(color)
    if not result.isValid():
        raise ValueError(f"无法解析的颜色：{color!r}")
    return result


def composite(fg: ColorLike, bg: ColorLike) -> QColor:
    """把（可能半透明的）前景色按 alpha 合成到不透明底色上，返回不透明的结果色。"""
    top, bottom = to_qcolor(fg), to_qcolor(bg)
    alpha = top.alphaF()
    channels = [round(t * alpha + b * (1 - alpha)) for t, b in
                ((top.red(), bottom.red()), (top.green(), bottom.green()),
                 (top.blue(), bottom.blue()))]
    return QColor(*channels)


def _linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(color: ColorLike) -> float:
    """WCAG 相对亮度（0 = 纯黑，1 = 纯白）；忽略 alpha。"""
    c = to_qcolor(color)
    return 0.2126 * _linear(c.red()) + 0.7152 * _linear(c.green()) + 0.0722 * _linear(c.blue())


def contrast_ratio(fg: ColorLike, bg: ColorLike) -> float:
    """前景色对底色的 WCAG 对比度（1–21）。前景半透明时先合成到底色上。"""
    background = to_qcolor(bg)
    if background.alpha() != 255:
        raise ValueError("底色须不透明：半透明底色请先用 composite() 合成到它下面那一层")
    foreground = composite(fg, background) if to_qcolor(fg).alpha() != 255 else to_qcolor(fg)
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)
