"""
历史页自动刷新后台化：查询在 QThread 内执行，结果经信号回主线程应用。
覆盖：过期结果丢弃、在途时跳过 tick、应用后统计/图表更新且不重置视图、
worker 结束后被释放、失败/隐藏/同步加载下的丢弃、退出 join。
不依赖固定 sleep：一律轮询等待（tests/helpers.poll_until）。
"""
import threading
from datetime import timedelta

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from PyQt5.QtWidgets import QApplication

from core.history_refresh_worker import HistoryRefreshResult
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
    widget = make_history_page(db, manager, services)
    widget.resize(1200, 800)
    widget._gate = None   # 测试可置入阻塞闸，teardown 统一放行
    yield widget
    if widget._gate is not None:
        widget._gate.set()
    widget._auto_refresh_timer.stop()
    worker = widget._refresh_worker
    if worker is not None:
        worker.wait(10000)
    _pump()
    widget.close()


@pytest.fixture
def shown(page, db, services):
    """已显示、已选中一个 20 点任务的页面；该任务视为运行中，让 _on_auto_refresh
    走后台刷新分支"""
    task = _seed_task(db, samples=20, status="running")
    services.task_status.set_running(task.task_id)
    page.show()
    page.select_task(task.task_id)
    page._auto_refresh_timer.stop()   # 只手动触发，避免定时器干扰
    page.task = task
    return page


def _append_points(db, task, start_index, count):
    db.save_data_points([
        DataPoint(task.task_id, BASE + timedelta(seconds=i), 1024.0 * (100 + i), METRIC)
        for i in range(start_index, start_index + count)])


def _block_table_query(page, db):
    """让工作线程内的表格查询阻塞到测试放行；返回 (entered 事件, 调用计数 list)"""
    gate = threading.Event()
    entered = threading.Event()
    calls = []
    real = db.points.fetch

    def blocking(*a, **k):
        # 只阻塞工作线程；主线程的同步加载（用户主动操作）不受影响
        if threading.current_thread() is not threading.main_thread():
            calls.append(1)
            entered.set()
            gate.wait(10)
        return real(*a, **k)

    db.points.fetch = blocking
    page._gate = gate
    return entered, calls


def _spy_apply(page, monkeypatch):
    applied = []
    real = page._apply_loaded_data

    def spy(*a, **k):
        applied.append(k.get('keep_view', a[-1] if a else None))
        return real(*a, **k)

    monkeypatch.setattr(page, '_apply_loaded_data', spy)
    return applied


# ---------- 结果应用 ----------

def test_result_applied_updates_stats_chart_and_keeps_view(shown, db, monkeypatch):
    page = shown
    task = page.task
    assert len(page._chart_x) > 0
    points_before = len(page._chart_x)
    rows_before = page.data_table.rowCount()

    # 模拟用户缩放：自动刷新不得重置视图
    page.chart_widget.setXRange(page._chart_x[2], page._chart_x[8], padding=0)
    x_range_before = page.chart_widget.viewRange()[0]   # 只比 X（Y 轴自适应是既有行为）
    auto_range_calls = []
    monkeypatch.setattr(
        page.chart_widget, 'enableAutoRange', lambda *a, **k: auto_range_calls.append(1))

    _append_points(db, task, 20, 15)
    page._on_auto_refresh()
    assert _wait(lambda: len(page._chart_x) > points_before)

    assert page.data_table.rowCount() == rows_before + 15
    assert "35" in page.table_hint_label.text()          # 共 35 条
    assert page.stat_value_labels["当前"].text() != "--"
    assert auto_range_calls == []
    assert page.chart_widget.viewRange()[0] == x_range_before


def test_refresh_query_runs_off_main_thread(shown, db):
    """查询在工作线程执行，而不是主线程"""
    page = shown
    threads = []
    real = db.points.fetch

    def spy(*a, **k):
        threads.append(threading.current_thread())
        return real(*a, **k)

    db.points.fetch = spy
    page._on_auto_refresh()
    assert _wait(lambda: page._refresh_worker is None and threads)
    assert threads[0] is not threading.main_thread()


# ---------- 过期结果丢弃 ----------

def _result(page, **overrides):
    base = dict(
        request_id=page._refresh_request_seq, task_id=page.current_task_id,
        metric_type=page.current_metric, range_key=page.current_range_key,
        last_dt=BASE, table_points=[DataPoint(page.current_task_id, BASE, 1.0, METRIC)],
        stats=None, chart_points=[DataPoint(page.current_task_id, BASE, 1.0, METRIC)])
    base.update(overrides)
    return HistoryRefreshResult(**base)


def test_stale_results_are_discarded(shown, monkeypatch):
    page = shown
    applied = _spy_apply(page, monkeypatch)

    page._on_refresh_result(_result(page, request_id=page._refresh_request_seq - 1))
    page._on_refresh_result(_result(page, task_id="other-task"))
    page._on_refresh_result(_result(page, metric_type=MetricType.CPU_PERCENT))
    page._on_refresh_result(_result(page, range_key="10m"))
    page._on_refresh_result(_result(page, error="boom"))
    page._on_refresh_result(_result(page, task_missing=True))
    assert applied == []

    # 标识全部匹配时才应用，且保持视图
    page._on_refresh_result(_result(page))
    assert applied == [True]


