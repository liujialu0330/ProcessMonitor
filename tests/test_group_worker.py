"""
进程组任务（tree / name）经 MonitorWorker 的端到端行为：假时钟 + 假 ProcessTreeSource，
真实 QThread 与真实数据库，不碰真实进程。

覆盖验收项：子进程每轮重建时成员曲线连续、合计持平；合计等于各成员之和；根退出结束任务而
子进程退出不结束；name 任务成员归零不结束；成员名超上限截断；task_members 留痕的首末时间与
PID 复用；data_updated 只带合计；members_changed 只在成员集合变化时发。
"""
import os
from datetime import timedelta

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import MonitorWorker
from core.process_tree import GroupSpec
from data.mappers import to_ms
from data.repos.point_repo import PointQuery
from tests.group_doubles import (ROOT_CREATE_TIME, ROOT_PID, FakeTreeSource, Frame, inst)
from tests.test_monitor_worker_clock import T0, SteppingClock, started  # noqa: F401
from tests.test_monitor_worker_clock import _run_to_end
from utils.metrics import MetricType, member_series_key

MEM = MetricType.MEMORY_PRIVATE
HANDLES = MetricType.NUM_HANDLES
COUNT = 'process_count'


def tree_spec():
    return GroupSpec.tree(ROOT_PID, ROOT_CREATE_TIME)


def run_group(db, started, frames, *, spec=None, metrics=(MEM, COUNT), cycles=6, interval=1.0,
              expected_create_time=ROOT_CREATE_TIME, process_name='app.exe', pid=ROOT_PID):
    """跑一个进程组任务到第 cycles 个周期结束（或任务自行结束）。返回 worker。
    frames(t, n) 给出模拟时刻 t（秒）、第 n 个周期（0 起）的组状态。"""
    spec = spec or tree_spec()
    clock = SteppingClock()
    holder = []

    def on_observe(t):
        # 启动检查与预热也会 observe；它们发生在 t=0，cycles>=2 时不会触发
        if t >= (cycles - 1) * interval - 1e-9:
            holder[0].stop()

    source = FakeTreeSource(lambda t: frames(t, int(round(t / interval))), clock,
                            on_observe=on_observe)
    worker = MonitorWorker(
        pid=pid, process_name=process_name, metric_types=list(metrics), interval=interval,
        db=db, clock=clock, create_time=expected_create_time, group_spec=spec,
        tree_source=source)
    worker.stops = []
    # 直连：测试线程没有事件循环，队列连接的槽不会被执行
    worker.task_stopped.connect(lambda _tid, reason: worker.stops.append(reason),
                                Qt.DirectConnection)
    worker.changed = []
    worker.members_changed.connect(worker.changed.append, Qt.DirectConnection)
    worker.updates = []
    worker.data_updated.connect(lambda _tid, values: worker.updates.append(values),
                                Qt.DirectConnection)
    worker.source = source
    holder.append(worker)
    _run_to_end(worker, started)
    return worker


def series(db, worker, metric, member=None):
    key = metric if member is None else member_series_key(metric, member)
    return [p.value for p in db.points.fetch(PointQuery(worker.task_id, key))]


def stamps(db, worker, metric, member=None):
    key = metric if member is None else member_series_key(metric, member)
    return [p.timestamp for p in db.points.fetch(PointQuery(worker.task_id, key))]


def root(**values):
    return inst('app.exe', ROOT_PID, ROOT_CREATE_TIME, **values)


# ========== 任务行 ==========

def test_task_row_records_target_kind_and_spec(db, started):
    worker = run_group(db, started, lambda t, n: [root(memory_private=10.0)], cycles=2)
    task = db.get_task(worker.task_id)
    assert task.target_kind == 'tree'
    assert GroupSpec.from_json('tree', task.target_spec) == tree_spec()
    assert worker.target_kind == 'tree'


def test_name_task_row_records_name_spec(db, started):
    spec = GroupSpec.by_name('Worker.exe')
    worker = run_group(db, started, lambda t, n: [inst('worker.exe', 1, memory_private=1.0)],
                       spec=spec, pid=0, process_name='Worker.exe', expected_create_time=None,
                       cycles=2)
    task = db.get_task(worker.task_id)
    assert task.target_kind == 'name' and task.pid == 0
    assert GroupSpec.from_json('name', task.target_spec) == spec


