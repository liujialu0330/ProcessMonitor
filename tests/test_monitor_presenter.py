"""
监控页 Presenter：用假表单、假面板、假 manager 与服务替身覆盖全部业务流程——
新建任务（指标、目标校验文案、重复 PID 确认）、停止确认、暂停/恢复、打点（点击时刻、
默认标签序列"基线 / 第 N 轮"、类型推断、失败提示）、卡片基线（5 个样本中位数、换基线、
删基线）、任务结束（卡片转已结束、表单清掉已退出进程、异常结束提示）、
指标不可用（管理员提示）、错误去重、上限提示、同配置再测三种情况、关闭卡片与表单折叠、
自动化方案（落库与补存、挂接自动打点、按方案停止的提示、再测沿用、停止后自动导出）、
免弹窗打点（托盘 / 全局快捷键）与"清除全部"已结束卡片。
"""
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QWidget

from core.automation.plan import AutomationPlan
from core.contracts import StopReason, TaskInfo, TaskStatus, UnavailableReason
from core.monitor_manager import BASELINE_LABEL, BaselineSamples, round_label
from core.process_launcher import LaunchSpec
from core.report_worker import ReportResult, ReportWorker
from data.errors import StorageError
from data.models import Marker, MonitorTask
from tests.doubles import DEFAULT_FAKE_NOW, FakePreferences, FakeProcessQuery
from tests.factories import make_services
from ui.presenters.monitor_presenter import (
    ABNORMAL_STOP_INFOBAR_MS, QUICK_MARKER_FAILED, QUICK_MARKER_NONE, QUICK_MARKER_OK,
    MonitorPresenter, resolve_error_text,
)
from ui.formatting import plan_summary
from ui.services import EVENT_TASK_ABNORMAL
from ui.widgets.monitor.new_task_form import TaskRequest, WaitRequest
from ui.widgets.monitor.process_picker import (
    ERROR_EMPTY, ERROR_NO_MATCH, ERROR_NOT_A_NUMBER, ERROR_NOT_FOUND, ResolveError,
)
from utils.metrics import LEAK_CHECK_PRESET, MetricType

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "beta.exe", 1002.5),
             (1003, "MPrintExp.exe", 1003.5)]
METRICS = (MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS)


# ---------------------------------------------------------------- 替身


class FakeManager(QObject):
    """只提供 Presenter 用到的 manager 接口；create_task 与真实实现一样同步发 task_added。"""

    task_added = pyqtSignal(str)
    task_started = pyqtSignal(str)
    task_stopped = pyqtSignal(str, object)
    data_updated = pyqtSignal(str, dict)
    error_occurred = pyqtSignal(str, str)
    metrics_unavailable = pyqtSignal(str, dict)
    task_limit_reached = pyqtSignal()
    markers_changed = pyqtSignal(str)
    # 与真实 manager 一致：暂停 / 恢复成功后同步发出
    task_paused = pyqtSignal(str)
    task_resumed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.infos: Dict[str, TaskInfo] = {}
        # 内存版打点表：与真实 manager 的序列/基线规则一致（真实规则由
        # test_monitor_manager_api 覆盖，这里只为驱动 Presenter）
        self.markers: Dict[str, List[Marker]] = {}
        self.seeds: Dict[str, BaselineSamples] = {}
        self.created: List[tuple] = []
        self.commands: List[tuple] = []
        self.running_pids = set()
        self.running = 0
        self.create_ok = True
        self.stop_result = True
        self.pause_result = True
        self.resume_result = True
        self.marker_ok = True
        self.paused_ids = set()
        self.maintenance = False
        self.exports = 0

    def create_task(self, pid, name, metrics, interval=None, note='', create_time=None):
        self.created.append((pid, name, list(metrics), interval, note, create_time))
        if not self.create_ok:
            return None
        task_id = f"t{len(self.created)}"
        self.infos[task_id] = TaskInfo(
            task_id=task_id, pid=pid, process_name=name, metric_types=tuple(metrics),
            interval=interval, note=note, status=TaskStatus.PENDING, start_time=None,
            end_time=None, create_time=create_time, is_paused=False, is_stopping=False)
        self.task_added.emit(task_id)
        return task_id

    def get_task_info(self, task_id):
        return self.infos.get(task_id)

    def has_running_task_for_pid(self, pid):
        return pid in self.running_pids

    def running_task_count(self):
        return self.running

    def start_task(self, task_id):
        self.commands.append(("start", task_id))
        return True

    def stop_task(self, task_id):
        self.commands.append(("stop", task_id))
        return self.stop_result

    def pause_task(self, task_id):
        self.commands.append(("pause", task_id))
        if self.pause_result:
            self.task_paused.emit(task_id)
        return self.pause_result

    def resume_task(self, task_id):
        self.commands.append(("resume", task_id))
        if self.resume_result:
            self.task_resumed.emit(task_id)
        return self.resume_result

    def is_task_paused(self, task_id):
        return task_id in self.paused_ids

    def is_maintenance_running(self):
        return self.maintenance

    def begin_export(self):
        if self.maintenance:
            return False
        self.exports += 1
        return True

    def end_export(self):
        self.exports -= 1

    def remove_task(self, task_id):
        self.commands.append(("remove", task_id))
        self.infos.pop(task_id, None)
        return True

    def add_marker(self, task_id, label, timestamp=None, kind='user'):
        self.commands.append(("marker", task_id, label, timestamp, kind))
        if not self.marker_ok:
            return None
        markers = self.markers.setdefault(task_id, [])
        if kind == 'baseline':
            markers[:] = [Marker(m.task_id, m.timestamp, m.label, m.id,
                                 'user' if m.kind == 'baseline' else m.kind)
                          for m in markers]
        marker = Marker(task_id, timestamp, label.strip(),
                        sum(len(v) for v in self.markers.values()) + 1, kind)
        markers.append(marker)
        self.markers_changed.emit(task_id)
        return marker

    def list_markers(self, task_id):
        return list(self.markers.get(task_id, []))

    def get_baseline(self, task_id, markers=None):
        markers = self.list_markers(task_id) if markers is None else markers
        return next((m for m in markers if m.kind == 'baseline'), None)

    def next_marker_label(self, task_id):
        markers = self.list_markers(task_id)
        if self.get_baseline(task_id, markers) is None:
            return BASELINE_LABEL
        return round_label(sum(1 for m in markers if m.kind == 'user') + 1)

    def baseline_samples(self, task_id, baseline=None):
        return self.seeds.get(task_id, BaselineSamples(marker=baseline))

    def delete_all_markers(self, task_id):
        self.markers[task_id] = []
        self.markers_changed.emit(task_id)


class FakeCard:
    """记录 Presenter 对卡片的调用。基线相关调用另存到 baseline / baseline_events，
    时间推进（tick / set_started_at）与刻度（set_markers）也单独记，避免干扰
    calls 的精确断言。"""

    _RECORDED = ("update_values", "increment_count", "mark_unavailable",
                 "set_paused", "set_stopping")

    def __init__(self, info: TaskInfo):
        self.info = info
        self.calls: List[tuple] = []
        self.ticks: List[datetime] = []
        self.started_at: Optional[datetime] = None
        self.markers: List[Marker] = []
        self.baseline: Optional[tuple] = None      # (时间, {指标: 值})
        self.baseline_events: List[tuple] = []
        self.tooltip = ''

    def __getattr__(self, name):
        if name in self._RECORDED:
            return lambda *args: self.calls.append((name,) + args)
        raise AttributeError(name)

    def tick(self, now):
        self.ticks.append(now)

    def setToolTip(self, text):
        self.tooltip = text

    def set_started_at(self, when):
        self.started_at = when

    def set_markers(self, markers):
        self.markers = list(markers)

    def set_baseline(self, timestamp, values=None):
        self.baseline = (timestamp, dict(values or {}))
        self.baseline_events.append(("set", timestamp))

    def set_baseline_value(self, metric, value):
        self.baseline[1][metric] = value
        self.baseline_events.append(("value", metric, value))

    def clear_baseline(self):
        self.baseline = None
        self.baseline_events.append(("clear",))


