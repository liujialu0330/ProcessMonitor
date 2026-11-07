"""
GroupAggregator：逐实例读数 → 合计序列 + 成员序列（纯逻辑，无线程、无数据库）

覆盖：水位类求和；累计类虚拟累计（表驱动：首现只记起点 / 正增量 / 回绕 / 实例消失 /
PID 复用）；CPU 使用率逐实例差分；进程总数；零成员周期；成员名截断；峰值类不适用；
成员合并不区分大小写；实例出现 / 消失与 members_changed。
"""
import pytest

from core.contracts import UnavailableReason
from core.group_aggregator import GroupAggregator
from core.process_tree import Observation
from tests.group_doubles import inst
from utils.metrics import (MAX_GROUP_MEMBERS, PROCESS_COUNT, MetricType, member_series_key)


def obs(instances, mono=1000.0):
    return Observation(taken_monotonic=mono, instances=tuple(instances))


def total_of(sample, metric):
    return sample.values[metric]


def member_of(sample, metric, member):
    return sample.values[member_series_key(metric, member)]


# ========== 水位类 ==========

def test_level_metrics_sum_over_instances_and_members():
    agg = GroupAggregator()
    sample = agg.update(obs([
        inst('chrome.exe', 11, memory_private=100.0, num_handles=10),
        inst('chrome.exe', 12, memory_private=50.0, num_handles=5),
        inst('gpu.exe', 13, memory_private=30.0, num_handles=2),
    ]), [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES])
    assert total_of(sample, MetricType.MEMORY_PRIVATE) == 180.0
    assert total_of(sample, MetricType.NUM_HANDLES) == 17.0
    assert member_of(sample, MetricType.MEMORY_PRIVATE, 'chrome.exe') == 150.0
    assert member_of(sample, MetricType.MEMORY_PRIVATE, 'gpu.exe') == 30.0
    # 合计 = 各成员之和
    members = sum(member_of(sample, MetricType.MEMORY_PRIVATE, k)
                  for k in ('chrome.exe', 'gpu.exe'))
    assert members == total_of(sample, MetricType.MEMORY_PRIVATE)


def test_process_count_total_and_per_member():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1), inst('a.exe', 2), inst('b.exe', 3)]),
                        [PROCESS_COUNT])
    assert sample.values[PROCESS_COUNT] == 3.0
    assert member_of(sample, PROCESS_COUNT, 'a.exe') == 2.0
    assert member_of(sample, PROCESS_COUNT, 'b.exe') == 1.0
    assert sample.instance_count == 3


def test_member_merge_is_case_insensitive_and_keeps_first_display_name():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('Worker.EXE', 1, memory_private=10.0),
                             inst('worker.exe', 2, memory_private=20.0)]),
                        [MetricType.MEMORY_PRIVATE])
    assert member_of(sample, MetricType.MEMORY_PRIVATE, 'worker.exe') == 30.0
    assert agg.tracked_members() == {'worker.exe': 'Worker.EXE'}


def test_children_rebuilt_every_cycle_keep_member_curve_continuous():
    """PID 每个周期都换、名字不变：成员序列和合计都是平的，成员只有一个"""
    agg = GroupAggregator()
    totals, members = [], []
    for n in range(6):
        sample = agg.update(obs([inst('worker.exe', 100 + n, memory_private=64.0),
                                 inst('worker.exe', 200 + n, memory_private=64.0)],
                                mono=1000.0 + n),
                            [MetricType.MEMORY_PRIVATE, PROCESS_COUNT])
        totals.append(total_of(sample, MetricType.MEMORY_PRIVATE))
        members.append(member_of(sample, MetricType.MEMORY_PRIVATE, 'worker.exe'))
        assert sample.values[PROCESS_COUNT] == 2.0
    assert totals == [128.0] * 6 and members == [128.0] * 6
    assert list(agg.tracked_members()) == ['worker.exe']


def test_instance_without_a_reading_is_left_out_of_that_metric_only():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1, memory_private=10.0),
                             inst('a.exe', 2, memory_private=5.0, num_handles=7)]),
                        [MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES])
    assert total_of(sample, MetricType.MEMORY_PRIVATE) == 15.0
    assert total_of(sample, MetricType.NUM_HANDLES) == 7.0


def test_metric_nobody_could_read_is_unavailable_with_the_reason():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1, unavailable={
        MetricType.MEMORY_USS: UnavailableReason.ACCESS_DENIED})]),
        [MetricType.MEMORY_USS])
    assert MetricType.MEMORY_USS not in sample.values
    assert sample.unavailable[MetricType.MEMORY_USS] is UnavailableReason.ACCESS_DENIED


