"""
WaitService（core/wait_service.py）：等待进程启动后自动开始。

用假时钟、假进程查询与假管理器驱动 poll()：出现即启动（取最新启动的同名进程、补 .exe）、
超时、取消、启动函数失败或抛异常、缺省启动函数（create_task + start_task）。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import pytest

from core.wait_service import (
    WAIT_FAILED_START, WAIT_FAILED_TIMEOUT, WaitMatch, WaitService, candidate_names,
)


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, 12, 0, 0)

    def now(self):
        return self.value

    def advance(self, seconds: float):
        self.value += timedelta(seconds=seconds)


class FakeQuery:
    def __init__(self):
        self.processes: Dict[str, List[Tuple[int, float]]] = {}
        self.calls: List[str] = []

    def find_pids_by_name(self, name):
        self.calls.append(name)
        return list(self.processes.get(name, []))


class FakeManager:
    def __init__(self, ok=True):
        self.ok = ok
        self.created = []
        self.started = []

    def create_task(self, pid, name, metrics, interval, note='', create_time=None):
        if not self.ok:
            return None
        self.created.append((pid, name, metrics, interval, note, create_time))
        return f"task-{pid}"

    def start_task(self, task_id):
        self.started.append(task_id)
        return True


@dataclass
class Req:
    name: str
    metrics: Tuple[str, ...] = ('memory_rss',)
    interval: float = 2.0
    note: str = "备注"


@pytest.fixture
def env(qapp):
    clock, query, manager = FakeClock(), FakeQuery(), FakeManager()
    service = WaitService(manager, query, clock)
    events = {'started': [], 'failed': []}
    service.started.connect(lambda w, t: events['started'].append((w, t)))
    service.failed.connect(lambda w, r: events['failed'].append((w, r)))
    yield service, clock, query, manager, events
    service.deleteLater()


def test_candidate_names_adds_exe():
    assert candidate_names('notepad') == ['notepad', 'notepad.exe']
    assert candidate_names('notepad.exe') == ['notepad.exe']


def test_process_appears_and_default_starter_creates_task(env):
    service, clock, query, manager, events = env
    wait_id = service.add(Req('app.exe'))
    assert wait_id == 'wait-1' and service.has_waits()
    service.poll()
    assert events == {'started': [], 'failed': []}
    query.processes['app.exe'] = [(100, 10.0)]
    service.poll()
    assert manager.created == [(100, 'app.exe', ['memory_rss'], 2.0, "备注", 10.0)]
    assert manager.started == ['task-100']
    assert events['started'] == [('wait-1', 'task-100')]
    assert not service.has_waits()
    service.poll()  # 已移除：不重复启动
    assert len(manager.created) == 1


def test_ambiguous_same_name_never_starts_arbitrary_process(env):
    service, clock, query, manager, events = env
    query.processes['app.exe'] = [(1, 5.0), (2, 50.0), (3, 20.0)]
    wait_id = service.add(Req('app.exe'))
    service.poll()
    assert manager.created == []
    assert not service.has_waits()
    assert events['failed'][0][0] == wait_id
    assert events['failed'][0][1].startswith('ambiguous:')
    assert '1, 2, 3' in events['failed'][0][1]


def test_missing_extension_tries_exe(env):
    service, clock, query, manager, events = env
    query.processes['app.exe'] = [(7, 1.0)]
    service.add(Req('app'))
    service.poll()
    assert query.calls == ['app', 'app.exe']
    assert manager.created[0][:2] == (7, 'app.exe')


def test_timeout_uses_injected_clock(env):
    service, clock, query, manager, events = env
    wait_id = service.add(Req('never.exe'), timeout_s=30)
    clock.advance(29.9)
    service.poll()
    assert events['failed'] == []
    clock.advance(0.1)
    service.poll()
    assert events['failed'] == [(wait_id, WAIT_FAILED_TIMEOUT)]
    assert not service.has_waits()


def test_no_timeout_waits_forever(env):
    service, clock, query, manager, events = env
    service.add(Req('never.exe'))
    clock.advance(10 ** 6)
    service.poll()
    assert events['failed'] == [] and service.has_waits()


def test_cancel_is_silent(env):
    service, clock, query, manager, events = env
    wait_id = service.add(Req('app.exe'), timeout_s=1)
    assert service.cancel(wait_id) is True
    assert service.cancel(wait_id) is False
    query.processes['app.exe'] = [(1, 1.0)]
    clock.advance(5)
    service.poll()
    assert events == {'started': [], 'failed': []}
    assert manager.created == []


def test_custom_starter_receives_match(env):
    service, clock, query, manager, events = env
    seen = []
    request = Req('app.exe')
    wait_id = service.add(request, starter=lambda w, m: seen.append((w, m)) or 'custom-task')
    query.processes['app.exe'] = [(9, 3.0)]
    service.poll()
    assert seen == [(wait_id, WaitMatch(pid=9, name='app.exe', create_time=3.0, request=request))]
    assert events['started'] == [(wait_id, 'custom-task')]
    assert manager.created == []


@pytest.mark.parametrize('starter', [lambda w, m: None,
                                     lambda w, m: (_ for _ in ()).throw(RuntimeError("坏"))])
def test_starter_failure_reports_start_failed(env, starter):
    service, clock, query, manager, events = env
    wait_id = service.add(Req('app.exe'), starter=starter)
    query.processes['app.exe'] = [(9, 3.0)]
    service.poll()
    assert events['failed'] == [(wait_id, WAIT_FAILED_START)]
    assert not service.has_waits()


def test_default_starter_failure(qapp):
    clock, query = FakeClock(), FakeQuery()
    service = WaitService(FakeManager(ok=False), query, clock)
    failed = []
    service.failed.connect(lambda w, r: failed.append(r))
    service.add(Req('app.exe'))
    query.processes['app.exe'] = [(1, 1.0)]
    service.poll()
    assert failed == [WAIT_FAILED_START]


def test_query_exception_keeps_waiting(env):
    service, clock, query, manager, events = env

    def broken(_name):
        raise OSError("枚举失败")
    query.find_pids_by_name = broken
    service.add(Req('app.exe'))
    service.poll()
    assert service.has_waits() and events == {'started': [], 'failed': []}


def test_starter_may_cancel_other_waits(env):
    """启动函数里取消别的等待：同一轮 poll 不再处理被取消的那条。"""
    service, clock, query, manager, events = env
    second = []

    def starter(wait_id, match):
        service.cancel(second[0])
        return 'task-a'
    service.add(Req('a.exe'), starter=starter)
    second.append(service.add(Req('b.exe')))
    query.processes['a.exe'] = [(1, 1.0)]
    query.processes['b.exe'] = [(2, 1.0)]
    service.poll()
    assert manager.created == []
    assert [t for _, t in events['started']] == ['task-a']


def test_id_prefix_and_request_lookup(qapp):
    service = WaitService(FakeManager(), FakeQuery(), FakeClock(), id_prefix='ipc-wait-')
    request = Req('x.exe')
    wait_id = service.add(request)
    assert wait_id == 'ipc-wait-1'
    assert service.wait_ids() == [wait_id]
    assert service.request_of(wait_id) is request
    assert service.request_of('nope') is None
