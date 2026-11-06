"""
进程选择器：刷新保留选择/手输文本、resolve 的结果与错误类型、forget_if、字段错误。
进程表用 FakeProcessQuery，不触达本机进程。
"""
import pytest

from tests.doubles import FakeProcessQuery
from ui.widgets.monitor.process_picker import (
    ERROR_EMPTY, ERROR_NO_MATCH, ERROR_NOT_A_NUMBER, ERROR_NOT_FOUND,
    ProcessPicker, ResolveError, Target,
)

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "beta.exe", 1002.5),
             (1003, "MPrintExp.exe", 1003.5)]


@pytest.fixture
def query():
    return FakeProcessQuery(PROCESSES)


@pytest.fixture
def picker(qapp, query):
    widget = ProcessPicker(query)
    widget.refresh()
    yield widget
    widget.close()


def _select(picker, pid):
    assert picker.combo.select_data(next(t for t in picker.targets() if t.pid == pid))


def test_refresh_lists_processes_without_selecting(picker):
    assert [t.pid for t in picker.targets()] == [1001, 1002, 1003]
    assert picker.text() == "" and picker.combo.current_data() is None


def test_refresh_keeps_selection_and_picks_up_changes(picker, query):
    _select(picker, 1002)
    query.processes.append((1004, "gamma.exe", 1004.5))
    picker.refresh()
    assert picker.resolve() == Target(1002, "beta.exe", 1002.5)
    assert Target(1004, "gamma.exe", 1004.5) in picker.targets()


def test_refresh_keeps_typed_text(picker):
    picker.set_text("be")
    picker.refresh()
    assert picker.text() == "be"


def test_refresh_drops_selection_of_vanished_process(picker, query):
    _select(picker, 1002)
    query.kill(1002)
    picker.refresh()
    assert picker.text() == "" and picker.combo.current_data() is None


def test_resolve_selected_item_carries_create_time(picker):
    _select(picker, 1003)
    assert picker.resolve() == Target(1003, "MPrintExp.exe", 1003.5)


def test_resolve_typed_pid_queries_name_without_create_time(picker, query):
    """手输 PID（列表可能已过期）没有可比对的创建时间，跳过 PID 复用比对"""
    picker.set_text(" 1002 ")
    assert picker.resolve() == Target(1002, "beta.exe", None)
    assert ("name_of", 1002) in query.calls


@pytest.mark.parametrize("text, expected", [
    ("", ResolveError(ERROR_EMPTY)),
    ("   ", ResolveError(ERROR_EMPTY)),
    ("不存在的进程", ResolveError(ERROR_NO_MATCH, "不存在的进程")),
    ("12a", ResolveError(ERROR_NO_MATCH, "12a")),
    ("-5", ResolveError(ERROR_NOT_A_NUMBER, "-5")),
    ("12.5", ResolveError(ERROR_NOT_A_NUMBER, "12.5")),
    ("999999", ResolveError(ERROR_NOT_FOUND, "999999", 999999)),
])
def test_resolve_errors(picker, text, expected):
    picker.set_text(text)
    assert picker.resolve() == expected


def test_forget_if_clears_selection_of_exited_process(picker, query):
    _select(picker, 1002)
    query.kill(1002)

    assert picker.forget_if(1002) is True

    assert picker.text() == "" and picker.combo.current_data() is None
    assert all(t.pid != 1002 for t in picker.targets())


def test_forget_if_clears_typed_pid_of_exited_process(picker, query):
    picker.set_text("1001")
    query.kill(1001)
    assert picker.forget_if(1001) is True
    assert picker.text() == ""


def test_forget_if_keeps_other_or_alive_selection(picker, query):
    _select(picker, 1001)
    query.alive = set()
    assert picker.forget_if(1002) is False         # 指向的是别的进程
    assert picker.resolve().pid == 1001

    _select(picker, 1002)
    query.alive = {1002}
    assert picker.forget_if(1002) is False         # 还活着（如用户手动停止）
    assert picker.resolve().pid == 1002


def test_error_shows_under_field_and_clears_on_edit(picker):
    picker.show()
    picker.set_error("PID 必须是正整数")
    assert picker.error_label.isVisible() and picker.error_text() == "PID 必须是正整数"

    picker.set_text("a")
    assert not picker.error_label.isVisible() and picker.error_text() == ""


def test_no_separate_pid_box(picker):
    """PID 直接在搜索框输入，不再有独立的 PID 快捷框"""
    from qfluentwidgets import LineEdit
    from qfluentwidgets import EditableComboBox
    edits = [w for w in picker.findChildren(LineEdit) if not isinstance(w, EditableComboBox)]
    assert edits == []
