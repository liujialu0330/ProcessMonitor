"""
历史页：默认范围与空状态文案、缓存失效、运行中任务自动刷新、自适应范围、统计条、
打点竖线、表格提示、任务下拉文案、经 manager 删除。
"""
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pyqtgraph as pg
import pytest

import ui.pages.history_page as history_module
from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from ui.pages.history_page import AUTO_REFRESH_INTERVAL_MS, HistoryPage
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 20, 0, 0)


def _seed_task(db, samples=10, note="", status="stopped", markers=(), name="demo.exe",
               start=BASE, metric=MetricType.MEMORY_PRIVATE):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=[metric],
        interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=samples),
        status=status, note=note)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), metric)
        for i in range(samples)])
    for offset, label in markers:
        db.add_marker(task.task_id, start + timedelta(seconds=offset), label)
    return task


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def page(qapp, db, manager):
    widget = HistoryPage(db=db, manager=manager)
    widget.resize(1200, 800)
    yield widget
    widget._auto_refresh_timer.stop()
    widget.close()


# ---------- B2：默认范围与空状态 ----------

def test_default_range_is_all_and_initial_empty_state(page):
    assert page.current_range_key == 'all'
    assert page.empty_title_label.text() == "暂无历史数据"


def test_empty_state_wording_depends_on_selected_range(page, db, monkeypatch):
    task = _seed_task(db, samples=3)
    page.select_task(task.task_id)
    monkeypatch.setattr(db, 'get_task_data_points', lambda *a, **k: [])

    page.current_range_key = 'all'
    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert page.empty_title_label.text() == "该任务暂无数据"

    page.current_range_key = '1h'
    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert page.empty_title_label.text() == "所选时间范围内暂无数据"


def test_refresh_invalidates_last_timestamp_cache(page, db):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    page.current_range_key = '1h'

    first_anchor = page._get_cached_last_dt(task.task_id)
    assert first_anchor == BASE + timedelta(seconds=4)

    # 新数据写入后，不刷新时锚点仍是缓存值；点刷新后锚点跟上
    db.save_data_points([DataPoint(
        task.task_id, BASE + timedelta(seconds=60), 1.0, MetricType.MEMORY_PRIVATE)])
    assert page._get_cached_last_dt(task.task_id) == first_anchor

    page._on_refresh_clicked()

    assert page._get_cached_last_dt(task.task_id) == BASE + timedelta(seconds=60)


