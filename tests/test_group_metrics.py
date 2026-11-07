"""
utils.metrics 的进程组部分：序列键拼接 / 拆分（全项目只走这几个纯函数）、聚合方式由 MetricKind 派生
（逐指标表驱动）、进程总数不污染 pid 任务的注册表、成员序列的显示名 / 单位 / 值类别。
"""
import pytest

from utils import metrics as m
from utils.metrics import (GROUP_REGISTRY, PROCESS_COUNT, REGISTRY, GroupAggregation,
                           MetricKind, MetricType)


def test_member_key_join_and_split_round_trip():
    key = m.member_series_key('memory_private', 'chrome.exe')
    assert key == 'memory_private@chrome.exe'
    assert m.split_member_key(key) == ('memory_private', 'chrome.exe')
    assert m.split_member_key('memory_private') == ('memory_private', None)
    assert m.is_member_series(key) and not m.is_member_series('memory_private')


def test_split_uses_first_separator_so_member_names_may_contain_at():
    assert m.split_member_key('cpu_percent@a@b.exe') == ('cpu_percent', 'a@b.exe')


def test_normalize_member_key():
    assert m.normalize_member_key('  Chrome.EXE ') == 'chrome.exe'
    assert m.normalize_member_key('') == ''


def test_no_registered_metric_key_contains_the_separator():
    assert not any(m.MEMBER_SEPARATOR in key for key in list(REGISTRY) + list(GROUP_REGISTRY))


# 每个常规指标的聚合方式由其 kind 决定；这里按 kind 逐一核对，新增指标自动被覆盖
EXPECTED_BY_KIND = {
    MetricKind.BYTES_KB: GroupAggregation.SUM,
    MetricKind.COUNT: GroupAggregation.SUM,
    MetricKind.PERCENT: GroupAggregation.SUM,
    MetricKind.SECONDS: GroupAggregation.DELTA_SUM,
    MetricKind.CUMULATIVE_COUNT: GroupAggregation.DELTA_SUM,
    MetricKind.CUMULATIVE_BYTES: GroupAggregation.DELTA_SUM,
    MetricKind.PEAK: GroupAggregation.NONE,
}


@pytest.mark.parametrize('key', sorted(REGISTRY))
def test_group_aggregation_follows_kind(key):
    spec = REGISTRY[key]
    expected = (GroupAggregation.NONE if not spec.group_applicable
                else EXPECTED_BY_KIND[spec.kind])
    assert m.group_aggregation(key) is expected


def test_cpu_priority_is_not_applicable_to_groups_even_though_it_is_a_count():
    assert REGISTRY[MetricType.CPU_PRIORITY].kind is MetricKind.COUNT
    assert m.group_aggregation(MetricType.CPU_PRIORITY) is GroupAggregation.NONE


def test_peak_metrics_are_not_applicable():
    peaks = [k for k, s in REGISTRY.items() if s.kind is MetricKind.PEAK]
    assert peaks and all(m.group_aggregation(k) is GroupAggregation.NONE for k in peaks)


def test_process_count_is_group_only_and_not_in_pid_registry():
    assert PROCESS_COUNT in GROUP_REGISTRY and PROCESS_COUNT not in REGISTRY
    assert PROCESS_COUNT not in m.all_metric_types()
    assert m.is_group_only_metric(PROCESS_COUNT)
    assert m.is_group_only_metric(m.member_series_key(PROCESS_COUNT, 'a.exe'))
    assert not m.is_group_only_metric(MetricType.MEMORY_PRIVATE)
    assert m.group_aggregation(PROCESS_COUNT) is GroupAggregation.SUM


def test_group_selectable_metrics_excludes_inapplicable_and_adds_process_count():
    selectable = m.group_selectable_metrics()
    assert PROCESS_COUNT in selectable
    assert MetricType.MEMORY_PEAK_WSET not in selectable
    assert MetricType.CPU_PRIORITY not in selectable
    assert MetricType.MEMORY_PRIVATE in selectable and MetricType.CPU_PERCENT in selectable


def test_filter_and_unsupported_keep_order():
    chosen = [MetricType.NUM_HANDLES, MetricType.MEMORY_PEAK_WSET, MetricType.CPU_PRIORITY,
              MetricType.MEMORY_PRIVATE]
    assert m.filter_group_metrics(chosen) == [MetricType.NUM_HANDLES, MetricType.MEMORY_PRIVATE]
    assert m.group_unsupported_metrics(chosen) == [MetricType.MEMORY_PEAK_WSET,
                                                   MetricType.CPU_PRIORITY]


def test_unknown_metric_is_not_applicable():
    assert m.group_aggregation('no_such_metric') is GroupAggregation.NONE


# ========== 成员序列沿用指标本身的属性 ==========

def test_member_series_inherit_metric_properties():
    series = m.member_series_key(MetricType.IO_READ_BYTES, 'a.exe')
    assert m.value_class(series) == m.value_class(MetricType.IO_READ_BYTES) == m.VALUE_CLASS_RATE
    assert m.is_kb_metric(series) and m.is_cumulative_metric(series)
    assert m.get_metric_unit(series) == m.get_metric_unit(MetricType.IO_READ_BYTES)
    assert m.is_integer_metric(m.member_series_key(PROCESS_COUNT, 'a.exe'))


def test_member_display_name_shows_member_in_parentheses():
    assert m.get_metric_display_name(m.member_series_key(MetricType.MEMORY_PRIVATE, 'a.exe')) \
        == f"{m.get_metric_display_name(MetricType.MEMORY_PRIVATE)}（a.exe）"
    assert m.get_metric_display_name(PROCESS_COUNT) == '进程总数'
    assert m.get_metric_unit(PROCESS_COUNT) == '个'
