"""
历史页统计条控件 StatsStrip：六格标题与文本（与历史页安全网钉死的期望逐字一致）、
判定胶囊、置灰、小字说明与依据行、按指标类别换标题、清空、按自身宽度的响应式列数与
"只在列数变化时重排"。
"""
from datetime import timedelta

import pytest
from qfluentwidgets import CaptionLabel

from core.history_query import build_request, run_history_query
from tests.test_history_query import _seed_series
from tests.test_safety_net_history import (
    BASE, MAX_BUCKETS, QUERY_CASES, QUERY_JUDGEMENTS, TABLE_LIMIT, _seed_baseline_dataset,
    _seed_query_dataset)
from ui.formatting import STAT_SLOTS, StatCell
from ui.widgets.history.chart_view import chart_display_unit
from ui.widgets.history.common import VIEWPORT_TO_CONTENT_OFFSET
from ui.widgets.history.stats_strip import StatsStrip
from utils.metrics import MetricType

DEFAULT_TITLES = ("开始值", "当前值", "变化量", "每小时增长", "最小", "最大")


@pytest.fixture
def strip(qapp):
    widget = StatsStrip()
    yield widget
    widget.close()


def _snapshot(db, task_id, metric, range_key='all'):
    return run_history_query(db, build_request(
        task_id, metric, range_key, table_limit=TABLE_LIMIT, max_buckets=MAX_BUCKETS))


def _render(strip, snapshot):
    unit = chart_display_unit(snapshot.request.metric, [p.value for p in snapshot.buckets])
    strip.set_snapshot(snapshot, unit)


def test_default_cells_order_and_captions(strip):
    assert tuple(strip.texts()) == DEFAULT_TITLES
    assert set(strip.texts().values()) == {"--"}
    assert [cell.slot for cell in strip.cells()] == list(STAT_SLOTS)
    captions = {label.text(): label.toolTip() for label in strip.findChildren(CaptionLabel)
                if label.text() in DEFAULT_TITLES}
    assert set(captions) == set(DEFAULT_TITLES) and all(captions.values())
    assert strip.pill_text() == "" and strip.dimmed_titles() == []


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, stat_texts",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_values_match_history_safety_net(
        strip, db, metric, range_key, since_minutes, rows, count, buckets,
        markers, stat_texts):
    """喂入与安全网同一数据集的查询快照，六格标题与文本、判定胶囊、置灰与安全网一致；
    值的 tooltip 以完整文本开头（"--" 时为空）。"""
    task = _seed_query_dataset(db)
    _render(strip, _snapshot(db, task.task_id, metric, range_key))

    assert strip.texts() == stat_texts
    pill, dimmed, rate_note = QUERY_JUDGEMENTS[(metric, range_key)]
    assert strip.pill_text() == pill
    assert strip.dimmed_titles() == dimmed
    assert list(strip.notes().values())[3] == rate_note
    for slot, (title, text) in zip(STAT_SLOTS, stat_texts.items()):
        tip = strip.value_label(slot).toolTip()
        assert tip.startswith(text) if text != "--" else tip == ""


def test_dimming_is_restored_when_data_becomes_sufficient(strip, db):
    task = _seed_query_dataset(db)
    _render(strip, _snapshot(db, task.task_id, MetricType.MEMORY_PRIVATE, '10m'))
    assert strip.dimmed_titles() == ["每分钟增长"]
    assert strip.cell_widget('rate').dimmed is True
    _render(strip, _snapshot(db, task.task_id, MetricType.MEMORY_PRIVATE, '1h'))
    assert strip.dimmed_titles() == []
    assert strip.cell_widget('rate').dimmed is False


