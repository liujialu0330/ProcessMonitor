"""卡片基线变更后的后台聚合、实时追加与旧结果隔离。"""
import threading
from datetime import datetime, timedelta

import pytest

from core.history_query import build_request, run_history_query
from core.contracts import StopReason
from core.monitor_manager import MonitorManager
from core.workers.base import CancelToken
from core.workers.live_stats_worker import LiveStatsRequest, LiveStatsWorker
from data.models import DataPoint
from tests.factories import make_monitor_page, make_services
from tests.helpers import dispose_widget, process_events_until
from ui.live_stats import LiveStats

START = datetime(2026, 11, 13)
METRIC = 'num_handles'


@pytest.fixture
def monitor(qapp, db):
    manager = MonitorManager(db=db, target_probe=lambda *_a, **_kw: None)
    services = make_services()
    page = make_monitor_page(db, manager, services)
    task_id = manager.create_task(123, 'sample.exe', [METRIC], 1)
    worker = manager.get_task(task_id)
    worker.task_model.start_time = START
    db.tasks.save(worker.task_model)
    card = page.board.card(task_id)
    yield page, manager, services, task_id, card
    page.presenter.shutdown_live_stats()
    services.threads.cancel_all()
    services.threads.shutdown_all()
    manager.stop_all_tasks()
    manager.join_all(1000)
    manager.remove_task(task_id)
    dispose_widget(page)


def samples(db, manager, task_id, start, count, value):
    points = [DataPoint(task_id, START + timedelta(seconds=i), value(i), METRIC)
              for i in range(start, start + count)]
    assert db.save_data_points(points)
    for point in points:
        manager.sample_updated.emit(task_id, {METRIC: point.value}, point.timestamp)


def settled(page, services):
    return process_events_until(lambda: not page.presenter._live_workers
                                and not services.threads.entries())


def test_removing_baseline_restores_full_history_and_retroactive_baseline(qapp, db, monitor):
    page, manager, services, task_id, card = monitor
    samples(db, manager, task_id, 0, 601, lambda i: 100 + i)
    assert card.live_verdict() == '持续上涨'
    marker = manager.add_marker(task_id, '基线', START + timedelta(seconds=600))
    assert settled(page, services)
    samples(db, manager, task_id, 601, 31, lambda _i: 700)
    assert manager.delete_marker(marker.id, task_id)
    assert settled(page, services)
    expected = run_history_query(db, build_request(task_id, METRIC, 'all', analysis=True))
    assert expected.judgement.verdict.value == 'rising'
    assert card.live_verdict() == '持续上涨'
    assert card.live_stats.rows[METRIC][2] == expected.stats['count'] == 632
    # 把较早的打点改作基线，重建范围应包括已落库的基线后样本。
    old = manager.add_marker(task_id, '历史轮次', START + timedelta(seconds=200))
    assert manager.set_baseline(task_id, old.id)
    assert settled(page, services)
    expected = run_history_query(db, build_request(task_id, METRIC, 'all', analysis=True))
    assert card.live_stats.rows[METRIC][2] == expected.stats['count'] == 432
    assert card.live_stats.rows[METRIC][0] == START + timedelta(seconds=200)


class ManualWorker(LiveStatsWorker):
    def start(self):
        pass

    def deliver(self, result):
        self.result.emit(result)
        self.finished.emit()


def manual_factory(page):
    workers = []

    def create(db, request):
        worker = ManualWorker(db, request)
        workers.append(worker)
        return worker

    page.presenter._live_worker_factory = create
    return workers


def test_sample_emitted_before_flush_is_merged_once_with_query_result(db, monitor):
    page, manager, services, task_id, card = monitor
    samples(db, manager, task_id, 0, 601, lambda i: i)
    workers = manual_factory(page)
    manager.add_marker(task_id, '基线', START + timedelta(seconds=300))
    result = workers[-1].execute(CancelToken())
    # 真实采集先发信号再落库；结果可能已经包含或尚未包含这一点。
    when = START + timedelta(seconds=601, microseconds=499)
    manager.sample_updated.emit(task_id, {METRIC: 601}, when)
    workers[-1].deliver(result)
    assert card.live_stats.rows[METRIC][2] == 302
    assert card.live_stats.rows[METRIC][1] == START + timedelta(seconds=601)
    assert db.save_data_points([DataPoint(task_id, when, 601, METRIC)])
    page.presenter._request_live_stats(task_id)
    workers[-1].deliver(workers[-1].execute(CancelToken()))
    assert card.live_stats.rows[METRIC][2] == 302  # 已在聚合中的缓存样本不重复。


