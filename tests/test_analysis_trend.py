"""
趋势拟合与判定（core/analysis/trend.py）的单元测试
拟合用手工可算的数据与 statistics.linear_regression 交叉验证；判定覆盖四种结论与各门槛边界。
"""
import math
import random
import statistics

import pytest

from core.analysis.trend import (
    CI_Z,
    MIN_POINTS,
    FitInput,
    Verdict,
    judge,
    linear_fit,
    min_span_for,
)


def make_input(xs, ys) -> FitInput:
    """由原始点构造聚合量，模拟数据层 SUM 查询的结果"""
    return FitInput(
        count=len(xs),
        sum_x=math.fsum(xs),
        sum_y=math.fsum(ys),
        sum_xx=math.fsum(x * x for x in xs),
        sum_xy=math.fsum(x * y for x, y in zip(xs, ys)),
        sum_yy=math.fsum(y * y for y in ys),
        span_seconds=(max(xs) - min(xs)) if xs else 0.0,
        first_value=ys[0] if ys else 0.0,
        last_value=ys[-1] if ys else 0.0,
        min_value=min(ys) if ys else 0.0,
        max_value=max(ys) if ys else 0.0,
    )


def synthetic(n, step, slope, r2, range_ratio=1.2) -> FitInput:
    """
    直接按目标 R² 构造聚合量：Sxy = b·Sxx，Syy = b²·Sxx / r²，y 均值取 0 免去相消误差。
    值域 = |整段拟合增量| × range_ratio，用来控制"增量占值域"的比例。
    """
    xs = [i * step for i in range(n)]
    sum_x = math.fsum(xs)
    sxx = math.fsum((x - sum_x / n) ** 2 for x in xs)
    sxy = slope * sxx
    syy = slope * slope * sxx / r2
    span = xs[-1]
    rng = abs(slope * span) * range_ratio
    return FitInput(
        count=n, sum_x=sum_x, sum_y=0.0, sum_xx=math.fsum(x * x for x in xs),
        sum_xy=sxy, sum_yy=syy, span_seconds=span,
        first_value=-rng / 2, last_value=rng / 2, min_value=-rng / 2, max_value=rng / 2,
    )


def line(n, step, slope=1.0, intercept=100.0) -> FitInput:
    xs = [i * step for i in range(n)]
    return make_input(xs, [intercept + slope * x for x in xs])


# ---------- linear_fit ----------

def test_perfect_line():
    xs = list(range(10))
    fit = linear_fit(make_input(xs, [2 * x + 1 for x in xs]))
    assert math.isclose(fit.slope_per_second, 2.0)
    assert math.isclose(fit.intercept, 1.0)
    assert math.isclose(fit.r2, 1.0)
    assert math.isclose(fit.stderr_slope, 0.0, abs_tol=1e-9)
    assert math.isclose(fit.slope_per_hour, 7200.0)


def test_constant_values_r2_is_one():
    xs = list(range(10))
    fit = linear_fit(make_input(xs, [5.0] * 10))
    assert fit.slope_per_second == 0.0
    assert math.isclose(fit.intercept, 5.0)
    assert fit.r2 == 1.0
    assert fit.stderr_slope == 0.0


def test_constant_large_values_no_cancellation_noise():
    """字节量级的恒定值：平方和 ~1e20，相减后的舍入噪声不能变成虚假方差"""
    xs = [i * 1.0 for i in range(1000)]
    fit = linear_fit(make_input(xs, [3.7e9] * 1000))
    assert fit.slope_per_second == 0.0
    assert fit.r2 == 1.0


def test_two_points():
    fit = linear_fit(make_input([0.0, 10.0], [1.0, 21.0]))
    assert math.isclose(fit.slope_per_second, 2.0)
    assert math.isclose(fit.intercept, 1.0)
    assert math.isclose(fit.r2, 1.0)
    assert math.isinf(fit.stderr_slope)
    assert fit.ci_contains_zero()


