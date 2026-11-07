"""
等待重启 / 续接 / 续采的加固用例（假时钟 + 假探针 + 假采集器 + 假查找）。

- 手动输入 PID 的任务：首次启动时把采集器读到的创建时间回填到任务行与内存，之后的 PID
  复用检查、续采与重启查找都以它为准；
- 退出码句柄的创建时间与预期不符 = PID 在确认存在与取句柄之间被复用，任务以 PID_REUSED
  结束；续接时候选进程的句柄对不上则放弃它继续等；
- 等待重启中途出错（如封口旧进程树时抛异常）：记日志、按退出原因结束任务，线程不能空转；
  即使异常绕过了包装，主循环顶部的兜底也要接住"仍是等待重启"的状态；
- 进入等待前任务处于暂停：续接成功后仍然暂停，不能悄悄恢复采集；
- 工具重启后续采：数据质量在库里已有值上累加，不是覆盖。
"""
import threading
import time
import uuid
from datetime import datetime

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import MonitorWorker, WorkerState
from core.process_tree import GroupSpec
from core.restart_finder import RestartHit
from data.models import MonitorTask, QualityStats
from data.repos.point_repo import PointQuery
from tests.helpers import poll_until
from tests.soak_doubles import (ACCESS_VIOLATION, METRICS, T0, ScriptedFinder, SegmentCollector,
                                build_rig, run_to_end)

METRIC = 'memory_private'
SHORT_JOIN_MS = 8000


@pytest.fixture
def started(qapp):
    workers = []
    yield workers
    for w in workers:
        w.stop()
        w.wait(5000)


def _values(db, task_id):
    return [p.value for p in db.points.fetch(PointQuery(task_id, METRIC))]


# ========== 手动 PID：创建时间回填 ==========

def test_manual_pid_create_time_is_backfilled_to_row_and_memory(db, started):
    rig = build_rig(db, create_time=None, segments={100: [1, 2]}, codes={100: 0}, resume=False)
    rig.collectors[100].create_time = 5000.0
    run_to_end(rig.worker, started)

    assert rig.worker.expected_create_time == 5000.0
    assert rig.worker.task_model.create_time == 5000.0
    assert db.tasks.get(rig.worker.task_id).create_time == 5000.0


def test_backfilled_create_time_is_used_when_looking_for_the_restart(db, started):
    finder = ScriptedFinder({1: RestartHit(200, 5100.0, 'app.exe')})
    rig = build_rig(db, create_time=None, segments={100: [1], 200: [2]},
                    codes={100: ACCESS_VIOLATION, 200: 0}, finder=finder, resume=True, limit=1)
    rig.collectors[100].create_time = 5000.0
    rig.collectors[200].create_time = 5100.0
    run_to_end(rig.worker, started)

    assert finder.targets[0].last_create_time == 5000.0
    assert rig.restarts == [(200, 1)]
    assert db.tasks.get(rig.worker.task_id).create_time == 5100.0


def test_unknown_collector_create_time_leaves_row_empty(db, started):
    rig = build_rig(db, create_time=None, segments={100: [1]}, codes={100: 0}, resume=False)
    run_to_end(rig.worker, started)
    assert rig.worker.expected_create_time is None
    assert db.tasks.get(rig.worker.task_id).create_time in (None, 0)


# ========== 句柄创建时间核对 ==========

def test_handle_with_other_create_time_means_pid_reused(db, started):
    rig = build_rig(db, create_time=5000.0, segments={100: [1, 2]}, codes={100: 0}, resume=False)
    rig.collectors[100].create_time = 5000.0
    rig.probes[100].create_time_value = 7777.0      # 取句柄时 PID 已被另一个进程占用
    run_to_end(rig.worker, started)

    assert rig.stops == [StopReason.PID_REUSED]
    assert _values(db, rig.worker.task_id) == []
    assert rig.probes[100].closed >= 1


