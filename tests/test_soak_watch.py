"""
长时间监控：崩溃取证与被测进程重启后的同任务续接（假时钟 + 假探针 + 假进程查找）。

覆盖：
- 0xC0000005 退出 -> StopReason.CRASHED、tasks.exit_code、crash 打点、CSV 元数据行；
  普通退出码（0 / 1 / 2 / 未知）仍是 PROCESS_EXITED、不写 crash 打点；
- 取不到句柄降级为"未知退出码"且整个任务只记一次日志；探针在任务结束与线程退出时关闭；
- 续接：同一 task_id、新 PID、restart 打点（"第 2 次启动 PID …"）、restart_count 递增、
  等待期间不写数据点、达到上限后按退出原因结束、等待中用户停止立即结束；
- 进程树任务按新根续接；按名监控不取证、不续接；
- 续接策略从 tasks.resume_on_restart / restart_limit 读（方案落库后生效）。
"""
import logging
from datetime import timedelta

import pytest
from PyQt5.QtCore import Qt

from core.contracts import StopReason
from core.monitor_task import WorkerState
from core.process_tree import GroupSpec
from core.report import metadata_lines
from core.restart_finder import RestartHit
from data.repos.point_repo import PointQuery
from tests.soak_doubles import (
    ACCESS_VIOLATION, T0, ScriptedFinder, build_rig, run_to_end,
)

METRIC = 'memory_private'


@pytest.fixture
def started(qapp):
    """登记启动过的 worker，用例结束兜底停止并等待，防止断言失败遗留线程"""
    workers = []
    yield workers
    for w in workers:
        w.stop()
        w.wait(5000)


def _stamps(db, task_id, metric=METRIC):
    return [p.timestamp for p in db.points.fetch(PointQuery(task_id, metric))]


def _values(db, task_id, metric=METRIC):
    return [p.value for p in db.points.fetch(PointQuery(task_id, metric))]


def _markers(db, task_id, kind=None):
    items = db.markers.list(task_id)
    return [m for m in items if kind is None or m.kind == kind]


# ========== 崩溃取证 ==========

def test_access_violation_exit_is_crashed_with_marker_and_exit_code(db, started):
    rig = build_rig(db, segments={100: [1, 2, 3]}, codes={100: ACCESS_VIOLATION})
    run_to_end(rig.worker, started)

    assert rig.stops == [StopReason.CRASHED]
    task = db.tasks.get(rig.worker.task_id)
    assert task.status == 'stopped'
    assert task.exit_code == ACCESS_VIOLATION
    assert task.stop_reason == 'crashed'
    crash = _markers(db, rig.worker.task_id, 'crash')
    assert len(crash) == 1
    assert crash[0].label == "异常退出 0xC0000005（访问冲突）"
    # 采到的三个样本都在，崩溃那一刻没有伪造数据点
    assert _values(db, rig.worker.task_id) == [1.0, 2.0, 3.0]
    # CSV 元数据与报告：写明退出码与中文说明
    lines = metadata_lines(task, exported_at=T0, range_text="全部", end_time=task.end_time)
    assert any("退出: 异常退出 0xC0000005（访问冲突）" in line for line in lines)


def test_negative_exit_code_from_probe_is_normalized(db, started):
    """句柄层给出有符号值时同样判为访问冲突，库里存无符号值"""
    rig = build_rig(db, segments={100: [1]}, codes={100: -1073741819})
    run_to_end(rig.worker, started)
    assert rig.stops == [StopReason.CRASHED]
    assert db.tasks.get(rig.worker.task_id).exit_code == ACCESS_VIOLATION


@pytest.mark.parametrize("code", [0, 1, 2])
def test_ordinary_exit_codes_stay_process_exited(db, started, code):
    """退出码 1（taskkill /f）与脚本的 exit 2 都是"被测进程已退出"，不是崩溃，也不写 crash 打点"""
    rig = build_rig(db, segments={100: [1, 2]}, codes={100: code})
    run_to_end(rig.worker, started)

    assert rig.stops == [StopReason.PROCESS_EXITED]
    task = db.tasks.get(rig.worker.task_id)
    assert task.exit_code == code
    assert task.stop_reason == 'process_exited'
    assert _markers(db, rig.worker.task_id, 'crash') == []
    lines = metadata_lines(task, exported_at=T0, range_text="全部", end_time=task.end_time)
    assert any(f"退出: 退出码 {code}" in line for line in lines)


