"""
分析纯函数
趋势拟合与判定（trend）、按打点边界的区间统计（intervals）、
空洞切断与累计量速率等序列辅助（series）。
只依赖标准库，不碰 Qt、不查数据库：输入由数据层的查询结果组装（fit_input_from_aggregates
只读取数据层聚合结果的字段，类型注解仅用于静态检查），输出交给 UI 层映射文案。
"""
from core.analysis.intervals import (
    Boundary,
    IntervalStats,
    RoundDelta,
    baseline_delta,
    mean_round_delta,
    round_deltas,
    split_intervals,
    value_near,
)
from core.analysis.series import (
    connect_mask,
    count_rises,
    per_second_rates,
)
from core.analysis.trend import (
    FitInput,
    Judgement,
    LinearFit,
    RateText,
    Verdict,
    fit_input_from_aggregates,
    judge,
    linear_fit,
    min_span_for,
    rate_display,
    rate_unit_for_span,
)

__all__ = [
    'Boundary',
    'FitInput',
    'IntervalStats',
    'Judgement',
    'LinearFit',
    'RateText',
    'RoundDelta',
    'Verdict',
    'baseline_delta',
    'connect_mask',
    'count_rises',
    'fit_input_from_aggregates',
    'judge',
    'linear_fit',
    'mean_round_delta',
    'min_span_for',
    'per_second_rates',
    'rate_display',
    'rate_unit_for_span',
    'round_deltas',
    'split_intervals',
    'value_near',
]
