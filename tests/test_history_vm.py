"""
历史数据视图模型（HistoryViewModel，分析页视图模型的基类 / HistoryAutoRefresh）：无控件单元测试。

用临时库 + 查询执行器替身（同步执行 / 手动放行）驱动，按信号断言行为：
选择语义（与 test_safety_net_history 的选择用例同口径）、
导航跳转只查一次、删除、范围与指标切换、锚点缓存、空状态与读取失败、加载态顺序、
过期结果丢弃、自动刷新的跳过/单飞/任务结束重载/失败提示去重、间隔分档；
基线存在性与"自基线"范围回落、打点变化（manager.markers_changed）后的静默重查；
最后用真实后台线程验证查询不在主线程、线程登记与释放。
"""
import dataclasses
import os
import threading
import uuid
from datetime import datetime, timedelta

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock, FakeTaskStatus
from tests.factories import make_services
from tests.helpers import (
    ManualDefer, ManualHistoryRunner, SyncHistoryRunner, poll_until,
)
from tests.history_dataset import _seed_query_dataset
from ui.viewmodels import (
    AUTO_REFRESH_INTERVAL_MS, HistoryAutoRefresh, HistoryViewModel,
    auto_refresh_interval_for,
)

MEM = "memory_private"
THREADS = "num_threads"
BASE = datetime(2026, 7, 16, 8, 0, 0)
NOW = BASE + timedelta(hours=3)


def _immediate(fn):
    fn()


class Recorder:
    """按发射顺序记录视图模型的全部信号：(信号名, 参数...)"""

    NAMES = ('tasks_changed', 'tasks_refreshed', 'metrics_changed', 'selection_changed',
             'snapshot_ready', 'empty_state', 'busy', 'error')

    def __init__(self, vm):
        self.events = []
        for name in self.NAMES:
            getattr(vm, name).connect(
                lambda *args, _n=name: self.events.append((_n, *args)))

    def of(self, name):
        return [e[1:] for e in self.events if e[0] == name]

    def names(self):
        return [e[0] for e in self.events]

    def clear(self):
        self.events.clear()


def _make_task(db, name, start, metrics=(MEM,), status="stopped", samples=3):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name,
        metric_types=list(metrics), interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=samples),
        status=status)
    db.save_task(task)
    if samples:
        db.save_data_points([
            DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), metric)
            for i in range(samples) for metric in metrics])
    return task


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def status():
    return FakeTaskStatus()


@pytest.fixture
def services(status):
    return make_services(clock=FakeClock(NOW), task_status=status)


@pytest.fixture
def runner(db):
    return SyncHistoryRunner(db)


def _vm(qapp, db, manager, services, runner, defer=_immediate, **kwargs):
    vm = HistoryViewModel(db, manager, services, worker_runner=runner, defer=defer, **kwargs)
    return vm, Recorder(vm)


@pytest.fixture
def vm_rec(qapp, db, manager, services, runner):
    return _vm(qapp, db, manager, services, runner)


# ====================================================================== 任务列表


def test_no_tasks_shows_empty_without_query(vm_rec, runner):
    vm, rec = vm_rec
    vm.reload_tasks()
    assert rec.of('tasks_changed') == [([], set(), NOW)]
    assert rec.of('empty_state') == [("暂无历史数据", "完成一次监控后，数据会显示在这里")]
    assert rec.of('selection_changed') == [(None, None, False)]
    assert rec.of('metrics_changed') == [([],)]
    assert runner.requests == [] and rec.of('busy') == []
    assert vm.current_task_id is None and not vm.can_delete


def test_task_list_failure_shows_read_error(vm_rec, db, monkeypatch, runner):
    vm, rec = vm_rec
    _make_task(db, "a.exe", BASE)

    def boom():
        raise StorageError("读取有数据的任务失败: disk I/O error")
    monkeypatch.setattr(db.tasks, 'list_with_data', boom)

    vm.reload_tasks()

    assert rec.of('empty_state') == [("读取数据失败", "数据库读取出错，可稍后点击刷新重试")]
    assert rec.of('error') == [("读取数据失败", "读取有数据的任务失败: disk I/O error")]
    assert vm.tasks == [] and vm.current_task_id is None and runner.requests == []


def test_tasks_changed_carries_newest_first_and_running_ids(vm_rec, db, status):
    vm, rec = vm_rec
    old = _make_task(db, "old.exe", BASE)
    new = _make_task(db, "new.exe", BASE + timedelta(hours=1), status="running")
    _make_task(db, "nodata.exe", BASE + timedelta(hours=2), samples=0)
    status.set_running(new.task_id)

    vm.reload_tasks()

    tasks, running, now = rec.of('tasks_changed')[0]
    assert [t.task_id for t in tasks] == [new.task_id, old.task_id]
    assert running == {new.task_id} and now == NOW


