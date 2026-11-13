"""
被测进程退出后续接与异常退出的界面：

- 文案：等待重启（第 N/M 次）、已续接提示、异常退出说明（访问冲突 0xC0000005）、自动化方案里的续接选项、
  统计依据里的"仅统计第 N 次启动后的数据"；
- 任务卡片：等待重启的状态胶囊（警示色）与暂停按钮、续接后换 PID、已结束行写异常退出原因与退出码；
- 任务面板：按快照建出等待重启的卡片、结束时把退出码交给卡片；
- Presenter：task_waiting_restart / task_restarted 驱动卡片与提示，CRASHED 结束带退出码，替身管理器没有这两个
  信号时自动跳过；
- 图表与迷你图：异常退出 / 重启 / 续采三种系统打点画成与用户打点不同的点划线，颜色取自主题令牌，
  悬停提示写明事件，不能在右键菜单里编辑；
- 一个全局保险：任何用例建出的主窗口都不会留下真实的更新检查。
"""
import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyqtgraph as pg
import pytest
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import Theme, qconfig

from core.analysis.trend import Judgement, LinearFit, Verdict
from core.automation.plan import AutomationPlan
from core.contracts import StopReason, TaskInfo, TaskStatus
from core.history_query import HistorySnapshot, build_request
from data.models import Marker
from tests.helpers import dispose_widget
from tests.test_monitor_presenter import (  # noqa: F401 夹具与替身
    METRICS, PROCESSES, FakeBoard, FakeCard, FakeForm, FakeManager, board, form, host,
    services)
from ui import theme_tokens as tokens
from ui.components.sparkline import EVENT_TICK_KINDS, TICK_KINDS, SparklineWidget
from ui.components.task_card import (
    STATUS_STYLES, STATUS_WAITING_RESTART, TaskCard)
from ui.formatting import (
    basis_text, crash_exit_text, plan_summary, restarted_hint_text, stop_detail_text,
    waiting_restart_text)
from ui.presenters.monitor_presenter import MonitorPresenter
from ui.widgets.analysis.marker_menu import SYSTEM_KINDS
from ui.widgets.history.chart_view import (
    EVENT_MARKER_KINDS, ChartView, event_marker_tooltip, marker_badges)
from ui.widgets.monitor.new_task_form import TaskRequest
from ui.widgets.monitor.task_board import TaskBoard
from utils.metrics import MetricType

