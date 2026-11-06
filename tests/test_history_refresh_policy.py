"""
历史页自动刷新策略的页面侧：页面给 HistoryAutoRefresh 的"本次是否跳过"判断——
明细视图、用户正在看图（悬停十字线/按住拖动）、主窗口最小化时跳过，定时器继续。
间隔分档、单飞、任务结束重载等调度规则见 tests/test_history_vm.py。
"""
from datetime import timedelta

import pytest

from core.monitor_manager import MonitorManager
from tests.doubles import FakeClock
from tests.factories import make_history_page, make_services
from tests.helpers import SyncHistoryRunner
from tests.test_history_page_flow import BASE, _seed_task, run_now


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=1)))


@pytest.fixture
def running_page(qapp, db, services):
    """已显示、选中了运行中任务的页面；runner.requests 记录每次查询"""
    task = _seed_task(db, samples=5, status="running")
    services.task_status.set_running(task.task_id)
    runner = SyncHistoryRunner(db)
    page = make_history_page(db, MonitorManager(db=db), services,
                             worker_runner=runner, defer=run_now)
    page.resize(1200, 800)
    page.show()
    page.select_task(task.task_id)
    page.runner = runner
    yield page
    page.auto_refresh.stop()
    page.close()


def _auto_requests(page):
    return [r for r in page.runner.requests if r.auto_refresh]


def test_auto_refresh_skipped_in_detail_view(running_page):
    page = running_page
    page.filter_bar.view_segmented.setCurrentItem('detail')
    page.auto_refresh.tick()
    assert _auto_requests(page) == []
    assert page.auto_refresh.is_active   # 定时器继续

    page.filter_bar.view_segmented.setCurrentItem('trend')
    page.auto_refresh.tick()
    assert len(_auto_requests(page)) == 1


def test_auto_refresh_skipped_while_hovering_chart(running_page):
    page = running_page
    page.chart_view.crosshair_line.setVisible(True)
    assert page.chart_view.is_interacting()
    page.auto_refresh.tick()
    assert _auto_requests(page) == []

    page.chart_view.crosshair_line.setVisible(False)
    page.auto_refresh.tick()
    assert len(_auto_requests(page)) == 1


def test_auto_refresh_skipped_when_window_minimized(running_page, monkeypatch):
    page = running_page
    monkeypatch.setattr(page.window(), 'isMinimized', lambda: True)
    page.auto_refresh.tick()
    assert _auto_requests(page) == []
    assert page.auto_refresh.is_active
