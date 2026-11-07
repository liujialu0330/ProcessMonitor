"""
MonitorManager 的进程组接口：create_task(target_kind=)、自动补进程总数、配额、TaskInfo 字段、
group_members、members_changed 转发、has_running_task_for_pid 只看单进程任务。
成员来源用注入的假实现（真实时钟、小周期），不碰真实进程。
"""
import os

import pytest

import config
from core.contracts import StopReason
from core.monitor_manager import MonitorManager
from core.process_tree import GroupSpec, Observation
from data.models import TARGET_NAME, TARGET_PID, TARGET_TREE
from tests.group_doubles import ROOT_CREATE_TIME, ROOT_PID, inst
from tests.helpers import poll_until, process_events_until, stop_and_join
from utils.metrics import MetricType

MEM = MetricType.MEMORY_PRIVATE


class StaticSource:
    """每次返回同一组实例；alive=False 时 tree 的根已退出"""

    def __init__(self, instances, alive=True):
        self.instances = tuple(instances)
        self.alive = alive
        self.specs = []

    def observe(self, spec, metrics, *, root_create_time=None, not_before=None):
        import time
        self.specs.append(spec)
        return Observation(taken_monotonic=time.monotonic(),
                           instances=self.instances if self.alive else (),
                           root_alive=self.alive,
                           root_create_time=ROOT_CREATE_TIME if self.alive else None,
                           source='fake')


@pytest.fixture
def make_manager(db, qapp):
    managers = []

    def factory(source=None):
        mgr = MonitorManager(
            db=db, tree_source_factory=(lambda spec: source) if source is not None else None)
        managers.append(mgr)
        return mgr
    yield factory
    for mgr in managers:
        stop_and_join(mgr)


def sample_source():
    return StaticSource([inst('app.exe', ROOT_PID, ROOT_CREATE_TIME, memory_private=10.0),
                         inst('w.exe', 6001, memory_private=5.0),
                         inst('w.exe', 6002, memory_private=5.0)])


# ========== create_task ==========

def test_create_tree_task_records_kind_spec_and_appends_process_count(make_manager, db):
    mgr = make_manager(sample_source())
    task_id = mgr.create_task(ROOT_PID, 'app.exe', [MEM], 0.05, create_time=ROOT_CREATE_TIME,
                              target_kind=TARGET_TREE)
    task = mgr.get_task(task_id)
    assert task.target_kind == TARGET_TREE
    assert task.metric_types == [MEM, 'process_count']
    assert task.group_spec == GroupSpec.tree(ROOT_PID, ROOT_CREATE_TIME)
    info = mgr.get_task_info(task_id)
    assert info.target_kind == TARGET_TREE and info.members_truncated is False


def test_process_count_is_not_duplicated(make_manager):
    mgr = make_manager(sample_source())
    task_id = mgr.create_task(ROOT_PID, 'app.exe', ['process_count', MEM], 0.05,
                              target_kind=TARGET_TREE)
    assert mgr.get_task(task_id).metric_types == ['process_count', MEM]


def test_create_name_task_forces_pid_zero_and_ignores_create_time(make_manager):
    mgr = make_manager(sample_source())
    task_id = mgr.create_task(1234, 'Worker.exe', [MEM], 0.05, create_time=99.0,
                              target_kind=TARGET_NAME)
    task = mgr.get_task(task_id)
    assert task.pid == 0 and task.expected_create_time is None
    assert task.group_spec == GroupSpec.by_name('Worker.exe')


def test_pid_task_is_unchanged_by_default(make_manager):
    mgr = make_manager()
    task_id = mgr.create_task(os.getpid(), 'x.exe', [MEM], 0.05)
    task = mgr.get_task(task_id)
    assert task.target_kind == TARGET_PID and task.group_spec is None
    assert task.metric_types == [MEM]
    assert mgr.group_members(task_id) == ()


def test_unknown_target_kind_is_rejected(make_manager):
    mgr = make_manager()
    with pytest.raises(ValueError):
        mgr.create_task(1, 'x.exe', [MEM], 0.05, target_kind='forest')


def test_group_task_takes_one_quota_slot(make_manager, monkeypatch):
    monkeypatch.setattr(config, 'MAX_MONITOR_TASKS', 2)
    mgr = make_manager(sample_source())
    assert mgr.create_task(ROOT_PID, 'app.exe', [MEM], 0.05, target_kind=TARGET_TREE)
    assert mgr.create_task(0, 'w.exe', [MEM], 0.05, target_kind=TARGET_NAME)
    assert mgr.create_task(os.getpid(), 'x.exe', [MEM], 0.05) is None


# ========== 运行期 ==========

def test_group_members_and_members_changed_after_start(make_manager):
    mgr = make_manager(sample_source())
    changed = []
    mgr.members_changed.connect(changed.append)
    task_id = mgr.create_task(ROOT_PID, 'app.exe', [MEM], 0.05, create_time=ROOT_CREATE_TIME,
                              target_kind=TARGET_TREE)
    assert mgr.start_task(task_id)
    assert poll_until(lambda: len(mgr.group_members(task_id)) == 2)
    members = {m.member_key: m for m in mgr.group_members(task_id)}
    assert members['w.exe'].instance_count == 2
    assert members['w.exe'].values[MEM] == 10.0
    assert process_events_until(lambda: changed == [task_id])
    # 成员集合没再变：再跑几个周期，转发信号不重复
    process_events_until(lambda: False, timeout=0.4)
    assert changed == [task_id]


def test_group_members_unknown_task_is_empty(make_manager):
    assert make_manager().group_members('nope') == ()


def test_root_exit_stops_the_task(make_manager):
    source = StaticSource([], alive=False)
    mgr = make_manager(source)
    reasons = []
    mgr.task_stopped.connect(lambda _tid, reason: reasons.append(reason))
    task_id = mgr.create_task(ROOT_PID, 'app.exe', [MEM], 0.05, target_kind=TARGET_TREE)
    assert mgr.start_task(task_id)
    assert process_events_until(lambda: reasons)
    assert reasons == [StopReason.PROCESS_NOT_FOUND]


def test_has_running_task_for_pid_counts_only_pid_tasks(make_manager):
    mgr = make_manager(sample_source())
    task_id = mgr.create_task(ROOT_PID, 'app.exe', [MEM], 0.05, create_time=ROOT_CREATE_TIME,
                              target_kind=TARGET_TREE)
    assert mgr.start_task(task_id)
    assert poll_until(lambda: len(mgr.group_members(task_id)) == 2)
    assert mgr.has_running_task_for_pid(ROOT_PID) is False
