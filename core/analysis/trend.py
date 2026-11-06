"""
趋势拟合与判定
对一段时间序列做最小二乘直线拟合，并给出"持续上涨 / 平稳 / 波动 / 数据不足"的结论。

输入只用聚合量（点数与各阶和），因此数据层可以直接用一条 SUM 聚合 SQL 提供，
不必把成千上万个点读进内存。累计计数类指标（IO 字节/次数、页面错误、CPU 时间等）
本来就单调增长，对它们做"增速外推"没有意义——本模块只负责计算，是否调用由调用方决定。
"""
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# 判定门槛默认值（judge 的同名参数可覆盖）
MIN_SPAN_FLOOR_SECONDS = 600.0      # 跨度下限：10 分钟
MIN_SPAN_INTERVALS = 30             # 跨度至少覆盖 30 个采集周期
MIN_POINTS = 30                     # 至少 30 个点
RISE_R2 = 0.6                       # 判"持续上涨"所需的最低 R²
RISE_FRACTION_OF_RANGE = 0.05       # 判"持续上涨"所需的拟合增量占值域的最低比例
STABLE_FRACTION_OF_RANGE = 0.2      # 拟合增量占值域低于此比例视为"幅度很小"
CI_Z = 1.96                         # 斜率置信区间的 z 值（约 95%）

# 置信度里数据量因子"满分"所需的倍数：点数达到 4×门槛、跨度达到 3×门槛时不再加分
FULL_POINTS_MULTIPLE = 4.0
FULL_SPAN_MULTIPLE = 3.0

# 中心化二阶矩相对原始平方和小于该比例时视为 0：SUM 来自数据库，原始量级可能很大
# （字节数 ~1e9，平方和 ~1e22），相减后的残差只剩浮点舍入噪声
_REL_EPS = 1e-12


@dataclass(frozen=True)
class FitInput:
    """
    直线拟合所需的聚合量，由数据层的 SUM 聚合查询提供。
    x 为相对秒数（相对该段第一个点），y 为指标值；sum_xx = Σx²，sum_xy = Σx·y，sum_yy = Σy²。
    """
    count: int
    sum_x: float
    sum_y: float
    sum_xx: float
    sum_xy: float
    sum_yy: float
    span_seconds: float     # 最后点 - 首点
    first_value: float
    last_value: float
    min_value: float
    max_value: float

    @property
    def value_range(self) -> float:
        return self.max_value - self.min_value


@dataclass(frozen=True)
class LinearFit:
    """直线拟合结果：y ≈ intercept + slope_per_second · x（x 为相对首点的秒数）"""
    slope_per_second: float
    intercept: float
    r2: float               # 决定系数 0..1；y 无方差（恒定值）时定义为 1.0
    stderr_slope: float     # 斜率标准误；count <= 2 时无残差自由度，为 inf

    @property
    def slope_per_hour(self) -> float:
        return self.slope_per_second * 3600.0

    def ci_contains_zero(self, z: float = CI_Z) -> bool:
        """斜率的 z 置信区间是否包含 0，即斜率在统计上与 0 无显著差别"""
        if math.isinf(self.stderr_slope):
            return True
        return abs(self.slope_per_second) <= z * self.stderr_slope


def _centered(sum_sq: float, sum_a: float, sum_b: float, n: int) -> float:
    """由原始和算中心化二阶矩 Σ(a-ā)(b-b̄) = Σab - Σa·Σb/n"""
    return sum_sq - sum_a * (sum_b / n)


def linear_fit(agg: FitInput) -> Optional[LinearFit]:
    """
    最小二乘直线拟合。count < 2 或 x 无方差（所有点同一时刻）时返回 None。

    只拿到原始和时无法真正"先去均值再求和"，这里用均值形式 Sxx = Σx² - Σx·x̄ 计算，
    并对抵消后只剩舍入噪声的结果截断为 0、R² 截断到 [0, 1]、残差平方和截断为非负，
    避免大数相消把负方差或 R² > 1 传给下游。x 是相对秒数（量级小），主要风险在 y 侧。

    - y 无方差：slope = 0、r2 = 1.0、stderr = 0（count > 2 时）
    - count == 2：两点确定一条直线，r2 = 1.0（y 不同时）、stderr = inf
    """
    n = agg.count
    if n < 2:
        return None
    sxx = _centered(agg.sum_xx, agg.sum_x, agg.sum_x, n)
    if sxx <= _REL_EPS * max(abs(agg.sum_xx), 1.0):
        return None
    sxy = _centered(agg.sum_xy, agg.sum_x, agg.sum_y, n)
    syy = _centered(agg.sum_yy, agg.sum_y, agg.sum_y, n)

    mean_x = agg.sum_x / n
    mean_y = agg.sum_y / n
    constant = (agg.max_value == agg.min_value) or syy <= _REL_EPS * abs(agg.sum_yy)
    if constant:
        slope = 0.0
        r2 = 1.0
        sse = 0.0
    else:
        slope = sxy / sxx
        r2 = min(1.0, max(0.0, (sxy * sxy) / (sxx * syy)))
        # SSE = Syy - b·Sxy = Syy·(1 - r²)；用 r² 形式，已截断过，不会出现负值
        sse = syy * (1.0 - r2)
    intercept = mean_y - slope * mean_x
    if n <= 2:
        stderr = math.inf
    else:
        stderr = math.sqrt(sse / (n - 2) / sxx)
    return LinearFit(slope_per_second=slope, intercept=intercept, r2=r2, stderr_slope=stderr)


