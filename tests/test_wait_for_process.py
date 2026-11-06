"""
等待进程启动后自动开始：表单发出 WaitRequest（只能按进程名）、Presenter 放占位卡、
每秒按名查询、进程出现即按请求参数建任务（同名多个取最新启动的）、取消等待、
折叠表单时取消勾选。进程表用 FakeProcessQuery，定时器由测试直接调用 poll_waits 驱动。
"""
import pytest
from PyQt5.QtWidgets import QWidget

from tests.doubles import FakePreferences, FakeProcessQuery
from tests.factories import make_services
from tests.test_monitor_presenter import FakeBoard, FakeForm, FakeManager
from ui.presenters.monitor_presenter import WAIT_POLL_INTERVAL_MS, MonitorPresenter
from ui.widgets.monitor.new_task_form import (
    START_TEXT, WAIT_START_TEXT, NewTaskForm, WaitRequest,
)
from ui.widgets.monitor.process_picker import ERROR_WAIT_NEEDS_NAME
from ui.widgets.monitor.task_board import TaskBoard
from ui.widgets.monitor.waiting_card import waiting_title
from utils.metrics import MetricType

METRICS = (MetricType.MEMORY_PRIVATE, MetricType.NUM_HANDLES)


@pytest.fixture
def query():
    return FakeProcessQuery([(1001, "alpha.exe", 1001.5)])


@pytest.fixture
def host(qapp):
    widget = QWidget()
    yield widget
    widget.close()


@pytest.fixture
def env(query, host):
    manager, form, board = FakeManager(), FakeForm(), FakeBoard()
    presenter = MonitorPresenter(
        manager, make_services(process_query=query), form, board, host)
    return presenter, manager, form, board


def _wait(name="notepad.exe", note="过夜", interval=2.0, metrics=METRICS):
    return WaitRequest(name=name, metrics=tuple(metrics), interval=interval, note=note)


# ---------------------------------------------------------------- Presenter


def test_wait_shows_placeholder_and_polls_every_second(env):
    presenter, manager, form, board = env
    wait_id = presenter.on_wait_requested(_wait())

    assert wait_id in presenter.wait_ids()
    assert board.waiting[wait_id] == ("notepad.exe", "过夜")
    assert manager.created == []
    assert presenter._wait_timer.isActive()
    assert presenter._wait_timer.interval() == WAIT_POLL_INTERVAL_MS == 1000
    # 占位卡也算"有内容"：表单折叠成一行
    assert form.collapsed[-1] is True


def test_process_appearing_starts_task_with_request_settings(env, query):
    presenter, manager, form, board = env
    wait_id = presenter.on_wait_requested(_wait())
    presenter.poll_waits()
    assert manager.created == []

    query.processes.append((2001, "notepad.exe", 2001.5))
    presenter.poll_waits()

    assert manager.created == [(2001, "notepad.exe", list(METRICS), 2.0, "过夜", 2001.5)]
    assert ("start", "t1") in manager.commands
    assert wait_id not in board.waiting and presenter.wait_ids() == []
    assert not presenter._wait_timer.isActive()


def test_already_running_process_starts_immediately(env, query):
    presenter, manager, form, board = env
    query.processes.append((2001, "notepad.exe", 2001.5))
    presenter.on_wait_requested(_wait())
    assert [c[0] for c in manager.created] == [2001]
    assert board.waiting == {} and not presenter._wait_timer.isActive()


def test_several_instances_pick_newest(env, query):
    presenter, manager, form, board = env
    presenter.on_wait_requested(_wait())
    query.processes.extend([(2001, "notepad.exe", 50.0), (2002, "Notepad.exe", 90.0)])
    presenter.poll_waits()
    assert [c[0] for c in manager.created] == [2002]


