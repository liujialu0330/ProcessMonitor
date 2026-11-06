"""
指标注册表（MetricSpec / REGISTRY）契约：

- 注册表是唯一来源：显示名、单位、说明、可选分组、废弃集合、泄漏预设、KB/整数集合
  全部由它生成，键集合逐一一致；
- 快照：现有 27 项的显示名/单位/整数性/是否 KB/说明逐项写死（取自重构前的实现），
  保证重构不改行为；
- kind 派生：累计类判定、迷你图最小跨度与重构前 sparkline 的规则逐项相等；
- 新增一个指标只改注册表：运行时登记一个临时 spec，各派生表自动包含。
"""
import pytest

from utils import metrics
from utils.metrics import (
    AVAILABLE_METRICS, DEPRECATED_METRICS, KB_UNIT_DIVISORS, LEAK_CHECK_PRESET,
    METRIC_DESCRIPTIONS, METRIC_DISPLAY_NAMES, METRIC_UNITS, MIN_SPAN_BYTES_KB,
    MIN_SPAN_COUNT, MIN_SPAN_PERCENT, RATE_NOT_APPLICABLE, REGISTRY, MetricKind,
    MetricSpec, MetricType, all_metric_types, display_unit_for, format_metric_delta,
    format_metric_rate, format_metric_value, get_metric_description,
    get_metric_display_name, get_metric_display_unit, get_metric_unit,
    is_cumulative_metric, is_integer_metric, is_kb_metric, min_span_for,
    register_metric, spec, unregister_metric,
)

# 重构前实现的逐项快照：key → (显示名, 单位, 整数性, 是否 KB, 说明)
SNAPSHOT = {
    'memory_rss': ('工作集内存', 'KB', False, True,
        '工作集：当前驻留在物理内存中的页，会被系统换出而随内存压力波动，适合看内存占用的实时水位，不宜单独用来判断泄漏'),
    'memory_percent': ('内存使用率', '%', False, False,
        '工作集占整机物理内存的百分比，适合评估被测进程对整机内存的压力'),
    'memory_peak_wset': ('工作集峰值', 'KB', False, True,
        '进程启动以来工作集的最大值，只增不减，适合查看内存使用的历史峰值'),
    'memory_private': ('私有字节', 'KB', False, True,
        '进程独占的已提交内存（Private Bytes），持续上涨是内存泄漏的主要信号'),
    'memory_peak_pagefile': ('私有字节峰值', 'KB', False, True,
        '私有字节的历史峰值，只增不减，适合查看某次操作期间的内存冲高'),
    'memory_paged_pool': ('分页池', 'KB', False, True,
        '进程占用的内核分页池，持续上涨多见于驱动或系统对象（如文件、注册表句柄）泄漏'),
    'memory_peak_paged_pool': ('分页池峰值', 'KB', False, True,
        '分页池的历史峰值，只增不减，适合排查内核资源的瞬时冲高'),
    'memory_nonpaged_pool': ('非分页池', 'KB', False, True,
        '进程占用的内核非分页池（常驻物理内存），持续上涨多见于驱动、设备或句柄泄漏'),
    'memory_peak_nonpaged_pool': ('非分页池峰值', 'KB', False, True,
        '非分页池的历史峰值，只增不减，适合排查内核资源的瞬时冲高'),
    'memory_num_page_faults': ('页面错误', '次', True, False,
        '累计缺页次数，增长过快说明内存被频繁换入换出，适合发现内存紧张或访问模式异常'),
    'memory_uss': ('唯一集大小', 'KB', False, True,
        '进程独占的物理内存（不含共享页），比工作集更接近真实占用；读取开销较大，且对更高权限进程可能因权限不足而不可用'),
    'cpu_percent': ('CPU使用率', '%', False, False,
        '进程 CPU 占用率（可超过 100%，按核数累计），适合发现空闲时仍高占用的死循环或忙等'),
    'cpu_user_time': ('CPU用户时间', '秒', False, False,
        '累计在用户态运行的 CPU 时间，适合比较各轮操作消耗的计算量'),
    'cpu_system_time': ('CPU系统时间', '秒', False, False,
        '累计在内核态运行的 CPU 时间，偏高多见于大量系统调用或 I/O'),
    'cpu_priority': ('优先级', '', True, False,
        '进程调度优先级，用于确认被测程序的优先级是否被改动'),
    'num_threads': ('线程数', '个', True, False,
        '进程内的线程总数，持续上涨说明线程创建后没有退出（线程泄漏）'),
    'num_handles': ('句柄数', '个', True, False,
        '进程打开的内核句柄数（文件、事件、互斥量等），持续上涨是句柄泄漏的信号，单进程上限约 1600 万'),
    'num_gdi_objects': ('GDI对象数', '个', True, False,
        '位图、画刷、字体等 GDI 资源数，单进程上限 10000，持续上涨会导致界面花屏或崩溃'),
    'num_user_objects': ('USER对象数', '个', True, False,
        '窗口、菜单、光标等 USER 对象数，单进程上限 10000，持续上涨说明窗口或菜单创建后没有销毁'),
    'num_ctx_switches_voluntary': ('自愿上下文切换', '次', True, False,
        '累计自愿上下文切换次数（线程主动等待让出 CPU），适合分析线程同步与阻塞'),
    'num_ctx_switches_involuntary': ('非自愿上下文切换', '次', True, False,
        '累计非自愿上下文切换次数（时间片用完被抢占）；已废弃：Windows 上无法采集，恒为 0'),
    'io_read_count': ('IO读取次数', '次', True, False,
        '累计读取操作次数，适合发现高频小块读取'),
    'io_write_count': ('IO写入次数', '次', True, False,
        '累计写入操作次数，适合发现高频小块写入'),
    'io_read_bytes': ('IO读取字节', 'KB', False, True,
        '累计读取字节数，适合发现异常的磁盘或网络读取量'),
    'io_write_bytes': ('IO写入字节', 'KB', False, True,
        '累计写入字节数，适合发现日志刷屏或临时文件持续增长'),
    'io_other_count': ('IO其他次数', '次', True, False,
        '累计其他 I/O（非读写，如设备控制）操作次数'),
    'io_other_bytes': ('IO其他字节', 'KB', False, True,
        '累计其他 I/O（非读写，如设备控制）传输的字节数'),
}

