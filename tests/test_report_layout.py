"""
报告图片版式纯函数（ui/widgets/analysis/report_layout.py）：按宽度折行、网格切行、列宽分配、
行高与设备像素比折算。宽度测量用 len（每个字宽 1），不依赖字体。
"""
import pytest

from ui.widgets.analysis.report_layout import (
    block_height, chunk, column_widths, grid_shape, logical_width, pixel_size, row_height,
    wrap_text)


def _measure(text):
    return len(text)


# ---------- 折行 ----------

def test_wrap_short_text_is_one_line():
    assert wrap_text("进程监控", 10, _measure) == ["进程监控"]
    assert wrap_text("", 10, _measure) == [""]


def test_wrap_120_char_note_fills_lines_without_losing_text():
    note = "回归测试" * 30
    assert len(note) == 120
    lines = wrap_text(note, 50, _measure)
    assert lines == [note[:50], note[50:100], note[100:]]
    assert all(len(line) <= 50 for line in lines)
    assert "".join(lines) == note


def test_wrap_keeps_latin_words_and_paths_whole():
    lines = wrap_text("被测 alpha beta gamma", 12, _measure)
    assert lines == ["被测 alpha", "beta gamma"]
    # 路径在分隔符后断开，不从目录名中间劈开
    lines = wrap_text(r"C:\Program\Demo\demo.exe", 12, _measure)
    assert lines[0] == "C:\\Program\\"
    assert "".join(lines) == r"C:\Program\Demo\demo.exe"


def test_wrap_does_not_start_line_with_closing_punctuation():
    lines = wrap_text("一二三四，五六", 4, _measure)
    assert lines == ["一二三", "四，五六"]
    assert not any(line.startswith("，") for line in lines)


def test_wrap_honours_explicit_newlines_and_narrow_width():
    assert wrap_text("甲\n乙丙", 10, _measure) == ["甲", "乙丙"]
    # 宽度比一个字还窄：每行至少一个字，且能结束
    assert wrap_text("甲乙丙", 0, _measure) == ["甲", "乙", "丙"]


# ---------- 网格与表格 ----------

def test_chunk_row_major_with_partial_last_row():
    items = [("开始", "a"), ("结束", "b"), ("时长", "c")]
    assert chunk(items, 2) == [(items[0], items[1]), (items[2],)]
    assert chunk([], 2) == []


def test_column_widths_sum_exactly_and_follow_weights():
    widths = column_widths(1520, (1.7, 1.0, 1.2, 1.2, 1.7, 1.2, 2.0, 0.6))
    assert sum(widths) == 1520
    assert widths[6] > widths[0] > widths[1] > widths[7]
    assert column_widths(10, (1, 1, 1)) == [4, 3, 3]
    assert column_widths(100, ()) == []


def test_row_height_takes_tallest_cell_and_minimum():
    assert row_height([1, 3, 2], 21, 7) == 3 * 21 + 14
    assert row_height([1], 21, 7, minimum=36) == 36
    assert row_height([], 21, 0) == 21
    assert block_height(0, 21) == 21
    assert block_height(3, 20.5) == 62


def test_grid_shape():
    assert grid_shape(0, 2) == (0, 0)
    assert grid_shape(1, 2) == (1, 1)
    assert grid_shape(3, 2) == (2, 2)
    assert grid_shape(4, 2) == (2, 2)


# ---------- 设备像素比 ----------

@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_logical_width_round_trips_to_fixed_pixel_width(dpr):
    logical = logical_width(1600, dpr)
    assert logical == pytest.approx(1600 / dpr)
    assert pixel_size(logical, dpr) == 1600


def test_pixel_size_rounds_up():
    assert pixel_size(100.2, 1.5) == 151
    assert pixel_size(0, 2.0) == 1
