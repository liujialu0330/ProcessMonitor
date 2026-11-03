"""
历史页自动刷新后台化：查询在 QThread 内执行，结果经信号回主线程应用。
覆盖：过期结果丢弃、在途时跳过 tick、应用后统计/图表更新且不重置视图、
worker 结束后被释放、失败/隐藏/同步加载下的丢弃、退出 join。
不依赖固定 sleep：一律轮询等待（tests/helpers.poll_until）。
"""
import threading
from datetime import timedelta

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtWidgets import QApplication

import ui.pages.history_page as history_module
from core.history_refresh_worker import HistoryRefreshResult
from core.monitor_manager import MonitorManager
from data.models import DataPoint
from tests.helpers import poll_until
from tests.test_history_page_flow import BASE, _seed_task
from ui.pages.history_page import HistoryPage
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
def page(qapp, db, manager, monkeypatch):
    widget = HistoryPage(db=db, manager=manager)
    widget.resize(1200, 800)
    # 默认视为运行中，让 _on_auto_refresh 走后台刷新分支
    monkeypatch.setattr(widget, '_is_task_running', lambda tid: True)
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
def shown(page, db):
    """已显示、已选中一个 20 点任务的页面"""
    task = _seed_task(db, samples=20, status="running")
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
    real = db.get_task_data_points

    def blocking(*a, **k):
        # 只阻塞工作线程；主线程的同步加载（用户主动操作）不受影响
        if threading.current_thread() is not threading.main_thread():
            calls.append(1)
            entered.set()
            gate.wait(10)
        return real(*a, **k)

    db.get_task_data_points = blocking
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
    real = db.get_task_data_points

    def spy(*a, **k):
        threads.append(threading.current_thread())
        return real(*a, **k)

    db.get_task_data_points = spy
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

    db.get_task_data_points = boom
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
    db.get_task_data_points = type(db).get_task_data_points.__get__(db)
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


def test_main_window_close_joins_history_refresh_worker():
    """MainWindow.closeEvent 的清理步骤里包含对历史页刷新线程的 join"""
    import inspect
    from ui.main_window import MainWindow
    source = inspect.getsource(MainWindow.closeEvent)
    assert "_refresh_worker" in source and "history_page" in source