def test_old_baseline_restart_and_closed_card_reject_inflight_results(db, monitor):
    page, manager, services, task_id, card = monitor
    samples(db, manager, task_id, 0, 700, lambda i: i)
    workers = manual_factory(page)
    first = manager.add_marker(task_id, '基线', START + timedelta(seconds=100))
    old = workers[-1]
    old_result = old.execute(CancelToken())
    manager.add_marker(task_id, '基线', START + timedelta(seconds=500))
    latest = workers[-1]
    old.deliver(old_result)
    assert card.live_stats.needs_rebuild
    latest.deliver(latest.execute(CancelToken()))
    assert card.live_stats.rows[METRIC][2] == 200
    # 重启后的有效段优先于旧基线，即使基线标记本身没变也必须重建。
    db.markers.add(task_id, START + timedelta(seconds=650), '重启', kind='restart')
    manager.markers_changed.emit(task_id)
    newest = workers[-1]
    newest.deliver(newest.execute(CancelToken()))
    assert card.live_stats.rows[METRIC][2] == 50
    page.presenter._request_live_stats(task_id)
    pending = workers[-1]
    payload = pending.execute(CancelToken())
    page.presenter.on_close(task_id)
    assert pending.is_cancelled
    pending.deliver(payload)
    assert task_id not in page.presenter._live_workers


