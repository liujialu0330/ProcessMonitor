"""
历史图 Y 轴刻度：按显示单位选取间距（等距、整洁数值）、计数类强制整数。
"""
import math

import pytest

from ui.components.chart_axes import (
    MetricAxisItem, convert_tick_levels, decimals_for_spacing, display_divisor
)

MB = 1024.0
GB = 1024.0 ** 2


def _is_nice(value: float) -> bool:
    """是 1/2/5 乘以 10 的整数次幂的"整洁"间距"""
    if value <= 0:
        return False
    mantissa = value / 10 ** math.floor(math.log10(value))
    return any(abs(mantissa - m) < 1e-6 for m in (1, 2, 5, 10))


def test_display_divisor():
    assert display_divisor('KB') == 1.0
    assert display_divisor('MB') == 1024.0
    assert display_divisor('GB') == 1024.0 ** 2
    assert display_divisor('') == 1.0
    assert display_divisor('个') == 1.0


def test_convert_tick_levels_scales_back_to_raw_values():
    levels = [(2.0, 0.0), (1.0, 0.5)]
    assert convert_tick_levels(levels, 1024.0) == [(2048.0, 0.0), (1024.0, 512.0)]


def test_convert_tick_levels_integer_only_drops_fractional_levels():
    levels = [(5.0, 0.0), (0.5, 0.0)]
    assert convert_tick_levels(levels, 1.0, integer_only=True) == [(5.0, 0.0)]
    # 全部被丢弃时回落到间距 1
    assert convert_tick_levels([(0.2, 0.0)], 1.0, integer_only=True) == [(1.0, 0.0)]


def test_decimals_for_spacing():
    assert decimals_for_spacing(5) == 0
    assert decimals_for_spacing(1) == 0
    assert decimals_for_spacing(0.5) == 1
    assert decimals_for_spacing(0.02) == 2
    assert decimals_for_spacing(0) == 0
    assert decimals_for_spacing(float('inf')) == 0


@pytest.mark.parametrize("unit, low, high", [
    ('MB', 185_845 * MB, 186_900 * MB),   # 约 180 GB 量级上的窄区间
    ('MB', 100 * MB, 5_000 * MB),
    ('GB', 2.0 * GB, 2.3 * GB),
    ('KB', 0.0, 100.0),
])
def test_byte_axis_ticks_are_equidistant_and_nice_in_display_unit(qapp, unit, low, high):
    """刻度在显示单位下等距，且间距是整洁数值（而不是按 KB 选间距后换算出的怪数）"""
    axis = MetricAxisItem(orientation='left')
    axis.enableAutoSIPrefix(False)
    axis.set_display_unit(unit)

    levels = axis.tickValues(low, high, 400)
    spacing, values = levels[0]
    divisor = display_divisor(unit)
    assert _is_nice(spacing / divisor)

    display_values = [v / divisor for v in values]
    steps = {round(b - a, 6) for a, b in zip(display_values, display_values[1:])}
    assert len(steps) == 1, f"刻度不等距: {display_values}"


def test_count_axis_ticks_are_integers_even_for_tiny_ranges(qapp):
    """计数类指标：范围只有 0~3 也不出现 0.5 之类的小数刻度"""
    axis = MetricAxisItem(orientation='left')
    axis.enableAutoSIPrefix(False)
    axis.set_display_unit('个', integer_only=True)

    levels = axis.tickValues(0.0, 3.0, 400)
    for spacing, values in levels:
        assert spacing >= 1
        assert all(float(v).is_integer() for v in values)
    labels = axis.tickStrings(levels[0][1], 1.0, levels[0][0])
    assert all('.' not in text for text in labels)


def test_tick_strings_use_thousands_and_unit_decimals(qapp):
    axis = MetricAxisItem(orientation='left')
    axis.set_display_unit('MB')
    assert axis.tickStrings([1024.0 * 1500, 1024.0 * 2500], 1.0, 1024.0 * 1000) == [
        '1,500', '2,500']
    # 间距小于 1 个显示单位时带小数
    assert axis.tickStrings([512.0], 1.0, 512.0) == ['0.5']


def test_date_axis_hides_ms_when_ticks_fall_on_whole_seconds(qapp):
    """亚秒层级但刻度都在整秒上：不显示「.000」；真有毫秒时才显示。"""
    from datetime import datetime

    from ui.components.chart_axes import ContextDateAxisItem

    axis = ContextDateAxisItem(orientation='bottom')
    base = datetime(2026, 11, 3, 9, 13, 35).timestamp()
    whole = axis.tickStrings([base, base + 1, base + 2], 1, 0.5)
    assert whole == ['09:13:35', '09:13:36', '09:13:37']
    fractional = axis.tickStrings([base, base + 0.5], 1, 0.5)
    assert fractional == ['09:13:35.000', '09:13:35.500']