class FakeBoard(QObject):
    stop_requested = pyqtSignal(str)
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    marker_requested = pyqtSignal(str)
    history_requested = pyqtSignal(str)
    restart_requested = pyqtSignal(str)
    close_requested = pyqtSignal(str)
    clear_finished_requested = pyqtSignal()
    wait_cancel_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.cards: Dict[str, FakeCard] = {}
        self.finished: Dict[str, tuple] = {}
        self.exit_codes: Dict[str, Optional[int]] = {}
        self.counts = None
        self.waiting: Dict[str, tuple] = {}

    def add_waiting(self, wait_id, name, detail=''):
        self.waiting[wait_id] = (name, detail)

    def remove_waiting(self, wait_id):
        self.waiting.pop(wait_id, None)

    def create_card(self, info):
        self.cards[info.task_id] = FakeCard(info)
        return self.cards[info.task_id]

    def remove_card(self, task_id):
        self.cards.pop(task_id, None)
        self.finished.pop(task_id, None)

    def card(self, task_id):
        return self.cards.get(task_id)

    def finish_card(self, task_id, reason, end_time, exit_code=None):
        self.finished[task_id] = (reason, end_time)
        self.exit_codes[task_id] = exit_code

    def set_counts(self, running, limit):
        self.counts = (running, limit)


class FakeForm(QObject):
    start_requested = pyqtSignal(object)
    wait_requested = pyqtSignal(object)
    target_invalid = pyqtSignal(object)
    choose_metrics_requested = pyqtSignal()
    browse_log_requested = pyqtSignal()
    browse_export_dir_requested = pyqtSignal()
    browse_snapshot_dir_requested = pyqtSignal()
    browse_exe_requested = pyqtSignal()
    browse_cwd_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._metrics: List[str] = []
        self.collapsed: List[bool] = []
        self.errors: List[str] = []
        self.filled: List[str] = []
        self.forgotten: List[int] = []
        self.interval_value = 4.0
        self.automation_plans: List[object] = []
        self.log_path_value = ''
        self.export_dir_value = ''
        self.snapshot_dir_value = ''
        self.launch_exe_value = ''
        self.launch_cwd_value = ''
        self.launch_errors: List[str] = []

    def set_metrics_summary(self, metrics):
        self._metrics = list(metrics)

    def metrics(self):
        return list(self._metrics)

    def interval(self):
        return self.interval_value

    def show_error(self, text):
        self.errors.append(text)

    def fill_search(self, text):
        self.filled.append(text)

    def forget_process_if(self, pid):
        self.forgotten.append(pid)
        return False

    def set_collapsed(self, collapsed):
        self.collapsed.append(collapsed)

    def set_automation_plan(self, plan):
        self.automation_plans.append(plan)

    def log_path(self):
        return self.log_path_value

    def set_log_path(self, path):
        self.log_path_value = path

    def export_dir(self):
        return self.export_dir_value

    def set_export_dir(self, path):
        self.export_dir_value = path

    def snapshot_dir(self):
        return self.snapshot_dir_value

    def set_snapshot_dir(self, path):
        self.snapshot_dir_value = path

    def launch_exe(self):
        return self.launch_exe_value

    def set_launch_exe(self, path):
        self.launch_exe_value = path

    def launch_cwd(self):
        return self.launch_cwd_value

    def set_launch_cwd(self, path):
        self.launch_cwd_value = path

    def show_launch_error(self, text):
        self.launch_errors.append(text)


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def services():
    return make_services(process_query=FakeProcessQuery(PROCESSES))


@pytest.fixture
def manager():
    return FakeManager()


@pytest.fixture
def form():
    return FakeForm()


@pytest.fixture
def board():
    return FakeBoard()


@pytest.fixture
def host(qapp):
    widget = QWidget()
    yield widget
    widget.close()


@pytest.fixture
def presenter(manager, services, form, board, host):
    return MonitorPresenter(manager, services, form, board, host)


def _request(pid=1001, name="alpha.exe", metrics=METRICS, note="", create_time=1001.5,
             interval=1.0):
    return TaskRequest(pid=pid, name=name, create_time=create_time,
                       metrics=tuple(metrics), interval=interval, note=note)


def _add_task(manager, pid=1001, name="demo.exe", metrics=METRICS, note="", interval=2.0):
    return manager.create_task(pid, name, metrics, interval, note=note)


# ---------------------------------------------------------------- 初始状态与指标


def test_initial_metrics_come_from_preferences(manager, form, board, host):
    prefs = FakePreferences(last_metrics=[MetricType.CPU_PERCENT])
    MonitorPresenter(manager, make_services(preferences=prefs), form, board, host)
    assert form.metrics() == [MetricType.CPU_PERCENT]
    assert form.collapsed == [False]
    assert board.counts == (0, 5)


def test_default_metrics_is_leak_check_preset(presenter, form):
    assert form.metrics() == list(LEAK_CHECK_PRESET)


def test_confirming_metric_dialog_remembers_selection(presenter, services, form, host):
    chosen = [MetricType.NUM_THREADS, MetricType.MEMORY_PRIVATE]
    services.dialogs.push("choose_metrics", chosen)

    form.choose_metrics_requested.emit()

    call = services.dialogs.last_call("choose_metrics")
    assert call["selected"] == list(LEAK_CHECK_PRESET)
    assert call["parent"] is host.window()
    assert form.metrics() == chosen
    assert services.preferences.remembered == [chosen]


def test_cancelling_metric_dialog_keeps_selection(presenter, services, form):
    services.dialogs.push("choose_metrics", None)
    form.choose_metrics_requested.emit()
    assert form.metrics() == list(LEAK_CHECK_PRESET)
    assert services.preferences.remembered == []


# ---------------------------------------------------------------- 开始监控


def test_start_creates_and_starts_task_with_request_fields(presenter, manager, form):
    form.start_requested.emit(_request(note="建模 200 次", interval=3.0))
    assert manager.created == [
        (1001, "alpha.exe", list(METRICS), 3.0, "建模 200 次", 1001.5)]
    assert manager.commands == [("start", "t1")]


def test_start_without_metrics_is_rejected(presenter, manager, form, services):
    form.start_requested.emit(_request(metrics=()))
    assert manager.created == []
    assert services.notifier.last("warning").content == "请先选择要监控的指标"


@pytest.mark.parametrize("error, text", [
    (ResolveError(ERROR_EMPTY), "请输入进程名或 PID，或从列表中选择进程"),
    (ResolveError(ERROR_NO_MATCH, "x"),
     "输入的内容与进程列表不匹配，请从下拉列表中选择，或直接输入 PID"),
    (ResolveError(ERROR_NOT_A_NUMBER, "-1"), "PID 必须是正整数"),
    (ResolveError(ERROR_NOT_FOUND, "999999", 999999), "进程 PID 999999 不存在或无法访问"),
])
def test_invalid_target_shows_field_error_not_infobar(
        presenter, form, services, manager, error, text):
    form.target_invalid.emit(error)
    assert form.errors == [text]
    assert services.notifier.notices == []
    assert manager.created == []


def test_resolve_error_text_rejects_unknown_kind():
    with pytest.raises(ValueError):
        resolve_error_text(ResolveError("??"))


