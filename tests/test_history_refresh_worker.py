"""
历史数据查询后台线程（HistoryRefreshWorker）：外壳本身（execute 调 run_history_query、
成功/存储故障/编程错误的打包），以及它在历史页上的生产装配：自动刷新结果在主线程
应用且不重置视图、页面隐藏后结果丢弃、线程结束后被释放、退出时带超时 join。
过期丢弃、单飞、失败提示去重等调度规则见 tests/test_history_vm.py。
不依赖固定 sleep：一律轮询等待（tests/helpers.poll_until）。
"""
import threading
from datetime import timedelta

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from data.models import DataPoint
from tests.doubles import FakeClock
from tests.factories import make_history_page, make_main_window, make_services
from tests.helpers import poll_until
from tests.test_history_page_flow import BASE, _seed_task
from utils.metrics import MetricType
from utils.thread_utils import shutdown_thread

METRIC = MetricType.MEMORY_PRIVATE


def _pump():
    """处理排队的跨线程信号（结果槽、finished 槽）"""
    QApplication.processEvents()


def _wait(condition, timeout=10.0):
    def check():
        _pump()
        return condition()
    return poll_until(check, timeout=timeout)


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def services():
    return make_services(clock=FakeClock(BASE + timedelta(hours=1)))


@pytest.fixture
def page(qapp, db, manager, services):
    """生产装配的历史页（每个请求一个后台线程、加载推迟到下一轮事件循环）"""
    widget = make_history_page(db, manager, services)
    widget.resize(1200, 800)
    widget.release = None   # 测试可置入阻塞闸，teardown 统一放行
    yield widget
    if widget.release is not None:
        widget.release.set()
    widget.auto_refresh.stop()
    _wait(lambda: services.threads.entries() == [])
    widget.close()


@pytest.fixture
def shown(page, db, services):
    """已显示、已选中一个 20 点任务的页面；该任务视为运行中，定时刷新会发起后台查询"""
    task = _seed_task(db, samples=20, status="running")
    services.task_status.set_running(task.task_id)
    page.show()
    page.auto_refresh.stop()   # 只手动触发，避免定时器干扰
    page.select_task(task.task_id)
    assert _wait(lambda: not page.vm.has_pending_query)
    page.task = task
    return page


def _append_points(db, task, start_index, count):
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=i), 1024.0 * (100 + i), METRIC)
        for i in range(start_index, start_index + count)])


def _block_table_query(page, db, monkeypatch):
    """让工作线程内的表格查询阻塞到测试放行；返回 entered 事件"""
    gate = threading.Event()
    entered = threading.Event()
    real = db.points.fetch

    def blocking(*a, **k):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            gate.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(db.points, 'fetch', blocking)
    page.release = gate
    return entered


def _query_thread(services):
    entry, = services.threads.entries()
    return entry.thread


# ---------- 页面上的后台刷新（真实线程） ----------

def test_auto_refresh_result_updates_page_and_keeps_view(shown, db, monkeypatch):
    page = shown
    chart = page.chart_view
    points_before = len(chart.x_values)
    rows_before = page.sample_table.data_table.rowCount()

    # 模拟用户缩放：自动刷新不得重置视图
    page.chart_view.chart_widget.setXRange(chart.x_values[2], chart.x_values[8], padding=0)
    x_range_before = page.chart_view.chart_widget.viewRange()[0]   # 只比 X（Y 轴自适应是既有行为）
    resets = []
    monkeypatch.setattr(chart, 'reset_view', lambda: resets.append(1))

    _append_points(db, page.task, 20, 15)
    page.auto_refresh.tick()
    assert _wait(lambda: len(chart.x_values) > points_before)

    assert page.sample_table.data_table.rowCount() == rows_before + 15
    assert "共 35 条" in page.sample_table.table_hint_label.text()
    assert page.stats_strip.texts()["当前"] != "--"
    assert resets == []
    assert page.chart_view.chart_widget.viewRange()[0] == x_range_before


def test_result_dropped_after_page_hidden(shown, db, services, monkeypatch):
    page = shown
    entered = _block_table_query(page, db, monkeypatch)
    page.auto_refresh.tick()
    assert entered.wait(10)

    snapshots = []
    page.vm.snapshot_ready.connect(snapshots.append)
    page.hide()                                  # 隐藏不阻塞等待，只让结果过期
    page.release.set()
    assert _wait(lambda: services.threads.entries() == [])
    assert snapshots == []


def test_query_thread_released_after_finished(shown, services):
    from PyQt5 import sip
    page = shown
    page.auto_refresh.tick()
    worker = _query_thread(services)

    assert _wait(lambda: services.threads.entries() == [])

    def deleted():
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _pump()
        return sip.isdeleted(worker)

    assert poll_until(deleted, timeout=10.0)


