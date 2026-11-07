"""process_catalog：只读进程查询（本机真实进程 + 替换 psutil 迭代的确定性用例）"""
import os

import psutil
import pytest

from core import process_catalog
from core.process_catalog import ProcessEntry
from core.process_collector import ProcessCollector
from tests.doubles import unavailable_snapshot_hub


class _FakeProc:
    def __init__(self, pid, name, create_time=None, error=None):
        self._info = {'pid': pid, 'name': name, 'create_time': create_time}
        self._error = error

    @property
    def info(self):
        if self._error is not None:
            raise self._error
        return self._info


@pytest.fixture
def fake_processes(monkeypatch):
    """替换 psutil 迭代；全系统快照设为不可用，确定性地走 psutil 回退路径"""
    monkeypatch.setattr(process_catalog, '_default_hub', unavailable_snapshot_hub)
    own = os.getpid()
    procs = [
        _FakeProc(30, "python.exe", 3.0),
        _FakeProc(own, "Python.exe", 1.0),
        _FakeProc(10, "Notepad.exe", 2.0),
        _FakeProc(11, "", 5.0),                      # 空名跳过
        _FakeProc(12, "   ", 5.0),                   # 空白名跳过
        _FakeProc(13, "gone.exe", error=psutil.NoSuchProcess(13)),
        _FakeProc(14, "denied.exe", error=psutil.AccessDenied(14)),
        _FakeProc(20, "python.exe", None),           # 创建时间读取失败
    ]
    monkeypatch.setattr(process_catalog.psutil, 'process_iter', lambda attrs: iter(procs))
    return own


def test_list_processes_sorted_and_filtered(fake_processes):
    entries = process_catalog.list_processes()
    assert all(isinstance(e, ProcessEntry) for e in entries)
    assert [e.name.lower() for e in entries] == sorted(e.name.lower() for e in entries)
    assert {e.pid for e in entries} == {30, fake_processes, 10, 20}
    # 仍是元组：旧调用方按 (pid, name, create_time) 解包
    pid, name, create_time = next(e for e in entries if e.pid == 10)
    assert (pid, name, create_time) == (10, "Notepad.exe", 2.0)


def test_find_pids_by_name_excludes_self_by_default(fake_processes):
    found = process_catalog.find_pids_by_name("PYTHON.EXE")
    assert sorted(found) == [(20, None), (30, 3.0)]


def test_find_pids_by_name_can_include_self(fake_processes):
    found = process_catalog.find_pids_by_name("python.exe", exclude_self=False)
    assert sorted(found) == sorted([(20, None), (30, 3.0), (fake_processes, 1.0)])


def test_find_pids_by_name_is_exact_match(fake_processes):
    assert process_catalog.find_pids_by_name("python") == []


def test_collector_static_methods_delegate(fake_processes):
    assert ProcessCollector.get_all_processes() == process_catalog.list_processes()
    assert [p for p, _ in ProcessCollector.find_process_by_name("note")] == [10]


# ========== 本机真实进程 ==========

def test_self_pid():
    assert process_catalog.self_pid() == os.getpid()


def test_real_list_contains_self_with_create_time():
    own = [e for e in process_catalog.list_processes() if e.pid == os.getpid()]
    assert len(own) == 1
    assert own[0].create_time == pytest.approx(psutil.Process().create_time())


def test_name_of_and_is_alive_for_self():
    assert process_catalog.name_of(os.getpid()) == psutil.Process().name()
    assert process_catalog.is_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [99999999, -1])
def test_missing_pid(pid):
    assert process_catalog.name_of(pid) is None
    assert process_catalog.is_alive(pid) is False


def test_access_denied_is_treated_as_unknown(monkeypatch):
    def denied(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr(process_catalog.psutil, 'Process', denied)
    assert process_catalog.name_of(4) is None
    assert process_catalog.is_alive(4) is False


def test_module_does_not_import_qt():
    """进程目录是纯查询模块，不依赖 Qt"""
    with open(process_catalog.__file__, encoding='utf-8') as f:
        source = f.read()
    assert 'PyQt5' not in source


# ========== 快照路径 ==========

def _snapshot_hub(*samples, fail=None):
    from core.snapshot_hub import SnapshotHub
    from tests.doubles import FakeSnapshotSource, fake_system_snapshot
    snapshots = [fail] if fail is not None else [fake_system_snapshot(samples)]
    return SnapshotHub(FakeSnapshotSource(snapshots=snapshots))


def test_list_from_snapshot_same_contract(monkeypatch):
    """口径与 psutil 路径相同：空名跳过、按名排序、PID 0 用 psutil 的固定名、带创建时间；
    不调用 psutil 遍历"""
    from tests.doubles import fake_process_sample
    monkeypatch.setattr(process_catalog.psutil, 'process_iter',
                        lambda *_a, **_k: pytest.fail("快照可用时不应遍历 psutil"))
    hub = _snapshot_hub(
        fake_process_sample(0, name='', create_time=0.0),
        fake_process_sample(4, name='System', create_time=0.0),
        fake_process_sample(30, name='python.exe', create_time=3.0),
        fake_process_sample(10, name='Notepad.exe', create_time=2.0),
        fake_process_sample(11, name='', create_time=5.0),
        fake_process_sample(12, name='   ', create_time=5.0),
    )
    entries = process_catalog.list_processes(hub)
    assert all(isinstance(e, ProcessEntry) for e in entries)
    assert [e.name for e in entries] == [
        'Notepad.exe', 'python.exe', 'System', 'System Idle Process']
    assert next(e for e in entries if e.pid == 0).create_time == 0.0
    assert (10, 'Notepad.exe', 2.0) in entries


def test_default_hub_is_used_by_queries(monkeypatch):
    from tests.doubles import fake_process_sample
    own = os.getpid()
    hub = _snapshot_hub(fake_process_sample(own, name='Python.exe', create_time=1.0),
                        fake_process_sample(30, name='python.exe', create_time=3.0),
                        fake_process_sample(10, name='Notepad.exe', create_time=2.0))
    monkeypatch.setattr(process_catalog, '_default_hub', lambda: hub)
    assert process_catalog.find_pids_by_name('PYTHON.EXE') == [(30, 3.0)]
    assert sorted(process_catalog.find_pids_by_name('python.exe', exclude_self=False)) == [
        (30, 3.0), (own, 1.0)]
    assert process_catalog.search_by_name('note') == [(10, 'Notepad.exe')]


def test_snapshot_failure_falls_back_to_psutil(fake_processes):
    from core.system_snapshot import SnapshotError
    hub = _snapshot_hub(fail=SnapshotError("偶发"))
    assert {e.pid for e in process_catalog.list_processes(hub)} == {30, fake_processes, 10, 20}


def test_psutil_process_query_passes_hub_through():
    from tests.doubles import fake_process_sample
    from ui.services.process_query import PsutilProcessQuery
    hub = _snapshot_hub(fake_process_sample(77, name='only.exe', create_time=7.0))
    query = PsutilProcessQuery(hub)
    assert query.list_processes() == [(77, 'only.exe', 7.0)]
    assert query.find_pids_by_name('ONLY.exe') == [(77, 7.0)]


@pytest.mark.skipif(os.name != 'nt', reason="仅 Windows")
def test_real_snapshot_list_matches_psutil_for_self():
    from core.snapshot_hub import SnapshotHub
    own = [e for e in process_catalog.list_processes(SnapshotHub()) if e.pid == os.getpid()]
    assert len(own) == 1
    assert own[0].name == psutil.Process().name()
    assert abs(own[0].create_time - psutil.Process().create_time()) <= 0.01