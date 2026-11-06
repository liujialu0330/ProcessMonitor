"""
历史页任何用户操作都不在主线程查数据（RULES.md §5：耗时操作不放 UI 线程）。

页面用生产装配（每个请求一个后台线程、加载推迟到下一轮事件循环），只把数据库换成
记录调用线程的包装：显示页面、切任务、切指标、切范围、点刷新、导航跳转、删除、
定时刷新之后，明细/统计/分桶/打点/锚点/任务记录的读取都必须发生在工作线程。
唯一允许在主线程的是任务列表（tasks.list_with_data，单条 SQL）。
"""
import threading
import uuid
from datetime import datetime, timedelta

import pytest

from core.monitor_manager import MonitorManager
from data.models import DataPoint, MonitorTask
from tests.doubles import FakeClock
from tests.factories import make_history_page, make_services
from tests.helpers import process_events_until
from utils.metrics import MetricType

BASE = datetime(2026, 7, 16, 8, 0, 0)
MEM = MetricType.MEMORY_PRIVATE
THREADS = MetricType.NUM_THREADS
# 允许在主线程执行的读取：任务列表（单条 SQL）
MAIN_THREAD_ALLOWED = {"tasks.list_with_data"}
# 一次完整加载必经的数据查询：断言它们确实被调用过，防止"什么都没查所以都不在主线程"
DATA_QUERIES = {"tasks.get", "points.fetch", "analytics.legacy_stats",
                "analytics.legacy_buckets", "markers.list"}


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


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=3)))


@pytest.fixture
def setup(qapp, db, services):
    old = _make_task(db, "old.exe", BASE)
    new = _make_task(db, "new.exe", BASE + timedelta(hours=1))
    spy = ThreadRecordingDb(db)
    page = make_history_page(spy, MonitorManager(db=db), services)
    page.resize(1200, 800)
    yield page, spy, old, new
    page.auto_refresh.stop()
    process_events_until(lambda: services.threads.entries() == [], timeout=10)
    page.close()


def _settle(page, services):
    """等查询全部返回、后台线程全部结束。"""
    assert process_events_until(
        lambda: not page.vm.has_pending_query and services.threads.entries() == [],
        timeout=10), "查询未在期限内返回"


def _assert_off_main_thread(spy, action):
    on_main = [name for name in spy.on_main_thread() if name not in MAIN_THREAD_ALLOWED]
    assert on_main == [], f"{action}：以下读取发生在主线程 {on_main}"


def test_user_actions_never_query_data_on_main_thread(setup, services):
    page, spy, old, new = setup
    bar = page.filter_bar

    actions = [
        ("显示页面", page.show),
        ("切任务", lambda: page.task_selector.combo.setCurrentIndex(1)),
        ("切指标", lambda: bar.metric_combo.setCurrentIndex(1)),
        ("切范围", lambda: bar.range_segmented.setCurrentItem('10m')),
        ("点刷新", bar.refresh_button.click),
        ("导航跳转", lambda: page.select_task(new.task_id)),
    ]
    for action, run in actions:
        spy.calls.clear()
        run()
        _settle(page, services)
        assert DATA_QUERIES <= spy.names(), f"{action}：没有发起完整的数据查询"
        _assert_off_main_thread(spy, action)

    assert page.current_task_id == new.task_id
    assert page.content_stack.currentWidget() is page.analysis_page


def test_delete_and_auto_refresh_query_off_main_thread(setup, services):
    page, spy, old, new = setup
    page.show()
    _settle(page, services)

    # 定时刷新（所选任务运行中）
    services.task_status.set_running(new.task_id)
    spy.calls.clear()
    page.auto_refresh.tick()
    _settle(page, services)
    assert DATA_QUERIES <= spy.names()
    _assert_off_main_thread(spy, "定时刷新")
    services.task_status.set_running(new.task_id, False)
    page.vm.reload_tasks()
    _settle(page, services)

    # 删除后重载：删除本身经 manager 写库，重载后的查询仍在后台
    services.dialogs.push('confirm', True)
    spy.calls.clear()
    bar = page.filter_bar
    bar.delete_action.trigger()
    _settle(page, services)
    assert page.current_task_id == old.task_id
    assert DATA_QUERIES <= spy.names()
    _assert_off_main_thread(spy, "删除后重载")