# ========== 成员曲线与合计 ==========

def test_children_rebuilt_every_cycle_member_curve_continuous_total_flat(db, started):
    """子进程每轮换 PID、名字不变：成员序列连续，合计持平，成员只有一个"""
    def frames(t, n):
        return [root(memory_private=100.0),
                inst('worker.exe', 6000 + n * 2, memory_private=50.0, ppid=ROOT_PID),
                inst('worker.exe', 6001 + n * 2, memory_private=50.0, ppid=ROOT_PID)]
    worker = run_group(db, started, frames, cycles=6)
    assert series(db, worker, MEM) == [200.0] * 6
    assert series(db, worker, MEM, 'worker.exe') == [100.0] * 6
    assert series(db, worker, MEM, 'app.exe') == [100.0] * 6
    assert series(db, worker, COUNT) == [3.0] * 6
    assert series(db, worker, COUNT, 'worker.exe') == [2.0] * 6
    assert [m.member_key for m in worker.group_members()] == ['app.exe', 'worker.exe']
    # 成员序列时间戳连续等距
    ts = stamps(db, worker, MEM, 'worker.exe')
    assert [ts[i + 1] - ts[i] for i in range(5)] == [timedelta(seconds=1)] * 5


def test_total_equals_sum_of_members_every_cycle(db, started):
    def frames(t, n):
        return [root(memory_private=100.0 + n, num_handles=10),
                inst('a.exe', 7001, memory_private=10.0 * n, num_handles=n, ppid=ROOT_PID),
                inst('b.exe', 7002, memory_private=5.0, num_handles=3, ppid=ROOT_PID),
                inst('b.exe', 7003, memory_private=5.0, num_handles=3, ppid=ROOT_PID)]
    worker = run_group(db, started, frames, metrics=(MEM, HANDLES, COUNT), cycles=6)
    for metric in (MEM, HANDLES, COUNT):
        total = series(db, worker, metric)
        parts = [series(db, worker, metric, m) for m in ('app.exe', 'a.exe', 'b.exe')]
        assert len(total) == 6
        assert total == [sum(col) for col in zip(*parts)], metric


def test_new_child_every_cycle_grows_process_count(db, started):
    def frames(t, n):
        return [root()] + [inst('leak.exe', 8000 + i, ppid=ROOT_PID) for i in range(n)]
    worker = run_group(db, started, frames, metrics=(COUNT,), cycles=6)
    assert series(db, worker, COUNT) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    # 成员序列从它首次出现的周期开始
    assert series(db, worker, COUNT, 'leak.exe') == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_cumulative_total_follows_positive_increment_rule(db, started):
    def frames(t, n):
        rows = [root(io_read_bytes=100.0 + 10 * n)]
        if 1 <= n <= 3:
            rows.append(inst('c.exe', 9000, io_read_bytes=5000.0 + 20 * n, ppid=ROOT_PID))
        return rows
    worker = run_group(db, started, frames, metrics=(MetricType.IO_READ_BYTES, COUNT), cycles=6)
    # n=0 起点；n=1 根 +10，c 首现不计；n=2 根 +10、c +20；n=3 同；n=4 c 消失不扣；n=5 根 +10
    assert series(db, worker, MetricType.IO_READ_BYTES) == [0.0, 10.0, 40.0, 70.0, 80.0, 90.0]


# ========== 结束语义 ==========

def test_root_exit_ends_the_task_with_process_exited(db, started):
    def frames(t, n):
        if n >= 3:
            return Frame([], root_alive=False)
        return [root(memory_private=1.0), inst('c.exe', 7001, memory_private=1.0)]
    worker = run_group(db, started, frames, cycles=10)
    assert worker.stops == [StopReason.PROCESS_EXITED]
    assert len(series(db, worker, MEM)) == 3
    assert worker.state.name == 'STOPPED'


