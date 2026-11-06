"""
分析纯函数
趋势拟合与判定（trend）、按打点边界的区间统计（intervals）。
只依赖标准库，不碰 Qt、不查数据库：输入由数据层的查询结果组装（fit_input_from_aggregates
只读取数据层聚合结果的字段，类型注解仅用于静态检查），输出交给 UI 层映射文案。
"""
from core.analysis.intervals import (
    Boundary,
    IntervalStats,
    baseline_delta,
    split_intervals,
    value_near,
)
from core.analysis.trend import (
    FitInput,
    Judgement,
    LinearFit,
    Verdict,
    fit_input_from_aggregates,
    judge,
    linear_fit,
    min_span_for,
)

__all__ = [
    'Boundary',
    'FitInput',
    'IntervalStats',
    'Judgement',
    'LinearFit',
    'Verdict',
    'baseline_delta',
    'fit_input_from_aggregates',
    'judge',
    'linear_fit',
    'min_span_for',
    'split_intervals',
    'value_near',
]
