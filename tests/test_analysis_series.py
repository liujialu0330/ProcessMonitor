"""
序列显示辅助（core.analysis.series）：空洞切断掩码、累计量每秒速率、峰值抬升次数。
"""
from datetime import datetime, timedelta

import pytest

from core.analysis.series import connect_mask, count_rises, crosses_gap, per_second_rates

T0 = datetime(2026, 1, 1, 10, 0, 0)


def _t(seconds):
    return T0 + timedelta(seconds=seconds)


def test_connect_mask_breaks_only_across_gaps():
    times = [0, 1, 2, 10, 11, 20, 21]
    gaps = [(2, 10), (11, 20)]
    assert connect_mask(times, gaps) == [True, True, False, True, False, True, True]


def test_connect_mask_with_downsampled_points_still_breaks():
    """降采样只挑真实点：跨空洞的相邻绘制点两端都在空洞外，仍能判出断开"""
    assert connect_mask([0, 5, 30, 40], [(8, 25)]) == [True, False, True, True]


def test_connect_mask_without_gaps_is_all_true():
    assert connect_mask([1, 2, 3], []) == [True, True, True]
    assert connect_mask([], [(1, 2)]) == []


def test_connect_mask_accepts_unsorted_gaps_and_datetimes():
    times = [_t(0), _t(5), _t(30), _t(60)]
    gaps = [(_t(30), _t(60)), (_t(5), _t(30))]
    assert connect_mask(times, gaps) == [True, False, False, True]


def test_crosses_gap_requires_both_ends_inside():
    gaps = [(10, 20)]
    starts = [10]
    assert crosses_gap(10, 20, starts, gaps)
    assert crosses_gap(5, 25, starts, gaps)
    assert not crosses_gap(11, 25, starts, gaps)
    assert not crosses_gap(5, 19, starts, gaps)


def test_per_second_rates_skip_gaps_and_zero_intervals():
    points = [(_t(0), 0.0), (_t(10), 50.0), (_t(10), 60.0), (_t(20), 80.0),
              (_t(100), 500.0), (_t(110), 520.0)]
    rates = per_second_rates(points, [(_t(20), _t(100))])
    assert rates == [(_t(10), 5.0), (_t(20), 2.0), (_t(110), 2.0)]


def test_per_second_rates_with_epoch_seconds():
    assert per_second_rates([(0.0, 1.0), (2.0, 5.0)]) == [(2.0, 2.0)]


def test_per_second_rates_needs_two_points():
    assert per_second_rates([]) == []
    assert per_second_rates([(_t(0), 1.0)]) == []


@pytest.mark.parametrize('values, rises', [
    ([], 0),
    ([5.0], 0),
    ([1.0, 1.0, 2.0, 2.0, 3.0], 2),
    ([3.0, 2.0, 4.0], 1),
])
def test_count_rises(values, rises):
    assert count_rises(values) == rises