# 重构前 AVAILABLE_METRICS 的分组与组内顺序（指标选择对话框的展示顺序）
AVAILABLE_SNAPSHOT = {
    "内存": [
        "memory_rss", "memory_percent", "memory_peak_wset", "memory_private",
        "memory_peak_pagefile", "memory_paged_pool", "memory_peak_paged_pool",
        "memory_nonpaged_pool", "memory_peak_nonpaged_pool", "memory_num_page_faults",
        "memory_uss",
    ],
    "CPU": ["cpu_percent", "cpu_user_time", "cpu_system_time", "cpu_priority"],
    "系统资源": [
        "num_threads", "num_handles", "num_gdi_objects", "num_user_objects",
        "num_ctx_switches_voluntary",
    ],
    "IO操作": [
        "io_read_bytes", "io_write_bytes", "io_read_count", "io_write_count",
        "io_other_bytes", "io_other_count",
    ],
}

# 单调不减、增速不可外推的指标
CUMULATIVE = {
    "memory_peak_wset", "memory_peak_pagefile", "memory_peak_paged_pool",
    "memory_peak_nonpaged_pool", "memory_num_page_faults",
    "cpu_user_time", "cpu_system_time",
    "num_ctx_switches_voluntary", "num_ctx_switches_involuntary",
    "io_read_bytes", "io_write_bytes", "io_read_count", "io_write_count",
    "io_other_bytes", "io_other_count",
}

_MEMBER_KEYS = {
    value for name, value in vars(MetricType).items()
    if not name.startswith("_") and isinstance(value, str)
}


@pytest.fixture
def temp_metric():
    """运行时登记一个临时指标，用完移除，不污染其他测试。"""
    temp = MetricSpec(
        "temp_metric_for_test", "临时指标", "KB", "测试分组", MetricKind.BYTES_KB,
        "仅用于测试的临时指标", in_leak_preset=True)
    register_metric(temp)
    try:
        yield temp
    finally:
        unregister_metric(temp.key)


# ---------- 键集合 ----------