# ====================================================================== 选择语义


def test_reload_auto_selects_newest_and_loads_once(vm_rec, db, runner):
    vm, rec = vm_rec
    _make_task(db, "old.exe", BASE)
    new = _make_task(db, "new.exe", BASE + timedelta(hours=1), metrics=(THREADS, MEM))

    vm.reload_tasks()

    assert vm.current_task_id == new.task_id and vm.selection_is_auto is True
    assert vm.current_metric == THREADS
    assert rec.of('metrics_changed') == [([THREADS, MEM],)]
    assert rec.of('selection_changed') == [(new.task_id, THREADS, False)]
    assert [(r.task_id, r.metric, r.range_key) for r in runner.requests] == [
        (new.task_id, THREADS, 'all')]
    snapshot, = rec.of('snapshot_ready')[0]
    assert len(snapshot.points) == 3 and vm.last_snapshot is snapshot


def test_select_task_is_explicit_survives_new_task_and_keeps_metric(vm_rec, db):
    """select_task 是显式选择：之后出现更新的任务、再次重载仍停在该任务，用户切过的指标也恢复"""
    vm, rec = vm_rec
    target = _make_task(db, "target.exe", BASE, metrics=(MEM, THREADS))
    vm.reload_tasks()
    assert vm.select_task(target.task_id) is True
    assert vm.selection_is_auto is False
    vm.select_metric(THREADS)
    assert vm.current_metric == THREADS

    _make_task(db, "newer.exe", BASE + timedelta(hours=1))
    rec.clear()
    vm.reload_tasks()

    assert vm.current_task_id == target.task_id and vm.selection_is_auto is False
    assert vm.current_metric == THREADS
    assert rec.of('selection_changed') == [(target.task_id, THREADS, True)]


def test_auto_selection_follows_newest_and_resets_metric(vm_rec, db):
    vm, _rec = vm_rec
    first = _make_task(db, "first.exe", BASE, metrics=(MEM, THREADS))
    vm.reload_tasks()
    assert vm.current_task_id == first.task_id and vm.selection_is_auto

    # 最新任务没变：重载保持原选择、自动状态与用户切过的指标
    vm.select_metric(THREADS)
    vm.reload_tasks()
    assert vm.current_task_id == first.task_id and vm.selection_is_auto
    assert vm.current_metric == THREADS

    second = _make_task(db, "second.exe", BASE + timedelta(hours=1), metrics=(THREADS, MEM))
    vm.reload_tasks()
    assert vm.current_task_id == second.task_id and vm.selection_is_auto
    assert vm.current_metric == THREADS

    third = _make_task(db, "third.exe", BASE + timedelta(hours=2))
    vm.reload_tasks()
    assert vm.current_task_id == third.task_id and vm.selection_is_auto
    assert vm.current_metric == MEM


