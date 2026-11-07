"""
AutoMarkerService：假时钟 + 假 manager（test_monitor_presenter.FakeManager）驱动，不真实等待。
- 定时打点按计划时刻准时、默认标签序列（基线 → 第 N 轮），落后不补打；
- 暂停期间不打点、不计时，恢复后从恢复时刻重新计时；
- 轮数 / 时长到达即请求停止并发 auto_stopped，之后不再打点；
- 任务结束自动解除跟踪，带"停止后自动导出"时发 export_requested；
- 同一时刻定时与日志同时触发各打一次（data_updated 与每秒 tick 都调用也不重复）；
- 阶段快照按期发 stage_snapshot_due（序号从 1 起），暂停不发不累计，落后只发一次；
- 与真实 MonitorManager 一起：自动与手动打点共用编号序列。
"""
import os
from datetime import timedelta

import pytest

from core.automation.auto_marker import MAX_LOG_MARKERS_PER_TICK, AutoMarkerService
from core.automation.plan import AutomationPlan
from core.automation.stop_conditions import AUTO_STOP_DURATION, AUTO_STOP_ROUNDS
from core.contracts import StopReason
from core.monitor_manager import BASELINE_LABEL, MonitorManager, round_label
from tests.doubles import FakeClock
from tests.test_monitor_presenter import FakeManager
from utils.metrics import MetricType

N = 10


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def manager(qapp):
    return FakeManager()


@pytest.fixture
def service(manager, clock):
    svc = AutoMarkerService(manager, clock)
    svc.stopped, svc.exports = [], []
    svc.auto_stopped.connect(lambda tid, reason: svc.stopped.append((tid, reason)))
    svc.export_requested.connect(lambda tid, d: svc.exports.append((tid, d)))
    svc.snapshots = []
    svc.stage_snapshot_due.connect(lambda tid, d, i: svc.snapshots.append((tid, d, i, clock.now())))
    return svc


def _task(manager):
    return manager.create_task(1001, "demo.exe", [MetricType.NUM_HANDLES], 1.0)


def _start(service, manager, plan):
    task_id = _task(manager)
    assert service.attach(task_id, plan)
    manager.task_started.emit(task_id)
    return task_id


def _markers(manager, task_id):
    return [(m.label, m.timestamp, m.kind) for m in manager.list_markers(task_id)]


def _run(service, clock, seconds, step=1):
    """以每秒一次 tick 推进 seconds 秒。"""
    for _ in range(int(seconds / step)):
        clock.advance(step)
        service.tick()


def test_timed_markers_are_on_schedule_with_default_labels(service, manager, clock):
    t0 = clock.now()
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))

    _run(service, clock, 3 * N + 5)

    assert _markers(manager, task_id) == [
        (BASELINE_LABEL, t0 + timedelta(seconds=N), 'baseline'),
        (round_label(1), t0 + timedelta(seconds=2 * N), 'user'),
        (round_label(2), t0 + timedelta(seconds=3 * N), 'user'),
    ]


def test_marker_time_is_scheduled_time_not_tick_time(service, manager, clock):
    """tick 有抖动（每 1.5 秒一次）时打点时刻仍是计划时刻，节奏锚定在起点"""
    t0 = clock.now()
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    _run(service, clock, 2 * N + 3, step=1.5)
    assert [ts for _l, ts, _k in _markers(manager, task_id)] == [
        t0 + timedelta(seconds=N), t0 + timedelta(seconds=2 * N)]


def test_late_tick_does_not_catch_up(service, manager, clock):
    t0 = clock.now()
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    clock.advance(3.5 * N)          # 休眠唤醒：错过了三轮
    service.tick()
    service.tick()
    assert [ts for _l, ts, _k in _markers(manager, task_id)] == [clock.now()]
    assert service.next_due(task_id) == clock.now() + timedelta(seconds=N)
    assert t0 < clock.now()


def test_starting_task_restarts_the_timer(service, manager, clock):
    task_id = _task(manager)
    service.attach(task_id, AutomationPlan(timed_marker_seconds=N))
    clock.advance(4)
    manager.task_started.emit(task_id)
    assert service.next_due(task_id) == clock.now() + timedelta(seconds=N)


def test_pause_stops_markers_and_resume_restarts_timer(service, manager, clock):
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    _run(service, clock, 7)
    manager.paused_ids.add(task_id)
    service.process(task_id)                 # 暂停命令成功后立即同步
    _run(service, clock, 5 * N)              # 暂停很久：不打点
    assert manager.list_markers(task_id) == []
    assert service.next_due(task_id) is None

    manager.paused_ids.discard(task_id)
    service.process(task_id)                 # 恢复命令成功后立即同步
    resumed = clock.now()
    assert service.next_due(task_id) == resumed + timedelta(seconds=N)
    _run(service, clock, N - 1)
    assert manager.list_markers(task_id) == []   # 暂停前的 7 秒不累计
    _run(service, clock, 1)
    assert [ts for _l, ts, _k in _markers(manager, task_id)] == [resumed + timedelta(seconds=N)]


