"""
首屏进程列表异步：监控页构造不在界面线程读取进程列表；读取在登记过的后台线程里
进行，同一时刻只跑一个、进行中的刷新请求合并为结束后补读一次；列表到达前直接输入
PID 照常可解析，到达后不覆盖用户的输入与选择；页面销毁 / 退出时线程经登记表收尾。

进程表用 BlockingProcessQuery：list_processes 阻塞到测试放行，并记录调用所在线程。
"""
import threading
import time

import pytest
from PyQt5 import sip

from core.monitor_manager import MonitorManager
from core.workers.process_list_worker import (
    SHUTDOWN_TIMEOUT_MS, THREAD_NAME, ProcessListWorker,
)
from tests.doubles import BlockingProcessQuery, FakeProcessQuery
from tests.factories import make_monitor_page, make_services
from tests.helpers import dispose_widget, process_events_until, stop_and_join
from ui.widgets.monitor.process_picker import (
    LOADING_PLACEHOLDER, SEARCH_PLACEHOLDER, ProcessPicker, Target,
)

PROCESSES = [(1001, "alpha.exe", 1001.5), (1002, "beta.exe", 1002.5),
             (1003, "MPrintExp.exe", 1003.5)]

# 构造函数的时间上限：同步读取时会一直阻塞到放行（BlockingProcessQuery 最长 5 秒），1 秒足以区分；
# 太紧会让机器忙（多个测试同时在跑）时偶发失败，构造本身单独跑约 0.05 秒
CONSTRUCT_BUDGET_SECONDS = 1.0


@pytest.fixture
def manager(db):
    mgr = MonitorManager(db=db)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def query():
    q = BlockingProcessQuery(PROCESSES)
    yield q
    q.release.set()   # 用例中途失败也不让后台线程一直阻塞


@pytest.fixture
def services(query):
    return make_services(process_query=query)


@pytest.fixture
def warm(qapp, db, manager):
    """先用不阻塞的进程表构造并释放一次监控页：首次构造要加载样式与字体，
    不应算进"构造函数不等进程列表"的计时里。"""
    page = make_monitor_page(db, manager, make_services(process_query=FakeProcessQuery()))
    assert process_events_until(lambda: not page.form.picker.is_loading())
    dispose_widget(page)


@pytest.fixture
def page(warm, db, manager, services, query):
    started = time.perf_counter()
    widget = make_monitor_page(db, manager, services)
    widget.construct_seconds = time.perf_counter() - started
    widget.resize(900, 700)
    widget.show()
    yield widget
    query.release.set()
    process_events_until(lambda: not services.threads.entries())
    dispose_widget(widget)


def _wait_loaded(page):
    assert process_events_until(lambda: not page.form.picker.is_loading()
                                and not page.services.threads.entries())


# ---------- 构造函数不读取进程列表 ----------

def test_constructor_returns_without_listing_on_ui_thread(page, query, services):
    assert page.construct_seconds < CONSTRUCT_BUDGET_SECONDS
    assert query.entered.wait(5), "首屏应发起一次后台读取"
    assert query.list_threads and threading.get_ident() not in query.list_threads

    entry, = services.threads.entries()
    assert isinstance(entry.thread, ProcessListWorker)
    assert (entry.name, entry.timeout_ms) == (THREAD_NAME, SHUTDOWN_TIMEOUT_MS)
    assert entry.cancel is not None


def test_loading_state_then_entries_arrive_via_event_loop(page, query):
    picker = page.form.picker
    assert query.entered.wait(5)
    assert picker.is_loading() and picker.targets() == []
    assert picker.combo.line_edit.placeholderText() == LOADING_PLACEHOLDER
    assert not page.refresh_button.isEnabled()

    query.release.set()
    _wait_loaded(page)

    assert [t.pid for t in picker.targets()] == [1001, 1002, 1003]
    assert picker.text() == "" and picker.combo.current_data() is None
    assert picker.combo.line_edit.placeholderText() == SEARCH_PLACEHOLDER
    assert page.refresh_button.isEnabled()


# ---------- 列表到达前后的用户输入 ----------

def test_typed_pid_resolves_before_list_arrives(page, query):
    picker = page.form.picker
    assert query.entered.wait(5) and picker.targets() == []

    picker.set_text("1002")
    assert picker.resolve() == Target(1002, "beta.exe", None)

    query.release.set()
    _wait_loaded(page)
    assert picker.text() == "1002"
    assert picker.resolve() == Target(1002, "beta.exe", None)


def test_arrival_keeps_typed_text_cursor_and_error(page, query):
    picker = page.form.picker
    assert query.entered.wait(5)
    picker.set_text("bet")
    picker.combo.line_edit.setCursorPosition(1)
    picker.set_error("请选择进程")

    query.release.set()
    _wait_loaded(page)

    assert picker.text() == "bet" and picker.combo.current_data() is None
    assert picker.combo.line_edit.cursorPosition() == 1
    assert picker.error_text() == "请选择进程", "列表到达不是用户编辑，不应清掉校验错误"
    assert len(picker.targets()) == 3