def test_user_choice_is_kept_when_newer_task_appears(vm_rec, db):
    vm, _rec = vm_rec
    old = _make_task(db, "old.exe", BASE)
    newer = _make_task(db, "newer.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    assert vm.current_task_id == newer.task_id

    vm.select_task(old.task_id, explicit=True)
    _make_task(db, "newest.exe", BASE + timedelta(hours=2))
    vm.reload_tasks()
    assert vm.current_task_id == old.task_id


def test_auto_selected_task_removed_falls_back_to_newest(vm_rec, db):
    vm, rec = vm_rec
    older = _make_task(db, "older.exe", BASE)
    newest = _make_task(db, "newest.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    assert vm.current_task_id == newest.task_id

    db.delete_task(newest.task_id)
    rec.clear()
    vm.reload_tasks()
    assert vm.current_task_id == older.task_id and vm.selection_is_auto
    assert rec.of('snapshot_ready') and not rec.of('empty_state')


def test_user_selected_task_removed_clears_selection(vm_rec, db, runner):
    vm, rec = vm_rec
    older = _make_task(db, "older.exe", BASE)
    _make_task(db, "newest.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    vm.select_task(older.task_id)

    db.delete_task(older.task_id)
    rec.clear()
    requests_before = len(runner.requests)
    vm.reload_tasks()

    assert vm.current_task_id is None and vm.current_metric is None
    assert len(vm.tasks) == 1
    assert rec.of('selection_changed') == [(None, None, False)]
    assert rec.of('empty_state') == [("请选择监控任务", "选定任务和指标后即可查看历史数据")]
    assert len(runner.requests) == requests_before


def test_select_task_outside_list_reloads_and_reports_result(vm_rec, db):
    """导航到当前列表里没有的任务：先重载列表；仍找不到时清空选择、列表保持完整"""
    vm, rec = vm_rec
    a = _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    b = _make_task(db, "b.exe", BASE + timedelta(hours=1))   # 列表载入之后才有数据

    assert vm.select_task(b.task_id) is True
    assert vm.current_task_id == b.task_id and vm.selection_is_auto is False

    nodata = _make_task(db, "nodata.exe", BASE + timedelta(hours=2), samples=0)
    rec.clear()
    assert vm.select_task(nodata.task_id) is False
    assert vm.current_task_id is None
    assert [t.task_id for t in vm.tasks] == [b.task_id, a.task_id]
    assert rec.of('empty_state') == [("请选择监控任务", "选定任务和指标后即可查看历史数据")]


def test_select_task_not_explicit_keeps_following_newest(vm_rec, db):
    vm, _rec = vm_rec
    old = _make_task(db, "old.exe", BASE)
    vm.reload_tasks()
    vm.select_task(old.task_id, explicit=False)
    assert vm.selection_is_auto is True
    newer = _make_task(db, "newer.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    assert vm.current_task_id == newer.task_id


def test_select_task_resets_metric_to_first(vm_rec, db):
    vm, rec = vm_rec
    task = _make_task(db, "a.exe", BASE, metrics=(MEM, THREADS))
    vm.reload_tasks()
    vm.select_metric(THREADS)
    rec.clear()
    vm.select_task(task.task_id)
    assert vm.current_metric == MEM
    assert rec.of('metrics_changed') == [([MEM, THREADS],)]
    assert rec.of('selection_changed') == [(task.task_id, MEM, True)]


def test_navigation_triggers_single_query(qapp, db, manager, services):
    """导航跳转：页面显示时重载（自动选最新）+ 选中目标任务，同一轮事件循环内只发一次查询"""
    runner = ManualHistoryRunner(db)
    defer = ManualDefer()
    vm, rec = _vm(qapp, db, manager, services, runner, defer=defer)
    old = _make_task(db, "old.exe", BASE)
    _make_task(db, "newer.exe", BASE + timedelta(hours=1))

    vm.reload_tasks()
    assert vm.select_task(old.task_id) is True
    assert runner.requests == []          # 还没发出
    defer.run()

    assert [r.task_id for r in runner.requests] == [old.task_id]
    runner.complete_all()
    assert rec.of('busy') == [(True,), (False,)]
    assert rec.of('snapshot_ready')[0][0].request.task_id == old.task_id


# ====================================================================== 指标与范围


def test_select_metric_loads_and_ignores_invalid(vm_rec, db, runner):
    vm, rec = vm_rec
    task = _make_task(db, "a.exe", BASE, metrics=(MEM, THREADS))
    vm.reload_tasks()
    count = len(runner.requests)

    vm.select_metric(MEM)                  # 与当前相同
    vm.select_metric("cpu_percent")        # 不属于该任务
    assert len(runner.requests) == count

    rec.clear()
    vm.select_metric(THREADS)
    assert rec.of('selection_changed') == [(task.task_id, THREADS, False)]
    assert runner.requests[-1].metric == THREADS
    assert rec.of('snapshot_ready')[0][0].request.metric == THREADS


def test_select_metric_without_task_is_ignored(vm_rec, runner):
    vm, _rec = vm_rec
    vm.select_metric(MEM)
    assert runner.requests == [] and vm.current_metric is None


def test_set_range_loads_and_persists_across_task_switch(vm_rec, db, runner):
    vm, _rec = vm_rec
    a = _make_task(db, "a.exe", BASE)
    b = _make_task(db, "b.exe", BASE + timedelta(hours=1))
    assert vm.range_key == 'all'
    vm.set_range('10m')                    # 尚未选中任务：只记下范围
    assert runner.requests == []

    vm.reload_tasks()
    assert runner.requests[-1].range_key == '10m'
    vm.set_range('1h')
    assert runner.requests[-1].range_key == '1h'
    count = len(runner.requests)
    vm.set_range('1h')                     # 相同范围不重查
    assert len(runner.requests) == count

    vm.select_task(a.task_id)
    assert vm.range_key == '1h' and runner.requests[-1].task_id == a.task_id
    assert b.task_id != a.task_id
    with pytest.raises(ValueError):
        vm.set_range('3d')


def test_anchor_cache_reused_between_range_and_metric_switches(vm_rec, db, runner, monkeypatch):
    vm, _rec = vm_rec
    task = _make_task(db, "a.exe", BASE, metrics=(MEM, THREADS), samples=5)
    vm.reload_tasks()
    anchor = BASE + timedelta(seconds=4)
    # 首次加载（含"全部"范围）现取锚点并回填缓存
    assert runner.requests[-1].anchor is None

    calls = []
    real = db.analytics.last_point_timestamp
    monkeypatch.setattr(db.analytics, 'last_point_timestamp',
                        lambda tid: calls.append(tid) or real(tid))
    vm.set_range('1h')
    vm.set_range('6h')
    vm.select_metric(THREADS)
    assert [r.anchor for r in runner.requests[-3:]] == [anchor] * 3
    assert runner.requests[-2].since == anchor - timedelta(hours=6)
    assert calls == []

    # 刷新/重新选任务让缓存失效：锚点跟上新数据
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=60), 1.0, MEM)])
    vm.refresh()
    assert runner.requests[-1].anchor is None
    snapshot = vm.last_snapshot
    assert snapshot.task_last_at == BASE + timedelta(seconds=60)
    vm.select_task(task.task_id)
    assert runner.requests[-1].anchor is None


