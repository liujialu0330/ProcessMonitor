"""
进程组成员解析（core.process_tree）：GroupSpec 往返、resolve_tree / resolve_by_name 纯函数、
SnapshotTreeSource 的快照路径（假快照）与 psutil 回退。

不碰真实进程（真实进程的端到端见 test_group_realproc.py）。
"""
from types import SimpleNamespace

import pytest

from core.process_tree import (GroupSpec, ProcLite, SnapshotTreeSource, resolve_by_name,
                               resolve_tree, same_create_time)
from data.models import TARGET_NAME, TARGET_TREE


def procs(*rows):
    """rows: (pid, parent_pid, name, create_time) → {pid: ProcLite}"""
    return {r[0]: ProcLite(*r) for r in rows}


# ========== GroupSpec ==========

def test_group_spec_json_round_trip():
    tree = GroupSpec.tree(123, 1000.5)
    assert GroupSpec.from_json(TARGET_TREE, tree.to_json()) == tree
    by_name = GroupSpec.by_name('  Chrome.exe ')
    assert by_name.name == 'Chrome.exe'
    assert GroupSpec.from_json(TARGET_NAME, by_name.to_json()) == by_name


def test_group_spec_tree_without_create_time_round_trips_as_none():
    spec = GroupSpec.tree(9)
    assert GroupSpec.from_json(TARGET_TREE, spec.to_json()).root_create_time is None


@pytest.mark.parametrize('kind,text', [
    (TARGET_TREE, None), (TARGET_TREE, '{}'), (TARGET_TREE, '{"root_pid": "x"}'),
    (TARGET_NAME, None), (TARGET_NAME, '{"name": "  "}'), (TARGET_TREE, '[1]'),
    (TARGET_TREE, 'not json'),
])
def test_group_spec_from_bad_json_raises_value_error(kind, text):
    with pytest.raises(ValueError):
        GroupSpec.from_json(kind, text)


def test_group_spec_rejects_unknown_kind_and_bool_pid():
    with pytest.raises(ValueError):
        GroupSpec('pid', root_pid=1)
    with pytest.raises(ValueError):
        GroupSpec.tree(True)


# ========== resolve_tree ==========

def test_resolve_tree_finds_all_descendants_root_first():
    table = procs((10, 1, 'root.exe', 100.0), (11, 10, 'a.exe', 101.0), (12, 11, 'b.exe', 102.0),
                  (13, 10, 'c.exe', 103.0), (99, 1, 'other.exe', 100.5))
    assert resolve_tree(table, 10) == [10, 11, 13, 12]


def test_resolve_tree_missing_root_is_none():
    assert resolve_tree(procs((11, 10, 'a.exe', 1.0)), 10) is None


def test_resolve_tree_root_pid_reuse_is_none():
    table = procs((10, 1, 'new.exe', 500.0))
    assert resolve_tree(table, 10, root_create_time=100.0) is None
    assert resolve_tree(table, 10, root_create_time=500.0) == [10]
    assert resolve_tree(table, 10, root_create_time=None) == [10]


def test_resolve_tree_child_older_than_parent_is_not_a_descendant():
    """孤儿的父 PID 被一个全新的进程复用：它的创建时间早于"父进程"，不是后代"""
    table = procs((10, 1, 'root.exe', 100.0), (11, 10, 'real.exe', 101.0),
                  (50, 10, 'orphan.exe', 50.0))
    assert resolve_tree(table, 10) == [10, 11]


def test_resolve_tree_known_member_stays_when_intermediate_parent_exits():
    """root → mid → leaf，mid 退出后 leaf 的 ppid 指向一个不存在 / 被复用的 PID：
    已确认过的成员（pid + 创建时间对得上）继续算"""
    table = procs((10, 1, 'root.exe', 100.0), (12, 11, 'leaf.exe', 102.0))
    assert resolve_tree(table, 10) == [10]
    assert resolve_tree(table, 10, known=[(12, 102.0)]) == [10, 12]
    # 创建时间对不上 = PID 被别的进程复用，不沿用
    assert resolve_tree(table, 10, known=[(12, 777.0)]) == [10]