def test_registry_keys_are_26_available_plus_1_deprecated():
    assert len(REGISTRY) == 27
    assert set(REGISTRY) == _MEMBER_KEYS
    available = [k for k, s in REGISTRY.items() if not s.deprecated]
    assert len(available) == 26
    assert [k for k, s in REGISTRY.items() if s.deprecated] == [
        MetricType.NUM_CTX_SWITCHES_INVOL]
    for key, item in REGISTRY.items():
        assert item.key == key


def test_every_derived_table_matches_registry_keys():
    keys = set(REGISTRY)
    assert set(METRIC_DISPLAY_NAMES) == keys
    assert set(METRIC_UNITS) == keys
    assert set(METRIC_DESCRIPTIONS) == keys
    assert set(all_metric_types()) | DEPRECATED_METRICS == keys
    assert not (set(all_metric_types()) & DEPRECATED_METRICS)
    assert DEPRECATED_METRICS == {k for k, s in REGISTRY.items() if s.deprecated}
    assert set(LEAK_CHECK_PRESET) == {k for k, s in REGISTRY.items() if s.in_leak_preset}
    assert metrics._KB_VALUE_METRICS == {k for k in keys if is_kb_metric(k)}
    assert metrics._INTEGER_METRICS == {k for k in keys if is_integer_metric(k)}
    for key, item in REGISTRY.items():
        assert METRIC_DISPLAY_NAMES[key] == item.name
        assert METRIC_UNITS[key] == item.unit
        assert METRIC_DESCRIPTIONS[key] == item.description


def test_available_groups_and_order_unchanged():
    assert AVAILABLE_METRICS == AVAILABLE_SNAPSHOT
    assert list(AVAILABLE_METRICS) == list(AVAILABLE_SNAPSHOT)


def test_leak_preset_order_unchanged():
    assert LEAK_CHECK_PRESET == [
        MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES,
        MetricType.NUM_GDI_OBJECTS, MetricType.NUM_USER_OBJECTS,
        MetricType.NUM_THREADS,
    ]


def test_kb_unit_divisors_single_source():
    """图表坐标轴的换算表就是 utils.metrics 的同一个对象"""
    from ui.components.chart_axes import DISPLAY_UNIT_DIVISORS
    assert DISPLAY_UNIT_DIVISORS is KB_UNIT_DIVISORS
    assert metrics._KB_DISPLAY_DIVISORS is KB_UNIT_DIVISORS
    assert KB_UNIT_DIVISORS == {
        "KB": 1.0, "MB": 1024.0, "GB": 1024.0 ** 2, "TB": 1024.0 ** 3}


# ---------- kind ----------

def test_kind_covers_all_members_and_every_spec_has_valid_kind():
    used = {item.kind for item in REGISTRY.values()}
    assert used == set(MetricKind)
    for item in REGISTRY.values():
        assert isinstance(item.kind, MetricKind)


def test_peak_kind_only_for_memory_peaks_in_kb():
    peaks = {k for k, s in REGISTRY.items() if s.kind is MetricKind.PEAK}
    assert peaks == {
        "memory_peak_wset", "memory_peak_pagefile",
        "memory_peak_paged_pool", "memory_peak_nonpaged_pool"}
    for key in peaks:
        assert REGISTRY[key].unit == "KB" and is_kb_metric(key)


def test_requires_and_priming_values():
    assert {s.requires for s in REGISTRY.values()} <= {"", "windows", "admin"}
    assert [k for k, s in REGISTRY.items() if s.needs_priming] == [MetricType.CPU_PERCENT]
    assert spec(MetricType.MEMORY_USS).requires == "admin"
    assert spec(MetricType.NUM_GDI_OBJECTS).requires == "windows"


# ---------- 快照 ----------

def test_snapshot_keys_match_registry():
    assert set(SNAPSHOT) == set(REGISTRY)


@pytest.mark.parametrize("key", sorted(SNAPSHOT))
def test_snapshot_name_unit_integer_kb_description(key):
    name, unit, integer, kb, description = SNAPSHOT[key]
    assert get_metric_display_name(key) == name
    assert get_metric_unit(key) == unit
    assert is_integer_metric(key) is integer
    assert is_kb_metric(key) is kb
    assert get_metric_description(key) == description


# ---------- 查询函数 ----------

def test_spec_lookup_canonicalizes_and_rejects_unknown():
    assert spec("memory_vms") is REGISTRY[MetricType.MEMORY_PRIVATE]
    assert spec(MetricType.NUM_HANDLES).name == "句柄数"
    with pytest.raises(KeyError):
        spec("no_such_metric")