# ====================================================================== 空状态与失败


def test_empty_state_wording_depends_on_range(qapp, db, manager, services, runner):
    vm, rec = _vm(qapp, db, manager, services, runner, table_limit=50, max_buckets=20)
    _seed_query_dataset(db)               # 线程数只采到 60 分钟，任务最后采样在 120 分钟
    vm.reload_tasks()
    vm.select_metric(THREADS)
    rec.clear()

    vm.set_range('10m')
    assert rec.of('empty_state') == [("所选时间范围内暂无数据", "尝试切换到「全部」查看完整历史")]
    assert vm.last_snapshot is None and rec.of('snapshot_ready') == []

    vm.set_range('all')
    assert rec.of('snapshot_ready')


def test_metric_without_data_in_all_range(vm_rec, db, monkeypatch):
    vm, rec = vm_rec
    _make_task(db, "a.exe", BASE)
    monkeypatch.setattr(db.points, 'fetch', lambda *a, **k: [])
    vm.reload_tasks()
    assert rec.of('empty_state') == [("该任务暂无数据", "产生新的采样数据后此处会自动更新")]
    assert rec.of('error') == []


@pytest.mark.parametrize("repo_attr, method", [
    ("tasks", "get"), ("points", "fetch"), ("analytics", "aggregates"),
    ("analytics", "gaps"), ("analytics", "legacy_buckets"), ("markers", "list"),
])
def test_load_failure_shows_read_error(vm_rec, db, monkeypatch, repo_attr, method):
    vm, rec = vm_rec
    _make_task(db, "a.exe", BASE)

    def boom(*a, **k):
        raise StorageError("读取数据点失败: database disk image is malformed")
    monkeypatch.setattr(getattr(db, repo_attr), method, boom)

    vm.reload_tasks()

    assert rec.of('empty_state') == [("读取数据失败", "数据库读取出错，可稍后点击刷新重试")]
    errors = rec.of('error')
    assert len(errors) == 1 and errors[0][0] == "读取数据失败" and "malformed" in errors[0][1]
    assert rec.of('snapshot_ready') == []
    assert rec.of('busy') == [(True,), (False,)]


def test_task_missing_on_manual_load_reports_error(qapp, db, manager, services):
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    task = _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    db.delete_task(task.task_id)          # 查询发出后、执行前任务被删
    runner.complete()
    assert rec.of('error') == [("错误", "无法加载任务信息")]
    assert rec.of('empty_state') == [] and rec.of('snapshot_ready') == []
    assert vm.is_busy is False


# ====================================================================== 加载态与过期丢弃


def test_busy_brackets_async_load(qapp, db, manager, services):
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    _make_task(db, "a.exe", BASE)

    vm.reload_tasks()
    assert vm.is_busy and rec.of('busy') == [(True,)]
    assert len(runner.pending) == 1 and vm.has_pending_query

    runner.complete()
    # 先发结果、再撤加载态
    assert [n for n in rec.names() if n in ('busy', 'snapshot_ready')] == [
        'busy', 'snapshot_ready', 'busy']
    assert rec.of('busy') == [(True,), (False,)]
    assert not vm.has_pending_query


def test_stale_result_is_discarded(qapp, db, manager, services):
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    _make_task(db, "a.exe", BASE, metrics=(MEM, THREADS))
    vm.reload_tasks()
    vm.select_metric(THREADS)             # 第一次查询还没回来就切了指标
    assert len(runner.pending) == 2

    runner.complete(0)                    # 旧结果到达：丢弃，仍在加载
    assert rec.of('snapshot_ready') == [] and vm.is_busy

    runner.complete(0)
    snapshot, = rec.of('snapshot_ready')[0]
    assert snapshot.request.metric == THREADS
    assert rec.of('busy') == [(True,), (False,)]


