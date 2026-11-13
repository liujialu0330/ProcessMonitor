"""批量开始先登记所有目标，一次延迟回复，等待/失败/取消彼此隔离。"""
import pytest

from core.ipc.commands import CommandContext, CommandDispatcher, CommandSettings
from core.ipc.protocol import Request
from tests.doubles import FakeClock
from tests.test_ipc_server import FakeConn, FakeIdentity
from tests.test_monitor_presenter import FakeManager as BaseManager


class Manager(BaseManager):
    def __init__(self):
        super().__init__()
        self.identities = {}
        self.groups = {}

    def can_add_task(self):
        return len(self.infos) < 5

    def update_task_identity(self, task_id, scenario, build):
        self.identities[task_id] = (scenario, build)
        return True

    def set_task_group(self, task_ids, group_id):
        self.groups.update(dict.fromkeys(task_ids, group_id))

    def delete_task_data(self, task_id):
        self.identities.pop(task_id, None)
        self.groups.pop(task_id, None)
        return True


class Lookup:
    def __init__(self):
        self.names = {'now.exe': [(200, 20.0)]}

    def find_pids_by_name(self, name):
        return self.names.get(name, [])

    def identity_of(self, pid):
        for name, matches in self.names.items():
            for candidate, created in matches:
                if candidate == pid:
                    return name, created
        return None


@pytest.fixture
def env(qapp, db):
    manager, lookup, clock = Manager(), Lookup(), FakeClock()
    dispatcher = CommandDispatcher(app_version='test',
        settings=CommandSettings(default_metrics=lambda: ('memory_private',)))
    dispatcher.set_context(CommandContext(manager, db, None, clock=clock, process_lookup=lookup))
    conn = FakeConn()
    yield dispatcher, manager, lookup, clock, conn
    for pending in list(dispatcher._pending):
        pending.cancel()
    dispatcher.deleteLater()


def send(env, targets, **plan_fields):
    dispatcher, _manager, _lookup, _clock, conn = env
    dispatcher.handle(conn, Request(id=7, cmd='start_plan', args={'plan': {
        'name': '批量方案', 'schema_version': 1, 'targets': targets, **plan_fields}}))
    return [entry for entry in conn.sent if 'ok' in entry]


def test_first_wait_does_not_block_second_target_start(env):
    dispatcher, manager, lookup, clock, conn = env
    assert send(env, [{'name': 'never.exe', 'wait': True, 'wait_timeout_s': 5},
                      {'pid': 200, 'scenario': '窗口', 'build': '2.0'}]) == []
    assert [row[0] for row in manager.created] == [200]
    assert manager.commands == [('start', 't1')]
    assert len(dispatcher._pending) == 1 and len(dispatcher._start_waits) == 1
    clock.advance(5)
    dispatcher.poll_start_waits()
    reply = [entry for entry in conn.sent if 'ok' in entry][0]
    assert reply['ok'] and reply['result']['started'] == reply['result']['failed'] == 1
    assert reply['result']['results'][0]['error']['code'] == 'timeout'
    assert reply['result']['results'][1]['result']['scenario'] == '窗口'
    assert not dispatcher.has_pending()


def test_all_waits_are_registered_then_reply_once_in_original_order(env):
    dispatcher, manager, lookup, _clock, conn = env
    send(env, [{'name': 'a.exe', 'wait': True}, {'name': 'b.exe', 'wait': True}])
    assert len(dispatcher._start_waits) == 2 and len(dispatcher._pending) == 1
    dispatcher._pending[0]._beat()
    assert len([entry for entry in conn.sent if entry.get('event') == 'heartbeat']) == 1
    lookup.names['b.exe'] = [(300, 30.0)]
    dispatcher.poll_start_waits()
    assert len(manager.created) == 1 and not any('ok' in entry for entry in conn.sent)
    lookup.names['a.exe'] = [(400, 40.0)]
    dispatcher.poll_start_waits()
    replies = [entry for entry in conn.sent if 'ok' in entry]
    assert len(replies) == 1
    results = replies[0]['result']['results']
    assert [item['result']['pid'] for item in results] == [400, 300]
    assert len(set(manager.groups.values())) == 1
    assert all(item['result']['group_id'] == replies[0]['result']['group_id'] for item in results)


