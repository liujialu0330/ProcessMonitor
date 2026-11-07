"""
监控页 Presenter 的进程组任务（进程树 / 同名进程）：

- 创建：请求带 target_kind 时 create_task 才收到 target_kind 关键字，并剔除对组不适用的指标；
  单个进程的调用与旧版逐项相同（不传 target_kind、不过滤）；
- 重复确认：同一根 PID 的进程树 / 同名的同名进程任务已在运行才确认，已结束的不算；
- 指标对话框：进程组任务用组模式，选择不写进"上次的指标"；
- 卡片成员摘要：members_changed 与每秒 tick 都把 manager.group_members 交给卡片，已结束的不再刷；
- 同配置再测：同名进程直接按名再开，进程树找唯一同名进程、找不到时切到进程树并填搜索框；
- 打点对话框与快速打点反馈里同名进程没有 PID。
复用 tests/test_monitor_presenter.py 的替身（FakeManager 的 create_task 没有 target_kind，正好用来
证明单个进程路径没有多传参数）。
"""
import dataclasses
from typing import Dict, List

import pytest
from PyQt5.QtCore import pyqtSignal

from core.contracts import StopReason, TaskInfo, TaskStatus
from core.group_aggregator import MemberSnapshot
from tests.test_monitor_presenter import (  # noqa: F401 夹具
    METRICS, PROCESSES, FakeBoard, FakeCard, FakeForm, FakeManager, board, form, host,
    services)
from ui.presenters.monitor_presenter import MonitorPresenter, resolve_error_text
from ui.widgets.monitor.new_task_form import TaskRequest
from ui.widgets.monitor.process_picker import ERROR_GROUP_NEEDS_NAME, ResolveError
from utils.metrics import PROCESS_COUNT, MetricType

PEAK = MetricType.MEMORY_PEAK_WSET
CPU = MetricType.CPU_PERCENT
RSS = MetricType.MEMORY_RSS


class GroupManager(FakeManager):
    """在 FakeManager 上补进程组接口：create_task 的 target_kind、members_changed 信号、
    group_members / list_task_infos。"""

    members_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.members: Dict[str, List[MemberSnapshot]] = {}
        self.truncated = set()
        self.kinds: List[str] = []

    def create_task(self, pid, name, metrics, interval=None, note='', create_time=None, *,
                    target_kind='pid'):
        self.created.append((pid, name, list(metrics), interval, note, create_time))
        self.kinds.append(target_kind)
        if not self.create_ok:
            return None
        task_id = f"t{len(self.created)}"
        self.infos[task_id] = TaskInfo(
            task_id=task_id, pid=pid, process_name=name, metric_types=tuple(metrics),
            interval=interval, note=note, status=TaskStatus.PENDING, start_time=None,
            end_time=None, create_time=create_time, is_paused=False, is_stopping=False,
            target_kind=target_kind)
        self.task_added.emit(task_id)
        return task_id

    def get_task_info(self, task_id):
        info = self.infos.get(task_id)
        if info is not None and task_id in self.truncated:
            info = dataclasses.replace(info, members_truncated=True)
        return info

    def group_members(self, task_id):
        return tuple(self.members.get(task_id, ()))

    def list_task_infos(self):
        return list(self.infos.values())

    def set_running(self, task_id):
        self.infos[task_id] = dataclasses.replace(
            self.infos[task_id], status=TaskStatus.RUNNING)


class GroupCard(FakeCard):
    def __init__(self, info):
        super().__init__(info)
        self.member_calls = []

    def set_members(self, members, truncated=False):
        self.member_calls.append((tuple(members), truncated))


class GroupBoard(FakeBoard):
    def create_card(self, info):
        self.cards[info.task_id] = GroupCard(info)
        return self.cards[info.task_id]


class KindForm(FakeForm):
    """带目标类型的表单替身。"""

    def __init__(self):
        super().__init__()
        self.kind = 'pid'
        self.kind_set = []

    def target_kind(self):
        return self.kind

    def set_target_kind(self, kind):
        self.kind = kind
        self.kind_set.append(kind)


@pytest.fixture
def manager():
    return GroupManager()


@pytest.fixture
def form():
    return KindForm()


@pytest.fixture
def board():
    return GroupBoard()


@pytest.fixture
def presenter(manager, services, form, board, host):
    return MonitorPresenter(manager, services, form, board, host)