def test_cancel_pending_drops_in_flight_result(qapp, db, manager, services):
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    _make_task(db, "a.exe", BASE)
    vm.reload_tasks()

    vm.cancel_pending()
    assert rec.of('busy') == [(True,), (False,)]
    runner.complete()
    assert rec.of('snapshot_ready') == []


def test_cancel_before_dispatch_sends_nothing(qapp, db, manager, services):
    runner = ManualHistoryRunner(db)
    defer = ManualDefer()
    vm, _rec = _vm(qapp, db, manager, services, runner, defer=defer)
    _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    vm.cancel_pending()
    defer.run()
    assert runner.requests == []


def test_manual_programming_error_shows_read_error(qapp, db, manager, services):
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    runner.fail("boom", storage_error=False)
    assert rec.of('empty_state') == [("读取数据失败", "数据库读取出错，可稍后点击刷新重试")]
    assert rec.of('error') == [("读取数据失败", "boom")]


# ====================================================================== 删除


def test_delete_goes_through_manager_and_reloads(vm_rec, db, manager, monkeypatch):
    vm, rec = vm_rec
    task = _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    assert vm.can_delete and vm.delete_hint == ""
    deleted = []
    real = manager.delete_task_data
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: deleted.append(tid) or real(tid))
    rec.clear()

    assert vm.delete_current() is True
    assert deleted == [task.task_id] and db.get_task(task.task_id) is None
    assert vm.current_task_id is None and vm.tasks == []
    assert rec.of('tasks_changed')[0][0] == []


def test_delete_explicit_selection_falls_to_select_state(vm_rec, db):
    vm, rec = vm_rec
    older = _make_task(db, "older.exe", BASE)
    _make_task(db, "newer.exe", BASE + timedelta(hours=1))
    vm.reload_tasks()
    vm.select_task(older.task_id)
    rec.clear()
    assert vm.delete_current() is True
    assert vm.current_task_id is None and len(vm.tasks) == 1
    assert rec.of('empty_state') == [("请选择监控任务", "选定任务和指标后即可查看历史数据")]


@pytest.mark.parametrize("by", ["db_status", "task_status"])
def test_delete_refused_while_running(vm_rec, db, manager, status, monkeypatch, by):
    vm, _rec = vm_rec
    task = _make_task(db, "a.exe", BASE, status="running" if by == "db_status" else "stopped")
    if by == "task_status":
        status.set_running(task.task_id)
    vm.reload_tasks()
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: pytest.fail("运行中不应删除"))

    assert vm.can_delete is False
    assert vm.delete_hint == "任务正在运行中，请先停止任务再删除"
    assert vm.delete_current() is False
    assert db.get_task(task.task_id) is not None


def test_delete_failure_keeps_selection(vm_rec, db, manager, monkeypatch, runner):
    vm, _rec = vm_rec
    task = _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    count = len(runner.requests)
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: False)
    assert vm.delete_current() is False
    assert vm.current_task_id == task.task_id and len(runner.requests) == count


def test_delete_without_selection(vm_rec, manager, monkeypatch):
    vm, _rec = vm_rec
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: pytest.fail("未选中不应删除"))
    assert vm.delete_current() is False


# ====================================================================== 自动刷新


@pytest.fixture
def running_vm(qapp, db, manager, services, status):
    """选中一个运行中任务的视图模型；查询手动放行"""
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    task = _make_task(db, "run.exe", BASE, status="running", samples=5)
    status.set_running(task.task_id)
    vm.reload_tasks()
    runner.complete_all()
    rec.clear()
    return vm, rec, runner, task


def test_tick_refreshes_running_task_in_background(running_vm, db):
    vm, rec, runner, task = running_vm
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=9), 1.0, MEM)])

    vm.on_tick()
    request = runner.requests[-1]
    assert request.auto_refresh is True and request.anchor is None   # 锚点现取
    assert rec.of('busy') == []                                       # 定时刷新不置忙
    runner.complete()
    snapshot, = rec.of('snapshot_ready')[0]
    assert snapshot.request.auto_refresh and len(snapshot.points) == 6
    assert snapshot.task_last_at == BASE + timedelta(seconds=9)
    assert rec.of('busy') == []


def test_tick_skipped_while_query_in_flight(running_vm):
    vm, _rec, runner, _task = running_vm
    vm.on_tick()
    vm.on_tick()
    vm.on_tick()
    assert len(runner.pending) == 1
    runner.complete()
    vm.on_tick()
    assert len(runner.pending) == 1 and len(runner.requests) == 3


def test_tick_skipped_when_guard_says_so(running_vm):
    vm, rec, runner, _task = running_vm
    skip = [True]
    vm.set_refresh_guard(lambda: skip[0])
    vm.on_tick()
    assert runner.pending == []
    assert rec.of('tasks_refreshed')          # 跳过查询仍刷新时长文案

    skip[0] = False
    vm.on_tick()
    assert len(runner.pending) == 1
    skip[0] = True                            # 结果回来时已进入应跳过的状态：丢弃
    runner.complete()
    assert rec.of('snapshot_ready') == []