def test_shutdown_thread_joins_query_thread_with_timeout(shown, db, services, monkeypatch):
    """退出路径：对在途查询线程做带超时 join，超时返回 False、放行后返回 True"""
    page = shown
    entered = _block_table_query(page, db, monkeypatch)
    page.auto_refresh.tick()
    assert entered.wait(10)

    worker = _query_thread(services)
    assert shutdown_thread(worker, timeout_ms=50) is False
    page.release.set()
    assert shutdown_thread(worker, timeout_ms=10000) is True
    assert _wait(lambda: services.threads.entries() == [])


def test_main_window_close_joins_history_refresh_worker(qapp, tmp_path, monkeypatch):
    """关窗真退出时，登记在 services.threads 里的在途刷新线程被带超时 join：
    closeEvent 返回时线程已结束（不 join 的话它还要再跑几百毫秒）。
    依赖主窗口 closeEvent 经 ShutdownCoordinator 收尾登记表。"""
    import time
    from unittest.mock import MagicMock
    from PyQt5.QtCore import QThread
    from data.database import Database

    class _SlowRefresh(QThread):
        def __init__(self):
            super().__init__()
            self.started_evt = threading.Event()

        def run(self):
            self.started_evt.set()
            time.sleep(0.5)

    db = Database(str(tmp_path / "close_join.db"))
    manager = MonitorManager(db=db)
    win = make_main_window(db, manager)
    win.about_page.check_update = lambda *args, **kwargs: None
    monkeypatch.setattr(QApplication, 'quit', MagicMock())
    worker = _SlowRefresh()
    win.services.threads.register(worker, name="历史自动刷新", timeout_ms=3000)
    try:
        worker.start()
        assert worker.started_evt.wait(5)
        win.closeEvent(MagicMock())
        assert worker.isFinished()
    finally:
        worker.wait(5000)
        manager.stop_all_tasks()


# ---------- 存储故障上报 ----------

def _run_inline(worker):
    """直接在当前线程执行 run，只验证结果打包；返回 (result_ready, result, failed) 记录"""
    ready, results, failures = [], [], []
    worker.result_ready.connect(ready.append, type=Qt.DirectConnection)
    worker.result.connect(results.append, type=Qt.DirectConnection)
    worker.failed.connect(failures.append, type=Qt.DirectConnection)
    worker.run()
    return ready, results, failures


def test_shell_runs_history_query_and_emits_snapshot(db):
    """线程外壳：execute 调 run_history_query，快照同时经 result_ready 与 result 发出"""
    from core.history_query import HistorySnapshot, build_request
    from core.history_refresh_worker import HistoryRefreshWorker
    task = _seed_task(db, samples=5)
    request = build_request(task.task_id, METRIC, '1h', seq=11, table_limit=3)

    worker = HistoryRefreshWorker(db, request)
    assert worker.request is request and worker.request_seq == 11
    ready, results, failures = _run_inline(worker)

    assert len(ready) == 1 and ready == results and failures == []
    snapshot = ready[0]
    assert isinstance(snapshot, HistorySnapshot)
    assert snapshot.request.request_seq == 11
    assert len(snapshot.points) == 3 and snapshot.stats['count'] == 5
    assert snapshot.task_last_at == BASE + timedelta(seconds=4)


def test_query_storage_error_is_reported_via_error_field(db):
    """仓储抛 StorageError：快照的 error 非空且标记为存储故障（不走 failed）"""
    from core.history_query import build_request
    from core.history_refresh_worker import HistoryRefreshWorker
    from data.errors import StorageError
    task = _seed_task(db, samples=5)

    def boom(*a, **k):
        raise StorageError("读取数据点失败: disk I/O error")

    db.points.fetch = boom
    worker = HistoryRefreshWorker(db, build_request(task.task_id, METRIC, 'all', seq=7))
    ready, _results, failures = _run_inline(worker)

    assert len(ready) == 1 and failures == []
    assert ready[0].request.request_seq == 7
    assert ready[0].storage_error is True and "disk I/O error" in ready[0].error


def test_unexpected_error_becomes_failed_snapshot(db):
    """编程错误：记日志、发 failed，result_ready 仍送出 error 非空的快照"""
    from core.history_query import build_request
    from core.history_refresh_worker import HistoryRefreshWorker
    task = _seed_task(db, samples=5)

    def boom(*a, **k):
        raise RuntimeError("db locked")

    db.points.fetch = boom
    worker = HistoryRefreshWorker(db, build_request(task.task_id, METRIC, 'all', seq=5))
    ready, results, failures = _run_inline(worker)

    assert results == [] and failures == ["db locked"]
    assert len(ready) == 1
    assert ready[0].error == "db locked" and ready[0].storage_error is False
    assert ready[0].request.request_seq == 5


def test_bad_arguments_rejected(db):
    from core.history_query import build_request
    from core.history_refresh_worker import HistoryRefreshWorker
    with pytest.raises(TypeError):
        HistoryRefreshWorker(db, 7)
    with pytest.raises(TypeError):
        HistoryRefreshWorker(db, build_request("t", METRIC, 'all'), "extra")