@pytest.mark.parametrize('xs, ys', [
    ([], []),
    ([0.0], [1.0]),
    ([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]),   # x 无方差
])
def test_degenerate_returns_none(xs, ys):
    assert linear_fit(make_input(xs, ys)) is None


def _reference(xs, ys):
    """标准库参考值：斜率/截距、R²、斜率标准误（按残差逐点算）"""
    ref = statistics.linear_regression(xs, ys)
    r2 = statistics.correlation(xs, ys) ** 2
    mx = statistics.fmean(xs)
    sse = math.fsum((y - (ref.intercept + ref.slope * x)) ** 2 for x, y in zip(xs, ys))
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    se = math.sqrt(sse / (len(xs) - 2) / sxx)
    return ref.slope, ref.intercept, r2, se


@pytest.mark.parametrize('seed, offset, slope, noise', [
    (1, 0.0, 0.5, 3.0),
    (2, 100.0, -0.2, 10.0),
    (3, 3.2e9, 1500.0, 2.0e6),     # 字节量级的内存值
    (4, 50.0, 0.0, 1.0),
])
def test_noisy_matches_statistics(seed, offset, slope, noise):
    rnd = random.Random(seed)
    xs = [i * 2.0 for i in range(500)]
    ys = [offset + slope * x + rnd.gauss(0, noise) for x in xs]
    fit = linear_fit(make_input(xs, ys))
    ref_slope, ref_intercept, ref_r2, ref_se = _reference(xs, ys)
    assert math.isclose(fit.slope_per_second, ref_slope, rel_tol=1e-6, abs_tol=1e-9)
    assert math.isclose(fit.intercept, ref_intercept, rel_tol=1e-6)
    assert math.isclose(fit.r2, ref_r2, rel_tol=1e-6, abs_tol=1e-9)
    assert math.isclose(fit.stderr_slope, ref_se, rel_tol=1e-6)


# ---------- min_span_for ----------

@pytest.mark.parametrize('interval, expected', [
    (1, 600.0),
    (20, 600.0),
    (30, 900.0),
    (60, 1800.0),
])
def test_min_span_for(interval, expected):
    assert math.isclose(min_span_for(interval), expected)


# ---------- judge：门槛边界 ----------

@pytest.mark.parametrize('count, verdict, reason', [
    (29, Verdict.INSUFFICIENT, 'few_points'),
    (30, Verdict.RISING, 'rising'),
])
def test_points_threshold(count, verdict, reason):
    # 周期 60s → 跨度门槛 1800s；点距 100s 保证跨度足够，只考察点数
    j = judge(line(count, 100.0), 60)
    assert (j.verdict, j.reason) == (verdict, reason)


@pytest.mark.parametrize('interval, step, verdict, reason', [
    # 周期 10s → 门槛 600s；30 点 × 步长 600/29 → 跨度恰好 600 算够
    (10, 600 / 29, Verdict.RISING, 'rising'),
    (10, 599 / 29, Verdict.INSUFFICIENT, 'short_span'),
    # 周期 30s → 门槛 900s
    (30, 900 / 29, Verdict.RISING, 'rising'),
    (30, 899 / 29, Verdict.INSUFFICIENT, 'short_span'),
])
def test_span_threshold(interval, step, verdict, reason):
    agg = line(30, step)
    j = judge(agg, interval, min_span_seconds=None)
    if verdict is Verdict.RISING:
        assert agg.span_seconds >= min_span_for(interval)
    assert (j.verdict, j.reason) == (verdict, reason)


def test_explicit_min_span_overrides_default():
    agg = line(30, 10.0)   # 跨度 290s
    assert judge(agg, 1).verdict is Verdict.INSUFFICIENT
    assert judge(agg, 1, min_span_seconds=290).verdict is Verdict.RISING


def test_insufficient_has_zero_confidence_and_no_fit():
    j = judge(line(10, 100.0), 1)
    assert j.verdict is Verdict.INSUFFICIENT
    assert j.fit is None
    assert j.confidence == 0.0