class Verdict(str, Enum):
    """趋势结论。中文文案由 UI 层映射。"""
    RISING = 'rising'               # 持续上涨
    STABLE = 'stable'               # 平稳
    FLUCTUATING = 'fluctuating'     # 波动
    INSUFFICIENT = 'insufficient'   # 数据不足

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Judgement:
    """
    判定结果。reason 是机器可读的原因键，UI 层映射中文：
    - INSUFFICIENT：'few_points' 点数不足 / 'short_span' 跨度不足 / 'degenerate' 无法拟合
    - RISING：'rising'
    - STABLE：'constant' 恒定值 / 'slope_not_significant' 斜率置信区间含 0 /
      'slope_below_threshold' 拟合增量相对值域很小且 R² 低
    - FLUCTUATING：'low_r2' R² 低且增量不小 / 'falling' 显著下降 /
      'rise_below_threshold' 拟合良好但增量相对值域太小
    """
    verdict: Verdict
    fit: Optional[LinearFit]
    reason: str
    confidence: float


def min_span_for(interval_seconds: float) -> float:
    """默认跨度门槛：max(10 分钟, 30 × 采集周期)"""
    return max(MIN_SPAN_FLOOR_SECONDS, MIN_SPAN_INTERVALS * max(0.0, interval_seconds))


def _ratio(value: float, full: float) -> float:
    if full <= 0:
        return 1.0
    return min(1.0, max(0.0, value / full))


def judge(agg: FitInput, interval_seconds: float, *,
          min_span_seconds: Optional[float] = None,
          min_points: int = MIN_POINTS,
          rise_r2: float = RISE_R2,
          rise_fraction_of_range: float = RISE_FRACTION_OF_RANGE,
          stable_fraction_of_range: float = STABLE_FRACTION_OF_RANGE,
          ci_z: float = CI_Z) -> Judgement:
    """
    给出趋势结论。记 Δ = slope × span（整段拟合增量），R = max - min（值域）。

    判定顺序：
    1. 数据不足：count < min_points → 'few_points'；span < min_span_seconds
       （默认 max(10 分钟, 30 × 周期)，恰好相等算够）→ 'short_span'；拟合失败 → 'degenerate'
    2. 恒定值（R = 0）→ 平稳 'constant'
    3. 持续上涨：slope > 0、R² ≥ rise_r2、斜率置信区间不含 0、Δ ≥ rise_fraction_of_range × R。
       值域 R 里既有趋势也有噪声，Δ/R 衡量"趋势占了整体起伏的多少"：纯噪声上的伪趋势
       Δ 远小于 R，干净的线性上涨 Δ ≈ R。默认 R² 门槛已隐含置信区间不含 0
       （n ≥ 30、R² ≥ 0.6 时 t > 6），置信区间条件只在调低 rise_r2 时起作用。
    4. 平稳：斜率置信区间含 0 → 'slope_not_significant'；
       或 |Δ| < stable_fraction_of_range × R 且 R² < rise_r2 → 'slope_below_threshold'
       （点数很多时极小的漂移也会"显著"，所以还要看幅度）
    5. 其余为波动：R² < rise_r2 → 'low_r2'；slope < 0 → 'falling'；否则 'rise_below_threshold'

    置信度 = 拟合因子 × (0.5 + 0.5 × 点数因子) × (0.5 + 0.5 × 跨度因子)，截断到 [0, 1]：
    - 拟合因子：持续上涨用 R²；平稳用 1 - R²（恒定值为 1）；波动用 1 - R²；数据不足整体为 0
    - 点数因子 = min(1, count / (4 × min_points))
    - 跨度因子 = min(1, span / (3 × 跨度门槛))
    刚过门槛时数据量两项约为 0.62 × 0.67 ≈ 0.42，数据充足时为 1，便于解释"再多采一会儿更可信"。
    """
    min_span = min_span_for(interval_seconds) if min_span_seconds is None else min_span_seconds
    if agg.count < min_points:
        return Judgement(Verdict.INSUFFICIENT, None, 'few_points', 0.0)
    if agg.span_seconds < min_span:
        return Judgement(Verdict.INSUFFICIENT, None, 'short_span', 0.0)
    fit = linear_fit(agg)
    if fit is None:
        return Judgement(Verdict.INSUFFICIENT, None, 'degenerate', 0.0)

    quantity = ((0.5 + 0.5 * _ratio(agg.count, FULL_POINTS_MULTIPLE * min_points))
                * (0.5 + 0.5 * _ratio(agg.span_seconds, FULL_SPAN_MULTIPLE * min_span)))

    def make(verdict: Verdict, reason: str, fit_factor: float) -> Judgement:
        confidence = min(1.0, max(0.0, fit_factor * quantity))
        return Judgement(verdict, fit, reason, confidence)

    value_range = agg.value_range
    if value_range <= 0:
        return make(Verdict.STABLE, 'constant', 1.0)

    increase = fit.slope_per_second * agg.span_seconds
    ci_zero = fit.ci_contains_zero(ci_z)
    if (fit.slope_per_second > 0 and fit.r2 >= rise_r2 and not ci_zero
            and increase >= rise_fraction_of_range * value_range):
        return make(Verdict.RISING, 'rising', fit.r2)

    if ci_zero:
        return make(Verdict.STABLE, 'slope_not_significant', 1.0 - fit.r2)
    if abs(increase) < stable_fraction_of_range * value_range and fit.r2 < rise_r2:
        return make(Verdict.STABLE, 'slope_below_threshold', 1.0 - fit.r2)

    if fit.r2 < rise_r2:
        reason = 'low_r2'
    elif fit.slope_per_second < 0:
        reason = 'falling'
    else:
        reason = 'rise_below_threshold'
    return make(Verdict.FLUCTUATING, reason, 1.0 - fit.r2)
