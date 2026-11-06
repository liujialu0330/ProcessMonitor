"""
历史图表坐标轴与刻度纯计算

- MetricAxisItem：Y 轴。刻度间距按"显示单位"选取（先把可见范围换算到 KB/MB/GB/TB
  再交给 pyqtgraph 选"整洁"间距，最后换算回原始值），保证刻度在显示单位下等距且为
  1/2/5 的整洁倍数；计数类指标强制整数刻度（间距至少 1）。
- ContextDateAxisItem：X 轴，亚分钟缩放下保留时分上下文；设了相对原点时改为
  "T+mm:ss"相对时间刻度（刻度间距取 1/2/5/10/15/30 秒、1/2/5/10/15/30 分、1/2/3/6/12 小时）。
"""
import math
from datetime import datetime
from typing import List, Optional, Tuple

import pyqtgraph as pg

from ui.formatting import format_relative
from utils.metrics import KB_UNIT_DIVISORS

# 指标原始值以 KB 存储；坐标轴只转换显示文本与刻度间距，不改动绘图数据与数据库语义。
# 换算表的唯一来源是 utils.metrics，这里保留旧名供调用方使用。
DISPLAY_UNIT_DIVISORS = KB_UNIT_DIVISORS

TickLevels = List[Tuple[float, float]]


def display_divisor(display_unit: str) -> float:
    """显示单位对应的"原始值 / 显示值"换算系数；非字节类单位为 1。"""
    return DISPLAY_UNIT_DIVISORS.get(display_unit or '', 1.0)


def convert_tick_levels(
        levels: TickLevels, divisor: float, integer_only: bool = False) -> TickLevels:
    """把显示单位下的刻度层级 [(间距, 偏移), ...] 换算回原始值。

    Args:
        levels: pyqtgraph tickSpacing 在显示单位下返回的层级（由粗到细）
        divisor: 原始值 / 显示值 的换算系数
        integer_only: 计数类指标——丢弃小于 1 的层级；全部被丢弃时回落到间距 1，
            保证 Y 轴不会出现 "0.5 个" 这样的刻度
    """
    converted: TickLevels = []
    for spacing, offset in levels:
        if integer_only and spacing < 1:
            continue
        converted.append((spacing * divisor, offset * divisor))
    if integer_only and not converted:
        converted.append((1.0 * divisor, 0.0))
    return converted


def decimals_for_spacing(scaled_spacing: float) -> int:
    """按显示单位下的刻度间距决定刻度文本的小数位数（间距 ≥ 1 取整，最多 4 位）。"""
    scaled_spacing = abs(scaled_spacing)
    if not math.isfinite(scaled_spacing) or scaled_spacing <= 0 or scaled_spacing >= 1:
        return 0
    return min(4, max(0, math.ceil(-math.log10(scaled_spacing))))


class MetricAxisItem(pg.AxisItem):
    """历史指标 Y 轴：固定使用当前视图的显示单位并格式化千分位。

    轴上仍接收数据库的原始数值：刻度间距与文本都在显示单位下计算，再换算回原始值，
    因此不会影响曲线、悬停吸附或导出图片的数据契约。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.display_unit = ''
        self.integer_only = False

    def set_display_unit(self, display_unit: str, integer_only: bool = False) -> None:
        """设置当前视图固定的单位与整数刻度开关，并使旧绘图缓存失效。"""
        self.display_unit = display_unit or ''
        self.integer_only = bool(integer_only)
        self.picture = None
        self.update()

    def tickSpacing(self, minVal, maxVal, size):
        divisor = display_divisor(self.display_unit)
        levels = super().tickSpacing(minVal / divisor, maxVal / divisor, size)
        return convert_tick_levels(levels, divisor, self.integer_only)

    def tickStrings(self, values, scale, spacing):
        divisor = display_divisor(self.display_unit)
        decimals = decimals_for_spacing(spacing * scale / divisor)
        if self.integer_only:
            decimals = 0
        return [f"{value * scale / divisor:,.{decimals}f}" for value in values]


# 相对时间刻度的候选间距（秒）
RELATIVE_TICK_SPACINGS = (
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400)
# 相对时间刻度之间的最小像素距离
RELATIVE_TICK_MIN_PX = 80


def relative_tick_spacing(span_seconds: float, size_px: float) -> float:
    """可见跨度与像素宽度下，相邻刻度至少相距 RELATIVE_TICK_MIN_PX 的最小候选间距。"""
    if span_seconds <= 0 or size_px <= 0:
        return RELATIVE_TICK_SPACINGS[0]
    wanted = span_seconds * RELATIVE_TICK_MIN_PX / size_px
    for spacing in RELATIVE_TICK_SPACINGS:
        if spacing >= wanted:
            return spacing
    days = math.ceil(wanted / 86400)
    return days * 86400


class ContextDateAxisItem(pg.DateAxisItem):
    """在亚分钟缩放下保留完整时间上下文的 DateAxisItem；可切换为相对时间刻度。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 相对时间原点（epoch 秒）；None 表示显示绝对时间
        self.relative_origin: Optional[float] = None

    def set_relative_origin(self, origin: Optional[float]) -> None:
        """设置相对时间原点（None 恢复绝对时间），并使刻度缓存失效。"""
        self.relative_origin = origin
        self.picture = None
        self.update()

    def tickValues(self, minVal, maxVal, size):
        origin = self.relative_origin
        if origin is None:
            return super().tickValues(minVal, maxVal, size)
        spacing = relative_tick_spacing(maxVal - minVal, size)
        first = math.ceil((minVal - origin) / spacing) * spacing
        values = []
        value = first
        while value + origin <= maxVal and len(values) < 200:
            values.append(value + origin)
            value += spacing
        return [(spacing, values)]

    def tickStrings(self, values, scale, spacing):
        if self.relative_origin is not None:
            return [format_relative(v - self.relative_origin) for v in values]
        return self._absolute_tick_strings(values, scale, spacing)

    def _absolute_tick_strings(self, values, scale, spacing):
        # DateAxisItem 在亚秒级默认只显示「36.500」一类秒数，
        # 对性能监控读图而言缺少时分上下文。其他尺度仍交还
        # pyqtgraph 的成熟自动分级逻辑。
        if spacing >= 60:
            return super().tickStrings(values, scale, spacing)

        # 亚秒层级的刻度如果恰好都落在整秒上，就不显示「.000」毫秒尾巴
        show_ms = spacing < 1 and any(abs(v - round(v)) > 1e-6 for v in values)
        strings = []
        try:
            for value in values:
                point_time = datetime.fromtimestamp(value)
                if show_ms:
                    strings.append(point_time.strftime('%H:%M:%S.%f')[:-3])
                else:
                    strings.append(point_time.strftime('%H:%M:%S'))
        except (OverflowError, OSError, ValueError):
            return super().tickStrings(values, scale, spacing)
        return strings