# ========== 累计类（虚拟累计） ==========

# (说明, [(各周期的实例读数: [(name, pid, 读数)])...], 期望的合计序列)
CUMULATIVE_CASES = [
    ('首次出现只记起点', [[('a.exe', 1, 100)]], [0.0]),
    ('只计正增量', [[('a.exe', 1, 100)], [('a.exe', 1, 130)], [('a.exe', 1, 150)]],
     [0.0, 30.0, 50.0]),
    ('读数不变不增', [[('a.exe', 1, 100)], [('a.exe', 1, 100)]], [0.0, 0.0]),
    ('回绕只重设起点不扣减', [[('a.exe', 1, 100)], [('a.exe', 1, 20)], [('a.exe', 1, 25)]],
     [0.0, 0.0, 5.0]),
    ('实例消失已计入的不扣回',
     [[('a.exe', 1, 100)], [('a.exe', 1, 160)], []], [0.0, 60.0]),
    ('新实例首现不计入它的存量',
     [[('a.exe', 1, 100)], [('a.exe', 1, 110), ('a.exe', 2, 9000)],
      [('a.exe', 1, 120), ('a.exe', 2, 9050)]], [0.0, 10.0, 70.0]),
    ('实例整体换新：旧实例的存量与新实例的起点都不凭空产生增量',
     [[('a.exe', 1, 100)], [('a.exe', 2, 5000)], [('a.exe', 2, 5010)]], [0.0, 0.0, 10.0]),
]


@pytest.mark.parametrize('label,cycles,expected', CUMULATIVE_CASES,
                         ids=[c[0] for c in CUMULATIVE_CASES])
def test_cumulative_metric_follows_positive_increment_rule(label, cycles, expected):
    agg = GroupAggregator()
    got = []
    for n, cycle in enumerate(cycles):
        sample = agg.update(
            obs([inst(name, pid, io_read_bytes=float(value)) for name, pid, value in cycle],
                mono=1000.0 + n), [MetricType.IO_READ_BYTES])
        if MetricType.IO_READ_BYTES in sample.values:
            got.append(sample.values[MetricType.IO_READ_BYTES])
    assert got == expected


def test_cumulative_member_series_only_counts_its_own_instances():
    agg = GroupAggregator()
    a1 = [inst('a.exe', 1, io_read_bytes=100.0), inst('b.exe', 2, io_read_bytes=500.0)]
    a2 = [inst('a.exe', 1, io_read_bytes=160.0), inst('b.exe', 2, io_read_bytes=510.0)]
    agg.update(obs(a1, 1000.0), [MetricType.IO_READ_BYTES])
    sample = agg.update(obs(a2, 1001.0), [MetricType.IO_READ_BYTES])
    assert member_of(sample, MetricType.IO_READ_BYTES, 'a.exe') == 60.0
    assert member_of(sample, MetricType.IO_READ_BYTES, 'b.exe') == 10.0
    assert total_of(sample, MetricType.IO_READ_BYTES) == 70.0


def test_pid_reuse_is_a_different_instance_for_cumulative_baseline():
    """同 PID、创建时间不同 = 另一个实例：新实例首现只记起点"""
    agg = GroupAggregator()
    agg.update(obs([inst('a.exe', 7, 1001.0, io_read_bytes=100.0)], 1000.0),
               [MetricType.IO_READ_BYTES])
    sample = agg.update(obs([inst('a.exe', 7, 2002.0, io_read_bytes=10.0)], 1001.0),
                        [MetricType.IO_READ_BYTES])
    assert total_of(sample, MetricType.IO_READ_BYTES) == 0.0


# ========== CPU 使用率 ==========

def test_cpu_percent_is_differenced_per_instance_and_summed():
    agg = GroupAggregator()
    base = [inst('a.exe', 1, cpu=10.0), inst('b.exe', 2, cpu=20.0)]
    agg.prime(obs(base, 1000.0))
    sample = agg.update(obs([inst('a.exe', 1, cpu=10.5), inst('b.exe', 2, cpu=21.0)], 1001.0),
                        [MetricType.CPU_PERCENT])
    assert total_of(sample, MetricType.CPU_PERCENT) == 150.0
    assert member_of(sample, MetricType.CPU_PERCENT, 'a.exe') == 50.0
    assert member_of(sample, MetricType.CPU_PERCENT, 'b.exe') == 100.0