def test_duplicate_pid_asks_for_confirmation(presenter, manager, services, host):
    manager.running_pids = {1001}
    services.dialogs.push("confirm", False, True)

    assert presenter.start_monitoring(_request()) is None
    assert manager.created == []
    assert presenter.start_monitoring(_request()) == "t1"
    assert len(manager.created) == 1

    calls = services.dialogs.calls_of("confirm")
    assert len(calls) == 2
    assert calls[0]["title"] == "已在监控该进程"
    assert "alpha.exe（PID 1001）" in calls[0]["content"]
    assert calls[0]["confirm_text"] == "继续创建" and calls[0]["cancel_text"] == "取消"
    assert calls[0]["parent"] is host.window()


def test_no_confirmation_when_pid_not_running(presenter, services):
    # ScriptedDialogs 未预设 confirm：若被调用会直接断言失败
    presenter.start_monitoring(_request())
    assert services.dialogs.calls_of("confirm") == []


def test_failed_create_does_not_start(presenter, manager):
    manager.create_ok = False
    assert presenter.start_monitoring(_request()) is None
    assert manager.commands == []


# ---------------------------------------------------------------- 卡片与计数


def test_task_added_creates_card_and_collapses_form(presenter, manager, board, form):
    manager.running = 1
    task_id = _add_task(manager, note="快照")
    assert board.cards[task_id].info.note == "快照"
    assert board.counts == (1, 5)
    assert form.collapsed == [False, True]
    assert presenter.task_ids() == [task_id]

    _add_task(manager)
    assert form.collapsed == [False, True]   # 已折叠，不重复折叠


def test_task_added_for_unknown_task_is_ignored(presenter, manager, board):
    manager.task_added.emit("ghost")
    assert board.cards == {}


def test_task_started_updates_counts_without_notice(presenter, manager, board, services):
    manager.running = 1
    manager.task_started.emit("t1")
    assert board.counts == (1, 5)
    # 卡片就在眼前，结果直接可见：不再弹"已启动"
    assert services.notifier.notices == []


def test_data_updated_updates_card_values_and_count(presenter, manager, board):
    task_id = _add_task(manager)
    manager.data_updated.emit(task_id, {MetricType.MEMORY_PRIVATE: 1.0})
    manager.data_updated.emit("unknown", {})
    # 收到数据的时刻经注入时钟传给卡片（"已运行"、"比开始时（N 分钟前）"）
    assert board.cards[task_id].calls == [
        ("update_values", {MetricType.MEMORY_PRIVATE: 1.0}, DEFAULT_FAKE_NOW),
        ("increment_count",)]


def test_task_started_sets_card_start_time_and_tick_refreshes_running_cards(
        presenter, manager, board, services):
    first = _add_task(manager)
    second = _add_task(manager)
    manager.task_started.emit(first)
    assert board.cards[first].started_at == DEFAULT_FAKE_NOW

    manager.task_stopped.emit(second, StopReason.USER)
    services.clock.advance(5)
    presenter.tick()

    assert board.cards[first].ticks == [DEFAULT_FAKE_NOW + timedelta(seconds=5)]
    assert board.cards[second].ticks == []   # 已结束的卡片时间定格，不再刷新


def test_tick_timer_runs_only_while_some_card_is_running(presenter, manager):
    assert not presenter._tick_timer.isActive()
    task_id = _add_task(manager)
    assert presenter._tick_timer.isActive()
    manager.task_stopped.emit(task_id, StopReason.USER)
    assert not presenter._tick_timer.isActive()


def test_data_updated_never_queries_database():
    """data_updated 每周期触发：槽里不能查库（采集次数为卡片内存计数）"""
    import inspect
    source = inspect.getsource(MonitorPresenter.on_data_updated)
    assert "get_sample_count" not in source and "db" not in source


def test_close_removes_card_and_expands_form(presenter, manager, board, form):
    task_id = _add_task(manager)
    manager.task_stopped.emit(task_id, StopReason.USER)

    board.close_requested.emit(task_id)

    assert board.cards == {} and presenter.task_ids() == []
    assert form.collapsed == [False, True, False]


def test_history_request_is_forwarded(presenter, manager, board):
    received = []
    presenter.history_requested.connect(received.append)
    board.history_requested.emit("t1")
    assert received == ["t1"]


# ---------------------------------------------------------------- 停止 / 暂停


def test_stop_requires_confirmation(presenter, manager, board, services):
    task_id = _add_task(manager)

    services.dialogs.push("confirm", False)
    board.stop_requested.emit(task_id)
    assert ("stop", task_id) not in manager.commands
    assert board.cards[task_id].calls == []

    services.dialogs.push("confirm", True)
    board.stop_requested.emit(task_id)
    assert ("stop", task_id) in manager.commands
    # 发出停止请求后卡片等 task_stopped：按钮禁用，防止窗口期内重复点击
    assert board.cards[task_id].calls == [("set_stopping",)]

    call = services.dialogs.last_call("confirm")
    assert call["title"] == "停止监控"
    assert call["confirm_text"] == "停止" and call["cancel_text"] == "取消"
    assert "demo.exe（PID 1001）" in call["content"]
    assert "停止后不能继续记录到这个任务；要继续请用“同配置再测”" in call["content"]


def test_stop_returning_false_is_tolerated(presenter, manager, board, services):
    task_id = _add_task(manager)
    services.dialogs.always("confirm", True)
    manager.stop_result = False
    board.stop_requested.emit(task_id)
    assert board.cards[task_id].calls == []
    assert services.notifier.notices == []


def test_stop_ignored_for_finished_or_unknown_task(presenter, manager, board, services):
    task_id = _add_task(manager)
    manager.task_stopped.emit(task_id, StopReason.USER)
    board.stop_requested.emit(task_id)
    board.stop_requested.emit("unknown")
    assert services.dialogs.calls_of("confirm") == []


def test_pause_resume_only_update_card_on_success(presenter, manager, board):
    task_id = _add_task(manager)
    manager.pause_result = manager.resume_result = False
    board.pause_requested.emit(task_id)
    board.resume_requested.emit(task_id)
    assert board.cards[task_id].calls == []

    manager.pause_result = manager.resume_result = True
    board.pause_requested.emit(task_id)
    board.resume_requested.emit(task_id)
    assert board.cards[task_id].calls == [("set_paused", True), ("set_paused", False)]


# ---------------------------------------------------------------- 打点


def _marker_commands(manager):
    return [c[1:] for c in manager.commands if c[0] == "marker"]


def test_marker_flow_default_label_sequence(presenter, manager, board, services, host):
    task_id = _add_task(manager, pid=1001, name="demo.exe")
    # 第一次用默认"基线"，第二次取消，第三次默认"第 1 轮"，第四次自定义
    services.dialogs.push("ask_marker_label", "基线", None, "第 1 轮", "第 3 轮建模")

    for _ in range(4):
        board.marker_requested.emit(task_id)

    calls = services.dialogs.calls_of("ask_marker_label")
    assert [c["default_label"] for c in calls] == ["基线", "第 1 轮", "第 1 轮", "第 2 轮"]
    assert all(c["parent"] is host.window() for c in calls)
    # 对话框显示进程名与 PID
    assert all((c["process_name"], c["pid"]) == ("demo.exe", 1001) for c in calls)
    assert [(c[1], c[3]) for c in _marker_commands(manager)] == [
        ("基线", "baseline"), ("第 1 轮", "user"), ("第 3 轮建模", "user")]
    assert presenter.next_marker_label(task_id) == "第 3 轮"
    assert services.notifier.levels() == ["success", "success", "success"]
    assert "基线" in services.notifier.messages()[0].content