def test_pause_detected_by_tick_alone(service, manager, clock):
    """暂停/恢复不经 Presenter（没人调 process）时，每秒 tick 也能察觉"""
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    _run(service, clock, 3)
    manager.paused_ids.add(task_id)
    _run(service, clock, 2 * N)
    manager.paused_ids.discard(task_id)
    _run(service, clock, 1)
    resumed = clock.now()
    _run(service, clock, N)
    assert [ts for _l, ts, _k in _markers(manager, task_id)] == [resumed + timedelta(seconds=N)]


def test_rounds_reached_stops_task(service, manager, clock):
    task_id = _start(service, manager,
                     AutomationPlan(timed_marker_seconds=N, stop_after_rounds=2))
    _run(service, clock, 2 * N)
    assert service.stopped == [] and ("stop", task_id) not in manager.commands

    _run(service, clock, N)                       # 第 2 轮打点 → 达到 2 轮
    assert [l for l, _t, _k in _markers(manager, task_id)] == [
        BASELINE_LABEL, round_label(1), round_label(2)]
    assert service.stopped == [(task_id, AUTO_STOP_ROUNDS)]
    assert manager.commands.count(("stop", task_id)) == 1

    _run(service, clock, 5 * N)                   # 已请求停止：不再打点、不重复停止
    assert len(manager.list_markers(task_id)) == 3
    assert manager.commands.count(("stop", task_id)) == 1


def test_manual_markers_count_as_rounds(service, manager, clock):
    task_id = _start(service, manager, AutomationPlan(stop_after_rounds=2))
    manager.add_marker(task_id, BASELINE_LABEL, clock.now(), kind='baseline')
    manager.add_marker(task_id, round_label(1), clock.now())
    service.tick()
    assert service.stopped == []
    manager.add_marker(task_id, round_label(2), clock.now())
    service.tick()
    assert service.stopped == [(task_id, AUTO_STOP_ROUNDS)]


def test_duration_reached_stops_even_while_paused(service, manager, clock):
    task_id = _start(service, manager, AutomationPlan(stop_after_seconds=30))
    _run(service, clock, 10)
    manager.paused_ids.add(task_id)
    _run(service, clock, 19)
    assert service.stopped == []
    _run(service, clock, 1)
    assert service.stopped == [(task_id, AUTO_STOP_DURATION)]
    assert ("stop", task_id) in manager.commands


def test_stop_request_refused_does_not_emit(service, manager, clock):
    manager.stop_result = False
    task_id = _start(service, manager, AutomationPlan(stop_after_seconds=5))
    _run(service, clock, 5)
    assert service.stopped == []
    _run(service, clock, 5)
    assert manager.commands.count(("stop", task_id)) == 1


def test_task_stop_detaches_and_requests_export(service, manager, clock):
    plan = AutomationPlan(timed_marker_seconds=N, export_on_stop=True, export_dir="D:\\out")
    task_id = _start(service, manager, plan)
    manager.task_stopped.emit(task_id, StopReason.PROCESS_EXITED)
    assert not service.is_attached(task_id)
    assert service.exports == [(task_id, "D:\\out")]
    _run(service, clock, 3 * N)
    assert manager.list_markers(task_id) == []


def test_task_stop_without_export_only_detaches(service, manager):
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    manager.task_stopped.emit(task_id, StopReason.USER)
    assert not service.is_attached(task_id) and service.exports == []


def test_untracked_task_signals_are_ignored(service, manager):
    task_id = _task(manager)
    manager.task_started.emit(task_id)
    manager.data_updated.emit(task_id, {})
    manager.task_stopped.emit(task_id, StopReason.USER)
    assert service.exports == [] and manager.list_markers(task_id) == []


def test_empty_plan_is_not_attached(service, manager):
    task_id = _task(manager)
    assert service.attach(task_id, AutomationPlan()) is False
    assert service.attach(task_id, None) is False
    assert not service.is_attached(task_id)


def test_data_updated_drives_processing(service, manager, clock):
    t0 = clock.now()
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    clock.advance(N)
    manager.data_updated.emit(task_id, {MetricType.NUM_HANDLES: 1.0})
    assert [ts for _l, ts, _k in _markers(manager, task_id)] == [t0 + timedelta(seconds=N)]