def test_cpu_new_instance_is_excluded_in_its_first_cycle():
    agg = GroupAggregator()
    agg.prime(obs([inst('a.exe', 1, cpu=10.0)], 1000.0))
    # 新进程带着已有的 5000 秒 CPU 时间出现：首个周期没有差分起点，不能算成 500000%
    sample = agg.update(obs([inst('a.exe', 1, cpu=10.5), inst('a.exe', 2, cpu=5000.0)], 1001.0),
                        [MetricType.CPU_PERCENT])
    assert total_of(sample, MetricType.CPU_PERCENT) == 50.0
    sample = agg.update(obs([inst('a.exe', 1, cpu=11.0), inst('a.exe', 2, cpu=5000.25)], 1002.0),
                        [MetricType.CPU_PERCENT])
    assert total_of(sample, MetricType.CPU_PERCENT) == 75.0


def test_cpu_all_instances_new_after_a_valid_point_writes_zero():
    agg = GroupAggregator()
    agg.prime(obs([inst('a.exe', 1, cpu=1.0)], 1000.0))
    agg.update(obs([inst('a.exe', 1, cpu=1.5)], 1001.0), [MetricType.CPU_PERCENT])
    sample = agg.update(obs([inst('a.exe', 2, cpu=9.0)], 1002.0), [MetricType.CPU_PERCENT])
    assert total_of(sample, MetricType.CPU_PERCENT) == 0.0


def test_cpu_without_any_baseline_writes_nothing():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1, cpu=1.0)], 1000.0), [MetricType.CPU_PERCENT])
    assert MetricType.CPU_PERCENT not in sample.values
    assert MetricType.CPU_PERCENT not in sample.unavailable


def test_cpu_unreadable_everywhere_is_unavailable():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1)]), [MetricType.CPU_PERCENT])
    assert sample.unavailable[MetricType.CPU_PERCENT] is UnavailableReason.ERROR


# ========== 不适用的指标 ==========

@pytest.mark.parametrize('metric', [MetricType.MEMORY_PEAK_WSET, MetricType.CPU_PRIORITY])
def test_metrics_not_applicable_to_groups_are_not_supported(metric):
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1, **{metric: 5.0})]),
                        [metric, MetricType.MEMORY_PRIVATE])
    assert metric not in sample.values
    assert sample.unavailable[metric] is UnavailableReason.NOT_SUPPORTED
    assert not any(k.startswith(metric + '@') for k in sample.values)


# ========== 零成员周期 ==========

def test_zero_member_cycle_writes_only_process_counts():
    agg = GroupAggregator()
    metrics = [MetricType.MEMORY_PRIVATE, MetricType.IO_READ_BYTES, PROCESS_COUNT]
    agg.update(obs([inst('a.exe', 1, memory_private=10.0, io_read_bytes=5.0)], 1000.0), metrics)
    sample = agg.update(obs([], 1001.0), metrics)
    assert sample.values == {PROCESS_COUNT: 0.0,
                             member_series_key(PROCESS_COUNT, 'a.exe'): 0.0}
    assert sample.instance_count == 0
    assert sample.unavailable == {}
    # 成员仍在列表里（已建序列），只是本周期没有实例
    assert [(m.member_key, m.instance_count) for m in sample.members] == [('a.exe', 0)]


def test_zero_member_from_the_start_has_no_members_and_count_zero():
    agg = GroupAggregator()
    sample = agg.update(obs([]), [MetricType.MEMORY_PRIVATE, PROCESS_COUNT])
    assert sample.values == {PROCESS_COUNT: 0.0}
    assert sample.members == ()


def test_cumulative_resumes_without_double_counting_after_zero_member_gap():
    agg = GroupAggregator()
    metrics = [MetricType.IO_READ_BYTES]
    agg.update(obs([inst('a.exe', 1, io_read_bytes=100.0)], 1000.0), metrics)
    agg.update(obs([inst('a.exe', 1, io_read_bytes=150.0)], 1001.0), metrics)
    agg.update(obs([], 1002.0), metrics)
    sample = agg.update(obs([inst('a.exe', 9, io_read_bytes=7000.0)], 1003.0), metrics)
    assert total_of(sample, MetricType.IO_READ_BYTES) == 50.0


# ========== 成员名截断 ==========

def test_member_names_are_capped_and_overflow_counts_in_total_only():
    agg = GroupAggregator()
    cap = MAX_GROUP_MEMBERS
    instances = [inst(f'p{i:02d}.exe', 100 + i, memory_private=1.0) for i in range(cap + 3)]
    sample = agg.update(obs(instances), [MetricType.MEMORY_PRIVATE, PROCESS_COUNT])
    assert len(agg.tracked_members()) == cap
    assert sample.members_truncated and agg.members_truncated
    assert total_of(sample, MetricType.MEMORY_PRIVATE) == float(cap + 3)
    assert sample.values[PROCESS_COUNT] == float(cap + 3)
    member_series = [k for k in sample.values if '@' in k]
    assert len(member_series) == cap * 2   # 每个成员：内存 + 进程数
    assert 'p22.exe' not in ' '.join(member_series)


