"""
分析页任何用户操作都不在主线程查数据（RULES.md §5：耗时操作不放 UI 线程）。

页面用生产装配（每个请求一个后台线程、加载推迟到下一轮事件循环），只把数据库换成
记录调用线程的包装：显示页面、切任务、切焦点、切范围、刷选自定义范围、刷新、导航跳转、
定时刷新、删除后重载、补加打点、打开对比、导出报告之后，数据读取都必须发生在工作线程。
唯一允许在主线程的是任务列表（tasks.list_with_data，单条 SQL）。
打点写库与删除经 manager（真实数据库，不经包装），不在此列。
"""
import os
import threading
import uuid
from datetime import datetime, timedelta

import pytest

from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_analysis_page, make_services
from tests.helpers import process_events_until
from ui.widgets.analysis.toolbar import EXPORT_REPORT
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 8, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
THREADS = MetricType.NUM_THREADS
# 允许在主线程执行的读取：任务列表（单条 SQL）
MAIN_THREAD_ALLOWED = {"tasks.list_with_data"}


class _RecordingRepo:
    """仓储包装：每次方法调用记下 ("仓储.方法", 调用线程) 后转给真实仓储。"""

    def __init__(self, repo, name, calls):
        self._repo, self._name, self._calls = repo, name, calls

    def __getattr__(self, attr):
        target = getattr(self._repo, attr)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self._calls.append((f"{self._name}.{attr}", threading.current_thread()))
            return target(*args, **kwargs)
        return call


class ThreadRecordingDb:
    """包装真实 Database：仓储与门面方法的每次调用都记录调用线程。"""

    REPOS = ("tasks", "points", "analytics", "markers")

    def __init__(self, db):
        self._db = db
        self.calls = []
        for name in self.REPOS:
            setattr(self, name, _RecordingRepo(getattr(db, name), name, self.calls))

    def __getattr__(self, attr):
        return _RecordingRepo(self._db, "db", self.calls).__getattr__(attr)

    def on_main_thread(self):
        main = threading.main_thread()
        return [name for name, thread in self.calls if thread is main]

    def names(self):
        return {name for name, _thread in self.calls}


def _make_task(db, name, start, metrics=(MEM, THREADS), status="stopped"):
    task = MonitorTask(
        task_id=str(uuid.uuid4()), pid=4321, process_name=name, metric_types=list(metrics),
        interval=1.0, start_time=start,
        end_time=None if status == "running" else start + timedelta(seconds=30),
        status=status)
    db.save_task(task)
    db.save_data_points([
        DataPoint(task.task_id, start + timedelta(seconds=i), 1024.0 * (100 + i), metric)
        for i in range(30) for metric in metrics])
    db.add_marker(task.task_id, start + timedelta(seconds=10), "第1轮")
    return task


# 分析页一次完整加载必经的数据查询（多指标 + min/max 带 + 打点区间聚合）
DATA_QUERIES = {"analytics.aggregates", "analytics.gaps", "analytics.buckets", "markers.list"}


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=3)))


@pytest.fixture
def setup(qapp, db, services):
    old = _make_task(db, "old.exe", BASE)
    new = _make_task(db, "new.exe", BASE + timedelta(hours=1))
    spy = ThreadRecordingDb(db)
    page = make_analysis_page(spy, MonitorManager(db=db), services)
    page.resize(1280, 1400)
    yield page, spy, old, new
    page.auto_refresh.stop()
    process_events_until(lambda: services.threads.entries() == [], timeout=10)
    page.close()


def _settle(page, services):
    assert process_events_until(
        lambda: not page.vm.has_pending_query and services.threads.entries() == [],
        timeout=15), "查询未在期限内返回"


def _assert_off_main_thread(spy, action):
    on_main = [name for name in spy.on_main_thread() if name not in MAIN_THREAD_ALLOWED]
    assert on_main == [], f"{action}：以下读取发生在主线程 {on_main}"