def test_history_rebuild_runs_off_ui_and_registry_cancel_suppresses_result(db, monitor, monkeypatch):
    page, manager, services, task_id, card = monitor
    samples(db, manager, task_id, 0, 601, lambda i: i)
    entered, release = threading.Event(), threading.Event()
    threads = []
    original = db.analytics.aggregates

    def blocked(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(db.analytics, 'aggregates', blocked)
    # _request_live_stats只发后台请求，不会在本线程等待数据库。
    page.presenter._request_live_stats(task_id)
    assert entered.wait(2)
    services.threads.cancel_all()
    release.set()
    assert settled(page, services)
    assert threads and all(thread != threading.get_ident() for thread in threads)
    assert card.live_stats.needs_rebuild  # 已取消的结果不能恢复为有效判定。


def test_recent_buffer_overflow_never_publishes_incomplete_history(db, monitor):
    page, manager, services, task_id, card = monitor
    card.live_stats.RECENT_LIMIT = 2
    for i in range(10):
        card.update_values({METRIC: i}, START + timedelta(seconds=i))
    card.live_stats.invalidate()
    empty = db.analytics.aggregates(task_id, METRIC)
    assert not card.restore_live_stats({METRIC: empty})
    assert card.live_stats.needs_rebuild
    assert len(card.live_stats._recent[METRIC]) == 2
    db.save_data_points([DataPoint(task_id, START + timedelta(seconds=i), i, METRIC)
                         for i in range(9)])
    assert card.restore_live_stats({METRIC: db.analytics.aggregates(task_id, METRIC)})
    assert card.live_stats.rows[METRIC][2] == 10


def test_duplicate_timestamp_waits_for_corrected_value_to_reach_database(db, monitor):
    page, manager, services, task_id, card = monitor
    samples(db, manager, task_id, 0, 601, lambda i: i)
    workers = manual_factory(page)
    page.presenter._request_live_stats(task_id)
    previous = workers[-1].execute(CancelToken())
    when = START + timedelta(seconds=600, microseconds=499)
    manager.sample_updated.emit(task_id, {METRIC: 900}, when)
    workers[-1].deliver(previous)
    assert card.live_stats.needs_rebuild
    assert db.save_data_points([DataPoint(task_id, when, 900, METRIC)])
    page.presenter._request_live_stats(task_id)
    workers[-1].deliver(workers[-1].execute(CancelToken()))
    assert not card.live_stats.needs_rebuild
    assert card.live_stats.rows[METRIC][2] == 601
    assert card.live_stats.rows[METRIC][9] == 900


def test_shutdown_discards_queued_results_and_prevents_late_rebuilds(db, monitor):
    page, manager, services, task_id, card = monitor
    samples(db, manager, task_id, 0, 601, lambda i: i)
    workers = manual_factory(page)
    page.presenter._request_live_stats(task_id)
    worker = workers[-1]
    queued = worker.execute(CancelToken())
    page.presenter.shutdown_live_stats()
    page.presenter.shutdown_live_stats()
    worker.deliver(queued)
    assert worker.is_cancelled
    assert card.live_stats.needs_rebuild
    manager.sample_updated.emit(task_id, {METRIC: 601}, START + timedelta(seconds=601))
    page.presenter._request_live_stats(task_id)
    assert len(workers) == 1
    assert not page.presenter._live_workers


@pytest.mark.parametrize('state', ['paused', 'finished'])
@pytest.mark.parametrize('failure', ['watermark', 'query'])
def test_delayed_rebuild_recovers_without_new_samples(db, monitor, monkeypatch, state, failure):
    page, manager, services, task_id, card = monitor
    monkeypatch.setattr('ui.presenters.monitor_presenter.LIVE_STATS_RETRY_DELAYS_MS', (1,))
    samples(db, manager, task_id, 0, 601, lambda i: i)
    workers = manual_factory(page)
    when = START + timedelta(seconds=600, microseconds=499)
    manager.sample_updated.emit(task_id, {METRIC: 900}, when)
    if state == 'finished':
        previous = workers[-1]
        manager.task_stopped.emit(task_id, StopReason.USER)
        previous.finished.emit()
        assert card.is_finished
    else:
        page.presenter._show_paused(task_id, True)
    current = workers[-1]
    if failure == 'watermark':
        current.result.emit(current.execute(CancelToken()))
    else:
        current.failed.emit('暂时无法读取数据库')
    assert card.live_stats.needs_rebuild
    # 结果抵达时线程尚未结束：此时不应并发启动另一个查询。
    assert not page.presenter._live_retries
    assert page.presenter._live_workers[task_id] is current
    current.finished.emit()
    assert task_id in page.presenter._live_retries
    previous_count = len(workers)
    assert db.save_data_points([DataPoint(task_id, when, 900, METRIC)])
    # 只有时间推进，既不恢复监控也不再发采样，落库后仍自动补一次查询。
    assert process_events_until(lambda: len(workers) == previous_count + 1)
    workers[-1].deliver(workers[-1].execute(CancelToken()))
    assert not card.live_stats.needs_rebuild
    assert card.live_stats.rows[METRIC][2] == 601
    assert card.live_stats.rows[METRIC][9] == 900
    assert not page.presenter._live_retries


def test_live_stats_retry_is_bounded_and_samples_do_not_skip_backoff(db, monitor):
    from ui.presenters.monitor_presenter import LIVE_STATS_RETRY_DELAYS_MS

    page, manager, services, task_id, card = monitor
    card.live_stats.RECENT_LIMIT = 2
    for i in range(10):
        card.update_values({METRIC: i}, START + timedelta(seconds=i))
    workers = manual_factory(page)
    page.presenter._request_live_stats(task_id)
    workers[-1].deliver(workers[-1].execute(CancelToken()))
    for attempt, delay in enumerate(LIVE_STATS_RETRY_DELAYS_MS):
        sequence, next_attempt, timer = page.presenter._live_retries[task_id]
        assert next_attempt == attempt + 1
        assert timer.interval() == delay and delay > 0
        count = len(workers)
        manager.sample_updated.emit(task_id, {METRIC: 10 + attempt},
                                    START + timedelta(seconds=10 + attempt))
        assert len(workers) == count  # 在途落库期间采样不能绕过退避。
        timer.stop()
        timer.timeout.emit()
        assert len(workers) == count + 1
        assert page.presenter._live_generations[task_id] != sequence
        workers[-1].deliver(workers[-1].execute(CancelToken()))
    assert len(workers) == 1 + len(LIVE_STATS_RETRY_DELAYS_MS)
    assert not page.presenter._live_workers
    assert not page.presenter._live_retries
    assert card.live_stats.needs_rebuild


@pytest.mark.parametrize('action', ['close', 'restart', 'shutdown'])
def test_late_retry_timeout_cannot_outlive_card_or_generation(db, monitor, action):
    page, manager, services, task_id, card = monitor
    card.live_stats.RECENT_LIMIT = 2
    for i in range(10):
        card.update_values({METRIC: i}, START + timedelta(seconds=i))
    workers = manual_factory(page)
    page.presenter._request_live_stats(task_id)
    workers[-1].deliver(workers[-1].execute(CancelToken()))
    _sequence, _attempt, timer = page.presenter._live_retries[task_id]
    if action == 'close':
        page.presenter.on_close(task_id)
    elif action == 'shutdown':
        page.presenter.shutdown_live_stats()
    else:
        db.markers.add(task_id, START + timedelta(seconds=9), '重启', kind='restart')
        manager.task_restarted.emit(task_id, 1)
    count = len(workers)
    assert not timer.isActive()
    timer.timeout.emit()  # stop 之前已经排队的 timeout 也必须被忽略。
    assert len(workers) == count
    assert not page.presenter._live_retries
    if action == 'restart':
        workers[-1].deliver(workers[-1].execute(CancelToken()))