def test_refresh_keeps_selected_target(page, query):
    picker = page.form.picker
    query.release.set()
    _wait_loaded(page)
    assert picker.combo.select_data(next(t for t in picker.targets() if t.pid == 1003))

    query.release.clear()
    query.processes.append((1004, "gamma.exe", 1004.5))
    page.refresh_processes()
    assert picker.is_loading()
    query.release.set()
    _wait_loaded(page)

    assert picker.resolve() == Target(1003, "MPrintExp.exe", 1003.5)
    assert Target(1004, "gamma.exe", 1004.5) in picker.targets()


# ---------- 同一时刻只跑一个，进行中的请求合并 ----------

def test_repeated_refresh_runs_one_worker_then_at_most_one_more(page, query, services):
    assert query.entered.wait(5)
    for _ in range(3):
        page.form.picker.refresh()
        assert len(services.threads.entries()) == 1
    page.refresh_processes()   # F5 / 按钮同一路径
    assert len(services.threads.entries()) == 1

    query.release.set()
    _wait_loaded(page)
    assert query.list_calls() == 2, "首屏一次 + 合并后的补读一次"


def test_refresh_button_disabled_while_loading(page, query):
    assert query.entered.wait(5)
    assert not page.refresh_button.isEnabled()
    query.release.set()
    _wait_loaded(page)
    assert page.refresh_button.isEnabled()

    query.release.clear()
    page.refresh_button.click()
    assert not page.refresh_button.isEnabled()
    query.release.set()
    _wait_loaded(page)
    assert page.refresh_button.isEnabled()
    assert query.list_calls() == 2


def test_stale_result_is_dropped(page, query):
    """发起后被作废的读取结果不应用（LatestRequestGate）"""
    picker = page.form.picker
    assert query.entered.wait(5)
    picker._gate.invalidate()
    query.release.set()
    _wait_loaded(page)
    assert picker.targets() == []
    assert not picker.is_loading()


def test_failed_listing_keeps_old_list_and_ends_loading(page, query):
    picker = page.form.picker
    query.release.set()
    _wait_loaded(page)

    query.fail_with = OSError("拒绝访问")
    page.refresh_processes()
    _wait_loaded(page)

    assert [t.pid for t in picker.targets()] == [1001, 1002, 1003]
    assert picker.combo.line_edit.placeholderText() == SEARCH_PLACEHOLDER
    assert page.refresh_button.isEnabled()


def test_refresh_if_stale_throttle_unchanged(page, query):
    """首屏读取算一次刷新：紧接着的显示 / 激活不重复读取"""
    assert query.entered.wait(5)
    assert page.form.picker.refresh_if_stale() is False
    query.release.set()
    _wait_loaded(page)
    assert query.list_calls() == 1


# ---------- 收线程 ----------

def test_page_destroyed_while_loading_worker_is_reaped_by_registry(
        warm, db, manager, services, query):
    page = make_monitor_page(db, manager, services)
    assert query.entered.wait(5)
    worker = services.threads.entries()[0].thread

    dispose_widget(page)
    assert worker.is_cancelled, "页面销毁后读取结果作废"
    assert worker.isRunning()

    query.release.set()
    assert process_events_until(lambda: not services.threads.entries())
    assert process_events_until(lambda: sip.isdeleted(worker))


def test_shutdown_waits_for_running_worker(page, query, services):
    assert query.entered.wait(5)
    worker = services.threads.entries()[0].thread
    threading.Timer(0.1, query.release.set).start()

    assert services.threads.shutdown_all() is True
    assert worker.isFinished() and worker.is_cancelled


# ---------- 线程与控件单元 ----------

def test_worker_emits_entries_with_request_seq(qapp):
    q = FakeProcessQuery(PROCESSES)
    worker = ProcessListWorker(q, request_seq=7)
    got = []
    worker.entries_ready.connect(lambda seq, entries: got.append((seq, entries)))
    worker.start()
    assert worker.wait(5000)
    assert process_events_until(lambda: got)
    assert got == [(7, sorted(PROCESSES, key=lambda e: e[1].lower()))]


def test_worker_failure_reports_message_without_entries(qapp):
    q = BlockingProcessQuery(PROCESSES)
    q.release.set()
    q.fail_with = OSError("拒绝访问")
    worker = ProcessListWorker(q, request_seq=1)
    got, failed = [], []
    worker.entries_ready.connect(lambda *a: got.append(a))
    worker.failed.connect(failed.append)
    worker.start()
    assert worker.wait(5000)
    assert process_events_until(lambda: failed)
    assert got == [] and failed == ["读取进程列表失败：拒绝访问"]


def test_picker_without_registry_stays_synchronous(qapp):
    q = BlockingProcessQuery(PROCESSES)
    q.release.set()
    picker = ProcessPicker(q)
    picker.refresh()
    assert [t.pid for t in picker.targets()] == [1001, 1002, 1003]
    assert q.list_threads == [threading.get_ident()]
    assert not picker.is_loading()
    picker.deleteLater()