def test_renamed_first_marker_is_still_baseline(presenter, manager, board, services):
    """默认名称是"基线"时用户改了名称，类型仍是基线；下一个默认名称是"第 1 轮"。"""
    task_id = _add_task(manager)
    services.dialogs.push("ask_marker_label", "预热完成")
    board.marker_requested.emit(task_id)
    assert _marker_commands(manager)[0][1:] == ("预热完成", DEFAULT_FAKE_NOW, "baseline")
    assert presenter.next_marker_label(task_id) == "第 1 轮"


def test_label_exactly_baseline_is_baseline_even_later(presenter, manager, board, services):
    task_id = _add_task(manager)
    services.dialogs.push("ask_marker_label", "基线", "基线")
    board.marker_requested.emit(task_id)
    board.marker_requested.emit(task_id)
    assert [c[3] for c in _marker_commands(manager)] == ["baseline", "baseline"]


def test_next_marker_label_unknown_task_is_none(presenter):
    assert presenter.next_marker_label("ghost") is None


def test_blank_marker_label_is_rejected(presenter, manager, board, services):
    task_id = _add_task(manager)
    services.dialogs.push("ask_marker_label", "   ")
    board.marker_requested.emit(task_id)
    assert not _marker_commands(manager)
    assert services.notifier.last("warning").content == "打点名称不能为空"


def test_marker_failure_is_reported(presenter, manager, board, services):
    task_id = _add_task(manager)
    services.dialogs.push("ask_marker_label", "基线")
    manager.marker_ok = False

    board.marker_requested.emit(task_id)

    assert services.notifier.levels() == ["error"]
    assert services.notifier.last("error").effective_duration == -1
    assert presenter.next_marker_label(task_id) == "基线"


def test_marker_time_is_click_time_not_confirm_time(presenter, manager, board, services):
    """打点时间取点击"打点"的时刻：用户输入名称花了 12 秒，落库时间不能是确认时刻"""
    clicked = datetime(2026, 11, 6, 10, 0, 0)
    services.clock.set(clicked)

    def typing():
        services.clock.advance(timedelta(seconds=12))
        return "开始第 3 轮"

    services.dialogs.push("ask_marker_label", typing)
    task_id = _add_task(manager)

    board.marker_requested.emit(task_id)

    assert _marker_commands(manager)[-1] == (task_id, "开始第 3 轮", clicked, "baseline")
    assert services.dialogs.last_call("ask_marker_label")["timestamp"] == clicked
    assert services.clock.now() == clicked + timedelta(seconds=12)
    assert "10:00:00" in services.notifier.last("success").content


# ---------------------------------------------------------------- 卡片基线


def _emit_samples(manager, task_id, *values):
    for private in values:
        manager.data_updated.emit(task_id, {MetricType.MEMORY_PRIVATE: float(private),
                                            MetricType.NUM_GDI_OBJECTS: private / 10})


def test_baseline_value_is_median_of_five_samples_after_marker(
        presenter, manager, board, services):
    task_id = _add_task(manager)
    card = board.cards[task_id]
    _emit_samples(manager, task_id, 100, 100)          # 打基线之前的样本不计入
    services.dialogs.push("ask_marker_label", "基线")
    board.marker_requested.emit(task_id)

    assert card.baseline == (DEFAULT_FAKE_NOW, {})      # 进入"基线采样中"
    assert card.markers and card.markers[0].kind == "baseline"   # 迷你图刻度

    _emit_samples(manager, task_id, 300, 900, 200, 100)
    assert card.baseline[1] == {}                        # 不足 5 个样本
    assert presenter.baseline_values(task_id) == {}

    _emit_samples(manager, task_id, 400, 5000)           # 第 5 个凑满；第 6 个不再计入
    private, gdi = MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS
    assert card.baseline[1] == {private: 300.0, gdi: 30.0}
    assert presenter.baseline_values(task_id) == {private: 300.0, gdi: 30.0}
    assert [e[0] for e in card.baseline_events] == ["set", "value", "value"]


def test_baseline_seeded_from_stored_samples(presenter, manager, board):
    """分析页把较早的打点设为基线：先用库里已有的样本，settled 时不足 5 个也算"""
    task_id = _add_task(manager)
    old = Marker(task_id, DEFAULT_FAKE_NOW - timedelta(hours=1), "第 2 轮", 7, "baseline")
    manager.markers[task_id] = [old]
    manager.seeds[task_id] = BaselineSamples(
        marker=old, samples={MetricType.MEMORY_PRIVATE: [10.0, 30.0, 20.0]}, settled=True)

    manager.markers_changed.emit(task_id)

    card = board.cards[task_id]
    assert card.baseline == (old.timestamp, {MetricType.MEMORY_PRIVATE: 20.0})
    # settled：之后的实时样本不再混进基线
    _emit_samples(manager, task_id, 999, 999, 999)
    assert presenter.baseline_values(task_id) == {MetricType.MEMORY_PRIVATE: 20.0}


def test_recent_baseline_seed_is_topped_up_by_live_samples(presenter, manager, board):
    task_id = _add_task(manager)
    marker = Marker(task_id, DEFAULT_FAKE_NOW, "基线", 3, "baseline")
    manager.markers[task_id] = [marker]
    manager.seeds[task_id] = BaselineSamples(
        marker=marker, samples={MetricType.MEMORY_PRIVATE: [1.0, 2.0, 3.0]}, settled=False)
    manager.markers_changed.emit(task_id)
    assert board.cards[task_id].baseline[1] == {}

    _emit_samples(manager, task_id, 4, 5)
    assert board.cards[task_id].baseline[1][MetricType.MEMORY_PRIVATE] == 3.0


def test_unchanged_baseline_is_not_reset_by_other_marker_changes(
        presenter, manager, board, services):
    task_id = _add_task(manager)
    services.dialogs.push("ask_marker_label", "基线", "第 1 轮")
    board.marker_requested.emit(task_id)
    _emit_samples(manager, task_id, 1, 2, 3, 4, 5)
    board.marker_requested.emit(task_id)     # 普通打点：基线不变，不重新采样

    card = board.cards[task_id]
    assert [e[0] for e in card.baseline_events].count("set") == 1
    assert card.baseline[1][MetricType.MEMORY_PRIVATE] == 3.0
    assert [m.label for m in card.markers] == ["基线", "第 1 轮"]


def test_deleting_baseline_returns_card_to_since_start(presenter, manager, board, services):
    task_id = _add_task(manager)
    services.dialogs.push("ask_marker_label", "基线")
    board.marker_requested.emit(task_id)

    manager.delete_all_markers(task_id)

    card = board.cards[task_id]
    assert card.baseline is None and card.baseline_events[-1] == ("clear",)
    assert presenter.baseline_values(task_id) is None
    assert presenter.next_marker_label(task_id) == "基线"


def test_markers_changed_for_unknown_task_is_ignored(presenter, manager):
    manager.markers_changed.emit("ghost")


def test_marker_ignored_for_finished_task(presenter, manager, board, services):
    task_id = _add_task(manager)
    manager.task_stopped.emit(task_id, StopReason.USER)
    board.marker_requested.emit(task_id)
    assert services.dialogs.calls_of("ask_marker_label") == []


# ---------------------------------------------------------------- 任务结束


def test_user_stop_finishes_card_removes_task_and_informs(presenter, manager, board,
                                                          services, form):
    task_id = _add_task(manager, pid=1002)

    manager.task_stopped.emit(task_id, StopReason.USER)

    assert board.finished[task_id] == (StopReason.USER, DEFAULT_FAKE_NOW)
    assert ("remove", task_id) in manager.commands
    assert form.forgotten == [1002]
    # 用户自己点的停止：卡片转为"已结束"即是结果，不再弹提示
    assert services.notifier.notices == []