def test_tick_does_nothing_for_stopped_task(qapp, db, manager, services, runner):
    vm, rec = _vm(qapp, db, manager, services, runner)
    _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    count = len(runner.requests)
    rec.clear()
    vm.on_tick()
    assert len(runner.requests) == count and rec.events == []


def test_task_end_triggers_one_reload_and_rereads_end_time(running_vm, db, status):
    vm, rec, runner, task = running_vm
    vm.on_tick()
    runner.complete()
    assert vm.can_delete is False

    end = BASE + timedelta(minutes=20)
    db.update_task_status(task.task_id, "stopped", end)
    status.set_running(task.task_id, False)
    rec.clear()
    vm.on_tick()

    # 时长文案：刚结束的任务换成带结束时间的记录
    tasks, running, _now = rec.of('tasks_refreshed')[0]
    assert running == set() and tasks[0].end_time == end
    # 整体重载一次：删除可用性随之更新
    assert len(rec.of('tasks_changed')) == 1
    assert vm.can_delete is True
    runner.complete_all()
    rec.clear()
    vm.on_tick()
    assert rec.of('tasks_changed') == [] and rec.of('tasks_refreshed') == []


def test_tasks_refreshed_only_while_some_task_running(running_vm, services):
    vm, rec, _runner, _task = running_vm
    vm.on_tick()
    tasks, running, now = rec.of('tasks_refreshed')[0]
    assert running == {tasks[0].task_id} and now == NOW


def test_auto_refresh_storage_error_notifies_once(running_vm):
    vm, rec, runner, _task = running_vm
    vm.on_tick()
    runner.fail("读取数据点失败: disk I/O error")
    vm.on_tick()
    runner.fail("again")
    assert rec.of('error') == [("读取数据失败", "读取数据点失败: disk I/O error")]
    assert rec.of('empty_state') == []        # 保持当前显示

    vm.on_tick()
    runner.complete()                         # 成功一次后复位
    vm.on_tick()
    runner.fail("x")
    assert len(rec.of('error')) == 2

    vm.on_tick()
    runner.fail("boom", storage_error=False)  # 非存储故障只记日志
    assert len(rec.of('error')) == 2


def test_auto_result_dropped_after_user_action(running_vm):
    vm, rec, runner, _task = running_vm
    vm.on_tick()
    vm.set_range('1h')                        # 用户操作：在途的刷新结果作废
    runner.complete(0)
    assert rec.of('snapshot_ready') == []
    runner.complete(0)
    assert rec.of('snapshot_ready')[0][0].request.range_key == '1h'


def test_user_selecting_task_resets_running_transition(running_vm, db, status):
    """所选任务运行中→用户改选他任务：不会因"之前在运行"而触发整体重载"""
    vm, rec, runner, task = running_vm
    vm.on_tick()
    runner.complete()
    other = _make_task(db, "other.exe", BASE - timedelta(days=1))
    vm.select_task(other.task_id)
    runner.complete_all()
    rec.clear()
    vm.on_tick()
    assert rec.of('tasks_changed') == []
    assert task.task_id in vm.running_ids


# ====================================================================== HistoryAutoRefresh


@pytest.mark.parametrize("count, expected", [
    (0, 5000), (199_999, 5000), (200_000, 15000), (999_999, 15000),
    (1_000_000, 30000), (5_000_000, 30000),
])
def test_interval_tiers(qapp, db, manager, services, runner, count, expected):
    vm, _rec = _vm(qapp, db, manager, services, runner)
    auto = HistoryAutoRefresh(vm)
    assert auto_refresh_interval_for(count) == expected
    assert auto.interval == AUTO_REFRESH_INTERVAL_MS == 5000
    auto.update_interval(count)
    assert auto.interval == expected


def test_interval_follows_snapshot_total_count(qapp, db, manager, services, runner,
                                               monkeypatch):
    """按显示范围的总条数分档（存在基线时统计只覆盖基线之后，不能用统计条数）"""
    vm, _rec = _vm(qapp, db, manager, services, runner)
    auto = HistoryAutoRefresh(vm)
    _make_task(db, "a.exe", BASE)
    real = db.analytics.aggregates
    monkeypatch.setattr(db.analytics, 'aggregates',
                        lambda *a, **k: dataclasses.replace(real(*a, **k), count=300_000))
    vm.reload_tasks()
    assert auto.interval == 15000