def test_unreadable_exit_code_degrades_to_unknown(db, started, caplog):
    """取不到句柄：按"未知退出码"结束，原因仍是被测进程已退出，日志只记一次"""
    caplog.set_level(logging.INFO)
    rig = build_rig(db, segments={100: [1, 2]}, codes={100: False})
    run_to_end(rig.worker, started)

    assert rig.stops == [StopReason.PROCESS_EXITED]
    task = db.tasks.get(rig.worker.task_id)
    assert task.exit_code is None
    assert task.stop_reason == 'process_exited'
    lines = metadata_lines(task, exported_at=T0, range_text="全部", end_time=task.end_time)
    assert any("被测进程已退出，退出码未知" in line for line in lines)
    unavailable = [r for r in caplog.records if "无法取得被测进程" in r.getMessage()]
    assert len(unavailable) == 1


def test_unavailable_handle_logged_once_across_restarts(db, started, caplog):
    """跨多次续接也只记一次：每次新进程都取不到句柄，日志不能刷屏"""
    caplog.set_level(logging.INFO)
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe'), 2: RestartHit(300, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2], 300: [3]},
                    codes={100: False, 200: False, 300: False},
                    finder=finder, resume=True, limit=5)
    # 第三个实例退出后找不到下一个：第 4 次查找时由用户停止
    finder.on_call = lambda n: rig.worker.stop() if n == 4 else None
    run_to_end(rig.worker, started)
    assert rig.restarts[:2] == [(200, 1), (300, 2)]
    unavailable = [r for r in caplog.records if "无法取得被测进程" in r.getMessage()]
    assert len(unavailable) == 1


def test_probe_factory_error_degrades_not_fails(db, started):
    from core.monitor_task import MonitorWorker
    from tests.soak_doubles import SegmentCollector, StepClock

    def broken(_pid):
        raise OSError("拒绝访问")

    worker = MonitorWorker(pid=100, process_name='a.exe', metric_types=[METRIC], interval=1.0,
                           db=db, clock=StepClock(), exit_probe_factory=broken,
                           collector_factory=lambda p, s: SegmentCollector([METRIC], [1]))
    stops = []
    worker.task_stopped.connect(lambda _t, r: stops.append(r), Qt.DirectConnection)
    run_to_end(worker, started)
    assert stops == [StopReason.PROCESS_EXITED]
    assert db.tasks.get(worker.task_id).exit_code is None


def test_probe_is_closed_when_task_ends(db, started):
    rig = build_rig(db, segments={100: [1]}, codes={100: ACCESS_VIOLATION})
    run_to_end(rig.worker, started)
    assert rig.probes[100].closed >= 1
    assert rig.worker._exit_probe is None


def test_probe_is_closed_when_loop_raises(db, started):
    """主循环抛出未预期异常、收尾又没能关句柄（线程退出路径）时，run() 的兜底也关闭句柄"""
    rig = build_rig(db, segments={100: [1]}, codes={100: None})

    def boom(force=False):
        raise RuntimeError("意外")

    rig.worker._refresh_restart_policy = boom
    rig.worker._teardown = lambda reason: None     # 收尾什么都不做：只剩 run() 的兜底
    run_to_end(rig.worker, started)
    assert rig.probes[100].closed >= 1
    assert rig.worker._exit_probe is None


def test_stop_by_user_keeps_user_reason_and_no_exit_code(db, started):
    rig = build_rig(db, segments={100: [1, 2, 3, 4, 5, 6]}, codes={100: None})
    rig.collectors[100].on_collect = lambda n: rig.worker.stop() if n == 3 else None
    run_to_end(rig.worker, started)
    assert rig.stops == [StopReason.USER]
    task = db.tasks.get(rig.worker.task_id)
    assert task.exit_code is None
    assert task.stop_reason == 'user'


# ========== 重启续接 ==========

