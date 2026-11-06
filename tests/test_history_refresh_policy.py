"""
历史页自动刷新策略：锚点读 tasks.last_sample_at、按数据量自适应间隔、
明细视图/悬停图表/窗口最小化时跳过刷新；select_task 找不到目标任务时直接返回并提示。
"""
from datetime import timedelta

import pytest

from core.monitor_manager import MonitorManager
from tests.doubles import FakeClock
from tests.factories import make_history_page, make_services
from tests.test_history_page_flow import BASE, _seed_task
from ui.pages.history_page import AUTO_REFRESH_INTERVAL_MS, HistoryPage
from utils.metrics import MetricType


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=1)))


@pytest.fixture
def page(qapp, db, manager, services):
    widget = make_history_page(db, manager, services)
    widget.resize(1200, 800)
    yield widget
    widget._auto_refresh_timer.stop()
    widget.close()


# ---------- 锚点 ----------

def test_anchor_uses_last_sample_at_without_max_query(page, db, monkeypatch):
    """时间范围锚点读 tasks.last_sample_at，不再执行 MAX(timestamp) 查询"""
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    calls = []
    monkeypatch.setattr(db, 'get_last_point_timestamp', lambda tid: calls.append(tid))
    page._invalidate_last_dt_cache()
    page.current_range_key = '1h'

    assert page._get_cached_last_dt(task.task_id) == BASE + timedelta(seconds=4)
    assert calls == []


def test_anchor_falls_back_to_query_when_last_sample_at_empty(page, db):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    with db._get_connection() as conn:
        conn.execute('UPDATE tasks SET last_sample_at = NULL WHERE task_id = ?',
                     (task.task_id,))
    page._invalidate_last_dt_cache()

    assert page._get_cached_last_dt(task.task_id) == BASE + timedelta(seconds=4)


# ---------- 自适应间隔 ----------

@pytest.mark.parametrize("count, expected", [
    (0, 5000), (199_999, 5000),
    (200_000, 15000), (999_999, 15000),
    (1_000_000, 30000), (5_000_000, 30000),
])
def test_auto_refresh_interval_adapts_to_count(page, count, expected):
    assert HistoryPage._auto_refresh_interval_for(count) == expected
    page._update_auto_refresh_interval(count)
    assert page._auto_refresh_timer.interval() == expected


def test_load_task_data_updates_refresh_interval_from_stats(page, db, monkeypatch):
    task = _seed_task(db, samples=5)
    page.select_task(task.task_id)
    assert page._auto_refresh_timer.interval() == AUTO_REFRESH_INTERVAL_MS

    real = db.get_metric_stats
    monkeypatch.setattr(
        db, 'get_metric_stats', lambda *a, **k: {**real(*a, **k), 'count': 300_000})
    page._load_task_data(task.task_id, MetricType.MEMORY_PRIVATE)
    assert page._auto_refresh_timer.interval() == 15000


# ---------- 跳过刷新 ----------

@pytest.fixture
def running_page(page, db, services, monkeypatch):
    task = _seed_task(db, samples=5)
    page.show()
    page.select_task(task.task_id)
    loads = []
    monkeypatch.setattr(page, '_start_auto_refresh_worker', lambda *a, **k: loads.append(1))
    services.task_status.set_running(task.task_id)
    page.loads = loads
    return page


def test_auto_refresh_skipped_in_detail_view(running_page):
    page = running_page
    page.view_stack.setCurrentIndex(1)
    page._on_auto_refresh()
    assert page.loads == []
    assert page._auto_refresh_timer.isActive()   # 定时器继续

    page.view_stack.setCurrentIndex(0)
    page._on_auto_refresh()
    assert page.loads == [1]


def test_auto_refresh_skipped_while_hovering_chart(running_page):
    page = running_page
    page.crosshair_line.setVisible(True)
    page._on_auto_refresh()
    assert page.loads == []

    page.crosshair_line.setVisible(False)
    page._on_auto_refresh()
    assert page.loads == [1]


def test_auto_refresh_skipped_when_window_minimized(running_page, monkeypatch):
    page = running_page
    monkeypatch.setattr(page.window(), 'isMinimized', lambda: True)
    page._on_auto_refresh()
    assert page.loads == []
    assert page._auto_refresh_timer.isActive()


# ---------- select_task 找不到目标 ----------

def test_select_task_not_found_returns_with_hint(page, db, services):
    """目标任务不在列表（无采样数据）：清空界面 + 提示，不静默选中第一个任务"""
    first = _seed_task(db, samples=3, name="a.exe")
    nodata = _seed_task(db, samples=0, name="nodata.exe")
    page.select_task(first.task_id)
    assert page.current_task_id == first.task_id
    services.notifier.clear()

    page.select_task(nodata.task_id)

    assert page.current_task_id is None
    assert page.task_combo.currentIndex() == -1
    assert page.empty_title_label.text() == "请选择监控任务"
    hints = services.notifier.messages()
    assert len(hints) == 1 and hints[0].level == "info"
    assert "暂无采样数据" in hints[0].content