def test_cache_is_reused_between_range_switches(page, db, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    calls = []
    real = db.get_last_point_timestamp
    monkeypatch.setattr(
        db, 'get_last_point_timestamp', lambda tid: calls.append(tid) or real(tid))

    page._on_time_range_changed('1h')
    page._on_time_range_changed('6h')

    assert len(calls) <= 1


# ---------- B3：自动刷新 ----------

def test_auto_refresh_timer_follows_visibility(page):
    assert AUTO_REFRESH_INTERVAL_MS == 5000
    assert page._auto_refresh_timer.interval() == 5000
    assert not page._auto_refresh_timer.isActive()

    page.show()
    assert page._auto_refresh_timer.isActive()
    page.hide()
    assert not page._auto_refresh_timer.isActive()


def test_auto_refresh_reloads_only_when_selected_task_is_running(page, db, manager, monkeypatch):
    task = _seed_task(db, samples=5)
    page.show()
    page.select_task(task.task_id)

    loads = []
    monkeypatch.setattr(
        page, '_start_auto_refresh_worker', lambda *a, **k: loads.append((a, k)))

    # 任务未在运行：不刷新
    monkeypatch.setattr(page, '_is_task_running', lambda tid: False)
    page._on_auto_refresh()
    assert loads == []

    # 运行中：交给后台 worker 刷新（keep_view 语义在应用结果时保证）
    monkeypatch.setattr(page, '_is_task_running', lambda tid: True)
    page._on_auto_refresh()
    assert len(loads) == 1

    # 刚结束：整体重载一次任务列表，之后不再重复
    reloads = []
    monkeypatch.setattr(page, '_load_tasks', lambda: reloads.append(1))
    monkeypatch.setattr(page, '_is_task_running', lambda tid: False)
    page._on_auto_refresh()
    page._on_auto_refresh()
    assert reloads == [1]


def test_auto_refresh_stops_when_page_not_visible(page):
    page.show()
    assert page._auto_refresh_timer.isActive()
    page.hide()
    page._auto_refresh_timer.start()   # 模拟隐藏后残留的一次触发
    page._on_auto_refresh()
    assert not page._auto_refresh_timer.isActive()


# ---------- B4：自适应范围 ----------

def test_reload_enables_auto_range_unless_keep_view(page, db, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    calls = []
    monkeypatch.setattr(page.chart_widget, 'enableAutoRange', lambda *a, **k: calls.append(1))

    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert calls == [1]

    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE, keep_view=True)
    assert calls == [1]


# ---------- B5：统计条 ----------

def test_stats_strip_shows_six_cells_from_metric_stats(page, db):
    task = _seed_task(db, samples=10)   # 100 MB -> 109 MB，每秒涨 1 MB
    page.select_task(task.task_id)

    texts = {key: label.text() for key, label in page.stat_value_labels.items()}
    assert tuple(texts) == ("起点", "当前", "变化", "增速", "最小", "最大")
    assert texts["起点"] == "100 MB"
    assert texts["当前"] == "109 MB"
    assert texts["变化"].startswith("+9 MB")
    assert texts["增速"].endswith("/小时") and texts["增速"].startswith("+")
    assert texts["最小"] == "100 MB" and texts["最大"] == "109 MB"


def test_stats_strip_is_cleared_without_data(page):
    page._clear_display()
    assert {label.text() for label in page.stat_value_labels.values()} == {"--"}


@pytest.mark.parametrize("width, columns", [(1400, 6), (800, 3), (400, 2)])
def test_stats_strip_wraps_responsively(page, width, columns):
    assert page._stats_column_count(width) == columns
    page._update_stats_layout(width)
    used_columns = {page.stats_layout.getItemPosition(i)[1]
                    for i in range(page.stats_layout.count())}
    assert max(used_columns) + 1 == columns


# ---------- B6：打点竖线 ----------

def test_markers_are_drawn_as_vertical_lines(page, db):
    task = _seed_task(db, samples=10, markers=[(2, "第1轮"), (6, "第2轮")])
    page.select_task(task.task_id)

    lines = [item for item in page.chart_widget.getPlotItem().items
             if isinstance(item, pg.InfiniteLine) and item is not page.crosshair_line]
    assert len(lines) == 2
    assert sorted(line.value() for line in lines) == [
        (BASE + timedelta(seconds=2)).timestamp(),
        (BASE + timedelta(seconds=6)).timestamp()]


def test_no_marker_lines_for_task_without_markers(page, db):
    task = _seed_task(db, samples=10)
    page.select_task(task.task_id)
    lines = [item for item in page.chart_widget.getPlotItem().items
             if isinstance(item, pg.InfiniteLine) and item is not page.crosshair_line]
    assert lines == []


# ---------- B7：表格提示 ----------

def test_table_hint_shows_shown_over_total(page, db, monkeypatch):
    task = _seed_task(db, samples=10)
    monkeypatch.setattr(history_module, 'TABLE_POINT_LIMIT', 4)
    page.select_task(task.task_id)

    assert page.table_hint_label.text() == "最新数据优先 · 显示最近 4 条 / 共 10 条"
    assert page.data_table.rowCount() == 4


def test_table_hint_uses_thousands_separator(page):
    points = [DataPoint("t", BASE + timedelta(seconds=i), 1.0, MetricType.NUM_THREADS)
              for i in range(3)]
    page._update_table(points, MetricType.NUM_THREADS, total=12345)
    assert "共 12,345 条" in page.table_hint_label.text()


# ---------- B8：任务下拉 ----------

def test_task_combo_text_and_single_query(page, db, manager, monkeypatch):
    long_note = "建模 200 次回归测试，连续运行过夜观察内存增长"
    _seed_task(db, samples=30, note=long_note, name="MPrintExp.exe")
    _seed_task(db, samples=0, name="nodata.exe")   # 没有数据的任务不出现

    counted = []
    monkeypatch.setattr(
        db, 'get_data_point_count', lambda *a, **k: counted.append(1) or 0)
    page._load_tasks()

    assert counted == [], "任务下拉不应再逐任务统计数据点"
    assert page.task_combo.count() == 1
    text = page.task_combo.itemText(0)
    assert "MPrintExp.exe" in text and "PID 4321" in text and "07-16 20:00" in text
    assert "…" in text and long_note not in text
    assert long_note in page.task_combo.toolTip()


def test_running_task_is_marked_in_combo(page, db, manager, monkeypatch):
    _seed_task(db, samples=5, status="running")
    monkeypatch.setattr(page, '_is_task_running', lambda tid: True)
    page._load_tasks()
    assert "运行中" in page.task_combo.itemText(0)


def test_select_task_picks_given_task(page, db):
    first = _seed_task(db, samples=3, name="a.exe", start=BASE)
    second = _seed_task(db, samples=3, name="b.exe", start=BASE + timedelta(hours=1))

    page.select_task(first.task_id)
    assert page.current_task_id == first.task_id
    page.select_task(second.task_id)
    assert page.current_task_id == second.task_id
    assert "b.exe" in page.task_combo.currentText()

    page.select_task("missing-task")
    # 不在列表中：回落到首项，不抛异常
    assert page.current_task_id in (first.task_id, second.task_id, None)


# ---------- B9：删除经 manager ----------

def test_delete_goes_through_manager(page, db, manager, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)

    class _Yes:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return True

    monkeypatch.setattr(history_module, 'MessageBox', _Yes)
    monkeypatch.setattr(history_module, 'InfoBar', SimpleNamespace(
        success=lambda **k: None, error=lambda **k: None, warning=lambda **k: None))
    deleted = []
    real = manager.delete_task_data
    monkeypatch.setattr(
        manager, 'delete_task_data', lambda tid: deleted.append(tid) or real(tid))

    page._on_delete_task_clicked()

    assert deleted == [task.task_id]
    assert db.get_task(task.task_id) is None
    assert page.current_task_id is None
    assert page.task_combo.count() == 0


def test_delete_failure_keeps_task(page, db, manager, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)

    class _Yes:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return True

    errors = []
    monkeypatch.setattr(history_module, 'MessageBox', _Yes)
    monkeypatch.setattr(history_module, 'InfoBar', SimpleNamespace(
        success=lambda **k: None, error=lambda **k: errors.append(k),
        warning=lambda **k: None))
    monkeypatch.setattr(manager, 'delete_task_data', lambda tid: False)

    page._on_delete_task_clicked()

    assert len(errors) == 1
    assert db.get_task(task.task_id) is not None