def test_restart_resumes_same_task_with_markers_and_no_fake_points(db, started):
    finder = ScriptedFinder({3: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1, 2], 200: [10, 11, 12]},
                    codes={100: ACCESS_VIOLATION, 200: 0}, finder=finder, resume=True, limit=1)
    run_to_end(rig.worker, started)
    tid = rig.worker.task_id

    # 同一个任务：重启后沿用 task_id，数据接着写；最终第二个实例正常退出
    assert rig.stops == [StopReason.PROCESS_EXITED]
    assert rig.restarts == [(200, 1)]
    assert rig.waiting_changes == [True, False]
    assert _values(db, tid) == [1.0, 2.0, 10.0, 11.0, 12.0]
    task = db.tasks.get(tid)
    assert task.pid == 200
    assert task.exit_code == 0
    assert task.stop_reason == 'process_exited'

    markers = [(m.kind, m.label) for m in _markers(db, tid) if m.kind in ('crash', 'restart')]
    assert markers == [('crash', "异常退出 0xC0000005（访问冲突）"),
                       ('restart', "第 2 次启动 PID 200")]

    # 等待期间（找了 3 次才找到）没有任何数据点：第二个实例的首个样本晚于崩溃标记
    stamps = _stamps(db, tid)
    crash_at = _markers(db, tid, 'crash')[0].timestamp
    restart_at = _markers(db, tid, 'restart')[0].timestamp
    assert stamps[1] <= crash_at < restart_at <= stamps[2]
    assert restart_at - crash_at == timedelta(seconds=2)   # 两次查找失败，每次间隔 1 秒
    assert finder.calls == 3
    assert finder.targets[0].last_pid == 100


def test_restart_limit_ends_task_with_last_exit_reason(db, started):
    """续接上限为 1：第二次退出时不再等待，按最后一次退出的原因结束（崩溃就是 CRASHED）"""
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe'), 2: RestartHit(300, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2], 300: [3]},
                    codes={100: 0, 200: ACCESS_VIOLATION, 300: 0},
                    finder=finder, resume=True, limit=1)
    run_to_end(rig.worker, started)
    tid = rig.worker.task_id

    assert rig.restarts == [(200, 1)]
    assert rig.stops == [StopReason.CRASHED]
    assert _values(db, tid) == [1.0, 2.0]
    assert db.tasks.get(tid).stop_reason == 'crashed'
    assert rig.worker.restart_count == 1
    assert rig.finder.calls == 1       # 达到上限后不再查找


def test_restart_disabled_ends_at_first_exit(db, started):
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2]}, codes={100: 0, 200: 0},
                    finder=finder, resume=False)
    run_to_end(rig.worker, started)
    assert rig.stops == [StopReason.PROCESS_EXITED]
    assert finder.calls == 0
    assert rig.waiting_changes == []
    assert _markers(db, rig.worker.task_id, 'restart') == []


def test_user_stop_while_waiting_ends_promptly(db, started):
    """等待重启时用户停止：不再查找，按退出原因（崩溃）结束，状态与数据收尾完整"""
    finder = ScriptedFinder()      # 永远找不到
    rig = build_rig(db, segments={100: [1, 2]}, codes={100: ACCESS_VIOLATION},
                    finder=finder, resume=True, limit=3)
    seen_states = []

    def on_call(n):
        if n == 1:
            seen_states.append(rig.worker.state)
        if n == 4:
            assert rig.worker.is_waiting_restart()
            rig.worker.stop()

    finder.on_call = on_call
    run_to_end(rig.worker, started)

    assert seen_states == [WorkerState.WAITING_RESTART]
    assert rig.stops == [StopReason.CRASHED]
    assert finder.calls == 4
    assert rig.waiting_changes == [True, False]
    assert rig.worker.state is WorkerState.STOPPED
    tid = rig.worker.task_id
    task = db.tasks.get(tid)
    assert task.status == 'stopped'
    assert task.stop_reason == 'crashed'
    assert _values(db, tid) == [1.0, 2.0]
    # 等待了 3 秒的模拟时间：结束时间落在等待之后
    assert task.end_time >= _markers(db, tid, 'crash')[0].timestamp + timedelta(seconds=3)


def test_pause_is_remembered_while_waiting_restart(db, started):
    finder = ScriptedFinder()
    rig = build_rig(db, segments={100: [1]}, codes={100: 0}, finder=finder, resume=True)
    results = []

    def on_call(n):
        if n == 1:
            results.append(rig.worker.pause())
        if n == 2:
            rig.worker.stop()

    finder.on_call = on_call
    run_to_end(rig.worker, started)
    assert results == [True]
    assert rig.stops == [StopReason.PROCESS_EXITED]


def test_restart_resets_exit_probe_to_new_process(db, started):
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2]},
                    codes={100: ACCESS_VIOLATION, 200: ACCESS_VIOLATION},
                    finder=finder, resume=True, limit=1)
    run_to_end(rig.worker, started)
    # 两个进程各有自己的句柄，且都被关闭
    assert rig.probes[100].closed >= 1 and rig.probes[200].closed >= 1
    assert rig.stops == [StopReason.CRASHED]
    assert [m.label for m in _markers(db, rig.worker.task_id, 'crash')] == [
        "异常退出 0xC0000005（访问冲突）", "异常退出 0xC0000005（访问冲突）"]


