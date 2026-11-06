"""
TaskSelector（离屏）：
- set_options 不发 task_selected（含 qfluentwidgets addItem 自动选中首项的情形）；
- keep_id 恢复选中、找不到时不选中任何项（显示占位文字）；
- select_by_id：程序选中发 explicit=False，silent 时不发；找不到返回 False 且不改选中；
- 用户点选（对内部 ComboBox setCurrentIndex）发 explicit=True；
- 逐项提示与当前项提示同步；update_option 原地更新文本与提示。
"""
import pytest
from PyQt5.QtWidgets import QAction

from ui.models.task_options import TaskOption
from ui.widgets.task_selector import TaskSelector

PLACEHOLDER = "选择监控任务"


def _opt(task_id, running=False):
    return TaskOption(task_id=task_id, text=f"{task_id}.exe · 文本",
                      tooltip=f"{task_id} 的完整信息", running=running)


OPTIONS = [_opt("a", running=True), _opt("b"), _opt("c")]


@pytest.fixture
def selector(qapp):
    widget = TaskSelector(placeholder=PLACEHOLDER)
    widget.received = []
    widget.task_selected.connect(lambda tid, explicit: widget.received.append((tid, explicit)))
    yield widget
    widget.close()


def test_set_options_emits_nothing_and_selects_nothing(selector):
    selector.set_options(OPTIONS)
    assert selector.received == []
    assert selector.current_id() is None and selector.current_index() == -1
    assert selector.count() == 3
    assert [selector.task_id_at(i) for i in range(3)] == ["a", "b", "c"]
    assert [selector.item_text(i) for i in range(3)] == [o.text for o in OPTIONS]
    # qfluentwidgets 的 addItem 会自动选中首项、clear 不恢复占位文字：控件内部已处理
    assert selector.combo.text() == PLACEHOLDER
    assert selector.tooltip() == ""


def test_set_options_restores_keep_id_silently(selector):
    selector.set_options(OPTIONS)
    selector.set_options(OPTIONS, keep_id="b")
    assert selector.current_id() == "b" and selector.current_index() == 1
    assert selector.combo.text() == OPTIONS[1].text
    assert selector.tooltip() == OPTIONS[1].tooltip
    assert selector.received == []

    # 要恢复的任务不在新列表里：不选中任何项，也不发信号
    selector.set_options(OPTIONS[:1] + OPTIONS[2:], keep_id="b")
    assert selector.current_id() is None
    assert selector.combo.text() == PLACEHOLDER
    assert selector.tooltip() == ""
    assert selector.received == []


def test_set_options_empty_shows_placeholder(selector):
    selector.set_options(OPTIONS, keep_id="a")
    selector.set_options([])
    assert selector.count() == 0 and selector.current_id() is None
    assert selector.combo.text() == PLACEHOLDER
    assert selector.received == []


def test_select_by_id_emits_programmatic_signal(selector):
    selector.set_options(OPTIONS)
    assert selector.select_by_id("c") is True
    assert selector.current_id() == "c"
    assert selector.received == [("c", False)]
    # 已是当前项也照样通知：调用方要的是"选中后的效果"
    assert selector.select_by_id("c") is True
    assert selector.received == [("c", False), ("c", False)]


def test_select_by_id_silent_and_missing(selector):
    selector.set_options(OPTIONS)
    assert selector.select_by_id("b", silent=True) is True
    assert selector.current_id() == "b" and selector.tooltip() == OPTIONS[1].tooltip
    assert selector.select_by_id("missing") is False
    assert selector.current_id() == "b"
    assert selector.received == []


def test_select_none_clears_selection(selector):
    selector.set_options(OPTIONS, keep_id="a")
    assert selector.select_by_id(None) is True
    assert selector.current_id() is None
    assert selector.combo.text() == PLACEHOLDER
    assert selector.received == [(None, False)]


def test_user_pick_is_explicit(selector):
    selector.set_options(OPTIONS)
    selector.combo.setCurrentIndex(2)          # 与用户在下拉里点选等价
    assert selector.received == [("c", True)]
    assert selector.tooltip() == OPTIONS[2].tooltip
    # 程序选中之后用户再点选：两种来源分得清
    selector.select_by_id("a")
    selector.combo.setCurrentIndex(1)
    assert selector.received == [("c", True), ("a", False), ("b", True)]


def test_update_option_in_place(selector):
    selector.set_options(OPTIONS, keep_id="a")
    new = TaskOption("a", "a.exe · 运行中 30分钟", "a 新提示", running=True)
    selector.update_option(0, new)
    assert selector.item_text(0) == new.text
    assert selector.combo.text() == new.text        # 当前项按钮文字一并更新
    assert selector.tooltip() == "a 新提示"
    assert selector.item_tooltip(0) == "a 新提示"
    assert selector.options()[0] == new

    # 非当前项：只改该项，不动当前提示
    other = TaskOption("b", "b 新文本", "b 新提示", running=False)
    selector.update_option(1, other)
    assert selector.item_text(1) == "b 新文本" and selector.item_tooltip(1) == "b 新提示"
    assert selector.tooltip() == "a 新提示"
    selector.update_option(9, other)                # 越界忽略
    assert selector.received == []


def test_dropdown_items_carry_tooltips(selector):
    """展开下拉时每个菜单项挂上对应选项的提示。"""
    selector.set_options(OPTIONS)
    menu = selector.combo._createComboMenu()
    # 解除父子关系：菜单随本用例的 Python 引用释放，不留待处理的延迟删除事件
    menu.setParent(None)
    actions = [QAction(o.text) for o in OPTIONS]
    for action in actions:
        menu.addAction(action)
    assert [a.toolTip() for a in actions] == [o.tooltip for o in OPTIONS]
    menu.close()