def test_abnormal_stop_is_prominent_and_routed_as_task_event(presenter, manager, services, host):
    """异常结束经 Notifier.route 发出：事件名决定窗口在后台时闪任务栏，渠道由 Notifier 选"""
    task_id = _add_task(manager)

    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)

    last = services.notifier.last()
    assert last.level == "warning" and last.effective_duration == ABNORMAL_STOP_INFOBAR_MS
    assert last.parent is host
    assert last.event == EVENT_TASK_ABNORMAL and last.key == EVENT_TASK_ABNORMAL
    assert "进程已终止" in last.content and "demo.exe（PID 1001）" in last.content


def test_stop_of_unknown_task_still_removes_it(presenter, manager, services):
    """task_stopped 槽内直接调用 remove_task：它不阻塞，无需再延后一轮事件循环"""
    manager.task_stopped.emit("t-unknown", StopReason.PROCESS_EXITED)
    assert manager.commands == [("remove", "t-unknown")]
    assert "t-unknown" in services.notifier.last("warning").content


# ---------------------------------------------------------------- 指标不可用 / 错误 / 上限


def test_unavailable_hint_mentions_admin_only_for_access_denied(presenter, manager, board,
                                                                services):
    task_id = _add_task(manager)
    gdi, uss = MetricType.NUM_GDI_OBJECTS, MetricType.MEMORY_USS

    manager.metrics_unavailable.emit(task_id, {gdi: UnavailableReason.NOT_IN_SESSION})
    content = services.notifier.last("warning").content
    assert "进程不在当前会话" in content and "管理员" not in content

    manager.metrics_unavailable.emit(task_id, {uss: UnavailableReason.ACCESS_DENIED})
    content = services.notifier.last("warning").content
    assert "权限不足" in content and "以管理员身份运行" in content
    assert board.cards[task_id].calls[-1] == (
        "mark_unavailable", {uss: UnavailableReason.ACCESS_DENIED})


def test_unavailable_for_unknown_task_is_ignored(presenter, manager, services):
    manager.metrics_unavailable.emit("ghost", {MetricType.MEMORY_USS: UnavailableReason.ERROR})
    assert services.notifier.notices == []


def test_task_error_infobar_is_persistent(presenter, manager, services):
    manager.error_occurred.emit("t1", "采集数据时发生错误: x")
    assert services.notifier.levels() == ["error"]
    assert services.notifier.last("error").effective_duration == -1


def test_repeated_task_error_is_not_stacked_until_closed(presenter, manager, services):
    for _ in range(3):
        manager.error_occurred.emit("t1", "采集数据时发生错误: x")
    manager.error_occurred.emit("t2", "采集数据时发生错误: x")

    errors = services.notifier.messages("error")
    assert len(errors) == 2
    assert presenter.shown_errors() == {("t1", "采集数据时发生错误: x"),
                                        ("t2", "采集数据时发生错误: x")}

    errors[0].bar.close()
    assert ("t1", "采集数据时发生错误: x") not in presenter.shown_errors()

    manager.error_occurred.emit("t1", "采集数据时发生错误: x")
    assert len(services.notifier.messages("error")) == 3


def test_limit_reached_warns(presenter, manager, services):
    manager.task_limit_reached.emit()
    last = services.notifier.last("warning")
    assert "最多只能同时监控 5 个任务" in last.content


# ---------------------------------------------------------------- 重新监控


def _finished_task(manager, name="demo.exe", metrics=METRICS, note="", interval=2.0):
    task_id = _add_task(manager, name=name, metrics=metrics, note=note, interval=interval)
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    return task_id


def test_restart_with_single_match_creates_task_with_same_settings(
        presenter, manager, board, services):
    task_id = _finished_task(manager, metrics=[MetricType.NUM_HANDLES, MetricType.NUM_THREADS],
                             note="第二轮", interval=5.0)
    services.process_query.processes.append((4321, "demo.exe", 777.25))
    manager.created.clear()

    board.restart_requested.emit(task_id)

    assert manager.created == [(4321, "demo.exe", [MetricType.NUM_HANDLES,
                                                   MetricType.NUM_THREADS], 5.0, "第二轮", 777.25)]


def test_restart_falls_back_to_form_interval(presenter, manager, board, services, form):
    task_id = _finished_task(manager, interval=None)
    services.process_query.processes.append((4321, "demo.exe", 1.0))
    manager.created.clear()
    board.restart_requested.emit(task_id)
    assert manager.created[0][3] == form.interval_value


@pytest.mark.parametrize("matches, keyword", [
    ([], "未找到"), ([(11, 1.0), (22, 2.0)], "找到 2 个")])
def test_restart_with_zero_or_many_matches_fills_search_box(
        presenter, manager, board, services, form, matches, keyword):
    task_id = _finished_task(manager)
    services.notifier.clear()
    services.process_query.processes.extend(
        (pid, "demo.exe", create_time) for pid, create_time in matches)
    manager.created.clear()

    board.restart_requested.emit(task_id)

    assert manager.created == []
    assert form.filled == ["demo.exe"]
    last = services.notifier.last("warning")
    assert keyword in last.title + last.content


def test_restart_matches_process_name_case_insensitively(presenter, manager, board, services):
    """按名查找经 ProcessQuery.find_pids_by_name（不区分大小写），不自行遍历进程表"""
    task_id = _finished_task(manager, name="mprintexp.EXE")
    services.process_query.calls.clear()
    manager.created.clear()

    board.restart_requested.emit(task_id)

    assert [(c[0], c[5]) for c in manager.created] == [(1003, 1003.5)]
    assert ('find_pids_by_name', None) in services.process_query.calls
    assert ('list_processes', None) not in services.process_query.calls


def test_restart_excludes_own_process(presenter, manager, board, services):
    """被测的同名进程退出后，"重新监控"不能因为只剩本程序同名而监控起自己"""
    query = services.process_query
    own = query.self_pid()
    task_id = _finished_task(manager, name="PYTHON.EXE")
    manager.created.clear()

    query.processes = [(own, "python.exe", 1.0), (99999, "python.exe", 2.0),
                       (12345, "other.exe", 3.0)]
    board.restart_requested.emit(task_id)
    assert [c[0] for c in manager.created] == [99999]

    manager.created.clear()
    query.processes = [(own, "python.exe", 1.0)]
    board.restart_requested.emit(task_id)
    assert manager.created == []
    assert "未找到" in services.notifier.last("warning").title


def test_restart_unknown_task_does_nothing(presenter, board, services):
    board.restart_requested.emit("ghost")
    assert services.notifier.notices == []


# ---------------------------------------------------------------- 免弹窗打点（托盘 / 全局快捷键）


def _feedback(presenter):
    received = []
    presenter.quick_marker_feedback.connect(lambda *args: received.append(args))
    return received


def test_quick_marker_without_tasks_reports_none(presenter, manager):
    received = _feedback(presenter)
    assert presenter.quick_marker() == []
    assert received == [(QUICK_MARKER_NONE, "没有可打点的任务", "请先在监控页开始监控")]
    assert manager.commands == []


def test_quick_marker_single_task_uses_default_label_and_now(presenter, manager, services):
    task_id = _add_task(manager, pid=1001)
    received = _feedback(presenter)

    markers = presenter.quick_marker()

    assert [(m.label, m.kind) for m in markers] == [(BASELINE_LABEL, "baseline")]
    assert ("marker", task_id, BASELINE_LABEL, DEFAULT_FAKE_NOW, "baseline") in manager.commands
    clock = DEFAULT_FAKE_NOW.strftime("%H:%M:%S")
    assert received == [(QUICK_MARKER_OK, f"已打点：基线 {clock}", "demo.exe · PID 1001")]
    # 不弹任何对话框
    assert presenter.services.dialogs.methods_called() == []

    presenter.quick_marker()
    assert received[-1][1] == f"已打点：第 1 轮 {clock}"