def test_child_exit_does_not_end_the_task(db, started):
    def frames(t, n):
        rows = [root(memory_private=1.0)]
        if n < 3:
            rows.append(inst('c.exe', 7001, memory_private=1.0, ppid=ROOT_PID))
        return rows
    worker = run_group(db, started, frames, cycles=8)
    assert worker.stops == [StopReason.USER]
    assert series(db, worker, COUNT) == [2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    assert series(db, worker, COUNT, 'c.exe') == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    # 子进程成员已退出：其内存序列在它消失的周期起不再写
    assert len(series(db, worker, MEM, 'c.exe')) == 3


def test_tree_root_missing_at_start_is_process_not_found(db, started):
    worker = run_group(db, started, lambda t, n: Frame([], root_alive=False), cycles=3)
    assert worker.stops == [StopReason.PROCESS_NOT_FOUND]
    assert series(db, worker, COUNT) == []


def test_name_task_zero_members_does_not_end_and_writes_only_counts(db, started):
    spec = GroupSpec.by_name('w.exe')

    def frames(t, n):
        return [inst('w.exe', 5, memory_private=7.0)] if n < 3 else []
    worker = run_group(db, started, frames, spec=spec, pid=0, process_name='w.exe',
                       expected_create_time=None, cycles=7)
    assert worker.stops == [StopReason.USER]
    assert series(db, worker, COUNT) == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    assert series(db, worker, COUNT, 'w.exe') == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    assert series(db, worker, MEM) == [7.0, 7.0, 7.0]   # 归零后留空洞，不写 0
    assert series(db, worker, MEM, 'w.exe') == [7.0, 7.0, 7.0]


def test_name_task_may_start_with_no_members(db, started):
    spec = GroupSpec.by_name('w.exe')

    def frames(t, n):
        return [inst('w.exe', 5, memory_private=7.0)] if n >= 2 else []
    worker = run_group(db, started, frames, spec=spec, pid=0, process_name='w.exe',
                       expected_create_time=None, cycles=5)
    assert worker.stops == [StopReason.USER]
    assert series(db, worker, COUNT) == [0.0, 0.0, 1.0, 1.0, 1.0]
    assert series(db, worker, MEM) == [7.0, 7.0, 7.0]


# ========== 截止与信号 ==========

def test_member_names_above_cap_are_truncated_but_counted_in_total(db, started):
    def frames(t, n):
        return [root()] + [inst(f'n{i:02d}.exe', 7000 + i, memory_private=1.0)
                           for i in range(24)]
    worker = run_group(db, started, frames, cycles=3)
    assert worker.members_truncated
    assert series(db, worker, COUNT) == [25.0] * 3
    assert len(worker.group_members()) == 20
    members = db.members.member_keys(worker.task_id)
    assert len(members) == 20
    assert series(db, worker, MEM) == [24.0] * 3   # 溢出的进程仍在合计里


def test_data_updated_carries_totals_only(db, started):
    worker = run_group(db, started, lambda t, n: [root(memory_private=3.0)], cycles=3)
    assert len(worker.updates) == 3
    for values in worker.updates:
        assert set(values) == {MEM, COUNT}


def test_members_changed_emitted_only_when_member_set_changes(db, started):
    def frames(t, n):
        rows = [root()]
        if n >= 2:
            rows.append(inst('a.exe', 7001, ppid=ROOT_PID))
        if n >= 4:
            rows.append(inst('a.exe', 7002, ppid=ROOT_PID))   # 同名新实例：成员集合不变
        return rows
    worker = run_group(db, started, frames, metrics=(COUNT,), cycles=6)
    assert len(worker.changed) == 2   # 首个周期 + a.exe 出现
    assert set(worker.changed) == {worker.task_id}


def test_pid_task_never_emits_members_changed_and_has_no_members(db, started):
    from tests.test_monitor_worker_clock import ScriptedCollector, stop_after
    clock = SteppingClock()
    holder = []
    worker = MonitorWorker(pid=os.getpid(), process_name='x.exe', metric_types=[MEM],
                           interval=1.0, db=db, clock=clock)
    worker.collector = ScriptedCollector([MEM], after_collect=stop_after(holder, 3))
    holder.append(worker)
    emitted = []
    worker.members_changed.connect(emitted.append, Qt.DirectConnection)
    _run_to_end(worker, started)
    assert emitted == [] and worker.group_members() == ()
    assert db.list_members(worker.task_id) == []
    assert db.get_task(worker.task_id).target_kind == 'pid'


# ========== task_members 留痕 ==========

def ms(seconds):
    return to_ms(T0 + timedelta(seconds=seconds))


def test_member_trail_records_first_and_last_seen(db, started):
    def frames(t, n):
        rows = [root()]
        if 1 <= n <= 3:
            rows.append(inst('c.exe', 7001, 1070.0, ppid=ROOT_PID))
        return rows
    worker = run_group(db, started, frames, metrics=(COUNT,), cycles=7)
    rows = {(r.pid, r.create_time): r for r in db.members.list_instances(worker.task_id)}
    assert rows[(ROOT_PID, ROOT_CREATE_TIME)].first_ms == ms(0)
    # 根进程一直活到任务结束：last_ms 是最后一个周期
    assert rows[(ROOT_PID, ROOT_CREATE_TIME)].last_ms == ms(6)
    child = rows[(7001, 1070.0)]
    assert (child.first_ms, child.last_ms) == (ms(1), ms(3))   # 最后一次见到是第 3 周期
    assert child.name == 'c.exe'


def test_member_trail_does_not_confuse_reused_pid(db, started):
    def frames(t, n):
        rows = [root()]
        if n <= 2:
            rows.append(inst('c.exe', 7001, 1070.0, ppid=ROOT_PID))
        elif n >= 3:
            rows.append(inst('c.exe', 7001, 2080.0, ppid=ROOT_PID))   # 同 PID、新实例
        return rows
    worker = run_group(db, started, frames, metrics=(COUNT,), cycles=6)
    rows = {(r.pid, r.create_time): r for r in db.members.list_instances(worker.task_id)}
    assert (rows[(7001, 1070.0)].first_ms, rows[(7001, 1070.0)].last_ms) == (ms(0), ms(2))
    assert (rows[(7001, 2080.0)].first_ms, rows[(7001, 2080.0)].last_ms) == (ms(3), ms(5))
    members = {m.member_key: m for m in db.members.list_members(worker.task_id)}
    assert members['c.exe'].instance_count == 2


def test_member_trail_closed_when_root_exits(db, started):
    def frames(t, n):
        if n >= 3:
            return Frame([], root_alive=False)
        return [root(), inst('c.exe', 7001, 1070.0, ppid=ROOT_PID)]
    worker = run_group(db, started, frames, metrics=(COUNT,), cycles=10)
    rows = {(r.pid, r.create_time): r for r in db.members.list_instances(worker.task_id)}
    assert all(r.last_ms == ms(2) for r in rows.values())


def test_group_members_snapshot_reflects_latest_cycle(db, started):
    def frames(t, n):
        rows = [root(memory_private=10.0)]
        if n < 2:
            rows.append(inst('c.exe', 7001, memory_private=5.0, ppid=ROOT_PID))
        return rows
    worker = run_group(db, started, frames, cycles=4)
    by_key = {m.member_key: m for m in worker.group_members()}
    assert by_key['app.exe'].instance_count == 1
    assert by_key['c.exe'].instance_count == 0    # 已建序列、本周期没有实例
    assert by_key['c.exe'].values == {COUNT: 0.0}
    assert by_key['app.exe'].values[MEM] == 10.0


def test_member_trail_write_failure_keeps_buffer_and_task_alive(db, started, monkeypatch):
    """留痕只是附带信息：写失败不影响采集，恢复后补写"""
    from data.errors import StorageError
    real = db.members.apply
    state = {'fail': True}

    def flaky(appeared, last_seen):
        if state['fail']:
            raise StorageError('模拟写失败')
        return real(appeared, last_seen)

    monkeypatch.setattr(db.members, 'apply', flaky)

    def frames(t, n):
        if n == 3:
            state['fail'] = False
        return [root(), inst('c.exe', 7001 + n, 1070.0 + n, ppid=ROOT_PID)]
    worker = run_group(db, started, frames, metrics=(COUNT,), cycles=6)
    assert worker.stops == [StopReason.USER]
    assert len(series(db, worker, COUNT)) == 6
    # 失败期间缓冲的出现记录在恢复后一并写入
    pids = {r.pid for r in db.members.list_instances(worker.task_id)}
    assert {7001, 7002, 7003, 7004, 7005, 7006} <= pids
