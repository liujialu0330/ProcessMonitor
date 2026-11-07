"""
命令行 / IPC 的 start target_kind（pid / tree / name）：参数组合校验、拒绝监控自身、同名多个不算
歧义、没有同名进程时的 wait 语义、进程组指标校验、status 与 verdict 回复里的进程组信息。
"""
import os
import subprocess
import sys

import pytest

from core.ipc import protocol as p
from core.ipc.commands import CommandContext, CommandDispatcher, CommandSettings
from core.monitor_manager import MonitorManager
from tests.group_doubles import inst
from tests.helpers import stop_and_join
from tests.test_group_manager import StaticSource
from tests.test_ipc_server import FakeConn, _call, _request


class Lookup:
    """进程查询替身：按名返回 [(pid, 创建时间)]；identity_of 返回自己登记的身份"""

    def __init__(self, matches=None, identities=None):
        self.matches = dict(matches or {})
        self.identities = dict(identities or {})

    def find_pids_by_name(self, name):
        return list(self.matches.get(name, []))

    def identity_of(self, pid):
        return self.identities.get(pid)


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    yield proc
    proc.kill()
    proc.wait(10)


@pytest.fixture
def manager(db, qapp):
    source = StaticSource([inst('x.exe', 5001, memory_rss=1.0)])
    mgr = MonitorManager(db=db, tree_source_factory=lambda spec: source)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def dispatcher(manager, db):
    settings = CommandSettings(default_metrics=lambda: ('memory_rss', 'memory_peak_wset'))
    d = CommandDispatcher(app_version='9.9.9', settings=settings)
    d.set_context(CommandContext(manager=manager, db=db, auto_marker=None))
    yield d
    d.deleteLater()


def start(dispatcher, args, lookup=None):
    if lookup is not None:
        dispatcher.context.process_lookup = lookup
        dispatcher.wait_service._query = lookup
    return _call(dispatcher, 'start', args)


# ========== tree ==========

def test_start_tree_creates_a_group_task(dispatcher, manager, sleeper):
    reply = start(dispatcher, {'pid': sleeper.pid, 'target_kind': 'tree'})
    assert reply['ok'] is True, reply
    result = reply['result']
    assert result['target_kind'] == 'tree' and result['pid'] == sleeper.pid
    task = manager.get_task(result['task_id'])
    assert task.target_kind == 'tree' and task.group_spec.root_pid == sleeper.pid
    assert task.group_spec.root_create_time is not None
    # 缺省指标里对组不适用的峰值类被剔除，并自动补进程总数
    assert task.metric_types == ['memory_rss', 'process_count']


def test_start_tree_is_visible_in_status(dispatcher, sleeper):
    reply = start(dispatcher, {'pid': sleeper.pid, 'target_kind': 'tree'})
    status = _call(dispatcher, 'status')['result']['tasks']
    (entry,) = [t for t in status if t['task_id'] == reply['result']['task_id']]
    assert entry['target_kind'] == 'tree' and entry['members_truncated'] is False


def test_start_pid_default_kind_is_unchanged(dispatcher, manager, sleeper):
    reply = start(dispatcher, {'pid': sleeper.pid, 'metrics': ['memory_rss']})
    assert reply['ok'] is True
    assert reply['result']['target_kind'] == 'pid'
    assert manager.get_task(reply['result']['task_id']).target_kind == 'pid'


def test_tree_needs_pid_and_rejects_name(dispatcher):
    assert start(dispatcher, {'name': 'a.exe', 'target_kind': 'tree'})['code'] == p.ERR_INVALID_ARGS
    assert start(dispatcher, {'target_kind': 'tree'})['code'] == p.ERR_INVALID_ARGS
    reply = start(dispatcher, {'pid': 1, 'name': 'a.exe', 'target_kind': 'tree'})
    assert reply['code'] == p.ERR_INVALID_ARGS


def test_tree_root_cannot_be_this_program(dispatcher):
    reply = start(dispatcher, {'pid': os.getpid(), 'target_kind': 'tree'})
    assert reply['code'] == p.ERR_INVALID_ARGS and "本程序自身" in reply['message']


def test_tree_unknown_pid_is_not_found(dispatcher):
    reply = start(dispatcher, {'pid': 0x7FFFFFF0, 'target_kind': 'tree'})
    assert reply['code'] == p.ERR_NOT_FOUND


def test_unknown_target_kind_is_invalid(dispatcher):
    reply = start(dispatcher, {'pid': 1, 'target_kind': 'forest'})
    assert reply['code'] == p.ERR_INVALID_ARGS and 'tree' in reply['message']


def test_explicit_inapplicable_metric_is_rejected_for_groups(dispatcher, sleeper):
    reply = start(dispatcher, {'pid': sleeper.pid, 'target_kind': 'tree',
                               'metrics': ['memory_rss', 'memory_peak_wset']})
    assert reply['code'] == p.ERR_INVALID_ARGS and '不适用于进程组' in reply['message']
    assert '工作集峰值' in reply['message']
    reply = start(dispatcher, {'pid': sleeper.pid, 'target_kind': 'tree',
                               'metrics': ['cpu_priority']})
    assert reply['code'] == p.ERR_INVALID_ARGS


def test_process_count_is_selectable_for_groups_only(dispatcher, sleeper):
    ok = start(dispatcher, {'pid': sleeper.pid, 'target_kind': 'tree',
                            'metrics': ['process_count', 'memory_rss']})
    assert ok['ok'] is True
    bad = start(dispatcher, {'pid': sleeper.pid, 'metrics': ['process_count']})
    assert bad['code'] == p.ERR_INVALID_ARGS and '未知' in bad['message']


# ========== name ==========