def test_quick_marker_all_tasks_share_one_moment(presenter, manager):
    first = _add_task(manager, pid=1001)
    second = _add_task(manager, pid=1002, name="beta.exe")
    received = _feedback(presenter)

    markers = presenter.quick_marker()

    assert {m.task_id for m in markers} == {first, second}
    assert {m.timestamp for m in markers} == {DEFAULT_FAKE_NOW}
    assert received[-1][0] == QUICK_MARKER_OK
    assert received[-1][1].startswith("已打点：2 个任务")


def test_quick_marker_one_task_from_tray_submenu(presenter, manager):
    _add_task(manager, pid=1001)
    second = _add_task(manager, pid=1002, name="beta.exe")
    markers = presenter.quick_marker(second)
    assert [m.task_id for m in markers] == [second]


def test_quick_marker_skips_finished_tasks(presenter, manager):
    task_id = _add_task(manager)
    manager.task_stopped.emit(task_id, StopReason.USER)
    received = _feedback(presenter)
    assert presenter.quick_marker() == []
    assert received[-1][0] == QUICK_MARKER_NONE
    assert presenter.marker_targets() == []


def test_quick_marker_failure_reported(presenter, manager):
    _add_task(manager)
    manager.marker_ok = False
    received = _feedback(presenter)
    assert presenter.quick_marker() == []
    assert received[-1][0] == QUICK_MARKER_FAILED


def test_marker_targets_label_process_and_pid(presenter, manager):
    task_id = _add_task(manager, pid=1001)
    assert presenter.marker_targets() == [(task_id, "demo.exe · PID 1001")]


def test_clear_finished_removes_only_finished(presenter, manager, board, form):
    done = _add_task(manager, pid=1001)
    alive = _add_task(manager, pid=1002)
    manager.task_stopped.emit(done, StopReason.USER)

    board.clear_finished_requested.emit()

    assert list(board.cards) == [alive]
    assert presenter.task_ids() == [alive]


# ---------------------------------------------------------------- 自动化方案


TIMED_PLAN = AutomationPlan(timed_marker_seconds=10)


class FakePlanStore:
    """TaskRepo 的 plan_json 部分：任务行出现（rows）前 save_plan 返回 False。"""

    def __init__(self):
        self.rows = set()
        self.saved: Dict[str, str] = {}
        self.calls = 0
        self.error = False

    def save_plan(self, task_id, plan_json):
        self.calls += 1
        if self.error:
            raise StorageError("磁盘已满")
        if task_id not in self.rows:
            return False
        self.saved[task_id] = plan_json
        return True


class FakeDb:
    def __init__(self):
        self.tasks = FakePlanStore()
        self.records: Dict[str, MonitorTask] = {}

    def get_task(self, task_id):
        return self.records.get(task_id)


@pytest.fixture
def plan_db():
    return FakeDb()


@pytest.fixture
def auto_presenter(manager, services, form, board, host, plan_db):
    return MonitorPresenter(manager, services, form, board, host, db=plan_db)


def test_start_with_plan_saves_plan_attaches_and_shows_summary(
        auto_presenter, manager, board, plan_db):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))

    assert manager.commands[-1] == ("start", task_id)
    assert auto_presenter.plan_of(task_id) == TIMED_PLAN
    assert auto_presenter.auto_marker.plan_of(task_id) == TIMED_PLAN
    assert board.cards[task_id].tooltip == plan_summary(TIMED_PLAN) == "自动打点 每 10 秒"
    # 任务行要等采集线程启动才写入：第一次写不进去，下一个采集周期补存，之后不再写
    assert plan_db.tasks.saved == {}
    plan_db.tasks.rows.add(task_id)
    manager.data_updated.emit(task_id, {METRICS[0]: 1.0})
    assert AutomationPlan.from_json(plan_db.tasks.saved[task_id]) == TIMED_PLAN
    calls = plan_db.tasks.calls
    manager.data_updated.emit(task_id, {METRICS[0]: 2.0})
    assert plan_db.tasks.calls == calls


def test_plan_saved_on_stop_when_no_cycle_ran(auto_presenter, manager, plan_db):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))
    plan_db.tasks.rows.add(task_id)
    manager.task_stopped.emit(task_id, StopReason.PROCESS_NOT_FOUND)
    assert task_id in plan_db.tasks.saved


def test_plan_storage_error_is_retried(auto_presenter, manager, plan_db):
    plan_db.tasks.error = True
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))
    plan_db.tasks.error = False
    plan_db.tasks.rows.add(task_id)
    manager.data_updated.emit(task_id, {METRICS[0]: 1.0})
    assert task_id in plan_db.tasks.saved


def test_start_without_plan_writes_nothing(auto_presenter, manager, plan_db, board):
    task_id = auto_presenter.on_start_requested(_request())
    plan_db.tasks.rows.add(task_id)
    manager.data_updated.emit(task_id, {METRICS[0]: 1.0})
    assert plan_db.tasks.calls == 0
    assert not auto_presenter.auto_marker.is_attached(task_id)
    assert board.cards[task_id].tooltip == ''


def test_empty_plan_is_treated_as_no_plan(auto_presenter, plan_db):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=AutomationPlan()))
    assert auto_presenter.plan_of(task_id) is None and plan_db.tasks.calls == 0


def test_invalid_plan_is_rejected_before_creating(auto_presenter, manager, services):
    bad = AutomationPlan(timed_marker_seconds=1)
    assert auto_presenter.on_start_requested(replace(_request(interval=5.0), plan=bad)) is None
    assert manager.created == []
    notice = services.notifier.last("warning")
    assert notice.title == "自动化设置有误" and "不能小于采集间隔" in notice.content


def test_presenter_tick_drives_timed_markers(auto_presenter, manager, services):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))
    services.clock.advance(10)
    auto_presenter.tick()
    assert [m.label for m in manager.list_markers(task_id)] == [BASELINE_LABEL]
    assert _marker_commands(manager)[-1][2] == services.clock.now()


def test_resume_restarts_timer_from_resume_moment(auto_presenter, manager, board, services):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))
    services.clock.advance(4)
    manager.paused_ids.add(task_id)
    board.pause_requested.emit(task_id)
    assert auto_presenter.auto_marker.next_due(task_id) is None
    services.clock.advance(60)
    manager.paused_ids.discard(task_id)
    board.resume_requested.emit(task_id)
    assert auto_presenter.auto_marker.next_due(task_id) == (
        services.clock.now() + timedelta(seconds=10))


def test_auto_stop_marks_card_stopping_and_informs(auto_presenter, manager, board, services):
    plan = AutomationPlan(stop_after_seconds=60)
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=plan))
    services.clock.advance(60)
    auto_presenter.tick()

    assert ("stop", task_id) in manager.commands
    assert ("set_stopping",) in board.cards[task_id].calls
    notice = services.notifier.last("info")
    assert notice.title == "已按方案自动停止"
    assert notice.content == "alpha.exe（PID 1001）：已运行满 1 分钟"
    # 不能再打点；随后的停止按"用户停止"处理，不弹异常结束提示
    assert task_id not in auto_presenter.active_task_ids()
    services.notifier.clear()
    manager.task_stopped.emit(task_id, StopReason.USER)
    assert services.notifier.messages("warning") == []


def test_restart_reuses_plan(auto_presenter, manager, services):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    services.process_query.processes[:] = [(4321, "alpha.exe", 9.0)]

    new_id = auto_presenter.on_restart(task_id)

    assert new_id is not None and new_id != task_id
    assert auto_presenter.plan_of(new_id) == TIMED_PLAN
    assert auto_presenter.auto_marker.is_attached(new_id)