def _group_request(kind, pid=1002, name="beta.exe", metrics=(CPU, PEAK, RSS)):
    return TaskRequest(pid=pid, name=name, create_time=1002.5 if pid else None,
                       metrics=tuple(metrics), interval=1.0, target_kind=kind)


def _member(key, count, name=None):
    return MemberSnapshot(key, name or key, count)


# ---------------------------------------------------------------- 创建


def test_pid_request_does_not_pass_target_kind_and_keeps_metrics(services, host):
    """单个进程路径：FakeManager.create_task 没有 target_kind 形参，多传就会 TypeError。"""
    plain_manager = FakeManager()
    plain_presenter = MonitorPresenter(plain_manager, services, KindForm(), GroupBoard(), host)
    plain_presenter.start_monitoring(TaskRequest(
        pid=1002, name="beta.exe", create_time=1002.5, metrics=(CPU, PEAK), interval=1.0))
    assert plain_manager.created == [(1002, "beta.exe", [CPU, PEAK], 1.0, "", 1002.5)]


@pytest.mark.parametrize("kind, pid", [('tree', 1002), ('name', 0)])
def test_group_request_passes_kind_and_filters_inapplicable_metrics(
        presenter, manager, kind, pid):
    task_id = presenter.start_monitoring(_group_request(kind, pid=pid))
    assert task_id is not None
    assert manager.kinds == [kind]
    assert manager.created[0][2] == [CPU, RSS]                  # 峰值类被剔除
    assert manager.infos[task_id].target_kind == kind


def test_group_card_shows_identity_with_kind(presenter, manager, board):
    tree = presenter.start_monitoring(_group_request('tree'))
    name = presenter.start_monitoring(_group_request('name', pid=0, name="chrome.exe"))
    assert board.card(tree).info.target_kind == 'tree'
    assert board.card(name).info.target_kind == 'name'
    assert presenter._tasks[tree].identity_text() == "beta.exe · 进程树 · 根 PID 1002"
    assert presenter._tasks[name].identity_text() == "chrome.exe · 同名进程"


def test_group_needs_name_error_text():
    text = resolve_error_text(ResolveError(ERROR_GROUP_NEEDS_NAME, "1002"))
    assert "进程名" in text and "PID" in text


# ---------------------------------------------------------------- 重复确认


def test_duplicate_running_tree_asks_for_confirmation(presenter, manager, services):
    first = presenter.start_monitoring(_group_request('tree'))
    manager.set_running(first)
    manager.created.clear()
    services.dialogs.push("confirm", False)

    assert presenter.start_monitoring(_group_request('tree')) is None
    assert manager.created == []
    assert "进程树" in services.dialogs.last_call("confirm")["title"]

    services.dialogs.push("confirm", True)
    assert presenter.start_monitoring(_group_request('tree')) is not None
    assert len(manager.created) == 1


def test_duplicate_name_group_compares_case_insensitively(presenter, manager, services):
    first = presenter.start_monitoring(_group_request('name', pid=0, name="Chrome.exe"))
    manager.set_running(first)
    services.dialogs.push("confirm", False)
    assert presenter.start_monitoring(
        _group_request('name', pid=0, name="chrome.exe")) is None
    assert "同名进程" in services.dialogs.last_call("confirm")["title"]


def test_finished_or_other_group_does_not_ask(presenter, manager, services):
    finished = presenter.start_monitoring(_group_request('tree'))        # 还是 PENDING：不在运行
    other_kind = presenter.start_monitoring(_group_request('name', pid=0, name="beta.exe"))
    manager.set_running(other_kind)
    assert presenter.start_monitoring(_group_request('tree')) is not None
    assert services.dialogs.calls_of("confirm") == []
    del finished


def test_group_task_does_not_trigger_pid_duplicate_check(presenter, manager, services):
    manager.running_pids.add(1002)                                       # 同 PID 有单进程任务
    assert presenter.start_monitoring(_group_request('tree')) is not None
    assert services.dialogs.calls_of("confirm") == []


# ---------------------------------------------------------------- 指标对话框


def test_group_metric_dialog_uses_group_mode_and_does_not_remember(
        presenter, services, form):
    form.kind = 'tree'
    services.dialogs.push("choose_metrics", [CPU, PROCESS_COUNT])
    before = services.preferences.last_metrics()
    form.choose_metrics_requested.emit()
    assert services.dialogs.last_call("choose_metrics")["group"] is True
    assert form.metrics() == [CPU, PROCESS_COUNT]
    assert list(services.preferences.last_metrics()) == before             # 不写进上次的指标