def test_display_unit_for_is_old_get_metric_display_unit():
    for key in REGISTRY:
        for ref in (0, 512, 2048, 3 * 1024.0 ** 2, 5 * 1024.0 ** 3):
            assert display_unit_for(key, ref) == get_metric_display_unit(key, ref)
    assert display_unit_for(MetricType.MEMORY_PRIVATE, 2048) == "MB"
    assert display_unit_for(MetricType.NUM_HANDLES, 1e9) == "个"
    assert display_unit_for("memory_pagefile", 5 * 1024.0 ** 3) == "TB"


def test_cumulative_metrics_list():
    assert {k for k in REGISTRY if is_cumulative_metric(k)} == CUMULATIVE
    assert is_cumulative_metric("no_such_metric") is False
    assert is_cumulative_metric("memory_vms") is False


def test_rate_not_applicable_for_cumulative_metrics():
    for key in REGISTRY:
        if key in CUMULATIVE:
            assert format_metric_rate(key, 1024.0) == RATE_NOT_APPLICABLE
            assert format_metric_rate(key, None) == RATE_NOT_APPLICABLE
        else:
            assert format_metric_rate(key, 1024.0).endswith("/小时")
    # 变化量与当前值仍照常显示
    assert format_metric_delta(MetricType.IO_READ_BYTES, 2048.0) == "+2 MB"
    assert format_metric_value(MetricType.IO_READ_COUNT, 12345) == "12,345 次"


def _old_sparkline_min_span(key, reference):
    """重构前 sparkline.min_span 的规则，逐项照抄（数据取自快照）。"""
    if key is None:
        return 0.0
    key = metrics.canonical_metric_type(key)
    _, _, integer, kb, _ = SNAPSHOT.get(key, ("", "", False, False, ""))
    if kb:
        return max(abs(reference) * 0.01, 64.0)
    if integer:
        return 4.0
    if key in ("memory_percent", "cpu_percent"):
        return 1.0
    return abs(reference) * 0.01


@pytest.mark.parametrize("key", sorted(SNAPSHOT) + ["memory_vms", "unknown_metric", None])
def test_min_span_for_matches_old_sparkline_rule(key):
    for reference in (0.0, 1.0, 37.5, 1000.0, -2048.0, 100 * 1024.0 ** 2):
        assert min_span_for(key, reference) == pytest.approx(
            _old_sparkline_min_span(key, reference))


def test_min_span_for_by_kind():
    assert min_span_for(MetricType.MEMORY_PRIVATE, 10.0) == MIN_SPAN_BYTES_KB
    assert min_span_for(MetricType.NUM_THREADS, 10.0) == MIN_SPAN_COUNT
    assert min_span_for(MetricType.CPU_PERCENT, 10.0) == MIN_SPAN_PERCENT
    assert min_span_for(MetricType.CPU_USER_TIME, 200.0) == pytest.approx(2.0)


# ---------- 新增指标只改注册表 ----------

def test_registering_a_metric_updates_every_derived_table(temp_metric):
    key = temp_metric.key
    assert spec(key) is temp_metric
    assert METRIC_DISPLAY_NAMES[key] == "临时指标"
    assert METRIC_UNITS[key] == "KB"
    assert METRIC_DESCRIPTIONS[key] == "仅用于测试的临时指标"
    assert AVAILABLE_METRICS["测试分组"] == [key]
    assert key in all_metric_types()
    assert key in metrics._KB_VALUE_METRICS
    assert key not in metrics._INTEGER_METRICS
    # 没有排位的预设项排在有排位的项之后
    assert LEAK_CHECK_PRESET[-1] == key
    assert format_metric_value(key, 2048.0) == "2 MB"
    assert min_span_for(key, 0.0) == MIN_SPAN_BYTES_KB


def test_unregistering_restores_tables(temp_metric):
    unregister_metric(temp_metric.key)
    assert temp_metric.key not in METRIC_DISPLAY_NAMES
    assert "测试分组" not in AVAILABLE_METRICS
    assert temp_metric.key not in LEAK_CHECK_PRESET
    assert len(REGISTRY) == 27
    assert AVAILABLE_METRICS == AVAILABLE_SNAPSHOT
