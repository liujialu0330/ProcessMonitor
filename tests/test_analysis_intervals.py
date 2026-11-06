"""
打点区间统计（core/analysis/intervals.py）的单元测试
"""
import math
from datetime import datetime, timedelta

import pytest

from core.analysis.intervals import (
    Boundary,
    RoundDelta,
    baseline_delta,
    mean_round_delta,
    round_deltas,
    split_intervals,
    value_near,
)

T0 = datetime(2026, 1, 1, 10, 0, 0)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def series(values, step=10.0, start=0.0):
    """等间隔点：第 i 个点在 start + i × step 秒"""
    return [(at(start + i * step), float(v)) for i, v in enumerate(values)]


def mark(seconds, label, kind='user'):
    return Boundary(at(seconds), label, kind)


# ---------- split_intervals ----------

def test_no_markers_single_interval():
    pts = series([100, 105, 103, 110])   # 0..30s
    [iv] = split_intervals(pts, [])
    assert (iv.index, iv.label) == (0, '结束')
    assert (iv.start, iv.end) == (at(0), at(30))
    assert math.isclose(iv.duration_seconds, 30.0)
    assert (iv.start_value, iv.end_value, iv.peak) == (100.0, 110.0, 110.0)
    assert math.isclose(iv.delta, 10.0)
    assert math.isclose(iv.rate_per_hour, 10.0 / 30 * 3600)
    assert iv.sample_count == 4
    assert iv.delta_vs_previous is None


def test_explicit_start_end_only():
    pts = series([1, 2, 3, 4, 5])        # 0..40s
    ivs = split_intervals(pts, [mark(-10, '任务开始', 'start'), mark(50, '任务结束', 'end')])
    assert len(ivs) == 1
    iv = ivs[0]
    assert (iv.start, iv.end, iv.label) == (at(-10), at(50), '任务结束')
    assert math.isclose(iv.duration_seconds, 60.0)
    assert iv.sample_count == 5
    assert math.isclose(iv.rate_per_hour, 4.0 / 60 * 3600)


def test_custom_first_last_labels():
    pts = series([1, 2, 3])
    ivs = split_intervals(pts, [mark(10, '基线', 'baseline')], first_label='S', last_label='E')
    assert [iv.label for iv in ivs] == ['基线', 'E']


def test_multiple_markers_and_delta_vs_previous():
    # 0..90s 共 10 点；基线 20s，第 1 轮 50s，第 2 轮 80s
    pts = series([100, 102, 110, 112, 115, 130, 131, 133, 140, 141])
    marks = [mark(50, '第 1 轮'), mark(20, '基线', 'baseline'), mark(80, '第 2 轮')]   # 乱序传入
    ivs = split_intervals(pts, marks)
    assert [iv.label for iv in ivs] == ['基线', '第 1 轮', '第 2 轮', '结束']
    assert [iv.index for iv in ivs] == [0, 1, 2, 3]
    # 左闭右开：20s 的点归基线之后的区间
    assert [iv.sample_count for iv in ivs] == [2, 3, 3, 2]
    assert [(iv.start_value, iv.end_value) for iv in ivs] == [
        (100.0, 102.0), (110.0, 115.0), (130.0, 133.0), (140.0, 141.0)]
    assert [iv.delta for iv in ivs] == [2.0, 5.0, 3.0, 1.0]
    assert [iv.delta_vs_previous for iv in ivs] == [None, 3.0, -2.0, -2.0]
    assert [iv.peak for iv in ivs] == [102.0, 115.0, 133.0, 141.0]
    assert [iv.duration_seconds for iv in ivs] == [20.0, 30.0, 30.0, 10.0]
    assert math.isclose(ivs[1].rate_per_hour, 5.0 / 30 * 3600)
    # 区间首尾相接、覆盖整段
    for a, b in zip(ivs, ivs[1:]):
        assert a.end == b.start
    assert (ivs[0].start, ivs[-1].end) == (at(0), at(90))


def test_last_interval_includes_end_point():
    pts = series([1, 2, 3, 4])   # 0..30s
    ivs = split_intervals(pts, [mark(10, 'A'), mark(30, 'B')])
    # 末点恰在最后一个打点上：不补终点，最后一个区间闭合包含它
    assert [iv.label for iv in ivs] == ['A', 'B']
    assert [iv.sample_count for iv in ivs] == [1, 3]
    assert ivs[-1].end_value == 4.0