def test_failed_target_does_not_prevent_later_target(env):
    replies = send(env, [{'pid': 123}, {'pid': 200}])
    result = replies[0]['result']
    assert result['started'] == 1 and result['failed'] == 1
    assert result['results'][0]['error']['code'] == 'not_found'
    assert result['results'][1]['ok']


def test_failure_after_wait_registration_cleans_wait_and_keeps_later_target(env, monkeypatch):
    dispatcher, manager, _lookup, _clock, _conn = env
    sync = dispatcher._sync_timer
    calls = []
    def fail_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('测试：登记后失败')
        sync()
    monkeypatch.setattr(dispatcher, '_sync_timer', fail_once)
    replies = send(env, [{'name': 'later.exe', 'wait': True}, {'pid': 200}])
    assert replies[0]['result']['failed'] == replies[0]['result']['started'] == 1
    assert [row[0] for row in manager.created] == [200]
    assert dispatcher.wait_service.wait_ids() == [] and dispatcher._start_waits == {}
    assert not dispatcher.has_pending()


def test_batch_target_and_connection_pending_limits_are_bounded(env):
    dispatcher, manager, _lookup, _clock, conn = env
    too_many = send(env, [{'name': f'{i}.exe', 'wait': True} for i in range(6)])
    assert too_many[-1]['code'] == 'invalid_args' and not dispatcher.has_pending()
    conn.sent.clear()
    for i in range(4):
        send(env, [{'name': f'{i}-{n}.exe', 'wait': True} for n in range(5)])
    assert len(dispatcher._pending) == 4 and len(dispatcher._start_waits) == 20
    replies = send(env, [{'pid': 200}])
    assert replies[-1]['code'] == 'busy' and manager.created == []


def test_disconnect_cancels_waits_but_preserves_started_tasks(env):
    dispatcher, manager, lookup, _clock, conn = env
    send(env, [{'name': 'a.exe', 'wait': True}, {'pid': 200}, {'name': 'b.exe', 'wait': True}])
    conn.writable = False
    for callback in conn._callbacks:
        callback()
    assert not dispatcher.has_pending() and dispatcher._start_waits == {}
    assert dispatcher.wait_service.wait_ids() == []
    assert manager.commands == [('start', 't1')]
    lookup.names['a.exe'] = [(300, 30.0)]
    dispatcher.poll_start_waits()
    assert len(manager.created) == 1 and not conn.sent


@pytest.mark.parametrize('version', [0, 2, True, '1'])
def test_schema_rejected_before_any_target_starts(env, version):
    dispatcher, manager, _lookup, _clock, _conn = env
    replies = send(env, [{'pid': 200}], schema_version=version)
    assert replies[0]['code'] == 'invalid_args'
    assert manager.created == [] and not dispatcher.has_pending()


def test_start_plan_has_same_integrity_gate_as_start(env):
    dispatcher, manager, _lookup, _clock, conn = env
    dispatcher.identity = FakeIdentity(0x3000)
    conn.integrity = 0x2000
    replies = send(env, [{'pid': 200}])
    assert replies[0]['code'] == 'forbidden' and manager.created == []


@pytest.mark.parametrize('waited', [False, True])
@pytest.mark.parametrize('failure', ['raise', 'false'])
def test_metadata_failure_rolls_back_before_start(env, monkeypatch, waited, failure):
    dispatcher, manager, lookup, _clock, conn = env
    def fail(*args):
        if failure == 'raise':
            raise ValueError('测试身份写入失败')
        return False
    monkeypatch.setattr(manager, 'update_task_identity', fail)
    args = {'scenario': '窗口'}
    args.update({'name': 'later.exe', 'wait': True} if waited else {'pid': 200})
    dispatcher.handle(conn, Request(id=9, cmd='start', args=args))
    if waited:
        lookup.names['later.exe'] = [(300, 30.0)]
        dispatcher.poll_start_waits()
    assert conn.sent[-1]['code'] == 'internal'
    assert manager.infos == {} and not any(command[0] == 'start' for command in manager.commands)
    assert not dispatcher.has_pending()


def test_old_single_start_and_automation_json_are_unchanged(env):
    dispatcher, manager, _lookup, _clock, conn = env
    dispatcher.handle(conn, Request(id=9, cmd='start', args={
        'pid': 200, 'plan': {'schema_version': 1, 'timed_marker_seconds': 10}}))
    assert conn.sent[-1]['ok'] and conn.sent[-1]['result']['task_id'] == 't1'
    assert manager.commands == [('start', 't1')]