def test_log_match_marks_with_named_label(service, manager, clock, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("Case 0 done\n", encoding="utf-8")
    task_id = _start(service, manager, AutomationPlan(
        log_path=str(log), log_pattern=r"Case (?P<label>\d+) done|ROUND"))
    with open(log, "a", encoding="utf-8") as f:
        f.write("Case 1 done\nROUND\n")
    clock.advance(1)
    service.tick()
    # 命名分组作打点名；没有时用默认名称。首个打点仍按默认序列判为基线
    assert [(l, k) for l, _t, k in _markers(manager, task_id)] == [
        ("1", 'baseline'), (round_label(1), 'user')]


def test_timed_and_log_at_same_moment_mark_once_each(service, manager, clock, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("", encoding="utf-8")
    task_id = _start(service, manager, AutomationPlan(
        timed_marker_seconds=N, log_path=str(log), log_pattern="ROUND"))
    clock.advance(N)
    with open(log, "a", encoding="utf-8") as f:
        f.write("ROUND\n")
    # 同一时刻：采集周期与每秒 tick 都来驱动
    manager.data_updated.emit(task_id, {})
    service.tick()
    service.process(task_id)
    assert len(manager.list_markers(task_id)) == 2


def test_log_lines_during_pause_are_discarded(service, manager, clock, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("", encoding="utf-8")
    task_id = _start(service, manager, AutomationPlan(log_path=str(log), log_pattern="ROUND"))
    manager.paused_ids.add(task_id)
    with open(log, "a", encoding="utf-8") as f:
        f.write("ROUND\n")
    service.tick()
    manager.paused_ids.discard(task_id)
    service.tick()
    assert manager.list_markers(task_id) == []


def test_log_flood_is_capped_per_tick(service, manager, clock, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("", encoding="utf-8")
    task_id = _start(service, manager, AutomationPlan(log_path=str(log), log_pattern="ROUND"))
    with open(log, "a", encoding="utf-8") as f:
        f.write("ROUND\n" * (MAX_LOG_MARKERS_PER_TICK + 5))
    service.tick()
    assert len(manager.list_markers(task_id)) == MAX_LOG_MARKERS_PER_TICK


def test_failed_marker_write_does_not_break_schedule(service, manager, clock):
    manager.marker_ok = False
    task_id = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    _run(service, clock, N)
    manager.marker_ok = True
    _run(service, clock, N)
    assert len(manager.list_markers(task_id)) == 1


# ---------------------------------------------------------------- 阶段快照

SNAP_DIR = "D:\\快照"
HOURS = 2
H = timedelta(hours=HOURS)


def _snapshot_plan(**extra):
    return AutomationPlan(stage_snapshot_hours=HOURS, stage_snapshot_dir=SNAP_DIR, **extra)


def test_stage_snapshot_due_on_schedule_with_index(service, manager, clock):
    t0 = clock.now()
    task_id = _start(service, manager, _snapshot_plan())
    _run(service, clock, 3 * H.total_seconds() + 30, step=30)
    assert service.snapshots == [
        (task_id, SNAP_DIR, 1, t0 + H), (task_id, SNAP_DIR, 2, t0 + 2 * H),
        (task_id, SNAP_DIR, 3, t0 + 3 * H)]
    # 只发信号：不打点、不停止任务
    assert manager.list_markers(task_id) == [] and service.stopped == []


def test_stage_snapshot_counts_from_attach_without_start(service, manager, clock):
    task_id = _task(manager)
    attached = clock.now()
    assert service.attach(task_id, _snapshot_plan())
    assert service.next_snapshot_due(task_id) == attached + H
    clock.advance(H)
    service.tick()
    assert [(i, at) for _t, _d, i, at in service.snapshots] == [(1, attached + H)]


def test_stage_snapshot_restarts_when_task_starts(service, manager, clock):
    task_id = _task(manager)
    service.attach(task_id, _snapshot_plan())
    clock.advance(600)
    manager.task_started.emit(task_id)
    assert service.next_snapshot_due(task_id) == clock.now() + H


def test_stage_snapshot_pause_does_not_fire_or_accumulate(service, manager, clock):
    task_id = _start(service, manager, _snapshot_plan())
    _run(service, clock, 1800, step=60)            # 暂停前跑了半小时
    manager.paused_ids.add(task_id)
    service.process(task_id)
    _run(service, clock, 5 * H.total_seconds(), step=60)
    assert service.snapshots == [] and service.next_snapshot_due(task_id) is None

    manager.paused_ids.discard(task_id)
    service.process(task_id)
    resumed = clock.now()
    _run(service, clock, H.total_seconds() - 60, step=60)
    assert service.snapshots == []                 # 暂停前的半小时不累计
    _run(service, clock, 60, step=60)
    assert [(i, at) for _t, _d, i, at in service.snapshots] == [(1, resumed + H)]


def test_stage_snapshot_late_fires_once_then_restarts(service, manager, clock):
    task_id = _start(service, manager, _snapshot_plan())
    clock.advance(3.5 * H)                         # 休眠唤醒：错过了三次
    service.tick()
    service.tick()
    assert [(i, at) for _t, _d, i, at in service.snapshots] == [(1, clock.now())]
    assert service.next_snapshot_due(task_id) == clock.now() + H
    clock.advance(H)
    service.tick()
    assert [i for _t, _d, i, _at in service.snapshots] == [1, 2]


def test_stage_snapshot_stops_after_auto_stop(service, manager, clock):
    task_id = _start(service, manager, _snapshot_plan(stop_after_seconds=int(H.total_seconds())))
    _run(service, clock, 3 * H.total_seconds(), step=60)
    # 到期与停止同一刻：快照照发一次（最后的阶段），之后已请求停止不再发
    assert [i for _t, _d, i, _at in service.snapshots] == [1]
    assert service.stopped == [(task_id, AUTO_STOP_DURATION)]


def test_stage_snapshot_ends_with_task(service, manager, clock):
    task_id = _start(service, manager, _snapshot_plan())
    manager.task_stopped.emit(task_id, StopReason.USER)
    _run(service, clock, 2 * H.total_seconds(), step=60)
    assert service.snapshots == []


# ---------------------------------------------------------------- 真实 manager


def test_auto_and_manual_markers_share_numbering_with_real_manager(qapp, db, clock):
    manager = MonitorManager(db)
    task_id = manager.create_task(os.getpid(), "python.exe", [MetricType.NUM_HANDLES], 1.0)
    try:
        service = AutoMarkerService(manager, clock)
        assert service.attach(task_id, AutomationPlan(timed_marker_seconds=N))
        # 任务行要等采集线程启动才写入；这里不启动线程，手工写入以便打点落库
        db.save_task(manager.get_task(task_id).task_model)

        _run(service, clock, N)
        manual = manager.add_marker(task_id, manager.next_marker_label(task_id), clock.now())
        _run(service, clock, N)

        labels = [(m.label, m.kind) for m in manager.list_markers(task_id)]
        assert labels == [(BASELINE_LABEL, 'baseline'), (round_label(1), 'user'),
                          (round_label(2), 'user')]
        assert manual.label == round_label(1)
    finally:
        manager.remove_task(task_id)


def test_automation_and_form_sources_take_time_from_injected_clock():
    """自动化模块与自动化表单区不直接读系统时间（测试才能用假时钟驱动）"""
    import inspect

    import core.automation.auto_marker as auto_marker_module
    import core.automation.log_watcher as log_watcher_module
    import core.automation.plan as plan_module
    import core.automation.stage_snapshot as stage_snapshot_module
    import core.automation.stop_conditions as stop_module
    import ui.widgets.monitor.automation_options as options_module
    for module in (auto_marker_module, log_watcher_module, plan_module, stop_module,
                   options_module, stage_snapshot_module):
        source = inspect.getsource(module)
        assert "datetime.now" not in source and "time.monotonic" not in source, module



# ---------------------------------------------------------------- 评审修复：任务间隔离、溢出兜底


def test_one_task_failure_does_not_block_others_and_logs_once(service, manager, clock, caplog):
    import logging
    bad = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    good = _start(service, manager, AutomationPlan(timed_marker_seconds=N))
    original = manager.next_marker_label

    def broken(task_id):
        if task_id == bad:
            raise RuntimeError("数据库坏了")
        return original(task_id)

    manager.next_marker_label = broken
    with caplog.at_level(logging.ERROR, logger="core.automation.auto_marker"):
        _run(service, clock, 3 * N)
        manager.data_updated.emit(bad, {})
    assert len(manager.list_markers(good)) == 3        # 坏任务不影响好任务
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and bad in r.getMessage()]
    assert len(errors) == 1                            # 同一任务只记一次 ERROR
    # 解除跟踪后再出错会重新记录
    service.detach(bad)
    assert bad not in service._failed


def test_unvalidated_huge_plan_does_not_crash_attach(service, manager, clock, caplog):
    """绕过校验的超大间隔（如手改库里的方案）：挂接不抛 OverflowError，放弃定时打点。"""
    import logging
    task_id = _task(manager)
    plan = AutomationPlan(timed_marker_seconds=10 ** 15, stage_snapshot_hours=10.0 ** 12,
                          stage_snapshot_dir=SNAP_DIR)
    with caplog.at_level(logging.ERROR, logger="core.automation.auto_marker"):
        assert service.attach(task_id, plan)
        manager.task_started.emit(task_id)
        _run(service, clock, 3)
    assert service.next_due(task_id) is None and service.next_snapshot_due(task_id) is None
    assert "超出范围" in caplog.text