def test_result_dropped_when_user_switches_range_while_in_flight(shown, db, monkeypatch):
    page = shown
    entered, _calls = _block_table_query(page, db)
    page._on_auto_refresh()
    assert entered.wait(10)

    applied = _spy_apply(page, monkeypatch)
    page._on_time_range_changed('10m')          # 用户主动操作：走同步加载
    sync_applies = len(applied)
    assert sync_applies == 1

    page._gate.set()
    assert _wait(lambda: page._refresh_worker is None)
    assert len(applied) == sync_applies         # 在途结果已过期，未再应用


def test_result_dropped_after_page_hidden(shown, db, monkeypatch):
    page = shown
    entered, _calls = _block_table_query(page, db)
    page._on_auto_refresh()
    assert entered.wait(10)

    applied = _spy_apply(page, monkeypatch)
    page.hide()                                  # 隐藏不阻塞等待，只让结果过期
    page._gate.set()
    assert _wait(lambda: page._refresh_worker is None)
    assert applied == []


def test_failed_query_keeps_current_display(shown, db, monkeypatch):
    page = shown
    rows = page.data_table.rowCount()
    applied = _spy_apply(page, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db locked")

    db.points.fetch = boom
    page._on_auto_refresh()
    assert _wait(lambda: page._refresh_worker is None)
    assert applied == []
    assert page.data_table.rowCount() == rows


# ---------- 单飞：在途时跳过 tick ----------

def test_tick_skipped_while_worker_in_flight(shown, db):
    page = shown
    entered, calls = _block_table_query(page, db)

    page._on_auto_refresh()
    assert entered.wait(10)
    first = page._refresh_worker
    assert first is not None and first.isRunning()

    page._on_auto_refresh()
    page._on_auto_refresh()
    assert page._refresh_worker is first
    assert calls == [1]                          # 只有一次查询

    page._gate.set()
    assert _wait(lambda: page._refresh_worker is None)

    # 上一个结束后，下一次 tick 才会启动新 worker
    del db.points.fetch   # 撤销实例上的替身，恢复类方法
    page._on_auto_refresh()
    assert page._refresh_worker is not None and page._refresh_worker is not first
    assert _wait(lambda: page._refresh_worker is None)


# ---------- 生命周期 ----------

def test_worker_released_after_finished(shown):
    from PyQt5 import sip
    page = shown
    page._on_auto_refresh()
    worker = page._refresh_worker
    assert worker is not None

    assert _wait(lambda: page._refresh_worker is None)

    def deleted():
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _pump()
        return sip.isdeleted(worker)

    assert poll_until(deleted, timeout=10.0)


def test_shutdown_thread_joins_refresh_worker_with_timeout(shown, db):
    """退出路径：对在途 worker 做带超时 join，超时返回 False、放行后返回 True"""
    page = shown
    entered, _calls = _block_table_query(page, db)
    page._on_auto_refresh()
    assert entered.wait(10)

    worker = page._refresh_worker
    assert shutdown_thread(worker, timeout_ms=50) is False
    page._gate.set()
    assert shutdown_thread(worker, timeout_ms=10000) is True
    assert _wait(lambda: page._refresh_worker is None)


def test_main_window_close_joins_history_refresh_worker(qapp, tmp_path, monkeypatch):
    """关窗真退出时，历史页经 background_threads() 登记的在途刷新线程被带超时
    join：closeEvent 返回时线程已结束（不 join 的话它还要再跑几百毫秒）"""
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
    win.history_page._refresh_worker = worker
    try:
        entries = win.history_page.background_threads()
        assert [(e.thread, e.cancel_fn, e.timeout_ms) for e in entries] ==             [(worker, None, 3000)]

        worker.start()
        assert worker.started_evt.wait(5)
        win.closeEvent(MagicMock())
        assert worker.isFinished()
    finally:
        worker.wait(5000)
        win.history_page._refresh_worker = None
        manager.stop_all_tasks()


# ---------- 存储故障上报 ----------

def test_query_storage_error_is_reported_via_error_field(db):
    """worker 内仓储抛 StorageError：结果的 error 非空且标记为存储故障"""
    from core.history_refresh_worker import HistoryRefreshWorker
    from data.errors import StorageError
    task = _seed_task(db, samples=5)

    def boom(*a, **k):
        raise StorageError("读取数据点失败: disk I/O error")

    db.points.fetch = boom
    worker = HistoryRefreshWorker(db, 7, task.task_id, METRIC, 'all', None, 50, 20)
    results = []
    worker.result_ready.connect(results.append, type=Qt.DirectConnection)
    worker.run()   # 直接在当前线程执行 run，只验证结果打包

    assert len(results) == 1
    assert results[0].request_id == 7
    assert results[0].storage_error is True and "disk I/O error" in results[0].error


def test_auto_refresh_storage_error_notifies_once_and_keeps_display(shown, services):
    """自动刷新读库失败：保持当前显示；连续失败只弹一次"读取数据失败"，成功后复位"""
    page = shown
    rows = page.data_table.rowCount()
    failing = _result(page, error="读取数据点失败: disk I/O error", storage_error=True)

    page._on_refresh_result(failing)
    page._on_refresh_result(_result(page, error="again", storage_error=True))
    errors = services.notifier.messages("error")
    assert [(n.title, n.content) for n in errors] == [
        ("读取数据失败", "读取数据点失败: disk I/O error")]
    assert page.data_table.rowCount() == rows
    assert page.content_stack.currentWidget() is page.analysis_page

    page._on_refresh_result(_result(page))            # 成功一次后复位
    page._on_refresh_result(_result(page, error="x", storage_error=True))
    assert len(services.notifier.messages("error")) == 2

    # 非存储故障（编程错误等）只记日志，不弹提示
    page._on_refresh_result(_result(page, error="boom"))
    assert len(services.notifier.messages("error")) == 2