def test_baseline_strip_shows_baseline_value_and_basis(strip, db):
    task = _seed_baseline_dataset(db)
    _render(strip, _snapshot(db, task.task_id, MetricType.MEMORY_PRIVATE))
    texts = strip.texts()
    assert texts["基线值"] == "141 MB" and texts["每轮增长"] == "+20 MB"
    assert strip.notes()["基线值"] == "基线 08:20:00"
    assert strip.basis() == "基线 08:20:00 起 · 1小时40分 · 86 点"
    assert strip.toolTip() == strip.basis()
    assert strip.pill_text() == "持续上涨"


def test_cumulative_metric_titles(strip, db):
    values = [1024.0 * 10 * i for i in range(40)]
    task = _seed_series(db, values, metric=MetricType.IO_READ_BYTES)
    _render(strip, _snapshot(db, task.task_id, MetricType.IO_READ_BYTES))
    texts = strip.texts()
    assert tuple(texts) == ("开始值", "当前值", "变化量", "平均速率", "峰值速率", "数据跨度")
    assert texts["平均速率"] == texts["峰值速率"] == "171 KB/秒"   # 10 MB/分 ≈ 170.67 KB/秒
    assert texts["数据跨度"] == "39分钟"
    assert strip.pill_text() == ""


def test_peak_metric_titles(strip, db):
    values = [100.0] * 10 + [150.0] * 10 + [180.0] * 10
    task = _seed_series(db, values, metric=MetricType.MEMORY_PEAK_WSET)
    _render(strip, _snapshot(db, task.task_id, MetricType.MEMORY_PEAK_WSET))
    texts = strip.texts()
    assert tuple(texts) == ("开始值", "当前值", "变化量", "峰值抬升", "最小", "最大")
    assert texts["峰值抬升"] == "2 次，共 +80 KB"


def test_set_cells_partial_update_and_clear(strip):
    strip.set_cells([StatCell("rate", "每小时增长", "+1 MB/小时", note="基于 1小时0分 / 61 点",
                              dimmed=True, pill="平稳")])
    assert strip.texts()["每小时增长"] == "+1 MB/小时"
    assert strip.pill_text() == "平稳" and strip.dimmed_titles() == ["每小时增长"]
    strip.clear()
    assert set(strip.texts().values()) == {"--"}
    assert {strip.value_label(slot).toolTip() for slot in STAT_SLOTS} == {""}
    assert strip.pill_text() == "" and strip.dimmed_titles() == [] and strip.basis() == ""


def _used_columns(strip):
    layout = strip.stats_layout
    return max(layout.getItemPosition(i)[1] for i in range(layout.count())) + 1


# 原页面按视口宽度：1400 → 6、800 → 3、400 → 2；控件按自身内容宽度，档位不变
@pytest.mark.parametrize("viewport_width, columns", [(1400, 6), (800, 3), (400, 2)])
def test_column_count_matches_legacy_viewport_rule(strip, viewport_width, columns):
    content_width = viewport_width - VIEWPORT_TO_CONTENT_OFFSET
    assert StatsStrip.column_count(content_width) == columns
    strip.update_columns(content_width)
    assert strip.columns() == columns
    assert _used_columns(strip) == columns


@pytest.mark.parametrize("content_width, columns", [
    (1020, 6), (1019, 3), (480, 3), (479, 2)])
def test_column_count_boundaries(content_width, columns):
    # 原阈值：视口 1100 / 560，扣除页边距 2×24 与卡片内边距 2×16
    assert StatsStrip.column_count(content_width) == columns


def test_relayout_only_when_column_count_changes(strip):
    strip.update_columns(1200)
    assert strip.update_columns(1100) is False
    assert strip.update_columns(700) is True
    assert strip.update_columns(600) is False
    assert strip.columns() == 3


@pytest.mark.parametrize("width, columns", [(1400, 6), (800, 3), (400, 2)])
def test_resize_drives_columns_by_own_width(qapp, strip, width, columns):
    """控件按自身宽度（减去左右内边距 2×16）决定列数。"""
    strip.show()
    strip.resize(width, 120)
    qapp.processEvents()
    assert strip.columns() == StatsStrip.column_count(width - 32) == columns
    assert _used_columns(strip) == columns