def test_resolve_tree_known_member_brings_its_own_children():
    table = procs((10, 1, 'root.exe', 100.0), (12, 11, 'leaf.exe', 102.0),
                  (13, 12, 'grand.exe', 103.0))
    assert resolve_tree(table, 10, known=[(12, 102.0)]) == [10, 12, 13]


def test_resolve_tree_excludes_own_pid_and_its_subtree_is_still_walked_by_others():
    table = procs((10, 1, 'root.exe', 100.0), (11, 10, 'me.exe', 101.0),
                  (12, 10, 'a.exe', 102.0))
    assert resolve_tree(table, 10, exclude=[11]) == [10, 12]


def test_resolve_tree_survives_cycles_and_self_parent():
    table = procs((10, 12, 'root.exe', 100.0), (11, 10, 'a.exe', 101.0),
                  (12, 11, 'b.exe', 102.0), (13, 13, 'self.exe', 103.0))
    assert sorted(resolve_tree(table, 10)) == [10, 11, 12]


def test_resolve_tree_never_includes_idle_and_system():
    table = procs((4, 0, 'System', 0.0), (10, 4, 'root.exe', 100.0), (0, 0, 'Idle', 0.0))
    assert resolve_tree(table, 10) == [10]
    assert resolve_tree(table, 4) is None


# ========== resolve_by_name ==========

def test_resolve_by_name_is_case_insensitive_and_sorted():
    table = procs((30, 1, 'Chrome.EXE', 1.0), (20, 1, 'chrome.exe', 2.0), (25, 1, 'x.exe', 3.0))
    assert resolve_by_name(table, ' CHROME.exe ') == [20, 30]


def test_resolve_by_name_excludes_self_and_system():
    table = procs((4, 0, 'x.exe', 0.0), (20, 1, 'x.exe', 2.0), (21, 1, 'x.exe', 3.0))
    assert resolve_by_name(table, 'x.exe', exclude=[21]) == [20]


def test_same_create_time_tolerance():
    assert same_create_time(100.0, 100.005)
    assert not same_create_time(100.0, 100.5)
    assert not same_create_time(None, 100.0)


# ========== SnapshotTreeSource：快照路径 ==========

class FakeSnapshot:
    """rows 为 (pid, 父 pid, 名称, 创建时间[, 线程数])；线程数缺省 1，0 表示已退出但仍被句柄握着。"""

    def __init__(self, rows, mono=50.0):
        self.processes = {
            r[0]: SimpleNamespace(pid=r[0], parent_pid=r[1], name=r[2], create_time=r[3],
                                  user_time=1.0, kernel_time=0.5,
                                  num_threads=r[4] if len(r) > 4 else 1)
            for r in rows}
        self.taken_at = SimpleNamespace(monotonic=mono)


class FakeHub:
    def __init__(self, snapshot, available=True):
        self.snapshot = snapshot
        self._available = available
        self.unavailable_reason = '测试：不可用'
        self.refreshed = 0

    def available(self):
        return self._available

    def get(self, max_age_s, not_before=None):
        return self.snapshot

    def refresh(self):
        self.refreshed += 1
        return self.snapshot


def test_snapshot_source_resolves_tree_members_and_cpu_seconds():
    snapshot = FakeSnapshot([(10, 1, 'root.exe', 100.0), (11, 10, 'a.exe', 101.0),
                             (99, 1, 'other.exe', 100.0)], mono=77.0)
    source = SnapshotTreeSource(FakeHub(snapshot), exclude_pids=[])
    observation = source.observe(GroupSpec.tree(10, 100.0), [], root_create_time=100.0)
    assert observation.source == 'snapshot'
    assert observation.root_alive and observation.root_create_time == 100.0
    assert [i.pid for i in observation.instances] == [10, 11]
    assert observation.taken_monotonic == 77.0
    assert observation.instances[0].cpu_seconds == 1.5


def test_snapshot_source_reports_root_exit_and_pid_reuse():
    source = SnapshotTreeSource(FakeHub(FakeSnapshot([(10, 1, 'new.exe', 900.0)])),
                                exclude_pids=[])
    observation = source.observe(GroupSpec.tree(10, 100.0), [], root_create_time=100.0)
    assert not observation.root_alive and observation.instances == ()
    gone = SnapshotTreeSource(FakeHub(FakeSnapshot([])), exclude_pids=[]).observe(
        GroupSpec.tree(10, 100.0), [], root_create_time=None)
    assert not gone.root_alive