def test_marker_before_first_point():
    pts = series([10, 20, 30], start=100)   # 100..120s
    ivs = split_intervals(pts, [mark(50, '基线', 'baseline'), mark(110, '第 1 轮')])
    # 早于首点的打点自己充当起点，不补空的头段
    assert [iv.label for iv in ivs] == ['第 1 轮', '结束']
    assert ivs[0].start == at(50)
    assert math.isclose(ivs[0].duration_seconds, 60.0)
    assert ivs[0].sample_count == 1
    assert ivs[0].delta == 0.0
    assert ivs[0].rate_per_hour == 0.0


def test_marker_after_last_point():
    pts = series([10, 20, 30])   # 0..20s
    ivs = split_intervals(pts, [mark(10, '第 1 轮'), mark(60, '第 2 轮')])
    assert [iv.label for iv in ivs] == ['第 1 轮', '第 2 轮']
    assert ivs[-1].end == at(60)
    assert ivs[-1].sample_count == 2
    assert ivs[-1].delta == 10.0
    assert math.isclose(ivs[-1].rate_per_hour, 10.0 / 50 * 3600)


def test_empty_interval_fields_are_none():
    pts = series([1, 2]) + series([5, 6], start=100)    # 0,10,100,110
    ivs = split_intervals(pts, [mark(20, 'A'), mark(50, 'B')])
    assert [iv.label for iv in ivs] == ['A', 'B', '结束']
    empty = ivs[1]
    assert empty.sample_count == 0
    assert math.isclose(empty.duration_seconds, 30.0)
    for field in ('start_value', 'end_value', 'delta', 'rate_per_hour', 'peak', 'delta_vs_previous'):
        assert getattr(empty, field) is None
    # 上一区间为空时，下一区间也无从比较
    assert ivs[2].delta == 1.0
    assert ivs[2].delta_vs_previous is None


def test_duplicate_marker_timestamps_keep_zero_length_interval():
    pts = series([1, 2, 3, 4])   # 0..30s
    ivs = split_intervals(pts, [mark(10, 'A'), mark(10, 'B')])
    assert [iv.label for iv in ivs] == ['A', 'B', '结束']
    zero = ivs[1]
    assert zero.duration_seconds == 0.0
    assert zero.sample_count == 0
    assert zero.rate_per_hour is None


def test_duplicate_point_timestamps():
    pts = [(at(0), 1.0), (at(10), 5.0), (at(10), 7.0), (at(20), 9.0)]
    ivs = split_intervals(pts, [mark(10, 'A')])
    assert [iv.sample_count for iv in ivs] == [1, 3]
    # 同一时刻的点保持传入顺序
    assert ivs[1].start_value == 5.0
    assert ivs[1].peak == 9.0


def test_unsorted_points():
    pts = list(reversed(series([1, 2, 3, 4])))
    [iv] = split_intervals(pts, [])
    assert (iv.start_value, iv.end_value) == (1.0, 4.0)


def test_zero_duration_single_point():
    [iv] = split_intervals([(at(0), 5.0)], [mark(0, 'X', 'start'), mark(0, 'Y', 'end')])
    assert iv.sample_count == 1
    assert iv.delta == 0.0
    assert iv.rate_per_hour is None


def test_points_outside_explicit_start_end_are_ignored():
    pts = series([1, 2, 3, 4, 5])   # 0..40s
    [iv] = split_intervals(pts, [mark(10, 'S', 'start'), mark(30, 'E', 'end')])
    assert iv.sample_count == 3
    assert (iv.start_value, iv.end_value) == (2.0, 4.0)


@pytest.mark.parametrize('points, boundaries', [
    ([], []),
    ([], [mark(0, 'A')]),
    ([(at(0), 1.0)], []),     # 单点：起点即终点，补齐后仍不足两个边界
])
def test_not_enough_boundaries(points, boundaries):
    assert split_intervals(points, boundaries) == []


def test_no_points_but_markers_give_empty_intervals():
    ivs = split_intervals([], [mark(0, 'A'), mark(10, 'B')])
    assert len(ivs) == 1
    assert ivs[0].sample_count == 0
    assert ivs[0].delta is None


# ---------- value_near ----------