T0 = datetime(2026, 11, 7, 10, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
ACCESS_VIOLATION = 0xC0000005


# ---------------------------------------------------------------- 文案


def test_waiting_restart_text_uses_next_restart_number_and_limit():
    assert waiting_restart_text(0, 3) == "等待重启（第 1/3 次）"
    assert waiting_restart_text(1, 3) == "等待重启（第 2/3 次）"
    assert waiting_restart_text(2, 20) == "等待重启（第 3/20 次）"
    assert waiting_restart_text(1, 0) == "等待重启"           # 上限未知时不写次数


def test_restarted_hint_names_launch_number_and_pid():
    assert restarted_hint_text(1, 4321) == "已续接：第 2 次启动 PID 4321"
    assert restarted_hint_text(2, 77) == "已续接：第 3 次启动 PID 77"
    assert restarted_hint_text(1, None) == "已续接：第 2 次启动"     # 同名进程没有单个 PID


@pytest.mark.parametrize("code, expected", [
    (ACCESS_VIOLATION, "异常退出：访问冲突 0xC0000005"),
    (-1073741819, "异常退出：访问冲突 0xC0000005"),               # 有符号写法是同一个值
    (0xC00000FD, "异常退出：栈溢出 0xC00000FD"),
    (0xC0001234, "异常退出：未知异常 0xC0001234"),
    (None, "被测进程异常退出"),
    (3, "异常退出：退出码 3"),
])
def test_crash_exit_text(code, expected):
    assert crash_exit_text(code) == expected


def test_stop_detail_text_only_adds_exit_code_for_crashes():
    assert stop_detail_text(StopReason.CRASHED, ACCESS_VIOLATION) == \
        "异常退出：访问冲突 0xC0000005"
    assert stop_detail_text(StopReason.CRASHED) == "被测进程异常退出"
    assert stop_detail_text(StopReason.PROCESS_EXITED, ACCESS_VIOLATION) == "进程已终止"
    assert stop_detail_text(StopReason.USER, 0) == "用户停止"


def test_plan_summary_mentions_restart_option():
    assert plan_summary(AutomationPlan(resume_on_restart=True, restart_limit=3)) == \
        "退出后续接（最多 3 次）"
    assert plan_summary(AutomationPlan(
        timed_marker_seconds=600, export_on_stop=True, resume_on_restart=True,
        restart_limit=5)) == "自动打点 每 10 分钟 · 停止后自动导出 · 退出后续接（最多 5 次）"
    assert plan_summary(AutomationPlan(restart_limit=5)) == ""     # 没勾选不提


def _snapshot(segment_number=1, segment_count=1):
    judgement = Judgement(Verdict.STABLE, LinearFit(0.0, 0.0, 0.0, 0.0), 'constant', 0.0)
    stats = {'count': 100, 'min': 1.0, 'max': 9.0, 'avg': 5.0, 'first': 1.0, 'first_ts': T0,
             'last': 9.0, 'last_ts': T0 + timedelta(minutes=20), 'slope_per_hour': None,
             'span_seconds': 1200.0}
    return HistorySnapshot(
        request=build_request("t", MEM, 'all'), stats=stats, interval_seconds=1.0,
        judgement=judgement, points=[object()],
        segment_number=segment_number, segment_count=segment_count)


def test_basis_text_appends_segment_note_only_after_restarts():
    assert basis_text(_snapshot()) == "20分钟 · 100 点"
    assert basis_text(_snapshot(2, 2)) == "20分钟 · 100 点 · 仅统计第 2 次启动后的数据"
    assert basis_text(_snapshot(3, 4)) == "20分钟 · 100 点 · 仅统计第 3 次启动后的数据"


# ---------------------------------------------------------------- 任务卡片


@pytest.fixture
def card(qapp):
    widget = TaskCard("t1", "demo.exe", 1234, [MEM], interval=1.0)
    widget.resize(900, 300)
    widget.show()
    yield widget
    dispose_widget(widget)


def _colors(label):
    return label.lightColor.name(), label.darkColor.name()


def test_waiting_restart_pill_text_color_and_pause_button(card):
    card.set_waiting_restart(True, 1, 3)
    assert card.status_label.text() == "● 等待重启（第 2/3 次）"
    assert card.status == STATUS_WAITING_RESTART and card.waiting_restart
    # 警示色（与"已暂停"同一令牌，文字不同）
    assert _colors(card.status_label) == ("#9d5d00", "#fce100")
    assert card.pause_button.isEnabled()
    assert "重启后的采集状态" in card.pause_button.toolTip()
    assert "等它重新启动" in card.status_label.toolTip()
    assert card.marker_button.isEnabled() and card.stop_button.isEnabled()   # 其余操作照常


def test_waiting_restart_tooltip_names_previous_exit(card):
    card.set_waiting_restart(True, 0, 3, ACCESS_VIOLATION)
    assert "上一个进程：异常退出：访问冲突 0xC0000005" in card.status_label.toolTip()


def test_leaving_waiting_restores_running_or_paused_state(card):
    card.set_waiting_restart(True, 1, 3)
    card.set_waiting_restart(False, 2, 3)
    assert card.status_label.text() == "● 运行中" and not card.waiting_restart
    assert card.pause_button.isEnabled() and card.pause_button.toolTip() != ""
    assert card.status_label.toolTip() == ""
    assert card.restart_count == 2

    card.set_paused(True)
    card.set_waiting_restart(True, 2, 3)
    card.set_waiting_restart(False, 2, 3)
    assert card.status_label.text() == "● 已暂停"          # 回到进入前的状态


def test_leaving_waiting_applies_the_real_paused_state(card):
    """卡片不知道任务进入等待前就是暂停的（命令行暂停，或卡片是等待期间才建出来的）：
    离开等待时按任务的真实暂停状态刷新暂停按钮与状态胶囊"""
    assert not card.is_paused
    card.set_waiting_restart(True, 1, 3)
    card.set_waiting_restart(False, 2, 3, paused=True)
    assert card.is_paused and card.status_label.text() == "● 已暂停"
    assert card.pause_button.text() == "恢复" and card.pause_button.isEnabled()
    assert "恢复采集" in card.pause_button.toolTip()

    # 反过来：卡片以为暂停着，任务实际已回到运行
    card.set_waiting_restart(True, 2, 3)
    card.set_waiting_restart(False, 3, 3, paused=False)
    assert not card.is_paused and card.status_label.text() == "● 运行中"
    assert card.pause_button.text() == "暂停"

    # 没给 paused：沿用卡片自己记的（旧行为）
    card.set_paused(True)
    card.set_waiting_restart(True, 3, 3)
    card.set_waiting_restart(False, 3, 3)
    assert card.is_paused and card.status_label.text() == "● 已暂停"


def test_paused_state_is_not_applied_to_stopping_card(card):
    card.set_waiting_restart(True, 0, 3)
    card.set_stopping()
    card.set_waiting_restart(False, 0, 3, paused=True)
    assert card.status_label.text() == "● 正在停止…" and not card.is_paused


def test_waiting_restart_does_not_override_stopping_or_finished(card):
    card.set_waiting_restart(True, 0, 3)
    card.set_stopping()
    assert card.status_label.text() == "● 正在停止…"
    card.set_waiting_restart(False, 0, 3)
    assert card.status_label.text() == "● 正在停止…" and not card.pause_button.isEnabled()

    other = TaskCard("t2", "demo.exe", 1, [MEM])
    other.set_finished(StopReason.PROCESS_EXITED, T0)
    other.set_waiting_restart(True, 0, 3)
    assert other.status_label.text() == "● 异常结束" and not other.waiting_restart
    dispose_widget(other)


def test_waiting_status_style_is_registered_with_caution_tokens():
    text, light, dark = STATUS_STYLES[STATUS_WAITING_RESTART]
    assert text == "等待重启"
    assert (light.name(), dark.name()) == tuple(
        c.name() for c in tokens.colors(tokens.CAUTION))


def test_set_pid_updates_identity_line(card):
    card.set_started_at(T0)
    card.set_pid(4321)
    assert card.pid == 4321
    assert card.meta_label.text().startswith("· PID 4321")


def test_finished_crash_row_shows_exit_code_text(card):
    card.set_started_at(T0)
    card.increment_count()
    card.set_finished(StopReason.CRASHED, T0 + timedelta(minutes=5), ACCESS_VIOLATION)
    assert card.status_label.text() == "● 异常结束"
    text = card.finished_detail_label.text()
    assert text.startswith("· 异常退出：访问冲突 0xC0000005 · 10:05:00")
    assert text.endswith("已记录 1 次")


def test_finished_crash_without_exit_code_keeps_generic_text(card):
    card.set_finished(StopReason.CRASHED, T0)
    assert card.finished_detail_label.text().startswith("· 被测进程异常退出")


def test_exit_code_only_matters_for_crashes(card):
    card.set_finished(StopReason.PROCESS_EXITED, T0, ACCESS_VIOLATION)
    assert card.finished_detail_label.text().startswith("· 进程已终止")


def test_sparkline_draws_system_event_ticks(card):
    for i in range(5):
        card.update_values({MEM: float(i)}, T0 + timedelta(seconds=i))
    card.set_markers([
        Marker("t1", T0 + timedelta(seconds=1), "异常退出 0xC0000005（访问冲突）", 1, "crash"),
        Marker("t1", T0 + timedelta(seconds=2), "第 2 次启动 PID 9", 2, "restart"),
        Marker("t1", T0 + timedelta(seconds=3), "工具重启后继续监控", 3, "recover"),
        Marker("t1", T0 + timedelta(seconds=4), "暂停", 4, "pause"),         # 仍不画
    ])
    assert [kind for _f, kind in card.sparkline.tick_positions()] == [
        "crash", "restart", "recover"]
    card.sparkline.grab()


def test_sparkline_tick_pens_tell_events_from_user_markers(qapp):
    from ui.components.sparkline import tick_pen
    for kind in EVENT_TICK_KINDS:
        pen = tick_pen(kind)
        assert pen.style() == Qt.DashDotLine
        assert pen.color().name() == tokens.pick(tokens.EVENT_MARKER_COLORS[kind], False).lower()
    assert tick_pen('user').style() == Qt.DashLine
    assert tick_pen('baseline').style() == Qt.SolidLine


def test_sparkline_event_kinds_follow_tokens(qapp):
    assert set(EVENT_TICK_KINDS) == set(tokens.EVENT_MARKER_COLORS) == {
        "crash", "restart", "recover"}
    assert set(EVENT_TICK_KINDS) < set(TICK_KINDS) and "pause" not in TICK_KINDS


# ---------------------------------------------------------------- 任务面板


def _info(**kwargs):
    base = dict(task_id="t1", pid=1234, process_name="demo.exe", metric_types=(MEM,),
                interval=1.0, note="", status=TaskStatus.RUNNING, start_time=None,
                end_time=None, create_time=None, is_paused=False, is_stopping=False)
    base.update(kwargs)
    return TaskInfo(**base)


def test_board_builds_waiting_card_from_snapshot(qapp):
    board_widget = TaskBoard()
    waiting = board_widget.create_card(_info(
        waiting_restart=True, restart_count=1, restart_limit=3, exit_code=ACCESS_VIOLATION))
    assert waiting.status_label.text() == "● 等待重启（第 2/3 次）"
    plain = board_widget.create_card(_info(task_id="t2"))
    assert plain.status_label.text() == "● 运行中"
    board_widget.close()


def test_board_passes_exit_code_to_finished_card(qapp):
    board_widget = TaskBoard()
    card = board_widget.create_card(_info())
    board_widget.finish_card("t1", StopReason.CRASHED, T0, ACCESS_VIOLATION)
    assert card.finished_detail_label.text().startswith("· 异常退出：访问冲突 0xC0000005")
    board_widget.create_card(_info(task_id="t2"))
    board_widget.finish_card("t2", StopReason.USER, T0)
    assert board_widget.card("t2").finished_detail_label.text().startswith("· 用户停止")
    board_widget.close()


# ---------------------------------------------------------------- Presenter


class RestartManager(FakeManager):
    """在 FakeManager 上补续接信号；任务快照里的续接次数、上限、退出码由用例设置。"""

    task_waiting_restart = pyqtSignal(str, bool)
    task_restarted = pyqtSignal(str, int)

    def create_task(self, pid, name, metrics, interval=None, note='', create_time=None, *,
                    target_kind='pid'):
        self.created.append((pid, name, list(metrics), interval, note, create_time))
        task_id = f"t{len(self.created)}"
        self.infos[task_id] = TaskInfo(
            task_id=task_id, pid=pid, process_name=name, metric_types=tuple(metrics),
            interval=interval, note=note, status=TaskStatus.PENDING, start_time=None,
            end_time=None, create_time=create_time, is_paused=False, is_stopping=False,
            target_kind=target_kind)
        self.task_added.emit(task_id)
        return task_id


class RestartCard(FakeCard):
    def __init__(self, info):
        super().__init__(info)
        self.waiting_calls = []
        self.paused_args = []
        self.pids = []

    def set_waiting_restart(self, waiting, restart_count=0, restart_limit=0, exit_code=None,
                            paused=None):
        self.waiting_calls.append((waiting, restart_count, restart_limit, exit_code))
        self.paused_args.append(paused)

    def set_pid(self, pid):
        self.pids.append(pid)


class RestartBoard(FakeBoard):
    def create_card(self, info):
        self.cards[info.task_id] = RestartCard(info)
        return self.cards[info.task_id]


@pytest.fixture
def restart_manager():
    return RestartManager()


@pytest.fixture
def restart_presenter(restart_manager, services, host):
    board_double = RestartBoard()
    presenter = MonitorPresenter(restart_manager, services, FakeForm(), board_double, host)
    return presenter, board_double


def _start(presenter, pid=1002, name="beta.exe"):
    request = TaskRequest(pid=pid, name=name, create_time=1002.5, metrics=(MEM,),
                          interval=1.0)
    return presenter.start_monitoring(request)


def _update_info(manager, task_id, **changes):
    manager.infos[task_id] = dataclasses.replace(manager.infos[task_id], **changes)


def test_waiting_signal_drives_card_with_snapshot_numbers(restart_presenter, restart_manager):
    presenter, board_double = restart_presenter
    task_id = _start(presenter)
    _update_info(restart_manager, task_id, waiting_restart=True, restart_count=1,
                 restart_limit=3, exit_code=ACCESS_VIOLATION)
    restart_manager.task_waiting_restart.emit(task_id, True)
    assert board_double.card(task_id).waiting_calls == [(True, 1, 3, ACCESS_VIOLATION)]
    _update_info(restart_manager, task_id, waiting_restart=False, restart_count=2)
    restart_manager.task_waiting_restart.emit(task_id, False)
    assert board_double.card(task_id).waiting_calls[-1][:3] == (False, 2, 3)


def test_leaving_waiting_hands_the_task_paused_state_to_the_card(
        restart_presenter, restart_manager):
    presenter, board_double = restart_presenter
    task_id = _start(presenter)
    card = board_double.card(task_id)
    _update_info(restart_manager, task_id, waiting_restart=True, restart_count=1, restart_limit=3)
    restart_manager.task_waiting_restart.emit(task_id, True)
    assert card.paused_args == [False]                # 等待期间也携带续接后的暂停意图
    # 续接前处于暂停：任务回到 PAUSED，快照里 is_paused 仍为真
    _update_info(restart_manager, task_id, waiting_restart=False, is_paused=True)
    restart_manager.task_waiting_restart.emit(task_id, False)
    assert card.paused_args[-1] is True
    restart_manager.task_restarted.emit(task_id, 2)
    assert card.paused_args[-1] is True


def test_waiting_end_refreshes_a_real_card_that_never_saw_the_pause(
        qapp, restart_manager, services, host):
    """端到端：真实卡片不知道暂停（暂停来自命令行，且发生在卡片建出来之前）；续接后任务回到
    暂停，卡片的暂停按钮与状态胶囊随 waiting_restart_changed(False) 刷新"""
    # 与真实监控页一致，让刷新定时器与其访问的控件由同一对象管理生命周期。
    scope = QWidget(host)

    class RealCardBoard(RestartBoard):
        def create_card(self, info):
            self.cards[info.task_id] = TaskCard(
                info.task_id, info.process_name, info.pid, list(info.metric_types), parent=scope)
            return self.cards[info.task_id]

    board_double = RealCardBoard()
    presenter = MonitorPresenter(restart_manager, services, FakeForm(), board_double, host,
                                 parent=scope)
    task_id = _start(presenter)
    card = board_double.card(task_id)
    try:
        _update_info(restart_manager, task_id, waiting_restart=True, restart_count=1,
                     restart_limit=3)
        restart_manager.task_waiting_restart.emit(task_id, True)
        assert card.status_label.text() == "● 等待重启（第 2/3 次）"
        _update_info(restart_manager, task_id, waiting_restart=False, is_paused=True)
        restart_manager.task_waiting_restart.emit(task_id, False)
        assert card.status_label.text() == "● 已暂停"
        assert card.pause_button.text() == "恢复" and card.pause_button.isEnabled()
    finally:
        dispose_widget(scope)
    from PyQt5 import sip
    assert sip.isdeleted(presenter) and sip.isdeleted(card)


def test_waiting_signal_for_unknown_or_finished_task_is_ignored(
        restart_presenter, restart_manager):
    presenter, board_double = restart_presenter
    restart_manager.task_waiting_restart.emit("t-unknown", True)           # 不抛异常
    task_id = _start(presenter)
    restart_manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    restart_manager.task_waiting_restart.emit(task_id, True)
    assert board_double.card(task_id).waiting_calls == []


def test_restarted_signal_updates_pid_and_hints(restart_presenter, restart_manager, services):
    presenter, board_double = restart_presenter
    task_id = _start(presenter)
    _update_info(restart_manager, task_id, pid=4321, restart_count=1, restart_limit=3)
    restart_manager.task_restarted.emit(task_id, 1)
    card = board_double.card(task_id)
    assert card.pids == [4321]
    assert presenter._tasks[task_id].pid == 4321
    assert card.waiting_calls[-1] == (False, 1, 3, None)
    notice = services.notifier.last("info")
    assert notice.title == "已续接" and notice.content == \
        "beta.exe：已续接：第 2 次启动 PID 4321"
    assert 0 < notice.duration <= 5000                                    # 短暂提示，不常驻


def test_restarted_signal_keeps_name_group_without_pid(
        restart_presenter, restart_manager, services):
    presenter, board_double = restart_presenter
    task_id = presenter.start_monitoring(TaskRequest(
        pid=0, name="chrome.exe", create_time=None, metrics=(MEM,), interval=1.0,
        target_kind='name'))
    restart_manager.task_restarted.emit(task_id, 1)
    assert board_double.card(task_id).pids == []                          # 同名进程没有单个 PID
    assert services.notifier.last("info").content == "chrome.exe：已续接：第 2 次启动"


def test_crash_stop_passes_exit_code_to_card_and_notice(
        restart_presenter, restart_manager, services):
    presenter, board_double = restart_presenter
    task_id = _start(presenter)
    _update_info(restart_manager, task_id, exit_code=ACCESS_VIOLATION)
    restart_manager.task_stopped.emit(task_id, StopReason.CRASHED)
    assert board_double.exit_codes[task_id] == ACCESS_VIOLATION
    notice = services.notifier.last("warning")
    assert notice.title == "监控任务异常结束"
    assert "异常退出：访问冲突 0xC0000005" in notice.content


def test_non_crash_stops_do_not_carry_exit_code(restart_presenter, restart_manager, services):
    presenter, board_double = restart_presenter
    first, second = _start(presenter), _start(presenter, pid=1003, name="gamma.exe")
    _update_info(restart_manager, first, exit_code=1)
    restart_manager.task_stopped.emit(first, StopReason.PROCESS_EXITED)
    restart_manager.task_stopped.emit(second, StopReason.USER)
    assert board_double.exit_codes == {first: None, second: None}
    assert services.notifier.last("warning").content.endswith("进程已终止")


def test_manager_without_restart_signals_is_skipped(services, host):
    """替身管理器（FakeManager）没有这两个信号：构造不抛异常，其余流程照常。"""
    plain = FakeManager()
    assert not hasattr(plain, 'task_waiting_restart')
    presenter = MonitorPresenter(plain, services, FakeForm(), RestartBoard(), host)
    task_id = _start(presenter)
    plain.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    assert presenter.board.exit_codes[task_id] is None


# ---------------------------------------------------------------- 图表打点


def _marker(seconds, label, kind='user'):
    return SimpleNamespace(timestamp=T0 + timedelta(seconds=seconds), label=label, kind=kind)


def _points(count=10):
    from data.models import DataPoint
    return [DataPoint("t", T0 + timedelta(seconds=i), 1024.0 * (100 + i), MEM)
            for i in range(count)]


EVENTS = [_marker(2, "异常退出 0xC0000005（访问冲突）", 'crash'),
          _marker(4, "第 2 次启动 PID 4321", 'restart'),
          _marker(6, "工具重启后继续监控", 'recover')]


@pytest.fixture
def chart(qapp):
    widget = ChartView()
    yield widget
    widget.close()


def _event_lines(chart_widget):
    return {kind: line for kind, _badge, line in chart_widget.marker_items()
            if kind in EVENT_MARKER_KINDS}


def test_event_markers_drawn_as_dash_dot_lines_in_token_colors(chart):
    chart.set_series(_points(), MEM, markers=[_marker(1, "第1轮")] + EVENTS)
    lines = _event_lines(chart)
    assert set(lines) == {"crash", "restart", "recover"}
    for kind, line in lines.items():
        pen = line.pen
        assert pen.style() == Qt.DashDotLine and pen.width() == 2
        assert pen.color().name() == tokens.pick(tokens.EVENT_MARKER_COLORS[kind], False).lower()
    # 与用户打点（虚线）区分：线型不同
    user_line = next(line for kind, _b, line in chart.marker_items() if kind == 'user')
    assert user_line.pen.style() == Qt.DashLine
    # 不占用编号徽标，也不参与徽标错开
    assert marker_badges([_marker(1, "第1轮")] + EVENTS) == ["①", None, None, None]
    assert len(chart.badge_layout()) == 1


def test_event_markers_have_short_labels_and_tooltips(chart):
    chart.set_series(_points(), MEM, markers=EVENTS)
    lines = _event_lines(chart)
    assert [lines[k].label.toPlainText() for k in ("crash", "restart", "recover")] == [
        "异常退出", "重启", "续采"]
    assert lines["crash"].toolTip() == "异常退出 0xC0000005（访问冲突） · 11-07 10:00:02"
    assert lines["restart"].toolTip() == \
        "被测进程重启并续接：第 2 次启动 PID 4321 · 11-07 10:00:04"
    assert lines["recover"].toolTip() == \
        "工具重启后继续监控：工具重启后继续监控 · 11-07 10:00:06"
    assert lines["restart"].label.toolTip() == lines["restart"].toolTip()


def test_event_marker_tooltip_does_not_repeat_event_name():
    assert event_marker_tooltip("crash", "异常退出 0xC0000005", "t") == "异常退出 0xC0000005 · t"
    assert event_marker_tooltip("restart", "第 2 次启动", "t") == \
        "被测进程重启并续接：第 2 次启动 · t"
    assert event_marker_tooltip("recover", "", "t") == "工具重启后继续监控 · t"


def test_compact_chart_draws_event_lines_without_labels(qapp):
    compact = ChartView(compact=True)
    compact.set_series(_points(), MEM, markers=EVENTS)
    lines = _event_lines(compact)
    assert set(lines) == {"crash", "restart", "recover"}
    assert all(getattr(line, 'label', None) is None for line in lines.values())
    compact.close()


def test_event_markers_follow_theme_and_survive_redraw(chart):
    chart.set_series(_points(), MEM, markers=EVENTS)
    light = {k: line.pen.color().name() for k, line in _event_lines(chart).items()}
    qconfig._theme = Theme.DARK
    qconfig.themeChanged.emit(Theme.DARK)
    dark = {k: line.pen.color().name() for k, line in _event_lines(chart).items()}
    qconfig._theme = Theme.LIGHT
    qconfig.themeChanged.emit(Theme.LIGHT)
    assert set(dark) == {"crash", "restart", "recover"}
    assert all(light[k] != dark[k] for k in light)
    for kind in dark:
        assert dark[kind] == tokens.pick(tokens.EVENT_MARKER_COLORS[kind], True).lower()


def test_event_markers_are_not_editable_from_the_context_menu(chart):
    chart.set_series(_points(), MEM, markers=[_marker(1, "第1轮")] + EVENTS)
    chart.chart_widget.getPlotItem().vb.setXRange(
        T0.timestamp(), (T0 + timedelta(seconds=9)).timestamp(), padding=0)
    huge = 10_000.0                       # 容差大到横向距离不是问题
    near = chart.marker_near((T0 + timedelta(seconds=2)).timestamp(), huge)
    assert near is not None and near.kind == 'user'         # 只会命中用户打点，不会命中异常退出
    assert set(EVENT_MARKER_KINDS) <= set(SYSTEM_KINDS)


def _laid_out(chart, seconds):
    from PyQt5.QtWidgets import QApplication
    chart.resize(900, 360)
    chart.show()
    QApplication.processEvents()
    chart.chart_widget.getPlotItem().vb.setXRange(
        T0.timestamp(), (T0 + timedelta(seconds=seconds)).timestamp(), padding=0)
    QApplication.processEvents()


def test_close_event_labels_are_staggered_and_far_ones_are_not(chart):
    """崩溃后几十秒就重启：两个短标签在屏幕上挤在一起时错开高度，不叠成一团。"""
    from ui.widgets.history.chart_view import EVENT_LABEL_STEP, EVENT_LABEL_TOP
    chart.set_series(_points(), MEM, markers=EVENTS)
    _laid_out(chart, 9)                       # 2、4、6 秒的事件相距约 200 像素
    assert chart.event_label_layout() == [EVENT_LABEL_TOP] * 3
    _laid_out(chart, 600)                     # 缩小后三个事件挤在几个像素内
    layout = chart.event_label_layout()
    assert layout == [EVENT_LABEL_TOP, EVENT_LABEL_TOP - EVENT_LABEL_STEP,
                      EVENT_LABEL_TOP - 2 * EVENT_LABEL_STEP]
    assert len(set(layout)) == 3 and min(layout) > 0


def test_event_label_stagger_leaves_numbered_badges_alone(chart):
    chart.set_series(_points(), MEM, markers=[_marker(1, "第1轮"), _marker(1.5, "第2轮")] + EVENTS)
    _laid_out(chart, 600)
    badges = chart.badge_layout()
    assert len(badges) == 2 and badges[0] != badges[1]      # 编号徽标按自己的规则错开
    assert len(chart.event_label_layout()) == 3


def test_hover_context_ignores_event_markers(chart):
    chart.set_series(_points(), MEM, markers=EVENTS)
    assert "打点" not in chart.hover_text(5)


# ---------------------------------------------------------------- 继续监控对话框


def _candidates(count, note=""):
    from core.session_recovery import RecoveryCandidate
    now = datetime(2026, 11, 7, 10, 30, 0)
    return [RecoveryCandidate(f"t{i}", f"app{i}.exe", 1000 + i, 'pid', now - timedelta(hours=3),
                              now - timedelta(minutes=4), note) for i in range(count)]


def test_recover_dialog_list_is_fluent_themed_and_never_scrolls_sideways(qapp):
    from PyQt5.QtWidgets import QWidget
    from qfluentwidgets import ListWidget
    from ui.dialogs.recover_dialog import RecoverDialog, candidate_text
    host = QWidget()
    host.resize(1280, 820)
    long_note = "长时间运行后内存持续上涨的现场排查任务" * 3
    dialog = RecoverDialog(_candidates(3, long_note), host)
    try:
        listing = dialog.list_widget
        assert isinstance(listing, ListWidget)                      # 深色主题下不是白底黑字
        assert listing.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        assert listing.textElideMode() == Qt.ElideRight
        for row in range(listing.count()):                          # 被省略的文字完整放在悬停里
            item = listing.item(row)
            assert item.toolTip() == item.text() == candidate_text(_candidates(3, long_note)[row])
        assert dialog.widget.minimumWidth() >= 520
    finally:
        dialog.deleteLater()
        host.deleteLater()


@pytest.mark.parametrize("count", [1, 3, 30])
def test_recover_dialog_list_height_is_clamped(qapp, count):
    from PyQt5.QtWidgets import QWidget
    from ui.dialogs import recover_dialog
    from ui.dialogs.recover_dialog import RecoverDialog
    host = QWidget()
    host.resize(1280, 820)
    dialog = RecoverDialog(_candidates(count), host)
    try:
        height = dialog.list_widget.maximumHeight()
        assert recover_dialog.LIST_MIN_HEIGHT <= height <= recover_dialog.LIST_MAX_HEIGHT
        assert dialog.list_widget.minimumHeight() == height
        if count == 30:
            assert height == recover_dialog.LIST_MAX_HEIGHT        # 任务多了在列表内滚动
    finally:
        dialog.deleteLater()
        host.deleteLater()


# ---------------------------------------------------------------- 全局保险


def test_every_main_window_gets_a_noop_update_check(db):
    """全局夹具在 MainWindow 构造末尾把 about_group.check_update 换成空函数：
    窗口里登记的"3 秒后检查更新"定时器不会留下真实的检查（曾在随后某个用例里抛 RuntimeError）。"""
    from tests.factories import make_main_window, make_services
    from core.monitor_manager import MonitorManager
    from ui.main_window import MainWindow

    services = make_services()
    window = make_main_window(db, MonitorManager(db=db), services)
    try:
        assert 'check_update' in vars(window.about_group)
        window.about_group.check_update(silent=True)
        assert "检查更新" not in [e.name for e in services.threads.entries()]   # 没有启动检查线程
    finally:
        dispose_widget(window)

    direct = make_services()
    other = MainWindow(db, MonitorManager(db=db), direct)
    try:
        assert 'check_update' in vars(other.about_group)
    finally:
        dispose_widget(other)