def test_restart_without_unique_match_fills_plan_into_form(auto_presenter, manager, services,
                                                           form):
    task_id = auto_presenter.on_start_requested(replace(_request(), plan=TIMED_PLAN))
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    services.process_query.processes[:] = []
    assert auto_presenter.on_restart(task_id) is None
    assert form.filled == ["alpha.exe"] and form.automation_plans == [TIMED_PLAN]


def test_restart_without_plan_leaves_form_plan_alone(auto_presenter, manager, services, form):
    task_id = auto_presenter.on_start_requested(_request())
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    services.process_query.processes[:] = []
    auto_presenter.on_restart(task_id)
    assert form.automation_plans == []


def test_wait_request_plan_is_applied_when_process_appears(auto_presenter, manager, services):
    auto_presenter.on_wait_requested(WaitRequest(
        name="late.exe", metrics=METRICS, interval=1.0, plan=TIMED_PLAN))
    services.process_query.processes.append((5555, "late.exe", 5.0))
    auto_presenter.poll_waits()
    assert len(manager.created) == 1
    assert auto_presenter.plan_of("t1") == TIMED_PLAN


def test_browse_log_and_export_dir_go_through_dialogs(auto_presenter, services, form):
    services.dialogs.push('ask_open_file', "D:\\logs\\app.log", None)
    services.dialogs.push('ask_directory', "D:\\out")
    form.browse_log_requested.emit()
    assert form.log_path_value == "D:\\logs\\app.log"
    form.browse_log_requested.emit()            # 取消：不改
    assert form.log_path_value == "D:\\logs\\app.log"
    assert services.dialogs.last_call('ask_open_file')['start_dir'] == "D:\\logs"
    form.browse_export_dir_requested.emit()
    assert form.export_dir_value == "D:\\out"


# ---------- 停止后自动导出 ----------


class FakeExportWorker(QObject):
    """ReportWorker 替身：start() 同步发出结果（或失败）与 finished。"""

    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()
    created: List["FakeExportWorker"] = []

    def __init__(self, db, job, fail=None):
        super().__init__()
        self.db, self.job, self.fail = db, job, fail
        FakeExportWorker.created.append(self)

    def cancel(self):
        pass

    def isFinished(self):
        return False

    def start(self):
        if self.fail:
            self.failed.emit(self.fail)
        else:
            self.result.emit(ReportResult((self.job.csv_path,), 3, 3))
        self.finished.emit()


def _record(task_id, start=DEFAULT_FAKE_NOW):
    return MonitorTask(task_id=task_id, pid=1001, process_name="alpha.exe",
                       metric_types=list(METRICS), interval=1.0, start_time=start,
                       end_time=start + timedelta(minutes=5), status='stopped',
                       sample_count=3)


def _export_presenter(manager, services, form, board, host, plan_db, fail=None):
    FakeExportWorker.created = []
    return MonitorPresenter(
        manager, services, form, board, host, db=plan_db,
        export_worker_factory=lambda db, job: FakeExportWorker(db, job, fail))


def test_task_end_triggers_csv_export(manager, services, form, board, host, plan_db, tmp_path):
    presenter = _export_presenter(manager, services, form, board, host, plan_db)
    out = tmp_path / "导出"
    plan = AutomationPlan(export_on_stop=True, export_dir=str(out))
    task_id = presenter.on_start_requested(replace(_request(), plan=plan))
    plan_db.records[task_id] = _record(task_id)

    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)

    [worker] = FakeExportWorker.created
    job = worker.job
    assert job.csv_path == str(out / "alpha_1001_20260101_120000.csv")
    assert job.image_path is None and job.summary_path is None
    assert any("任务结束后自动导出" in line for line in job.metadata)
    assert out.is_dir()
    assert manager.exports == 0                      # begin/end 成对
    assert services.notifier.last("success").title == "已自动导出"


def test_export_does_not_overwrite_existing_file(manager, services, form, board, host,
                                                 plan_db, tmp_path):
    presenter = _export_presenter(manager, services, form, board, host, plan_db)
    (tmp_path / "alpha_1001_20260101_120000.csv").write_text("旧", encoding="utf-8")
    plan_db.records["x"] = _record("x")
    assert presenter.on_export_requested("x", str(tmp_path))
    assert FakeExportWorker.created[0].job.csv_path == str(
        tmp_path / "alpha_1001_20260101_120000_2.csv")


def test_export_refused_during_maintenance(manager, services, form, board, host, plan_db,
                                           tmp_path):
    presenter = _export_presenter(manager, services, form, board, host, plan_db)
    plan_db.records["x"] = _record("x")
    manager.maintenance = True
    assert presenter.on_export_requested("x", str(tmp_path)) is False
    assert FakeExportWorker.created == []
    assert "正在清理数据库" in services.notifier.last("error").content


def test_export_failure_is_reported(manager, services, form, board, host, plan_db, tmp_path):
    presenter = _export_presenter(manager, services, form, board, host, plan_db,
                                  fail="导出失败：磁盘已满")
    plan_db.records["x"] = _record("x")
    presenter.on_export_requested("x", str(tmp_path))
    notice = services.notifier.last("error")
    assert notice.title == "自动导出失败" and "磁盘已满" in notice.content
    assert manager.exports == 0


def test_export_without_task_record_is_reported(manager, services, form, board, host, plan_db,
                                                tmp_path):
    presenter = _export_presenter(manager, services, form, board, host, plan_db)
    assert presenter.on_export_requested("missing", str(tmp_path)) is False
    assert services.notifier.last("error").title == "自动导出失败"


class _InlineReportWorker(ReportWorker):
    """真实 ReportWorker 的导出逻辑，但在调用线程里同步执行：本用例只验证"写出的文件"，
    不依赖跨线程信号投递（e2e 冒烟真退出调用过 QApplication.quit() 之后，同一进程里
    工作线程发往界面线程的排队信号不再送达，真线程版本会在全量运行时超时）。"""

    def start(self):
        self.run()
        self.finished.emit()


def test_export_writes_real_csv(db, manager, services, form, board, host, tmp_path):
    """真实 ReportWorker 导出逻辑 + 真实库：文件确实写出，导出线程登记到 services.threads"""
    from data.models import DataPoint
    db.save_task(_record("real"))
    db.save_data_points([DataPoint("real", DEFAULT_FAKE_NOW + timedelta(seconds=i), 1.0 + i,
                                   METRICS[0]) for i in range(3)])
    registered = []
    services.threads.register = lambda worker, **kw: registered.append(kw['name'])
    presenter = MonitorPresenter(manager, services, form, board, host, db=db,
                                 export_worker_factory=_InlineReportWorker)

    assert presenter.on_export_requested("real", str(tmp_path))
    assert registered == ["自动导出"]
    assert manager.exports == 0
    assert [n.title for n in services.notifier.messages()] == ["已自动导出"], (
        services.notifier.messages())
    text = (tmp_path / "alpha_1001_20260101_120000.csv").read_text(encoding="utf-8-sig")
    assert "进程: alpha.exe (PID 1001)" in text


# ---------------------------------------------------------------- 暂停 / 恢复同步


def test_external_pause_and_resume_update_card(presenter, manager, board):
    """命令行、托盘等别处直接调 manager 暂停 / 恢复：卡片跟着变。"""
    task_id = _add_task(manager)

    manager.pause_task(task_id)
    manager.resume_task(task_id)

    assert board.cards[task_id].calls == [("set_paused", True), ("set_paused", False)]


def test_external_pause_for_unknown_task_is_ignored(presenter, manager, board):
    manager.task_paused.emit("unknown")
    assert board.cards == {}