def test_snapshot_source_zombie_root_counts_as_exited():
    """根进程已退出、只是被句柄握着（快照里线程数为 0）：按根已退出处理，成员清空"""
    source = SnapshotTreeSource(FakeHub(FakeSnapshot([(10, 1, 'root.exe', 100.0, 0),
                                                      (11, 10, 'a.exe', 101.0)])),
                                exclude_pids=[])
    observation = source.observe(GroupSpec.tree(10, 100.0), [], root_create_time=100.0)
    assert not observation.root_alive
    assert observation.instances == ()
    assert observation.root_create_time is None


def test_snapshot_source_zombie_member_is_not_counted():
    source = SnapshotTreeSource(FakeHub(FakeSnapshot([(10, 1, 'root.exe', 100.0),
                                                      (11, 10, 'dead.exe', 101.0, 0),
                                                      (12, 10, 'live.exe', 102.0)])),
                                exclude_pids=[])
    observation = source.observe(GroupSpec.tree(10, 100.0), [], root_create_time=100.0)
    assert observation.root_alive
    assert [i.pid for i in observation.instances] == [10, 12]


def test_snapshot_source_zombie_name_instance_is_not_counted():
    source = SnapshotTreeSource(FakeHub(FakeSnapshot([(20, 1, 'x.exe', 1.0, 0),
                                                      (21, 1, 'x.exe', 2.0)])),
                                exclude_pids=[])
    observation = source.observe(GroupSpec.by_name('x.exe'), [])
    assert [i.pid for i in observation.instances] == [21]


def test_snapshot_source_name_group_is_always_alive_even_when_empty():
    source = SnapshotTreeSource(FakeHub(FakeSnapshot([(1, 0, 'other.exe', 1.0)])),
                                exclude_pids=[])
    observation = source.observe(GroupSpec.by_name('x.exe'), [])
    assert observation.root_alive and observation.instances == ()


def test_snapshot_source_never_includes_own_process_by_default():
    import os
    me = os.getpid()
    snapshot = FakeSnapshot([(me, 1, 'me.exe', 1.0), (me + 100000, 1, 'me.exe', 2.0)])
    observation = SnapshotTreeSource(FakeHub(snapshot)).observe(GroupSpec.by_name('me.exe'), [])
    assert [i.pid for i in observation.instances] == [me + 100000]


def test_snapshot_source_keeps_known_member_after_parent_exits():
    spec = GroupSpec.tree(10, 100.0)
    first = FakeSnapshot([(10, 1, 'root.exe', 100.0), (11, 10, 'mid.exe', 101.0),
                          (12, 11, 'leaf.exe', 102.0)])
    hub = FakeHub(first)
    source = SnapshotTreeSource(hub, exclude_pids=[])
    assert [i.pid for i in source.observe(spec, [], root_create_time=100.0).instances] \
        == [10, 11, 12]
    hub.snapshot = FakeSnapshot([(10, 1, 'root.exe', 100.0), (12, 11, 'leaf.exe', 102.0)])
    assert [i.pid for i in source.observe(spec, [], root_create_time=100.0).instances] \
        == [10, 12]


def test_snapshot_source_same_snapshot_twice_forces_refresh_when_not_before_given():
    hub = FakeHub(FakeSnapshot([(1, 0, 'x.exe', 1.0)]))
    source = SnapshotTreeSource(hub, exclude_pids=[])
    spec = GroupSpec.by_name('x.exe')
    source.observe(spec, [], not_before=1.0)
    source.observe(spec, [], not_before=2.0)
    assert hub.refreshed == 1


def test_snapshot_source_falls_back_to_psutil_when_hub_unavailable():
    import os
    source = SnapshotTreeSource(FakeHub(None, available=False), exclude_pids=[])
    own_name = __import__('psutil').Process(os.getpid()).name()
    observation = source.observe(GroupSpec.by_name(own_name), [])
    assert observation.source == 'psutil'
    assert os.getpid() in {i.pid for i in observation.instances}