def test_truncation_flag_and_reserved_slots_are_sticky():
    agg = GroupAggregator(max_members=2)
    agg.update(obs([inst('a.exe', 1), inst('b.exe', 2), inst('c.exe', 3)]), [PROCESS_COUNT])
    assert agg.members_truncated
    # a、b 先到先得，之后即使消失也终身占位；c 不会顶替
    sample = agg.update(obs([inst('c.exe', 3)]), [PROCESS_COUNT])
    assert list(agg.tracked_members()) == ['a.exe', 'b.exe']
    assert sample.members_truncated
    assert sample.values[PROCESS_COUNT] == 1.0
    assert member_series_key(PROCESS_COUNT, 'c.exe') not in sample.values


def test_truncation_logs_only_once(caplog):
    agg = GroupAggregator(max_members=1, label='t1')
    with caplog.at_level('WARNING', logger='core.group_aggregator'):
        agg.update(obs([inst('a.exe', 1), inst('b.exe', 2)]), [PROCESS_COUNT])
        agg.update(obs([inst('a.exe', 1), inst('b.exe', 2), inst('c.exe', 3)]), [PROCESS_COUNT])
    assert sum('上限' in r.message for r in caplog.records) == 1


def test_simultaneous_new_names_are_admitted_by_creation_time_not_enumeration_order():
    agg = GroupAggregator(max_members=1)
    agg.update(obs([inst('late.exe', 1, 1500.0), inst('early.exe', 2, 1100.0)]),
               [PROCESS_COUNT])
    assert list(agg.tracked_members()) == ['early.exe']


# ========== 出现 / 消失与 members_changed ==========

def test_appeared_and_disappeared_instances():
    agg = GroupAggregator()
    first = agg.update(obs([inst('a.exe', 1), inst('a.exe', 2)]), [PROCESS_COUNT])
    assert {i.pid for i in first.appeared} == {1, 2} and first.disappeared == ()
    second = agg.update(obs([inst('a.exe', 2), inst('a.exe', 3)]), [PROCESS_COUNT])
    assert {i.pid for i in second.appeared} == {3}
    assert {i.pid for i in second.disappeared} == {1}
    assert {i.pid for i in second.alive} == {2, 3}
    assert {i.pid for i in agg.alive_instances()} == {2, 3}


def test_pid_reuse_shows_as_disappear_plus_appear():
    agg = GroupAggregator()
    agg.update(obs([inst('a.exe', 7, 1001.0)]), [PROCESS_COUNT])
    sample = agg.update(obs([inst('a.exe', 7, 2002.0)]), [PROCESS_COUNT])
    assert [(i.pid, i.create_time) for i in sample.appeared] == [(7, 2002.0)]
    assert [(i.pid, i.create_time) for i in sample.disappeared] == [(7, 1001.0)]


def test_members_changed_only_when_member_set_changes():
    agg = GroupAggregator()
    flags = []
    cycles = [
        [inst('a.exe', 1)],                                # 首个周期：变化
        [inst('a.exe', 1), inst('a.exe', 2)],              # 只是实例数涨：不变
        [inst('a.exe', 5), inst('a.exe', 6)],              # PID 全换、成员不变：不变
        [inst('a.exe', 5), inst('b.exe', 7)],              # 新成员：变化
        [inst('b.exe', 7)],                                # a 本周期无实例：变化
        [inst('b.exe', 7)],                                # 不变
        [],                                                # 全部归零：变化
    ]
    for n, cycle in enumerate(cycles):
        flags.append(agg.update(obs(cycle, 1000.0 + n), [PROCESS_COUNT]).members_changed)
    assert flags == [True, False, False, True, True, False, True]


def test_member_snapshot_carries_per_member_values():
    agg = GroupAggregator()
    sample = agg.update(obs([inst('a.exe', 1, memory_private=10.0),
                             inst('a.exe', 2, memory_private=5.0)]),
                        [MetricType.MEMORY_PRIVATE, PROCESS_COUNT])
    (member,) = sample.members
    assert member.member_key == 'a.exe' and member.name == 'a.exe'
    assert member.instance_count == 2
    assert member.values == {MetricType.MEMORY_PRIVATE: 15.0, PROCESS_COUNT: 2.0}
