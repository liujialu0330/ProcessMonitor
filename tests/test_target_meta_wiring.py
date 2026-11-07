"""
被测目标元数据的接线（D-3）：任务启动后 manager 在后台线程探测（注入假探针），结果写进
tasks.meta_json；任务行晚于探测结果落库时，在首个 data_updated（或 task_stopped）补存；
退出路径的 join_all 当场交付并补存；探针异常不影响任务；显示缩放由主窗口经 UiServices
交给 manager；监控页卡片的悬停提示追加被测程序摘要行。
"""
import os
import threading
import time

import pytest
from PyQt5.QtCore import Qt

import config
from core.monitor_manager import MonitorManager
from core.target_info import TargetInfo
from tests.factories import make_main_window, make_monitor_page, make_services
from tests.helpers import dispose_widget, process_events_until, stop_and_join
from utils.metrics import MetricType

METRICS = [MetricType.MEMORY_PRIVATE]
EXE = r"C:\Apps\demo.exe"


class FakeProbe:
    """记录调用参数的假探针；delay 模拟耗时，error 模拟探针抛异常。"""

    def __init__(self, *, delay: float = 0.0, error: Exception = None):
        self.calls = []
        self.delay = delay
        self.error = error
        self.threads = []

    def __call__(self, pid, *, clock, dpi_scale=None):
        self.calls.append({'pid': pid, 'dpi_scale': dpi_scale})
        self.threads.append(threading.current_thread())
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return TargetInfo(pid=pid, exe_path=EXE, file_version="1.2.3.4",
                          tool_version=config.APP_VERSION, dpi_scale=dpi_scale,
                          captured_at=clock.now())


@pytest.fixture
def probe():
    return FakeProbe()


@pytest.fixture
def manager(qapp, db, probe):
    mgr = MonitorManager(db=db, target_probe=probe)
    yield mgr
    stop_and_join(mgr)


def _start(manager, interval=0.5):
    task_id = manager.create_task(os.getpid(), "python.exe", METRICS, interval=interval)
    assert task_id and manager.start_task(task_id)
    return task_id


def _ready_recorder(manager):
    ready = []
    manager.target_info_ready.connect(lambda tid, info: ready.append((tid, info)))
    return ready


def _gate_save_task(db, monkeypatch):
    """让采集线程启动时写任务行阻塞到测试放行（模拟任务行晚于探测结果落库）。"""
    gate = threading.Event()
    real = db.save_task

    def gated(task):
        if threading.current_thread() is not threading.main_thread():
            gate.wait(10)
        return real(task)

    monkeypatch.setattr(db, 'save_task', gated)
    return gate


def test_meta_saved_after_start_in_background(manager, db, probe):
    ready = _ready_recorder(manager)
    task_id = _start(manager)
    assert process_events_until(lambda: db.tasks.load_meta(task_id) is not None, timeout=10)
    info = TargetInfo.from_json(db.tasks.load_meta(task_id))
    assert info.pid == os.getpid() and info.exe_path == EXE and info.file_version == "1.2.3.4"
    # 探针不在界面线程里跑
    assert probe.threads and probe.threads[0] is not threading.main_thread()
    assert ready and ready[0][0] == task_id and ready[0][1].exe_path == EXE
    assert manager.target_info(task_id) == ready[0][1]
    assert db.get_task(task_id).meta_json == db.tasks.load_meta(task_id)


def test_meta_retried_on_first_data_when_row_written_late(manager, db, monkeypatch):
    gate = _gate_save_task(db, monkeypatch)
    ready = _ready_recorder(manager)
    task_id = _start(manager)
    try:
        assert process_events_until(lambda: ready, timeout=10)
        # 探测结果先到：任务行还不存在，写不进去，挂起
        assert db.get_task(task_id) is None
        assert db.tasks.load_meta(task_id) is None
    finally:
        gate.set()
    # 任务仍在运行时（首个采集周期到达后）就已补存，不必等到停止
    assert process_events_until(lambda: db.tasks.load_meta(task_id) is not None, timeout=10)
    assert manager.is_task_active(task_id)
    assert TargetInfo.from_json(db.tasks.load_meta(task_id)).exe_path == EXE