# ---------------------------------------------------------------- 启动被测程序

LAUNCH_SPEC = LaunchSpec(exe=r"D:\app\demo.exe", args=("--mode", "soak"), cwd=r"D:\app")


def _launch_request(close_on_stop=False, note=""):
    return replace(_request(pid=0, name="demo.exe", create_time=None, note=note),
                   launch=LAUNCH_SPEC, close_on_stop=close_on_stop)


def _pump_until(condition, timeout=5.0):
    from PyQt5.QtWidgets import QApplication
    from tests.helpers import poll_until
    return poll_until(lambda: (QApplication.processEvents(), condition())[1], timeout=timeout)


def test_launch_success_creates_task_with_launched_identity(presenter, manager, services):
    task_id = presenter.start_monitoring(_launch_request())

    assert services.launcher.launched == [LAUNCH_SPEC]
    # 用启动结果（PID 5001、创建时间 5001.5）建任务，而不是表单里的占位 0
    assert manager.created == [(5001, "demo.exe", list(METRICS), 1.0, "", 5001.5)]
    assert manager.commands == [("start", task_id)]
    assert presenter.is_launched(task_id)


def test_form_launch_request_goes_through_start_requested(presenter, manager, services, form):
    form.start_requested.emit(_launch_request())

    assert services.launcher.launched == [LAUNCH_SPEC]
    assert manager.created[0][0] == 5001


def test_launch_failure_creates_no_task_and_shows_error_near_form(
        presenter, manager, services, form):
    services.launcher.fail("找不到程序：D:\\app\\demo.exe")

    assert presenter.start_monitoring(_launch_request()) is None

    assert manager.created == []
    assert form.launch_errors == ["找不到程序：D:\\app\\demo.exe"]
    # 就近显示，不另弹提示条
    assert services.notifier.notices == []


def test_launch_skips_duplicate_pid_confirmation(presenter, manager, services):
    manager.running_pids.add(5001)
    presenter.start_monitoring(_launch_request())
    assert services.dialogs.calls_of("confirm") == []


def test_launch_refused_during_maintenance_launches_nothing(presenter, manager, services):
    manager.maintenance = True

    assert presenter.start_monitoring(_launch_request()) is None

    assert services.launcher.launched == []
    assert services.notifier.last().level == "warning"


def test_launch_refused_when_task_limit_reached(presenter, manager, services):
    manager.can_add_task = lambda: False

    assert presenter.start_monitoring(_launch_request()) is None

    assert services.launcher.launched == []
    assert manager.created == []


def test_launched_but_create_failed_warns_program_still_running(presenter, manager, services):
    manager.create_ok = False

    assert presenter.start_monitoring(_launch_request()) is None

    assert services.notifier.last().level == "warning"
    assert "5001" in services.notifier.last().content


def test_close_on_stop_closes_program_after_user_stop(presenter, manager, services):
    task_id = presenter.start_monitoring(_launch_request(close_on_stop=True))

    manager.task_stopped.emit(task_id, StopReason.USER)

    assert _pump_until(lambda: services.launcher.terminated)
    assert services.launcher.terminated == [(5001, 5001.5)]


def test_close_on_stop_leaves_program_alone_when_it_exits_by_itself(
        presenter, manager, services):
    task_id = presenter.start_monitoring(_launch_request(close_on_stop=True))

    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)

    assert not _pump_until(lambda: services.launcher.terminated, timeout=0.3)


def test_without_close_on_stop_user_stop_leaves_program_running(presenter, manager, services):
    task_id = presenter.start_monitoring(_launch_request(close_on_stop=False))

    manager.task_stopped.emit(task_id, StopReason.USER)

    assert not _pump_until(lambda: services.launcher.terminated, timeout=0.3)


def test_restart_of_launched_task_relaunches_program(presenter, manager, board, services):
    task_id = presenter.start_monitoring(_launch_request(close_on_stop=True, note="第一轮"))
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    manager.created.clear()

    new_id = presenter.on_restart(task_id)

    assert services.launcher.launched == [LAUNCH_SPEC, LAUNCH_SPEC]
    assert manager.created == [(5002, "demo.exe", list(METRICS), 1.0, "第一轮", 5002.5)]
    assert presenter.is_launched(new_id)
    # 新任务沿用"停止时关闭"
    manager.task_stopped.emit(new_id, StopReason.USER)
    assert _pump_until(lambda: services.launcher.terminated)
    assert services.launcher.terminated == [(5002, 5002.5)]


def test_browse_exe_and_cwd_go_through_dialogs(presenter, services, form):
    services.dialogs.push('ask_open_file', r"D:\app\demo.exe")
    services.dialogs.push('ask_directory', r"D:\work", None)

    form.browse_exe_requested.emit()
    form.browse_cwd_requested.emit()
    form.browse_cwd_requested.emit()            # 取消：不改

    assert form.launch_exe_value == r"D:\app\demo.exe"
    assert form.launch_cwd_value == r"D:\work"
    # 工作目录对话框从程序所在目录开始
    assert services.dialogs.calls_of('ask_directory')[0]['start_dir'] == r"D:\app"


# ---------------------------------------------------------------- 阶段快照


class FakeSnapshotWriter(QObject):
    written = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.writes: List[tuple] = []
        self.forgotten: List[str] = []

    def write(self, task_id, directory, index):
        self.writes.append((task_id, directory, index))
        return True

    def forget(self, task_id):
        self.forgotten.append(task_id)


@pytest.fixture
def snap_writer():
    return FakeSnapshotWriter()


@pytest.fixture
def snap_presenter(manager, services, form, board, host, snap_writer):
    return MonitorPresenter(manager, services, form, board, host, snapshot_writer=snap_writer)


def test_stage_snapshot_due_is_written_by_writer(snap_presenter, snap_writer):
    snap_presenter.auto_marker.stage_snapshot_due.emit("t1", r"D:\snap", 3)
    assert snap_writer.writes == [("t1", r"D:\snap", 3)]


def test_stage_snapshot_failure_warns_with_task_name(snap_presenter, manager, snap_writer,
                                                     services):
    task_id = _add_task(manager, name="demo.exe")

    snap_writer.failed.emit(task_id, "磁盘已满")

    notice = services.notifier.last()
    assert notice.level == "warning"
    assert "demo.exe" in notice.content and "磁盘已满" in notice.content


def test_stage_snapshot_written_is_reported(snap_presenter, manager, snap_writer, services):
    task_id = _add_task(manager, name="demo.exe")

    snap_writer.written.emit(task_id, [r"D:\snap\stage_01.png", r"D:\snap\stage_01.csv"])

    assert services.notifier.last().level == "info"
    assert "stage_01.png" in services.notifier.last().content


def test_closing_cards_forgets_snapshot_state(snap_presenter, manager, board, snap_writer):
    first = _add_task(manager)
    second = _add_task(manager)
    manager.task_stopped.emit(first, StopReason.USER)
    manager.task_stopped.emit(second, StopReason.USER)

    board.close_requested.emit(first)
    board.clear_finished_requested.emit()

    assert snap_writer.forgotten == [first, second]


def test_browse_snapshot_dir_goes_through_dialogs(snap_presenter, services, form):
    services.dialogs.push('ask_directory', r"D:\snap")
    form.snapshot_dir_value = r"D:\old"

    form.browse_snapshot_dir_requested.emit()

    assert form.snapshot_dir_value == r"D:\snap"
    assert services.dialogs.last_call('ask_directory')['start_dir'] == r"D:\old"


def test_default_snapshot_writer_renders_with_report_sheet(presenter):
    from ui.widgets.analysis.report_sheet import render_report_for
    assert presenter.snapshot_writer._renderer is render_report_for
