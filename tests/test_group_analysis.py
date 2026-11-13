"""
进程组任务的分析与判定：假时钟跑出一整段历史（约 11 分钟、5 秒一点），再用与分析页 / 命令行
相同的 run_history_query 查合计与成员序列，核对判定口径。

覆盖验收项：子进程每轮重建（PID 变、名字不变）时成员曲线连续、合计持平、不判上涨；每轮新增
一个不退出的子进程时进程总数判上涨；总量持平而成员此消彼长时，整体仍因成员上涨判 rising；
成员判定接口（member_verdicts / member_overall）；命令行 compute_verdict 与证据包带成员结论。
"""
from types import SimpleNamespace

import pytest

from core import verdict as v
from core.evidence import analysis_verdict, overall_line, readme_text
from core.history_query import (TaskAnalysis, build_request, load_member_snapshots,
                                member_series_keys, run_history_query)
from core.ipc.commands import compute_verdict
from tests.group_doubles import ROOT_CREATE_TIME, ROOT_PID, inst
from tests.test_group_worker import run_group
from tests.test_monitor_worker_clock import started  # noqa: F401
from utils.metrics import MetricType, member_series_key

MEM = MetricType.MEMORY_PRIVATE
COUNT = 'process_count'
INTERVAL = 5.0
CYCLES = 140          # 695 秒：满足趋势判定的最短跨度（10 分钟）与点数要求


def analysis_of(db, worker, metrics=(MEM, COUNT)) -> TaskAnalysis:
    task_id = worker.task_id
    snapshots = {m: run_history_query(db, build_request(task_id, m, 'all', analysis=True))
                 for m in metrics}
    members = load_member_snapshots(db, task_id, metrics, 'all', analysis=True)
    return TaskAnalysis(task_id, 'all', snapshots, member_snapshots=members)


def root(**values):
    return inst('app.exe', ROOT_PID, ROOT_CREATE_TIME, **values)


@pytest.fixture
def rebuilt_children(db, started):  # noqa: F811
    """每个周期两个 worker.exe 子进程整体换 PID，内存恒定"""
    def frames(t, n):
        return [root(memory_private=100.0),
                inst('worker.exe', 6000 + n * 2, memory_private=50.0, ppid=ROOT_PID),
                inst('worker.exe', 6001 + n * 2, memory_private=50.0, ppid=ROOT_PID)]
    return run_group(db, started, frames, cycles=CYCLES, interval=INTERVAL)


@pytest.fixture
def leaking_children(db, started):  # noqa: F811
    """每个周期多一个不退出的 leak.exe 子进程，内存各 10"""
    def frames(t, n):
        return [root(memory_private=100.0)] + [
            inst('leak.exe', 8000 + i, memory_private=10.0, ppid=ROOT_PID) for i in range(n)]
    return run_group(db, started, frames, cycles=CYCLES, interval=INTERVAL)


@pytest.fixture
def seesaw_members(db, started):  # noqa: F811
    """a.exe 内存每周期 +10、b.exe 每周期 -10：合计恒定，成员各自单调"""
    def frames(t, n):
        return [root(memory_private=100.0),
                inst('a.exe', 7001, memory_private=2000.0 + 10 * n, ppid=ROOT_PID),
                inst('b.exe', 7002, memory_private=4000.0 - 10 * n, ppid=ROOT_PID)]
    return run_group(db, started, frames, cycles=CYCLES, interval=INTERVAL)


# ========== 验收：重建式子进程不被误判 ==========

def test_children_rebuilt_every_cycle_are_not_judged_rising(db, rebuilt_children):
    analysis = analysis_of(db, rebuilt_children)
    assert analysis.snapshots[MEM].judgement.verdict.value in ('stable', 'fluctuating')
    assert analysis.snapshots[COUNT].judgement.verdict.value in ('stable', 'fluctuating')
    assert analysis.member_keys == ['app.exe', 'worker.exe']
    verdicts = v.member_verdicts(analysis)
    assert set(verdicts['worker.exe']) == {MEM, COUNT}
    assert 'rising' not in verdicts['worker.exe'].values()
    assert v.overall_verdict(analysis) != 'rising'
    # 成员曲线连续：全部周期都有点，没有被 PID 变化切成多段
    worker_mem = analysis.member_snapshot(MEM, 'worker.exe')
    assert worker_mem.total_count == CYCLES


# ========== 验收：每轮新增子进程 → 进程总数上涨 ==========

def test_new_child_every_cycle_makes_process_count_rise(db, leaking_children):
    analysis = analysis_of(db, leaking_children)
    assert analysis.snapshots[COUNT].judgement.verdict.value == 'rising'
    assert v.metric_verdict(analysis.snapshots[COUNT]) == 'rising'
    assert v.overall_verdict(analysis) == 'rising'
    members = v.member_verdicts(analysis)
    assert members['leak.exe'][COUNT] == 'rising'
    assert v.member_overall(analysis)['leak.exe'] == 'rising'
    assert v.member_overall(analysis)['app.exe'] != 'rising'


# ========== 总量持平、成员此消彼长 ==========