def test_meta_retried_on_stop_when_no_sample_was_collected(manager, db, monkeypatch):
    gate = _gate_save_task(db, monkeypatch)
    ready = _ready_recorder(manager)
    seen_at_stop = []
    task_id = _start(manager, interval=5.0)
    # 接在 manager 的处理之后：收到 task_stopped 时元数据应已写进库
    manager.task_stopped.connect(
        lambda tid, _reason: seen_at_stop.append(db.tasks.load_meta(tid)), Qt.DirectConnection)
    try:
        assert process_events_until(lambda: ready, timeout=10)
        assert manager.stop_task(task_id)
    finally:
        gate.set()
    assert process_events_until(lambda: seen_at_stop, timeout=10)
    assert seen_at_stop[0] is not None
    assert TargetInfo.from_json(seen_at_stop[0]).exe_path == EXE


def test_join_all_delivers_and_saves_pending_probe(qapp, db):
    """退出路径：探针还没结束就 join_all，返回时元数据已落库（不靠事件循环）。"""
    manager = MonitorManager(db=db, target_probe=FakeProbe(delay=0.3))
    task_id = _start(manager)
    assert stop_and_join(manager)
    assert db.tasks.load_meta(task_id) is not None


def test_probe_failure_does_not_affect_task(qapp, db):
    probe = FakeProbe(error=RuntimeError("探针炸了"))
    manager = MonitorManager(db=db, target_probe=probe)
    ready = _ready_recorder(manager)
    updates = []
    manager.data_updated.connect(lambda tid, values: updates.append(tid))
    try:
        task_id = _start(manager)
        assert process_events_until(lambda: updates, timeout=10)
        assert probe.calls and manager.is_task_active(task_id)
        assert ready == [] and manager.target_info(task_id) is None
        assert db.tasks.load_meta(task_id) is None
    finally:
        assert stop_and_join(manager)


def test_dpi_scale_provider(manager, probe):
    manager.set_dpi_scale_provider(lambda: 1.5)
    _start(manager)
    manager.set_dpi_scale_provider(lambda: (_ for _ in ()).throw(RuntimeError("无屏幕")))
    _start(manager)
    assert process_events_until(lambda: len(probe.calls) == 2, timeout=10)
    assert [c['dpi_scale'] for c in probe.calls] == [1.5, None]


def test_main_window_passes_dpi_scale_to_manager(manager, db, probe):
    window = make_main_window(db, manager, make_services(dpi_scale=lambda: 1.25))
    try:
        window.about_group.check_update = lambda *a, **k: None
        _start(manager)
        assert process_events_until(lambda: probe.calls, timeout=10)
        assert probe.calls[0]['dpi_scale'] == 1.25
    finally:
        dispose_widget(window)


def test_default_probe_records_real_process(qapp, db):
    """生产装配（不注入探针）：探测的是真实的被测进程（这里是测试进程自己）。"""
    manager = MonitorManager(db=db)
    try:
        task_id = _start(manager)
        assert process_events_until(lambda: db.tasks.load_meta(task_id) is not None, timeout=15)
    finally:
        assert stop_and_join(manager)
    info = TargetInfo.from_json(db.tasks.load_meta(task_id))
    assert info.pid == os.getpid()
    assert os.path.normcase(os.path.basename(info.exe_path or '')).startswith('python')
    assert info.tool_version == config.APP_VERSION and info.captured_at is not None


def test_monitor_card_tooltip_shows_target(manager, db):
    page = make_monitor_page(db, manager)
    try:
        task_id = _start(manager)
        assert process_events_until(
            lambda: page.board.card(task_id) is not None and EXE in page.board.card(task_id).toolTip(),
            timeout=10)
        assert page.board.card(task_id).target_summary()[0] == f"被测程序: {EXE}（文件版本 1.2.3.4）"
    finally:
        dispose_widget(page)