def test_name_group_accepts_many_same_name_processes(dispatcher, manager):
    lookup = Lookup({'chrome.exe': [(11, 1.0), (12, 2.0), (13, 3.0)]})
    reply = start(dispatcher, {'name': 'chrome.exe', 'target_kind': 'name'}, lookup)
    assert reply['ok'] is True, reply
    result = reply['result']
    assert result['target_kind'] == 'name' and result['pid'] == 0
    assert result['name'] == 'chrome.exe'
    task = manager.get_task(result['task_id'])
    assert task.target_kind == 'name' and task.group_spec.name == 'chrome.exe'
    assert task.metric_types == ['memory_rss', 'process_count']


def test_name_group_resolves_missing_extension(dispatcher):
    lookup = Lookup({'chrome.exe': [(11, 1.0)]})
    reply = start(dispatcher, {'name': 'chrome', 'target_kind': 'name'}, lookup)
    assert reply['ok'] is True and reply['result']['name'] == 'chrome.exe'


def test_name_group_without_process_is_not_found(dispatcher):
    reply = start(dispatcher, {'name': 'nobody.exe', 'target_kind': 'name'}, Lookup())
    assert reply['code'] == p.ERR_NOT_FOUND


def test_name_group_with_wait_starts_immediately_with_zero_members(dispatcher, manager):
    conn = FakeConn()
    dispatcher.context.process_lookup = Lookup()
    dispatcher.handle(conn, _request('start', {'name': 'later', 'target_kind': 'name',
                                               'wait': True}))
    reply = conn.sent[-1]
    assert reply['ok'] is True, reply
    assert reply['result']['name'] == 'later.exe'
    assert dispatcher.has_pending() is False


def test_name_group_cannot_be_this_programs_own_process_name(dispatcher):
    own = os.path.basename(sys.executable)
    reply = start(dispatcher, {'name': own, 'target_kind': 'name'},
                  Lookup({own: [(os.getpid(), 1.0)]}))
    assert reply['code'] == p.ERR_INVALID_ARGS and '本程序自身' in reply['message']
    stem = start(dispatcher, {'name': own.rsplit('.', 1)[0], 'target_kind': 'name'},
                 Lookup({own: [(os.getpid(), 1.0)]}))
    assert stem['code'] == p.ERR_INVALID_ARGS


def test_name_kind_rejects_pid_and_launch(dispatcher):
    assert start(dispatcher, {'pid': 1, 'target_kind': 'name'})['code'] == p.ERR_INVALID_ARGS
    reply = start(dispatcher, {'launch': {'exe': 'C:\\x\\a.exe'}, 'target_kind': 'name'})
    assert reply['code'] == p.ERR_INVALID_ARGS


def test_duplicate_name_is_still_ambiguous_for_the_default_kind(dispatcher):
    lookup = Lookup({'dup.exe': [(11, 1.0), (12, 2.0)]})
    reply = start(dispatcher, {'name': 'dup.exe'}, lookup)
    assert reply['code'] == p.ERR_INVALID_ARGS and reply['pids'] == [11, 12]


# ========== launch + tree ==========

class LaunchResult:
    def __init__(self, pid, create_time):
        self.ok = True
        self.pid = pid
        self.name = 'app.exe'
        self.create_time = create_time
        self.error = None


class FakeLauncher:
    def __init__(self, pid, create_time):
        self.result = LaunchResult(pid, create_time)

    def launch(self, spec):
        return self.result


def test_launch_with_tree_monitors_the_launched_process_tree(dispatcher, manager, sleeper):
    import psutil
    create_time = psutil.Process(sleeper.pid).create_time()
    dispatcher.context.launcher = FakeLauncher(sleeper.pid, create_time)
    reply = start(dispatcher, {'launch': {'exe': 'C:\\x\\app.exe'}, 'target_kind': 'tree'})
    assert reply['ok'] is True, reply
    task = manager.get_task(reply['result']['task_id'])
    assert task.target_kind == 'tree'
    assert task.group_spec.root_pid == sleeper.pid
    assert reply['result']['launched'] is True


# ========== verdict 回复 ==========

def test_verdict_reply_carries_members_for_group_tasks(dispatcher, db):
    """走与 verdict 命令相同的 TaskAnalysisWorker：进程组任务的回复里有 members"""
    from core.ipc.commands import TaskAnalysisWorker, compute_verdict
    from core.workers.base import CancelToken
    from data.models import DataPoint, MonitorTask
    from datetime import datetime, timedelta
    base = datetime(2026, 3, 1, 9, 0, 0)
    task = MonitorTask(task_id='g-1', pid=100, process_name='app.exe',
                       metric_types=['memory_private', 'process_count'], interval=5.0,
                       start_time=base, end_time=base + timedelta(seconds=1000),
                       status='stopped', target_kind='tree', target_spec='{}')
    db.save_task(task)
    points = []
    for i in range(160):
        stamp = base + timedelta(seconds=5 * i)
        points += [DataPoint('g-1', stamp, 100.0, 'memory_private'),
                   DataPoint('g-1', stamp, 2.0, 'process_count'),
                   DataPoint('g-1', stamp, 10.0 * i + 1, 'memory_private@leak.exe'),
                   DataPoint('g-1', stamp, 1.0, 'process_count@leak.exe')]
    assert db.save_data_points(points)
    worker = TaskAnalysisWorker(db, 'g-1', task.metric_types, analysis=False)
    analysis = worker.execute(CancelToken())
    result = compute_verdict(analysis)
    assert result['verdict'] == 'rising'
    assert [g['key'] for g in result['members']] == ['leak.exe']
    leak = result['members'][0]
    assert leak['verdict'] == 'rising'
    assert {m['metric'] for m in leak['metrics']} == {'memory_private', 'process_count'}