def test_user_actions_never_query_data_on_main_thread(setup, services):
    page, spy, old, new = setup
    bar = page.toolbar
    brush = (BASE + timedelta(hours=1, seconds=5), BASE + timedelta(hours=1, seconds=25))

    actions = [
        ("显示页面", page.show, True),
        ("切任务", lambda: page.task_selector.combo.setCurrentIndex(1), True),
        ("切焦点", lambda: page.summary_table.table.cellClicked.emit(1, 0), False),
        ("切范围", lambda: bar.range_segmented.setCurrentItem('10m'), True),
        ("视图切换", lambda: bar.view_segmented.setCurrentItem('overlay'), False),
        ("相对时间", lambda: bar.time_segmented.setCurrentItem('relative'), False),
        ("点刷新", bar.refresh_button.click, True),
        ("导航跳转", lambda: page.select_task(new.task_id), True),
        ("刷选范围", lambda: page.trend_panel.overview.select(
            brush[0].timestamp(), brush[1].timestamp()), True),
        ("回到全部", lambda: bar.range_segmented.setCurrentItem('all'), True),
    ]
    for action, run, queries in actions:
        spy.calls.clear()
        run()
        _settle(page, services)
        if queries:
            assert DATA_QUERIES <= spy.names(), f"{action}：没有发起完整的数据查询"
        _assert_off_main_thread(spy, action)

    assert page.current_task_id == new.task_id
    assert page.content_stack.currentWidget() is page.body
    assert len(page.summary_table.row_texts()) == 2


def test_marker_compare_and_export_off_main_thread(setup, services, tmp_path):
    page, spy, old, new = setup
    page.show()
    _settle(page, services)
    page.select_task(new.task_id)
    _settle(page, services)

    # 右键补加打点：写库经 manager，之后的静默重查在后台
    services.dialogs.push('ask_marker_label', "第2轮")
    spy.calls.clear()
    assert page.marker_flow.add_at((new.start_time + timedelta(seconds=20)).timestamp())
    _settle(page, services)
    assert DATA_QUERIES <= spy.names()
    _assert_off_main_thread(spy, "补加打点")
    assert len(page.interval_table.row_texts()) == 3

    # 打开对比并选择对比任务
    spy.calls.clear()
    page.toolbar.compare_button.setChecked(True)
    page.compare_panel.task_selected.emit(old.task_id)
    _settle(page, services)
    assert "analytics.aggregates" in spy.names()
    _assert_off_main_thread(spy, "任务对比")
    assert page.compare_panel.chart.legend_names() == ["当前任务", "对比任务"]

    # 导出报告：图片在主线程离屏渲染（不读库），明细在导出线程读取
    out = tmp_path / "out"
    out.mkdir()
    services.dialogs.push('ask_save_path', str(out / "r_报告.png"))
    spy.calls.clear()
    page.toolbar.export_requested.emit(EXPORT_REPORT)
    assert process_events_until(
        lambda: services.threads.entries() == [] and services.notifier.messages('success'),
        timeout=20)
    assert spy.names(), "导出没有读取数据"
    _assert_off_main_thread(spy, "导出报告")
    assert sorted(os.listdir(out)) == ["r.csv", "r_报告.png", "r_摘要.csv"]


def test_auto_refresh_and_delete_reload_off_main_thread(setup, services):
    """定时刷新（所选任务运行中）与删除后的重载：查询同样都在后台线程。"""
    page, spy, old, new = setup
    page.show()
    _settle(page, services)

    services.task_status.set_running(new.task_id)
    spy.calls.clear()
    page.auto_refresh.tick()
    _settle(page, services)
    assert DATA_QUERIES <= spy.names()
    _assert_off_main_thread(spy, "定时刷新")
    services.task_status.set_running(new.task_id, False)
    page.vm.reload_tasks()
    _settle(page, services)

    # 删除经 manager 写库，删除后重载的查询仍在后台
    services.dialogs.push('confirm', True)
    spy.calls.clear()
    page.toolbar.delete_action.trigger()
    _settle(page, services)
    assert page.current_task_id == old.task_id
    assert DATA_QUERIES <= spy.names()
    _assert_off_main_thread(spy, "删除后重载")
