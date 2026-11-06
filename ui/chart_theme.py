"""
图表主题配色助手
按当前应用主题（浅色/深色）返回分析页图表配色，供 ChartView
（ui/widgets/history/chart_view.py）在构造时与 qconfig.themeChanged 回调中统一取色，
避免图表在深色主题下背景刺眼、坐标轴文字看不清。
"""
from qfluentwidgets import isDarkTheme


def chart_colors() -> dict:
    """返回当前主题下的图表配色

    Returns:
        dict: 键 background/axis/text/curve/crosshair/marker，值均为十六进制颜色字符串；
              深色主题下曲线/文字整体提亮，保证与背景的对比度达标
    """
    if isDarkTheme():
        return {
            'background': '#2b2b2b',
            'axis': '#d0d0d0',
            'text': '#d0d0d0',
            'curve': '#44D7D1',
            'crosshair': '#8a8a8a',
            'marker': '#FFB454',
        }
    return {
        'background': '#ffffff',
        'axis': '#606060',
        'text': '#606060',
        'curve': '#0A8F8F',
        'crosshair': '#a0a0a0',
        'marker': '#C2410C',
    }


# min/max 带的填充不透明度（0–255）：只作背景衬托，不抢中心线
BAND_ALPHA = 46

# 多曲线（叠加 Δ%、任务对比）的系列色：首色与单曲线同色，其余按色相拉开，
# 深色主题整体提亮
_SERIES_LIGHT = ('#0A8F8F', '#C2410C', '#6D28D9', '#2563EB', '#B45309',
                 '#BE185D', '#15803D', '#4B5563')
_SERIES_DARK = ('#44D7D1', '#FFB454', '#C4B5FD', '#7AB8FF', '#FCD34D',
                '#F9A8D4', '#86EFAC', '#CBD5E1')


def series_colors() -> tuple:
    """当前主题下的多曲线系列色（按序取用，超出时循环）。"""
    return _SERIES_DARK if isDarkTheme() else _SERIES_LIGHT
