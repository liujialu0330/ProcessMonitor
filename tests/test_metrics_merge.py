"""指标合并（私有字节）与 GDI/USER 指标定义契约。"""
import sys

import psutil
import pytest

from utils.metrics import (
    AVAILABLE_METRICS, DEPRECATED_METRICS, LEGACY_METRIC_ALIASES,
    METRIC_DESCRIPTIONS, METRIC_DISPLAY_NAMES, METRIC_UNITS, REGISTRY, MetricType,
    all_metric_types, canonical_metric_type, format_metric_value,
    get_metric_display_name, get_metric_display_unit, get_metric_unit,
)

OLD_KEYS = ("memory_vms", "memory_pagefile")


def test_available_metrics_total_and_groups():
    """可选共 26 项（内存 11 / CPU 4 / 系统资源 5 / IO 6），不含已合并的旧键与已废弃指标"""
    sizes = {group: len(items) for group, items in AVAILABLE_METRICS.items()}
    assert sizes == {"内存": 11, "CPU": 4, "系统资源": 5, "I/O": 6}
    all_metrics = [m for items in AVAILABLE_METRICS.values() for m in items]
    assert len(all_metrics) == 26
    assert len(set(all_metrics)) == 26
    for old in OLD_KEYS:
        assert old not in all_metrics
    assert not hasattr(MetricType, "MEMORY_VMS")
    assert not hasattr(MetricType, "MEMORY_PAGEFILE")


def test_gui_metrics_follow_handles_in_system_group():
    group = AVAILABLE_METRICS["系统资源"]
    i = group.index(MetricType.NUM_HANDLES)
    assert group[i + 1:i + 3] == [MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS]


def test_private_bytes_names_and_keys():
    assert MetricType.MEMORY_PRIVATE == "memory_private"
    assert MetricType.MEMORY_PEAK_PAGEFILE == "memory_peak_pagefile"
    assert get_metric_display_name(MetricType.MEMORY_PRIVATE) == "私有字节"
    assert get_metric_display_name(MetricType.MEMORY_PEAK_PAGEFILE) == "私有字节峰值"


def test_legacy_keys_display_as_private_bytes_in_kb():
    """数据库迁移没成功时，旧键也按私有字节显示并按 KB 格式化"""
    assert LEGACY_METRIC_ALIASES == {
        "memory_vms": "memory_private", "memory_pagefile": "memory_private"}
    for old in OLD_KEYS:
        assert canonical_metric_type(old) == "memory_private"
        assert get_metric_display_name(old) == "私有字节"
        assert get_metric_unit(old) == "KB"
        assert get_metric_display_unit(old, 2048) == "MB"
        assert format_metric_value(old, 36004.0) == "35.16 MB"
    assert canonical_metric_type("memory_rss") == "memory_rss"


def test_gui_object_metrics_names_units_and_integer_format():
    assert get_metric_display_name(MetricType.NUM_GDI_OBJECTS) == "GDI对象数"
    assert get_metric_display_name(MetricType.NUM_USER_OBJECTS) == "USER对象数"
    assert get_metric_unit(MetricType.NUM_GDI_OBJECTS) == "个"
    assert format_metric_value(MetricType.NUM_GDI_OBJECTS, 1234.0) == "1,234 个"
    assert format_metric_value(MetricType.NUM_USER_OBJECTS, 56.0) == "56 个"


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有这三个字段")
def test_guard_vms_pagefile_private_are_identical():
    """守护：合并依据是 Windows 上三个字段数值相同；psutil 升级后若不再成立需重新评估"""
    info = psutil.Process().memory_info()
    assert info.private == info.pagefile == info.vms, (
        "合并依据失效：psutil 的 private/pagefile/vms 不再相同"
        f"（private={info.private}, pagefile={info.pagefile}, vms={info.vms}），"
        "需重新评估'私有字节'合并方案")


def test_involuntary_ctx_switches_deprecated_but_still_displayable():
    """非自愿上下文切换在 Windows 恒为 0：不再可选，但旧数据仍能按中文名、单位显示"""
    key = MetricType.NUM_CTX_SWITCHES_INVOL
    assert key == "num_ctx_switches_involuntary"
    assert key in DEPRECATED_METRICS
    assert key not in all_metric_types()
    for items in AVAILABLE_METRICS.values():
        assert key not in items
    assert get_metric_display_name(key) == "非自愿上下文切换"
    assert get_metric_unit(key) == "次"
    assert format_metric_value(key, 1234.0) == "1,234 次"


def test_registry_tables_cover_available_plus_deprecated():
    """显示名/单位/说明三张表的键 = 可选指标 ∪ 已废弃指标 = 注册表键，两者不相交"""
    available = set(all_metric_types())
    assert not (available & DEPRECATED_METRICS)
    expected = available | DEPRECATED_METRICS
    assert set(REGISTRY) == expected
    assert set(METRIC_DISPLAY_NAMES) == expected
    assert set(METRIC_UNITS) == expected
    assert set(METRIC_DESCRIPTIONS) == expected