def test_name_without_exe_suffix_also_matches(env, query):
    presenter, manager, form, board = env
    presenter.on_wait_requested(_wait(name="notepad"))
    query.processes.append((2001, "notepad.exe", 2001.5))
    presenter.poll_waits()
    assert [(c[0], c[1]) for c in manager.created] == [(2001, "notepad.exe")]


def test_cancel_removes_placeholder_and_stops_polling(env, query):
    presenter, manager, form, board = env
    wait_id = presenter.on_wait_requested(_wait())
    board.wait_cancel_requested.emit(wait_id)

    assert board.waiting == {} and presenter.wait_ids() == []
    assert not presenter._wait_timer.isActive()
    assert form.collapsed[-1] is False      # 面板空了，表单展开
    query.processes.append((2001, "notepad.exe", 2001.5))
    presenter.poll_waits()
    assert manager.created == []


def test_wait_without_metrics_is_rejected(env):
    presenter, manager, form, board = env
    assert presenter.on_wait_requested(_wait(metrics=())) is None
    assert board.waiting == {}
    assert presenter.services.notifier.last("warning").content == "请先选择要监控的指标"


def test_form_signal_reaches_presenter(env):
    presenter, manager, form, board = env
    form.wait_requested.emit(_wait())
    assert len(board.waiting) == 1


# ---------------------------------------------------------------- 表单与面板


@pytest.fixture
def real_form(qapp, query):
    widget = NewTaskForm(query, FakePreferences(default_interval=3))
    widget.picker.refresh()
    widget.waits, widget.requests, widget.invalid = [], [], []
    widget.wait_requested.connect(widget.waits.append)
    widget.start_requested.connect(widget.requests.append)
    widget.target_invalid.connect(widget.invalid.append)
    yield widget
    widget.close()


def test_wait_option_changes_button_and_emits_wait_request(real_form):
    real_form.set_metrics_summary([MetricType.NUM_HANDLES])
    assert real_form.start_button.text() == START_TEXT
    real_form.set_wait_enabled(True)
    assert real_form.start_button.text() == WAIT_START_TEXT

    real_form.picker.set_text("notepad.exe")
    real_form.note_input.setText(" 夜间 ")
    real_form.submit()

    assert real_form.requests == []
    assert real_form.waits == [WaitRequest(
        name="notepad.exe", metrics=(MetricType.NUM_HANDLES,), interval=3.0, note="夜间")]


@pytest.mark.parametrize("text", ["", "1234"])
def test_wait_needs_process_name(real_form, text):
    real_form.set_wait_enabled(True)
    real_form.picker.set_text(text)
    real_form.submit()
    assert real_form.waits == []
    assert real_form.invalid[-1].kind == ERROR_WAIT_NEEDS_NAME


def test_wait_option_hides_no_match_hint(real_form):
    real_form.picker.set_text("notepad.exe")
    assert real_form.picker.no_match_visible()
    real_form.set_wait_enabled(True)
    assert not real_form.picker.no_match_visible()


def test_collapsing_form_unchecks_wait(real_form):
    real_form.set_wait_enabled(True)
    real_form.set_collapsed(True)
    assert not real_form.wait_enabled()
    assert real_form.start_button.text() == START_TEXT


def test_board_waiting_card_and_cancel(qapp):
    board = TaskBoard()
    board.resize(900, 600)
    board.show()
    try:
        cancelled = []
        board.wait_cancel_requested.connect(cancelled.append)
        board.set_counts(0, 5)
        card = board.add_waiting("wait-1", "notepad.exe", "过夜")

        assert card.title_label.text() == waiting_title("notepad.exe") == "等待 notepad.exe 启动…"
        assert board.waiting_ids() == ["wait-1"]
        assert not board.empty_state.isVisible()
        assert board.summary() == "运行中 0/5 · 等待启动 1"

        card.cancel_button.click()
        assert cancelled == ["wait-1"]
        board.remove_waiting("wait-1")
        assert board.waiting_card("wait-1") is None
        assert board.empty_state.isVisible()
    finally:
        board.close()