def test_flat_total_with_rising_member_is_overall_rising(db, seesaw_members):
    analysis = analysis_of(db, seesaw_members, metrics=(MEM,))
    assert v.metric_verdict(analysis.snapshots[MEM]) in ('stable', 'fluctuating')
    assert v.member_verdicts(analysis)['a.exe'][MEM] == 'rising'
    assert v.overall_verdict(analysis) == 'rising'
    # 去掉成员后同一份合计判不出上涨：证明整体上涨来自成员
    totals_only = TaskAnalysis(analysis.task_id, 'all', analysis.snapshots)
    assert v.overall_verdict(totals_only) != 'rising'


def test_load_member_snapshots_lists_member_series_in_creation_order(db, seesaw_members):
    keys = member_series_keys(db, seesaw_members.task_id, [MEM, COUNT])
    assert keys[:2] == [member_series_key(MEM, 'app.exe'), member_series_key(COUNT, 'app.exe')]
    assert member_series_key(MEM, 'b.exe') in keys
    # 只要 metrics 里的指标
    only = member_series_keys(db, seesaw_members.task_id, [COUNT])
    assert all(k.startswith(COUNT + '@') for k in only)


def test_load_member_snapshots_cancel_returns_none(db, seesaw_members):
    assert load_member_snapshots(db, seesaw_members.task_id, [MEM], 'all',
                                 is_cancelled=lambda: True) is None


def test_pid_task_analysis_has_no_members(db):
    assert load_member_snapshots(db, 'no-such-task', [MEM], 'all') == {}
    analysis = TaskAnalysis('t', 'all', {})
    assert analysis.member_snapshots == {} and analysis.member_keys == []
    assert analysis.member_snapshot(MEM, 'a.exe') is None


# ========== 判定接口（替身，不查库） ==========

def snap(verdict=None, points=(1, 2, 3)):
    judgement = SimpleNamespace(verdict=verdict, reason=None) if verdict else None
    return SimpleNamespace(judgement=judgement, points=list(points))


def fake_analysis(totals, members):
    return SimpleNamespace(snapshots=totals, member_snapshots=members)


def test_member_verdicts_group_by_member_and_strip_the_suffix():
    analysis = fake_analysis(
        {MEM: snap('stable')},
        {member_series_key(MEM, 'a.exe'): snap('rising'),
         member_series_key(COUNT, 'a.exe'): snap('stable'),
         member_series_key(MEM, 'b.exe'): snap('insufficient')})
    assert v.member_verdicts(analysis) == {
        'a.exe': {MEM: 'rising', COUNT: 'stable'}, 'b.exe': {MEM: 'insufficient'}}
    assert v.member_overall(analysis) == {'a.exe': 'rising', 'b.exe': 'insufficient'}


@pytest.mark.parametrize('totals, members, expected', [
    ('stable', ['stable'], 'stable'),
    ('stable', ['rising'], 'rising'),
    ('rising', ['stable'], 'rising'),
    ('insufficient', ['stable'], 'stable'),      # 合计数据不足但成员有结论
    ('insufficient', ['insufficient'], 'insufficient'),
])
def test_overall_verdict_combines_total_and_members(totals, members, expected):
    analysis = fake_analysis(
        {MEM: snap(totals)},
        {member_series_key(MEM, f'm{i}.exe'): snap(x) for i, x in enumerate(members)})
    assert v.overall_verdict(analysis) == expected


def test_analysis_without_member_attribute_is_a_pid_analysis():
    analysis = SimpleNamespace(snapshots={MEM: snap('stable')})
    assert v.member_verdicts(analysis) == {}
    assert v.overall_verdict(analysis) == 'stable'


# ========== 命令行 verdict 与证据包 ==========

def test_compute_verdict_lists_member_conclusions(db, seesaw_members):
    analysis = analysis_of(db, seesaw_members, metrics=(MEM,))
    result = compute_verdict(analysis)
    assert result['verdict'] == 'rising'
    assert [m['key'] for m in result['metrics']] == [MEM]
    groups = {g['key']: g for g in result['members']}
    assert groups['a.exe']['verdict'] == 'rising'
    entry = groups['a.exe']['metrics'][0]
    assert entry['key'] == member_series_key(MEM, 'a.exe')
    assert entry['metric'] == MEM and entry['member'] == 'a.exe'
    assert entry['rate_per_hour'] is not None and entry['rate_per_hour'] > 0


def test_compute_verdict_of_pid_task_has_no_members_field(db, rebuilt_children):
    analysis = analysis_of(db, rebuilt_children)
    totals_only = TaskAnalysis(analysis.task_id, 'all', analysis.snapshots)
    assert 'members' not in compute_verdict(totals_only)


def test_evidence_verdict_and_readme_carry_members(db, seesaw_members):
    analysis = analysis_of(db, seesaw_members, metrics=(MEM,))
    verdict = analysis_verdict(analysis)
    assert verdict['overall'] == 'rising'
    groups = {g['key']: g for g in verdict['members']}
    assert groups['a.exe']['verdict'] == 'rising'
    assert groups['b.exe']['metrics'][0]['member'] == 'b.exe'
    line = overall_line(verdict)
    assert '（a.exe）' in line   # 结论里点名是哪个成员在涨
    task = db.get_task(seesaw_members.task_id)
    text = readme_text(task, {'verdict': verdict}, target=None, files=[],
                       generated_at=task.start_time)
    assert '成员 a.exe：持续上涨' in text
    assert '进程树，根进程 PID' in text