def test_handle_with_matching_create_time_is_accepted(db, started):
    rig = build_rig(db, create_time=5000.0, segments={100: [1, 2]}, codes={100: 0}, resume=False)
    rig.collectors[100].create_time = 5000.0
    rig.probes[100].create_time_value = 5000.004    # 在容差内
    run_to_end(rig.worker, started)
    assert rig.stops == [StopReason.PROCESS_EXITED]
    assert _values(db, rig.worker.task_id) == [1.0, 2.0]


def test_restart_candidate_whose_handle_does_not_match_is_skipped(db, started):
    """找到的候选在被接上前一刻已退出、PID 被别的进程占用：放弃它，继续等下一个"""
    finder = ScriptedFinder({1: RestartHit(200, 6000.0, 'app.exe'),
                             2: RestartHit(300, 6100.0, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2], 300: [3]},
                    codes={100: ACCESS_VIOLATION, 200: 0, 300: 0},
                    finder=finder, resume=True, limit=1)
    rig.collectors[200].create_time = 6000.0
    rig.collectors[300].create_time = 6100.0
    rig.probes[200].create_time_value = 1.0         # 对不上
    rig.probes[300].create_time_value = 6100.0
    run_to_end(rig.worker, started)

    assert rig.restarts == [(300, 1)]
    assert rig.probes[200].closed >= 1
    assert _values(db, rig.worker.task_id) == [1.0, 3.0]


# ========== 等待重启中途出错 ==========

class _StateReads:
    """统计 MonitorWorker.state 被读取的次数：主循环空转时它会飙到成千上万。"""

    def __init__(self, monkeypatch):
        self.count = 0
        original = MonitorWorker.state.fget

        def counting(worker):
            self.count += 1
            return original(worker)

        monkeypatch.setattr(MonitorWorker, 'state', property(counting))


def _join_or_fail(worker, rig_stops):
    ended = worker.wait(SHORT_JOIN_MS)
    if not ended:
        worker.stop()
        worker.wait(5000)
    assert ended, "线程没有在时限内结束（空转？）"


def test_error_while_releasing_tree_members_ends_task_without_spinning(db, started, monkeypatch):
    reads = _StateReads(monkeypatch)
    rig = build_rig(db, segments={100: [1, 2]}, codes={100: ACCESS_VIOLATION},
                    finder=ScriptedFinder(), resume=True, limit=3,
                    group_spec=GroupSpec.tree(100, 1.0))

    def boom():
        raise RuntimeError("封口旧进程树失败")

    rig.worker._release_group_instances = boom
    errors = []
    rig.worker.error_occurred.connect(lambda _t, text: errors.append(text), Qt.DirectConnection)
    started.append(rig.worker)
    rig.worker.start()
    _join_or_fail(rig.worker, rig.stops)

    assert rig.stops == [StopReason.CRASHED]            # 按被测进程的退出原因结束
    assert rig.waiting_changes == [True, False]
    assert any("等待被测进程重启时出错" in text for text in errors)
    assert rig.worker.state is WorkerState.STOPPED
    assert reads.count < 500, f"主循环疑似空转：state 被读了 {reads.count} 次"
    assert db.tasks.get(rig.worker.task_id).stop_reason == 'crashed'


def test_main_loop_guard_catches_waiting_state_left_behind(db, started, monkeypatch):
    """异常若绕过了 _wait_for_restart 的包装（状态仍是等待重启），主循环顶部的兜底也要
    按最后一次退出的原因结束任务，而不是被每周期的异常处理反复吞掉、空转"""
    reads = _StateReads(monkeypatch)
    rig = build_rig(db, segments={100: [1, 2]}, codes={100: ACCESS_VIOLATION},
                    finder=ScriptedFinder(), resume=True, limit=3)

    def leaves_state_behind(outcome):
        rig.worker._state = WorkerState.WAITING_RESTART
        raise RuntimeError("绕过包装的异常")

    rig.worker._wait_for_restart = leaves_state_behind
    started.append(rig.worker)
    rig.worker.start()
    _join_or_fail(rig.worker, rig.stops)

    assert rig.stops == [StopReason.CRASHED]
    assert reads.count < 500, f"主循环疑似空转：state 被读了 {reads.count} 次"


