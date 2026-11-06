"""
SparklineWidget 单元测试

覆盖抽稀（成对合并、覆盖任务开始至今）、纵轴最小跨度，以及 paintEvent 四态
（0点/1点/平线/常规）不崩溃、不留下异常痕迹。PyQt5 虚方法（paintEvent/eventFilter）
里的异常在 pytest 表面不一定会红——eventFilter 内异常若无防护，既不保证抛出
AssertionError 也不保证进程干净退出，"测试没红"不能作为验证依据，必须额外
捕获 stderr 断言不含 Traceback 文本。
"""
import contextlib
import io

import pytest

from ui.components.sparkline import (
    MIN_SPAN_BYTES_KB, MIN_SPAN_COUNT, MIN_SPAN_PERCENT, SparklineWidget,
    TrendDownsampler, compute_y_range, merge_pairs, min_span
)
from utils.metrics import MetricType, min_span_for


def test_merge_pairs_averages_adjacent_values():
    """相邻两点取平均；奇数个时末尾落单点原样保留"""
    assert merge_pairs([1.0, 3.0, 5.0, 7.0]) == [2.0, 6.0]
    assert merge_pairs([1.0, 3.0, 10.0]) == [2.0, 10.0]
    assert merge_pairs([]) == []


def test_downsampler_bounded_and_covers_whole_history():
    """点数始终有界，且曲线始终覆盖"开始至今"：首尾趋势不丢"""
    sampler = TrendDownsampler(max_points=8)
    for i in range(1000):
        sampler.append(float(i))

    series = sampler.series()
    assert len(series) <= 8
    # 覆盖全程：最早的点接近起点，最新的点接近当前值
    assert series[0] < 300
    assert series[-1] > 700
    # 单调上涨的数据抽稀后依然单调
    assert series == sorted(series)


def test_downsampler_stride_doubles_on_merge():
    """达到上限后全体成对合并、每个点覆盖的采集次数翻倍"""
    sampler = TrendDownsampler(max_points=4)
    for i in range(4):
        sampler.append(float(i))
    assert sampler.stride == 2
    assert sampler.series() == [0.5, 2.5]

    sampler.append(4.0)
    sampler.append(6.0)
    assert sampler.series() == [0.5, 2.5, 5.0]


def test_downsampler_shows_pending_latest_sample():
    """尚未攒满一个点的最新采集也反映在曲线末端"""
    sampler = TrendDownsampler(max_points=4)
    for i in range(4):
        sampler.append(float(i))
    sampler.append(100.0)
    assert sampler.series()[-1] == 100.0


def test_min_span_rules_by_metric_kind():
    """字节类 max(当前值1%, 64KB)；计数类至少 4；百分比至少 1 个百分点"""
    assert min_span(MetricType.MEMORY_PRIVATE, 1000.0) == MIN_SPAN_BYTES_KB
    big = 100 * 1024 * 1024.0  # 100 GB（KB 单位）
    assert min_span(MetricType.MEMORY_PRIVATE, big) == pytest.approx(big * 0.01)
    assert min_span(MetricType.NUM_GDI_OBJECTS, 5000.0) == MIN_SPAN_COUNT
    assert min_span(MetricType.CPU_PERCENT, 50.0) == MIN_SPAN_PERCENT
    assert min_span(None, 5.0) == 0.0


def test_min_span_delegates_to_metric_registry():
    """迷你图不再自带指标分类表，规则统一由 utils.metrics.min_span_for 按 kind 派生"""
    for metric in (MetricType.MEMORY_PERCENT, MetricType.IO_READ_BYTES,
                   MetricType.CPU_SYSTEM_TIME, MetricType.NUM_HANDLES, "unknown"):
        for reference in (0.0, 80.0, 1e6):
            assert min_span(metric, reference) == min_span_for(metric, reference)


def test_compute_y_range_expands_tiny_jitter_to_minimum_span():
    """微小抖动不被拉伸成满幅：跨度不足下限时围绕中点扩展"""
    low, high = compute_y_range([100000.0, 100010.0, 100005.0], MetricType.MEMORY_PRIVATE)
    assert high - low == pytest.approx(100005.0 * 0.01)  # 当前值的 1%
    assert low < 100000.0 < 100010.0 < high

    low, high = compute_y_range([100.0, 101.0], MetricType.NUM_THREADS)
    assert high - low == pytest.approx(MIN_SPAN_COUNT)


def test_compute_y_range_keeps_real_wide_range():
    """真实的大幅变化不受下限影响"""
    low, high = compute_y_range([10.0, 500.0], MetricType.NUM_THREADS)
    assert (low, high) == (10.0, 500.0)


def test_append_bounded_by_max_points(qapp):
    """追加点数远超上限后，绘制序列仍有界"""
    widget = SparklineWidget(max_points=6)
    for i in range(200):
        widget.append(float(i))
    assert len(widget.series()) <= 6


def test_clear_empties(qapp):
    """clear 后绘制序列为空"""
    widget = SparklineWidget(max_points=6)
    widget.append(1.0)
    widget.append(2.0)
    assert len(widget.series()) == 2

    widget.clear()
    assert widget.series() == []


def test_paint_no_crash(qapp):
    """
    paintEvent 覆盖四态（0点/1点/平线/常规）均不抛异常、不留 Traceback。

    widget.grab() 会同步触发一次 paintEvent。用 contextlib.redirect_stderr
    捕获期间的 stderr 输出，断言其中不含 "Traceback" 文本，防止"pytest 没红"
    掩盖真实的绘制异常。
    """
    widget = SparklineWidget(metric_type=MetricType.MEMORY_PRIVATE)
    widget.resize(120, 36)

    stderr_capture = io.StringIO()
    with contextlib.redirect_stderr(stderr_capture):
        # 0 个点：不画任何内容
        widget.grab()

        # 1 个点：画一个圆点
        widget.append(10.0)
        widget.grab()

        # 平线：min == max，画一条水平线
        widget.append(10.0)
        widget.append(10.0)
        widget.grab()

        # 常规：数值有起伏
        widget.clear()
        for value in (1.0, 5.0, 3.0, 8.0, 2.0):
            widget.append(value)
        widget.grab()

        # 大量点触发多次抽稀后仍能正常绘制
        for i in range(2000):
            widget.append(float(i))
        widget.grab()

    stderr_output = stderr_capture.getvalue()
    assert "Traceback" not in stderr_output