def test_interval_ignores_baseline_limited_stats(qapp, db, manager, services, runner):
    vm, _rec = _vm(qapp, db, manager, services, runner)
    auto = HistoryAutoRefresh(vm)
    task = _make_task(db, "a.exe", BASE, samples=6)
    db.markers.add(task.task_id, BASE + timedelta(seconds=4), "基线", 'baseline')
    snapshots = []
    vm.snapshot_ready.connect(snapshots.append)
    vm.reload_tasks()
    assert snapshots[0].total_count == 6 and snapshots[0].stats['count'] == 2
    assert auto.interval == 5000


def test_start_stop_and_tick(qapp, db, manager, services, status):
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    hovering = [False]
    auto = HistoryAutoRefresh(vm, should_skip=lambda: hovering[0])
    task = _make_task(db, "run.exe", BASE, status="running")
    status.set_running(task.task_id)
    vm.reload_tasks()
    runner.complete_all()

    assert not auto.is_active
    auto.start()
    assert auto.is_active

    hovering[0] = True
    auto.tick()
    assert runner.pending == []
    hovering[0] = False
    auto.tick()
    assert len(runner.pending) == 1

    rec.clear()
    auto.stop()                               # 隐藏：停表并让在途结果作废
    assert not auto.is_active
    runner.complete()
    assert rec.of('snapshot_ready') == []


# ====================================================================== 真实后台线程


@pytest.fixture
def real_vm(qapp, db, manager, services):
    vm = HistoryViewModel(db, manager, services)
    rec = Recorder(vm)
    yield vm, rec
    poll_until(lambda: (QApplication.processEvents() or True)
               and services.threads.entries() == [], timeout=10)


def _pump_until(condition, timeout=10.0):
    def check():
        QApplication.processEvents()
        return condition()
    return poll_until(check, timeout=timeout)


def test_default_runner_queries_off_main_thread(real_vm, db, services, monkeypatch):
    vm, rec = real_vm
    _make_task(db, "a.exe", BASE)
    threads = []
    real = db.points.fetch
    monkeypatch.setattr(db.points, 'fetch',
                        lambda *a, **k: threads.append(threading.current_thread()) or real(*a, **k))
    gate = threading.Event()
    real_get = db.tasks.get

    def blocking_get(*a, **k):
        gate.wait(10)
        return real_get(*a, **k)
    monkeypatch.setattr(db.tasks, 'get', blocking_get)

    vm.reload_tasks()
    assert rec.of('busy') == [(True,)]
    assert _pump_until(lambda: services.threads.entries() != [])
    entry, = services.threads.entries()
    assert (entry.name, entry.cancel, entry.timeout_ms) == ("历史数据查询", None, 3000)

    gate.set()
    assert _pump_until(lambda: rec.of('snapshot_ready') and services.threads.entries() == [])
    assert threads and all(t is not threading.main_thread() for t in threads)
    assert rec.of('busy') == [(True,), (False,)]


def test_default_defer_coalesces_navigation(real_vm, db, monkeypatch):
    vm, rec = real_vm
    old = _make_task(db, "old.exe", BASE)
    _make_task(db, "newer.exe", BASE + timedelta(hours=1))
    queried = []
    real = db.points.fetch
    monkeypatch.setattr(db.points, 'fetch',
                        lambda q: queried.append(q.task_id) or real(q))

    vm.reload_tasks()
    vm.select_task(old.task_id)
    assert _pump_until(lambda: rec.of('snapshot_ready'))
    assert queried == [old.task_id]


def test_runner_reports_failure_when_worker_ends_without_result(real_vm, db, services, monkeypatch):
    """线程收尾本身出错、没送出结果：执行器在线程结束时补一个失败快照，加载态不会卡住"""
    from core.history_refresh_worker import HistoryRefreshWorker
    vm, rec = real_vm
    _make_task(db, "a.exe", BASE)

    def broken(self, exc):
        raise RuntimeError("收尾出错")
    monkeypatch.setattr(HistoryRefreshWorker, 'handle_failure', broken)
    monkeypatch.setattr(db.points, 'fetch', lambda *a, **k: 1 / 0)

    vm.reload_tasks()
    assert _pump_until(lambda: rec.of('busy') == [(True,), (False,)])
    assert rec.of('error') == [("读取数据失败", "后台查询异常结束")]


def test_viewmodel_modules_hold_no_widgets():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("history_vm.py", "history_auto_refresh.py"):
        with open(os.path.join(root, "ui", "viewmodels", name), encoding="utf-8") as f:
            text = f.read()
        assert "QtWidgets" not in text and "qfluentwidgets" not in text, name


# ====================================================================== 基线与打点变化


class _SignalRecorder:
    def __init__(self, signal):
        self.values = []
        signal.connect(lambda *args: self.values.append(args[0] if len(args) == 1 else args))