def test_pid_metric_dialog_is_unchanged(presenter, services, form):
    services.dialogs.push("choose_metrics", [CPU])
    form.choose_metrics_requested.emit()
    assert "group" not in services.dialogs.last_call("choose_metrics")
    assert list(services.preferences.last_metrics()) == [CPU]


# ---------------------------------------------------------------- 卡片成员摘要


def test_members_changed_pushes_members_to_group_card(presenter, manager, board):
    task_id = presenter.start_monitoring(_group_request('tree'))
    manager.members[task_id] = [_member('app.exe', 1), _member('worker.exe', 2)]
    manager.members_changed.emit(task_id)
    members, truncated = board.card(task_id).member_calls[-1]
    assert [(m.member_key, m.instance_count) for m in members] == [
        ('app.exe', 1), ('worker.exe', 2)]
    assert truncated is False


def test_members_truncated_flag_reaches_card(presenter, manager, board):
    task_id = presenter.start_monitoring(_group_request('tree'))
    manager.truncated.add(task_id)
    manager.members_changed.emit(task_id)
    assert board.card(task_id).member_calls[-1][1] is True


def test_tick_refreshes_group_members_only_while_running(presenter, manager, board):
    group = presenter.start_monitoring(_group_request('tree'))
    pid = presenter.start_monitoring(TaskRequest(
        pid=1001, name="alpha.exe", create_time=1001.5, metrics=METRICS, interval=1.0))
    manager.members[group] = [_member('app.exe', 3)]
    presenter.tick()
    assert [len(c[0]) for c in board.card(group).member_calls] == [1]
    assert not hasattr(board.card(pid), 'member_calls') or not board.card(pid).member_calls
    manager.task_stopped.emit(group, StopReason.USER)
    manager.members[group] = [_member('app.exe', 4)]
    presenter.tick()
    manager.members_changed.emit(group)
    assert len(board.card(group).member_calls) == 1                      # 结束后不再刷新


def test_members_changed_for_unknown_task_is_ignored(presenter, manager):
    manager.members_changed.emit("t-unknown")                            # 不抛异常


# ---------------------------------------------------------------- 同配置再测


def _finished_group(presenter, manager, kind, **kwargs):
    task_id = presenter.start_monitoring(_group_request(kind, **kwargs))
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    manager.created.clear()
    manager.kinds.clear()
    return task_id


def test_restart_name_group_starts_directly_by_name(presenter, manager, board):
    task_id = _finished_group(presenter, manager, 'name', pid=0, name="chrome.exe")
    board.restart_requested.emit(task_id)
    assert manager.kinds == ['name']
    assert manager.created[0][:2] == (0, "chrome.exe")


def test_restart_tree_with_single_match_starts_tree(presenter, manager, board, services):
    task_id = _finished_group(presenter, manager, 'tree', pid=1002, name="beta.exe")
    services.process_query.processes[:] = [(4321, "beta.exe", 9.5)]
    board.restart_requested.emit(task_id)
    assert manager.kinds == ['tree']
    assert manager.created[0][0] == 4321


def test_restart_tree_without_match_switches_form_to_tree_and_fills_search(
        presenter, manager, board, services, form):
    task_id = _finished_group(presenter, manager, 'tree', pid=1002, name="beta.exe")
    services.process_query.processes[:] = []
    board.restart_requested.emit(task_id)
    assert manager.created == []
    assert form.kind_set == ['tree'] and form.filled == ["beta.exe"]


# ---------------------------------------------------------------- 打点


def test_marker_dialog_has_no_pid_for_name_group(presenter, manager, board, services):
    task_id = presenter.start_monitoring(_group_request('name', pid=0, name="chrome.exe"))
    services.dialogs.push("ask_marker_label", "基线")
    board.marker_requested.emit(task_id)
    call = services.dialogs.last_call("ask_marker_label")
    assert call["process_name"] == "chrome.exe" and call["pid"] is None


def test_marker_dialog_shows_root_pid_for_tree_group(presenter, manager, board, services):
    task_id = presenter.start_monitoring(_group_request('tree'))
    services.dialogs.push("ask_marker_label", "基线")
    board.marker_requested.emit(task_id)
    assert services.dialogs.last_call("ask_marker_label")["pid"] == 1002