def test_waiting_task_keeps_running_status_in_db(db, started):
    """等待重启期间任务行仍是 running（任务没有结束）"""
    finder = ScriptedFinder()
    rig = build_rig(db, segments={100: [1]}, codes={100: 0}, finder=finder, resume=True)
    statuses = []

    def on_call(n):
        if n == 2:
            statuses.append(db.tasks.get(rig.worker.task_id).status)
            rig.worker.stop()

    finder.on_call = on_call
    run_to_end(rig.worker, started)
    assert statuses == ['running']


def test_restart_policy_read_from_db_when_not_injected(db, started):
    """未注入策略时从 tasks.resume_on_restart / restart_limit 读：方案落库后才生效"""
    from core.automation.plan import AutomationPlan
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2]}, codes={100: 0, 200: 0},
                    finder=finder, resume=None)
    worker = rig.worker
    # 任务行在线程启动时才写入；采集线程第一次采样后、进程退出前方案被保存
    worker.task_model.start_time = T0
    db.save_task(worker.task_model)
    db.tasks.save_plan(worker.task_id, AutomationPlan(resume_on_restart=True,
                                                      restart_limit=1).to_json())
    assert db.tasks.load_restart_policy(worker.task_id) == (True, 1)
    run_to_end(worker, started)
    assert rig.restarts == [(200, 1)]
    assert rig.stops == [StopReason.PROCESS_EXITED]


# ========== 进程树与按名监控 ==========

def test_tree_task_resumes_on_new_root(db, started):
    spec = GroupSpec.tree(100, 1.0)
    finder = ScriptedFinder({1: RestartHit(200, 5.0, 'app.exe')})
    rig = build_rig(db, segments={100: [1, 2], 200: [3]}, codes={100: ACCESS_VIOLATION, 200: 0},
                    finder=finder, resume=True, limit=1, group_spec=spec)
    run_to_end(rig.worker, started)
    tid = rig.worker.task_id
    assert rig.restarts == [(200, 1)]
    assert rig.stops == [StopReason.PROCESS_EXITED]
    assert rig.worker.group_spec == GroupSpec.tree(200, 5.0)
    assert _values(db, tid) == [1.0, 2.0, 3.0]
    task = db.tasks.get(tid)
    assert task.pid == 200
    # target_spec 仍是任务创建时的根：它是创建时的事实
    assert GroupSpec.from_json('tree', task.target_spec) == spec


def test_name_task_has_no_forensics_and_never_restarts(db, started):
    """按名监控没有单一被测进程：不取退出码句柄、不等待重启"""
    spec = GroupSpec.by_name('app.exe')
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, pid=0, segments={0: [1, 2]}, codes={0: ACCESS_VIOLATION},
                    finder=finder, resume=True, limit=3, group_spec=spec)
    run_to_end(rig.worker, started)
    assert finder.calls == 0
    assert rig.waiting_changes == []
    assert rig.probes[0].reads == 0
    task = db.tasks.get(rig.worker.task_id)
    assert task.exit_code is None
    assert rig.stops == [StopReason.PROCESS_EXITED]


def test_user_stop_after_restart_does_not_inherit_previous_exit_code(db, started):
    """崩溃后续接成功、新进程仍在运行时用户停止：任务按"用户停止"结束，库里不留上一个进程的退出码；
    那次崩溃只以 crash 打点的形式留在时间线里"""
    finder = ScriptedFinder({1: RestartHit(200, None, 'app.exe')})
    rig = build_rig(db, segments={100: [1], 200: [2, 3, 4, 5, 6]},
                    codes={100: ACCESS_VIOLATION, 200: None}, finder=finder, resume=True, limit=3)
    rig.collectors[200].on_collect = lambda n: rig.worker.stop() if n == 3 else None
    run_to_end(rig.worker, started)

    tid = rig.worker.task_id
    assert rig.stops == [StopReason.USER]
    task = db.tasks.get(tid)
    assert task.stop_reason == 'user'
    assert task.exit_code is None
    assert rig.worker.last_exit_code is None
    assert [m.kind for m in _markers(db, tid) if m.kind in ('crash', 'restart')] == [
        'crash', 'restart']
    lines = metadata_lines(task, exported_at=T0, range_text="全部", end_time=task.end_time)
    assert not any(line.startswith("# 退出:") for line in lines)