class MarkerManager(QObject):
    """带 markers_changed 信号的 manager 替身（视图模型只用到它与 delete_task_data）"""

    markers_changed = pyqtSignal(str)

    def delete_task_data(self, task_id):
        return False


def _add_baseline(db, task, seconds=1):
    return db.markers.add(task.task_id, BASE + timedelta(seconds=seconds), "基线", 'baseline')


def test_baseline_changed_follows_selected_task(vm_rec, db):
    vm, _rec = vm_rec
    plain = _make_task(db, "plain.exe", BASE - timedelta(days=1))
    with_baseline = _make_task(db, "base.exe", BASE)
    _add_baseline(db, with_baseline)
    seen = _SignalRecorder(vm.baseline_changed)

    vm.reload_tasks()
    assert vm.has_baseline is True and seen.values[-1] is True
    vm.select_task(plain.task_id)
    assert vm.has_baseline is False and seen.values[-1] is False


def test_baseline_known_even_when_range_is_empty(qapp, db, manager, services, runner,
                                                 monkeypatch):
    """范围内无数据（空状态）时也要知道有没有基线，"自基线"才能选"""
    vm, rec = _vm(qapp, db, manager, services, runner)
    task = _make_task(db, "a.exe", BASE, metrics=(MEM, THREADS))
    _add_baseline(db, task)
    monkeypatch.setattr(db.points, 'fetch', lambda *a, **k: [])
    vm.reload_tasks()
    assert rec.of('empty_state') and vm.has_baseline is True


def test_set_range_accepts_baseline_key(vm_rec, db, runner):
    vm, _rec = vm_rec
    task = _make_task(db, "a.exe", BASE, samples=6)
    _add_baseline(db, task, seconds=3)
    vm.reload_tasks()
    vm.set_range('baseline')
    request = runner.requests[-1]
    assert request.range_key == 'baseline'
    assert vm.last_snapshot.request.since == BASE + timedelta(seconds=3)
    assert len(vm.last_snapshot.points) == 3


def test_baseline_range_falls_back_to_all_when_baseline_removed(vm_rec, db):
    vm, rec = vm_rec
    task = _make_task(db, "a.exe", BASE, samples=6)
    marker_id = _add_baseline(db, task, seconds=3)
    vm.reload_tasks()
    vm.set_range('baseline')
    resets = _SignalRecorder(vm.range_reset)

    db.markers.delete(marker_id)
    vm.refresh()

    assert resets.values == ['all'] and vm.range_key == 'all'
    assert vm.has_baseline is False
    snapshot, = rec.of('snapshot_ready')[-1]
    assert snapshot.request.since is None and len(snapshot.points) == 6


def test_markers_changed_requeries_current_task_quietly(qapp, db, services, status):
    manager = MarkerManager()
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    task = _make_task(db, "a.exe", BASE, samples=6)
    other = _make_task(db, "b.exe", BASE - timedelta(days=1))
    vm.reload_tasks()
    runner.complete_all()
    rec.clear()

    manager.markers_changed.emit(other.task_id)     # 不是当前任务：忽略
    assert runner.pending == []

    vm.set_refresh_guard(lambda: True)              # 用户正在看图也要应用
    _add_baseline(db, task)
    manager.markers_changed.emit(task.task_id)
    request = runner.pending[0][0]
    assert request.auto_refresh is True             # 保留缩放、不置忙
    runner.complete()
    snapshot, = rec.of('snapshot_ready')[0]
    assert snapshot.baseline is not None and vm.has_baseline is True
    assert rec.of('busy') == []


def test_markers_changed_during_user_load_reissues_it(qapp, db, services):
    manager = MarkerManager()
    runner = ManualHistoryRunner(db)
    vm, rec = _vm(qapp, db, manager, services, runner)
    task = _make_task(db, "a.exe", BASE, samples=6)
    vm.reload_tasks()
    assert vm.is_busy and len(runner.pending) == 1

    _add_baseline(db, task)
    manager.markers_changed.emit(task.task_id)
    assert len(runner.pending) == 2 and runner.pending[1][0].auto_refresh is False
    runner.complete(0)                              # 旧结果作废
    assert rec.of('snapshot_ready') == [] and vm.is_busy
    runner.complete(0)
    assert rec.of('snapshot_ready')[0][0].baseline is not None
    assert not vm.is_busy


def test_manager_without_markers_changed_is_supported(qapp, db, services, runner):
    """manager 尚无 markers_changed 信号（旧版或替身）时照常构造"""
    class Plain:
        def delete_task_data(self, task_id):
            return False
    vm, _rec = _vm(qapp, db, Plain(), services, runner)
    _make_task(db, "a.exe", BASE)
    vm.reload_tasks()
    assert vm.last_snapshot is not None
