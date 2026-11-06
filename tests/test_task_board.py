"""
任务面板：建卡/移除、配额标题、空状态、已结束分组、卡片按钮信号转发；
以及卡片本身的停止图标（不再用撤销图标 CANCEL）。
"""
from datetime import datetime

import pytest
from qfluentwidgets import FluentIcon

from core.contracts import StopReason, TaskInfo, TaskStatus
from ui.components.task_card import StopIcon, TaskCard
from ui.widgets.monitor.task_board import TaskBoard
from utils.metrics import MetricType

END = datetime(2026, 1, 1, 12, 30, 0)


def _info(task_id, pid=1001, name="demo.exe", note=""):
    return TaskInfo(
        task_id=task_id, pid=pid, process_name=name,
        metric_types=(MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS),
        interval=2.0, note=note, status=TaskStatus.PENDING, start_time=None,
        end_time=None, create_time=None, is_paused=False, is_stopping=False)


@pytest.fixture
def board(qapp):
    widget = TaskBoard()
    widget.resize(900, 600)
    widget.show()
    yield widget
    widget.close()


def test_empty_board_shows_empty_state(board):
    board.set_counts(0, 5)
    assert board.count_label.text() == "监控任务 · 0/5 运行中"
    assert board.empty_state.isVisible()
    assert not board.finished_label.isVisible()


def test_create_card_from_snapshot(board):
    card = board.create_card(_info("t1", note="过夜测试"))
    assert board.card("t1") is card
    assert (card.process_name, card.pid, card.interval, card.note) == (
        "demo.exe", 1001, 2.0, "过夜测试")
    # 身份行带上快照里的 PID 与采集间隔
    assert card.meta_label.text() == "· PID 1001 · 每 2 秒"
    assert card.metric_types == [MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS]
    assert not board.empty_state.isVisible()
    assert list(board.cards()) == ["t1"]


def test_finished_card_moves_to_finished_group(board):
    board.create_card(_info("t1"))
    board.create_card(_info("t2"))
    board.set_counts(1, 5)

    board.finish_card("t1", StopReason.USER, END)

    assert board.card("t1").is_finished
    assert board.finished_count() == 1
    assert board.count_label.text() == "监控任务 · 1/5 运行中 · 1 个已结束"
    assert board.finished_label.isVisible()
    # 卡片位于"已结束"分组标签之下，运行中的卡片在它之上
    assert board.card("t1").mapTo(board, board.card("t1").rect().topLeft()).y() \
        > board.finished_label.mapTo(board, board.finished_label.rect().topLeft()).y() \
        > board.card("t2").mapTo(board, board.card("t2").rect().topLeft()).y()


def test_remove_card_restores_empty_state(board):
    board.create_card(_info("t1"))
    board.finish_card("t1", StopReason.USER, END)
    board.remove_card("t1")
    assert board.card("t1") is None and board.cards() == {}
    assert board.empty_state.isVisible() and not board.finished_label.isVisible()
    board.remove_card("t1")   # 重复移除不报错


@pytest.mark.parametrize("button, signal", [
    ("marker_button", "marker_requested"),
    ("analysis_button", "history_requested"),
    ("pause_button", "pause_requested"),
    ("stop_button", "stop_requested"),
])
def test_running_card_buttons_are_forwarded(board, button, signal):
    card = board.create_card(_info("t1"))
    received = []
    getattr(board, signal).connect(received.append)
    getattr(card, button).click()
    assert received == ["t1"]


def test_paused_card_forwards_resume(board):
    card = board.create_card(_info("t1"))
    card.set_paused(True)
    received = []
    board.resume_requested.connect(received.append)
    card.pause_button.click()
    assert received == ["t1"]


@pytest.mark.parametrize("button, signal", [
    ("history_button", "history_requested"),
    ("restart_button", "restart_requested"),
    ("close_button", "close_requested"),
])
def test_finished_card_buttons_are_forwarded(board, button, signal):
    card = board.create_card(_info("t1"))
    board.finish_card("t1", StopReason.PROCESS_EXITED, END)
    received = []
    getattr(board, signal).connect(received.append)
    getattr(card, button).click()
    assert received == ["t1"]


def test_add_existing_finished_card_goes_to_finished_group(board):
    card = TaskCard("t9", "x.exe", 1, [MetricType.NUM_THREADS])
    card.set_finished(StopReason.USER, END)
    board.add_card(card)
    assert board.finished_count() == 1 and board.card("t9") is card


def test_stop_button_uses_stop_icon_not_cancel(qapp):
    card = TaskCard("t1", "demo.exe", 1, [MetricType.NUM_THREADS])
    icon = card.stop_button._icon
    assert isinstance(icon, StopIcon)
    assert icon is not FluentIcon.CANCEL and icon is not FluentIcon.CLOSE
    assert not icon.icon().isNull()
    assert not card.stop_button.icon().pixmap(16, 16).isNull()
    card.close()