@pytest.mark.parametrize('r2, verdict, reason', [
    (0.59, Verdict.FLUCTUATING, 'low_r2'),
    (0.6, Verdict.RISING, 'rising'),
    (0.61, Verdict.RISING, 'rising'),
])
def test_r2_threshold(r2, verdict, reason):
    agg = synthetic(n=40, step=30.0, slope=0.5, r2=r2)
    j = judge(agg, 10)
    assert math.isclose(j.fit.r2, r2, rel_tol=1e-12)
    assert (j.verdict, j.reason) == (verdict, reason)


def test_rise_fraction_threshold():
    # 拟合很好但增量只占值域 1%：不算持续上涨
    agg = synthetic(n=40, step=30.0, slope=0.5, r2=0.9, range_ratio=100.0)
    j = judge(agg, 10)
    assert (j.verdict, j.reason) == (Verdict.FLUCTUATING, 'rise_below_threshold')
    # 门槛调低到 0.5% 后成立
    j = judge(agg, 10, rise_fraction_of_range=0.005)
    assert j.verdict is Verdict.RISING


# ---------- judge：四种结论 ----------

def test_rising_clean_line():
    j = judge(line(120, 30.0, slope=0.01), 30)
    assert j.verdict is Verdict.RISING
    assert math.isclose(j.fit.slope_per_hour, 36.0)
    assert 0.0 < j.confidence <= 1.0


def test_stable_constant():
    xs = [i * 30.0 for i in range(60)]
    j = judge(make_input(xs, [1024.0] * 60), 30)
    assert (j.verdict, j.reason) == (Verdict.STABLE, 'constant')


def test_stable_noise_slope_not_significant():
    xs = [i * 30.0 for i in range(60)]
    ys = [100.0 + (1.0 if i % 2 else -1.0) for i in range(60)]   # 锯齿，无趋势
    j = judge(make_input(xs, ys), 30)
    assert j.fit.ci_contains_zero(CI_Z)
    assert (j.verdict, j.reason) == (Verdict.STABLE, 'slope_not_significant')


def test_stable_small_but_significant_drift():
    # 点数多时极小漂移也显著（CI 不含 0），但增量只占值域 10%、R² 低 → 仍算平稳
    agg = synthetic(n=2000, step=5.0, slope=0.01, r2=0.3, range_ratio=10.0)
    j = judge(agg, 5)
    assert not j.fit.ci_contains_zero()
    assert (j.verdict, j.reason) == (Verdict.STABLE, 'slope_below_threshold')


def test_fluctuating_low_r2_large_change():
    agg = synthetic(n=60, step=30.0, slope=0.5, r2=0.3, range_ratio=1.5)
    j = judge(agg, 30)
    assert not j.fit.ci_contains_zero()
    assert (j.verdict, j.reason) == (Verdict.FLUCTUATING, 'low_r2')


def test_fluctuating_falling():
    j = judge(line(60, 30.0, slope=-0.5), 30)
    assert (j.verdict, j.reason) == (Verdict.FLUCTUATING, 'falling')


# ---------- 置信度 ----------

def test_confidence_grows_with_data():
    small = judge(line(MIN_POINTS, 900 / 29), 30)          # 刚过门槛
    large = judge(line(4 * MIN_POINTS, 2700 / 119), 30)   # 点数 4×、跨度 3×
    assert small.verdict is large.verdict is Verdict.RISING
    assert math.isclose(small.confidence, (0.5 + 0.5 * 0.25) * (0.5 + 0.5 / 3))
    assert math.isclose(large.confidence, 1.0)
    assert small.confidence < large.confidence


def test_confidence_uses_r2_for_rising():
    agg = synthetic(n=4 * MIN_POINTS, step=30.0, slope=0.5, r2=0.8)
    j = judge(agg, 1, min_span_seconds=agg.span_seconds / 3)
    assert j.verdict is Verdict.RISING
    assert math.isclose(j.confidence, 0.8)


def test_verdict_is_str_enum():
    assert Verdict.RISING == 'rising'
    assert str(Verdict.INSUFFICIENT) == 'insufficient'
