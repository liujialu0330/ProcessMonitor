"""
图表主题配色助手
按当前应用主题（浅色/深色）返回历史页图表配色，供 history_page.py 在构造时与
qconfig.themeChanged 回调中统一取色，避免图表在深色主题下背景刺眼、坐标轴文字
看不清（v1.3.0 批2 A6）。
"""
from qfluentwidgets import isDarkTheme


def chart_colors() -> dict:
    """返回当前主题下的图表配色

    Returns:
        dict: 键 background/axis/text/curve/crosshair，值均为十六进制颜色字符串；
              深色主题下曲线/文字整体提亮，保证与背景的对比度达标
    """
    if isDarkTheme():
        return {
            'background': '#2b2b2b',
            'axis': '#d0d0d0',
            'text': '#d0d0d0',
            'curve': '#44D7D1',
            'crosshair': '#8a8a8a',
        }
    return {
        'background': '#ffffff',
        'axis': '#606060',
        'text': '#606060',
        'curve': '#0A8F8F',
        'crosshair': '#a0a0a0',
    }