def test_value_near_odd_n():
    pts = series([10, 50, 20, 30, 40, 1000, 5])   # 0..60s
    # 离 25s 最近的 5 个：20s(20)、30s(30)、10s(50)、40s(40)、0s(10)
    assert value_near(pts, at(25), n=5) == 30.0


def test_value_near_even_n():
    pts = series([10, 20, 30, 40])
    # 离 15s 最近的 2 个：10s(20)、20s(30) → 中位数 25
    assert value_near(pts, at(15), n=2) == 25.0


def test_value_near_tie_prefers_earlier():
    pts = series([10, 20, 30])
    # 10s 与 20s 到 15s 等距：n=1 取较早的点
    assert value_near(pts, at(15), n=1) == 20.0


def test_value_near_resists_spike():
    pts = series([100, 100, 9999, 100, 100])
    assert value_near(pts, at(20), n=5) == 100.0


def test_value_near_fewer_points_than_n():
    assert value_near(series([1, 3]), at(0), n=5) == 2.0


def test_value_near_duplicate_timestamps():
    pts = [(at(0), 1.0), (at(10), 4.0), (at(10), 6.0), (at(20), 100.0)]
    assert value_near(pts, at(10), n=2) == 5.0


def test_value_near_after_only():
    pts = series([1, 2, 3, 4, 5, 6])   # 0..50s
    # 只取 30s 及之后：4、5、6
    assert value_near(pts, at(30), n=5, after_only=True) == 5.0
    assert value_near(pts, at(60), after_only=True) is None


def test_value_near_empty_and_invalid_n():
    assert value_near([], at(0)) is None
    with pytest.raises(ValueError):
        value_near(series([1]), at(0), n=0)


# ---------- baseline_delta ----------

def test_baseline_delta():
    pts = series([100, 101, 99, 100, 102, 120, 130])   # 0..60s
    # 离 20s 最近 5 个：20(99)、10(101)、30(100)、0(100)、40(102) → 中位数 100
    assert baseline_delta(pts, at(20)) == 30.0


def test_baseline_delta_uses_latest_point_even_if_unsorted():
    pts = list(reversed(series([100, 100, 100, 150])))
    assert baseline_delta(pts, at(0), n=1) == 50.0


def test_baseline_delta_empty():
    assert baseline_delta([], at(0)) is None


# ---------- 逐轮增量与每轮均值 ----------

def test_round_deltas_pairs_adjacent_boundaries_in_time_order():
    values = {T0: 10.0, T0 + timedelta(minutes=5): 13.0, T0 + timedelta(minutes=9): 18.0}
    calls = []

    def value_at(ts):
        calls.append(ts)
        return values[ts]

    boundaries = [
        Boundary(T0 + timedelta(minutes=9), "第2轮", 'user'),
        Boundary(T0, "基线", 'baseline'),
        Boundary(T0 + timedelta(minutes=5), "第1轮", 'user'),
    ]
    rounds = round_deltas(boundaries, value_at)
    assert rounds == [
        RoundDelta(0, "第1轮", T0, T0 + timedelta(minutes=5), 10.0, 13.0, 3.0),
        RoundDelta(1, "第2轮", T0 + timedelta(minutes=5), T0 + timedelta(minutes=9),
                   13.0, 18.0, 5.0),
    ]
    assert sorted(calls) == sorted(values)   # 每个边界只取一次值
    assert mean_round_delta(rounds) == 4.0


def test_round_deltas_missing_value_gives_none_delta():
    rounds = round_deltas(
        [Boundary(T0, "a", 'user'), Boundary(T0 + timedelta(minutes=1), "b", 'user')],
        lambda ts: None if ts == T0 else 5.0)
    assert rounds[0].delta is None and rounds[0].end_value == 5.0
    assert mean_round_delta(rounds) is None


def test_round_deltas_needs_two_boundaries():
    assert round_deltas([Boundary(T0, "a", 'user')], lambda ts: 1.0) == []
    assert round_deltas([], lambda ts: 1.0) == []


def test_mean_round_delta_skips_none_and_accepts_interval_stats():
    pts = [(T0 + timedelta(minutes=i), float(i * i)) for i in range(7)]
    intervals = split_intervals(pts, [Boundary(T0 + timedelta(minutes=3), "m", 'user')])
    # [0,3) 增量 4 - 0 = 4；[3,6] 增量 36 - 9 = 27
    assert mean_round_delta(intervals) == pytest.approx((4 + 27) / 2)
    assert mean_round_delta([]) is None
