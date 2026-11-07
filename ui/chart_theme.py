"""
图表主题配色
按当前应用主题（浅色/深色）返回分析页图表配色，供 ChartView（ui/widgets/history/chart_view.py）、
OverlayChart、OverviewStrip 在构造时与 qconfig.themeChanged 回调中统一取色。

图表背景一律透明，由所在卡片（或概览条下的窗口底）提供底色：写死一个"接近卡片"的底色
在 Mica 与悬停下都会露出一块色差明显的矩形。palette 里的 background 只用于导出图片
这类需要不透明底的场合。各前景色对所在底色的 WCAG 对比度由 tests/test_color_contrast.py
逐项断言（文字 ≥ 4.5:1，图形元素 ≥ 3:1）。颜色源头见 ui/theme_tokens.py。
"""
from typing import Dict, Optional

from PyQt5.QtCore import Qt
from qfluentwidgets import isDarkTheme

from ui import theme_tokens as tokens

# min/max 带的填充不透明度（0–255）：只作背景衬托，不抢中心线
BAND_ALPHA = 46
# 空洞阴影的不透明度（0–255）
GAP_SHADE_ALPHA = 36
# 概览条选区填充的不透明度（0–255）
SELECTION_ALPHA = 40
# 横向网格线的不透明度（0–1，pyqtgraph showGrid 的 alpha，取轴线色）：只作辅助参考
GRID_ALPHA = 0.18

# 两套 palette（下标 0 浅色、1 深色）。键的含义：
#   background      导出图片的不透明底（= 卡片常态底色）；屏幕上的图表背景透明
#   surface / surface_hover / window  对比度参照：卡片常态、卡片悬停、窗口底（概览条所在）
#   axis            坐标轴线与刻度线（图形）
#   text            刻度文字、轴标题、空洞说明（文字）
#   curve           单曲线、min/max 带的基色（图形；与迷你图同色，按文字门槛校验）
#   crosshair       悬停十字线（图形）
#   marker          打点竖线与编号徽标（徽标是文字）
#   system_marker   暂停/恢复的底部短竖线（图形）
#   crash_marker / restart_marker / recover_marker
#                   被测进程异常退出 / 重启续接 / 工具重启后续采的点划竖线与标签（标签是文字）
#   gap_shade       空洞阴影的基色（按 GAP_SHADE_ALPHA 半透明叠加）
#   selection       概览条选区边线（选中色条，图形；填充按 SELECTION_ALPHA）
#   overview_curve  概览条走势线（图形）
_PALETTES = tuple({
    'background': tokens.SURFACE[i],
    'surface': tokens.SURFACE[i],
    'surface_hover': tokens.SURFACE_HOVER[i],
    'window': tokens.WINDOW[i],
    'axis': ('#606060', '#d0d0d0')[i],
    'text': ('#606060', '#d0d0d0')[i],
    'curve': tokens.DATA[i],
    'crosshair': ('#767676', '#8a8a8a')[i],
    'marker': tokens.ATTENTION[i],
    'system_marker': ('#858585', '#8f8f8f')[i],
    'crash_marker': tokens.EVENT_MARKER_COLORS['crash'][i],
    'restart_marker': tokens.EVENT_MARKER_COLORS['restart'][i],
    'recover_marker': tokens.EVENT_MARKER_COLORS['recover'][i],
    'gap_shade': ('#606060', '#d0d0d0')[i],
    'selection': tokens.DATA[i],
    'overview_curve': ('#6e6e6e', '#9e9e9e')[i],
} for i in (0, 1))

# 多曲线（叠加 Δ%、任务对比）的系列色：首色与单曲线同色，其余按色相拉开，
# 深色主题整体提亮；每色对卡片底 ≥ 3:1
_SERIES_LIGHT = (tokens.DATA[0], '#C2410C', '#6D28D9', '#2563EB', '#B45309',
                 '#BE185D', '#15803D', '#4B5563')
_SERIES_DARK = (tokens.DATA[1], '#FFB454', '#C4B5FD', '#7AB8FF', '#FCD34D',
                '#F9A8D4', '#86EFAC', '#CBD5E1')


# 绘图区（pyqtgraph GraphicsView）透明：背景画刷置空之外，视口还会按调色板自动填充
# （Qt 默认浅灰），只在祖先样式表恰好写了 background: transparent 时才看不出来，
# 所以在控件自身上声明，不依赖所在页面
TRANSPARENT_VIEW_QSS = "background: transparent; border: none;"


def make_transparent(view) -> None:
    """把 pyqtgraph 的 PlotWidget / GraphicsView 背景设为透明，底色交给所在卡片或窗口。"""
    view.setBackground(None)
    if view.styleSheet() != TRANSPARENT_VIEW_QSS:
        view.setStyleSheet(TRANSPARENT_VIEW_QSS)


def is_transparent(view) -> bool:
    """绘图区背景画刷是否透明。"""
    brush = view.backgroundBrush()
    return brush.style() == Qt.NoBrush or brush.color().alpha() == 0


def chart_palette(dark: Optional[bool] = None) -> Dict[str, str]:
    """指定主题（None 为当前主题）的图表配色副本，值均为 "#rrggbb"。"""
    if dark is None:
        dark = isDarkTheme()
    return dict(_PALETTES[1 if dark else 0])


def chart_colors() -> Dict[str, str]:
    """当前主题下的图表配色（键见本模块 _PALETTES 的说明）。"""
    return chart_palette()


def series_colors(dark: Optional[bool] = None) -> tuple:
    """当前主题（或指定主题）下的多曲线系列色（按序取用，超出时循环）。"""
    if dark is None:
        dark = isDarkTheme()
    return _SERIES_DARK if dark else _SERIES_LIGHT
