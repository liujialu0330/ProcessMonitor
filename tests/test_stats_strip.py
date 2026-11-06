"""
历史页统计条控件 StatsStrip：六格文本与 tooltip（与历史页安全网钉死的期望逐字一致）、
清空、按自身宽度的响应式列数与"只在列数变化时重排"。
"""
from datetime import timedelta

import pytest

from tests.helpers import fetch_history_render_inputs
from tests.test_safety_net_history import (
    BASE, MAX_BUCKETS, QUERY_CASES, TABLE_LIMIT, _seed_query_dataset)
from ui.widgets.history.chart_view import chart_display_unit
from ui.widgets.history.common import VIEWPORT_TO_CONTENT_OFFSET
from ui.widgets.history.stats_strip import STAT_KEYS, STAT_TOOLTIPS, StatsStrip


@pytest.fixture
def strip(qapp):
    widget = StatsStrip()
    yield widget
    widget.close()


def _caption_tooltips(strip):
    from qfluentwidgets import CaptionLabel
    return {label.text(): label.toolTip()
            for label in strip.findChildren(CaptionLabel)}


def test_default_cells_order_and_captions(strip):
    assert tuple(strip.stat_value_labels) == (
        "起点", "当前", "变化", "增速", "最小", "最大")
    assert STAT_KEYS == tuple(strip.stat_value_labels)
    assert set(strip.texts().values()) == {"--"}
    assert _caption_tooltips(strip) == STAT_TOOLTIPS


@pytest.mark.parametrize(
    "metric, range_key, since_minutes, rows, count, buckets, markers, stat_texts",
    QUERY_CASES, ids=[f"{m}-{r}" for m, r, *_ in QUERY_CASES])
def test_values_match_history_safety_net(
        strip, db, metric, range_key, since_minutes, rows, count, buckets,
        markers, stat_texts):
    """喂入与安全网同一数据集的查询结果，六格文本与安全网钉死的期望逐字一致，
    tooltip 为完整文本（"--" 时为空）。"""
    task = _seed_query_dataset(db)
    since = None if since_minutes is None else BASE + timedelta(minutes=since_minutes)
    _table, stats, chart_points, _markers = fetch_history_render_inputs(
        db, task.task_id, metric, since, TABLE_LIMIT, MAX_BUCKETS)
    unit = chart_display_unit(metric, [p.value for p in chart_points])

    strip.set_values(stats, metric, unit)

    assert strip.texts() == stat_texts
    for key, label in strip.stat_value_labels.items():
        expected_tip = "" if stat_texts[key] == "--" else stat_texts[key]
        assert label.toolTip() == expected_tip


def test_flow_dataset_texts(strip):
    """历史页流程测试的数据集：100 MB → 109 MB，每秒涨 1 MB。"""
    stats = {'first': 1024.0 * 100, 'last': 1024.0 * 109, 'min': 1024.0 * 100,
             'max': 1024.0 * 109, 'count': 10, 'slope_per_hour': 1024.0 * 3600}
    strip.set_values(stats, "memory_private", "MB")
    texts = strip.texts()
    assert texts["起点"] == "100 MB" and texts["当前"] == "109 MB"
    assert texts["变化"].startswith("+9 MB")
    assert texts["增速"].startswith("+") and texts["增速"].endswith("/小时")
    assert texts["最小"] == "100 MB" and texts["最大"] == "109 MB"


def test_clear_resets_texts_and_tooltips(strip):
    strip.set_texts({key: "1 MB" for key in STAT_KEYS})
    strip.clear()
    assert set(strip.texts().values()) == {"--"}
    assert {label.toolTip() for label in strip.stat_value_labels.values()} == {""}


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