# ========== 等待前处于暂停 ==========

def test_pause_before_waiting_is_remembered_after_restart(db, started):
    finder = ScriptedFinder({2: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1.0, 2.0], 200: [10.0, 11.0]},
                    codes={100: ACCESS_VIOLATION, 200: 0}, finder=finder, resume=True, limit=1)
    old, worker = rig.collectors[100], rig.worker
    original = old.collect_metrics

    def collect_then_pause_when_exhausted(metric_types):
        if old.calls >= len(old.samples):
            worker.pause()          # 用户恰在被测进程退出的这一刻点了暂停
        return original(metric_types)

    old.collect_metrics = collect_then_pause_when_exhausted
    wake = worker._wake

    def on_wait(event, timeout):
        if timeout is None:         # 暂停等待：真实阻塞，直到 resume() / stop() 置位
            event.wait(10)

    rig.clock.on_wait = on_wait
    started.append(worker)
    worker.start()

    assert poll_until(lambda: rig.restarts, timeout=10)
    assert poll_until(lambda: worker.state is WorkerState.PAUSED, timeout=5)
    time.sleep(0.3)
    # 续接上了新进程，但仍是暂停：没有采新进程的数据
    assert worker.state is WorkerState.PAUSED
    assert rig.collectors[200].calls == 0
    assert _values(db, worker.task_id) == [1.0, 2.0]

    assert worker.resume()
    assert worker.wait(SHORT_JOIN_MS)
    assert _values(db, worker.task_id) == [1.0, 2.0, 10.0, 11.0]
    assert rig.stops == [StopReason.PROCESS_EXITED]
    assert wake is worker._wake


def test_running_task_is_running_again_after_restart(db, started):
    """对照：等待前没暂停，续接后照常采集"""
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1.0], 200: [2.0, 3.0]},
                    codes={100: 0, 200: 0}, finder=finder, resume=True, limit=1)
    run_to_end(rig.worker, started)
    assert _values(db, rig.worker.task_id) == [1.0, 2.0, 3.0]


# ========== 续采：数据质量累加 ==========

def _fresh_quality(db, started):
    rig = build_rig(db, segments={100: [1, 2, 3]}, codes={100: 0}, resume=False)
    run_to_end(rig.worker, started)
    return db.tasks.load_quality(rig.worker.task_id)


def test_recovered_task_accumulates_quality_instead_of_overwriting(db, started):
    fresh = _fresh_quality(db, started)
    task_id = str(uuid.uuid4())
    db.save_task(MonitorTask(task_id=task_id, pid=100, process_name='app.exe',
                             metric_types=list(METRICS), interval=1.0, start_time=T0,
                             end_time=None, status='running'))
    db.tasks.save_quality(task_id, QualityStats(planned_cycles=100, done_cycles=90,
                                                missed_cycles=10, lag_p95_ms=50,
                                                collect_p95_ms=7))
    rig = build_rig(db, task_id=task_id, segments={100: [1, 2, 3]}, codes={100: 0},
                    resume=False, recovering=True, start_time=T0)
    run_to_end(rig.worker, started)

    quality = db.tasks.load_quality(task_id)
    # 同样的三个样本：续采后的周期数 = 库里已有的 + 这一次的
    assert quality.planned_cycles == 100 + fresh.planned_cycles
    assert quality.done_cycles == 90 + fresh.done_cycles
    assert quality.missed_cycles == 10 + fresh.missed_cycles
    assert quality.lag_p95_ms == max(50, fresh.lag_p95_ms)     # p95 无法相加，取较大者
    assert quality.collect_p95_ms == max(7, fresh.collect_p95_ms)


def test_fresh_task_quality_is_only_this_run(db, started):
    quality = _fresh_quality(db, started)
    assert quality.done_cycles == quality.planned_cycles
    assert quality.done_cycles < 10
