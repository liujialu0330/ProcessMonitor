"""
可搜索下拉框：set_items 一次完成清空/填充/不选中/恢复；current_data 的文本一致校验；
回车规则（精确匹配、纯数字、补全首项）；信号只在用户选择时发出。
"""
import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from ui.widgets.searchable_combo import SearchableCombo

ITEMS = [("alpha.exe · PID 1", ("alpha", 1)),
         ("beta.exe · PID 2", ("beta", 2)),
         ("Alphabet.exe · PID 3", ("alphabet", 3))]


@pytest.fixture
def combo(qapp):
    widget = SearchableCombo(number_matcher=lambda data, n: data[1] == n)
    widget.set_items(ITEMS)
    selected = []
    widget.selected.connect(selected.append)
    widget.received = selected
    yield widget
    widget.close()


def _enter(combo):
    QTest.keyClick(combo.line_edit, Qt.Key_Return)


def test_set_items_fills_without_selecting(combo):
    assert combo.count() == 3
    assert combo.text() == ""
    assert combo.current_data() is None
    assert combo.received == []


def test_set_items_clears_typed_text_even_without_selection(combo):
    """库的 clear() 在没有选中项时不清手输文本：set_items 必须自己清掉"""
    combo.set_text("手输的内容")
    combo.set_items(ITEMS[:1])
    assert combo.text() == "" and combo.count() == 1


def test_set_items_restores_kept_selection_silently(combo):
    combo.set_text("beta.exe · PID 2")
    combo.received.clear()

    combo.set_items(ITEMS, keep=("beta", 2))

    assert combo.current_data() == ("beta", 2)
    assert combo.text() == "beta.exe · PID 2"
    assert combo.received == []   # 恢复选中不算用户选择


def test_set_items_drops_kept_selection_that_disappeared(combo):
    combo.set_items(ITEMS[:1], keep=("beta", 2))
    assert combo.current_data() is None and combo.text() == ""


def test_typing_exact_item_text_selects_it(combo):
    combo.set_text("alpha.exe · PID 1")
    assert combo.current_data() == ("alpha", 1)
    assert combo.received == [("alpha", 1)]


def test_current_data_requires_text_to_match_selected_item(combo):
    combo.set_text("alpha.exe · PID 1")
    combo.set_text("alpha.exe · PID")
    assert combo.current_data() is None


def test_select_data(combo):
    assert combo.select_data(("alphabet", 3)) is True
    assert combo.text() == "Alphabet.exe · PID 3"
    assert combo.select_data(("nope", 0)) is False


def test_text_edited_only_for_user_edits(combo):
    edited, changed = [], []
    combo.text_edited.connect(edited.append)
    combo.text_changed.connect(changed.append)
    combo.set_text("be")
    assert edited == [] and changed == ["be"]
    QTest.keyClicks(combo.line_edit, "t")
    assert edited == ["bet"]


def test_enter_on_exact_text_selects(combo):
    combo.set_text("beta.exe · PID 2")
    combo.received.clear()
    _enter(combo)
    assert combo.current_data() == ("beta", 2)
    assert combo.count() == 3   # 不会把文本加成新项


def test_enter_picks_first_substring_match_case_insensitively(combo):
    combo.set_text("ALPHA")
    _enter(combo)
    assert combo.current_data() == ("alpha", 1)
    assert combo.received == [("alpha", 1)]


def test_enter_on_unmatched_text_keeps_text_and_adds_no_item(combo):
    combo.set_text("不存在")
    _enter(combo)
    assert combo.count() == 3
    assert combo.text() == "不存在" and combo.current_data() is None


def test_enter_on_number_uses_number_matcher(combo):
    combo.set_text("3")
    _enter(combo)
    assert combo.current_data() == ("alphabet", 3)


def test_enter_on_unknown_number_keeps_digits(combo):
    """纯数字不套用补全首项（"1" 是很多项文本的子串），找不到对应项时原样保留"""
    combo.set_text("42")
    _enter(combo)
    assert combo.text() == "42" and combo.current_data() is None
    assert combo.received == []


def test_enter_on_number_without_matcher_keeps_digits(qapp):
    widget = SearchableCombo()
    widget.set_items(ITEMS)
    widget.set_text("1")
    _enter(widget)
    assert widget.text() == "1" and widget.current_data() is None
    widget.close()


def test_enter_on_empty_text_does_nothing(combo):
    _enter(combo)
    assert combo.count() == 3 and combo.received == []
