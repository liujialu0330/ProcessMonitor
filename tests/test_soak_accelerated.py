"""
加速浸泡：假时钟 + 假采集器驱动单个 MonitorWorker 模拟连续运行 7 天。

周期 60 秒（约 1 万个周期），真实 QThread 运行、真实落库（临时目录）。期间：
- 每 6 小时注入一次 5 分钟"系统休眠"：等待结束时单调时钟与墙钟一起向前跳 300 秒，
  调度应当重置截止时间、不补采，库里留下一个空洞；
- 每天一次采集失败：采集器返回空字典（全部指标不可用），该周期不落库、不算采样。

结束后检查不变式：数据质量（计划 == 完成 + 跳过；跳过次数 == 休眠注入次数，跳过周期数 ==
每次休眠覆盖的周期数之和；除去注入的休眠，完成率 100%）；sample_count == 实际行数；last_sample_at == 最后一行时间；
空洞数 == 休眠注入次数（db.analytics.gaps 口径，与分析页一致）；每指标行数相等；
无重复时间戳；分钟汇总与写时空洞 == 从样本整体重算的结果（且写时空洞数 == 休眠次数）；
原始路径与汇总路径的分析查询结果一致；分析页整批查询（'all', analysis=True）能算出判定。

默认不跑（@pytest.mark.soak，pytest.ini 默认排除）：
    python -m pytest tests/test_soak_accelerated.py -m soak -s
"""
import os
import random
import time
from datetime import timedelta

import pytest

from core.contracts import StopReason
from core.history_query import build_request, run_history_query
from data.repos.analytics_repo import AnalyticsRepo
from data.repos.point_repo import PointQuery
from tests.test_analytics_rollup_path import close
from tests.test_rollup import assert_rollups_consistent
from tests.test_monitor_worker_clock import (
    ScriptedCollector, SteppingClock, make_clocked_worker,
)

pytestmark = pytest.mark.soak

METRICS = ['memory_private', 'num_handles', 'num_gdi_objects', 'num_user_objects',
           'num_threads']
INTERVAL = 60.0
SIM_SECONDS = 7 * 86400
SLEEP_EVERY = 6 * 3600       # 每 6 小时休眠一次
SLEEP_SECONDS = 300          # 每次休眠 5 分钟
CYCLES_PER_DAY = int(86400 // INTERVAL)
FAIL_OFFSET = CYCLES_PER_DAY // 2   # 采集失败放在每天中段，避开休眠时刻
# 计划门槛：加速浸泡 < 90 秒（真实时间）
MAX_REAL_SECONDS = 90.0


class SleepyClock(SteppingClock):
    """在模拟时间跨过每个 6 小时整点的那次等待之后，额外跳过 5 分钟（模拟系统休眠）。"""

    def __init__(self):
        super().__init__()
        self.next_sleep = SLEEP_EVERY
        self.sleeps = []   # 每次休眠前的模拟秒数

    def wait(self, event, timeout=None):
        woke = super().wait(event, timeout)
        if not woke and self.elapsed() >= self.next_sleep:
            self.sleeps.append(self.elapsed())
            self.advance(SLEEP_SECONDS)
            self.next_sleep += SLEEP_EVERY
        return woke


def _values(rng: random.Random):
    """缓慢上涨 + 噪声：私有字节约 +2 MB/天，句柄 +30/天，其余小幅抖动"""
    def produce(n: int):
        day = n / CYCLES_PER_DAY
        return {
            'memory_private': 80_000 + 2_000 * day + rng.uniform(-50, 50),
            'num_handles': float(int(400 + 30 * day + rng.randint(-3, 3))),
            'num_gdi_objects': float(int(120 + 5 * day + rng.randint(-1, 1))),
            'num_user_objects': float(int(60 + 2 * day + rng.randint(-1, 1))),
            'num_threads': float(24 + rng.randint(-2, 2)),
        }
    return produce


def test_seven_days_accelerated_soak(db, qapp, tmp_path):
    assert os.path.commonpath([os.path.abspath(db.db_path), str(tmp_path)]) == str(tmp_path)

    clock = SleepyClock()
    produce = _values(random.Random(20261106))
    failures = []
    holder = []

    def values(n: int):
        if n % CYCLES_PER_DAY == FAIL_OFFSET:
            failures.append(n)
            return {}
        return produce(n)

    def after_collect(_n: int):
        if clock.elapsed() >= SIM_SECONDS:
            holder[0].stop()

    worker = make_clocked_worker(
        db, clock, metrics=METRICS, interval=INTERVAL,
        collector_factory=lambda w: ScriptedCollector(METRICS, values, after_collect))
    holder.append(worker)

    begin = time.perf_counter()
    worker.start()
    finished = worker.wait(int(MAX_REAL_SECONDS * 2 * 1000))
    elapsed = time.perf_counter() - begin
    if not finished:
        worker.stop()
        worker.wait(10000)
    assert finished, "加速浸泡未在时限内结束"

    task_id = worker.task_id
    cycles = worker.collector.calls
    expected_samples = cycles - len(failures)
    print(f"\n加速浸泡：模拟 {clock.elapsed() / 86400:.2f} 天，{cycles} 个周期，"
          f"休眠 {len(clock.sleeps)} 次，采集失败 {len(failures)} 次，真实耗时 {elapsed:.1f} s")

    # 收尾
    assert worker.teardowns == [StopReason.USER]
    assert worker.stops == [StopReason.USER]
    # 6h、12h……168h 各一次（最后一次落在第 7 天整点，之后采一次即停止）
    assert len(clock.sleeps) == SIM_SECONDS // SLEEP_EVERY
    assert len(failures) == 7   # 每天一次

    # 数据质量：每次休眠跳过 SLEEP_SECONDS / INTERVAL 个周期、记一次重置；采集失败
    # （全部指标不可用）的周期照常算完成
    quality = db.tasks.load_quality(task_id)
    injected_missed = len(clock.sleeps) * int(SLEEP_SECONDS // INTERVAL)
    assert worker.cycle_stats.resets == len(clock.sleeps)
    assert quality.missed_cycles == injected_missed
    assert quality.done_cycles == cycles
    assert quality.planned_cycles == quality.done_cycles + quality.missed_cycles
    assert quality.done_cycles / (quality.planned_cycles - injected_missed) >= 0.99
    # 计划周期包含休眠时段：完成率 = 1 - 休眠占比（7 天里 28 次 × 5 分钟 ≈ 1.4%）
    assert quality.completion_ratio >= 0.98
    print(f"数据质量：计划 {quality.planned_cycles}，完成 {quality.done_cycles}，"
          f"跳过 {quality.missed_cycles}，完成率 {quality.completion_ratio:.4f}，"
          f"调度延迟 p95 {quality.lag_p95_ms} ms")

    # 每指标行数相等、无重复时间戳、同一周期各指标共用同一时间戳
    per_metric = {m: [p.timestamp for p in db.points.fetch(PointQuery(task_id, m))]
                  for m in METRICS}
    reference = per_metric[METRICS[0]]
    assert len(reference) == expected_samples
    assert len(set(reference)) == len(reference)
    for metric, stamps in per_metric.items():
        assert stamps == reference, f"{metric} 的时间戳序列与其他指标不一致"

    # 汇总列与数据点一致
    task = db.tasks.get(task_id)
    assert task.sample_count == expected_samples
    assert task.last_sample_at == reference[-1]
    assert task.status == 'stopped'
    assert db.analytics.sample_count(task_id) == expected_samples

    # 空洞数 == 休眠注入次数；采集失败（间隔 2 个周期）不构成空洞
    gaps = db.analytics.gaps(task_id, INTERVAL)
    assert len(gaps) == len(clock.sleeps)
    for start, end in gaps:
        assert end - start >= timedelta(seconds=SLEEP_SECONDS)
    deltas = {b - a for a, b in zip(reference, reference[1:])}
    assert timedelta(seconds=INTERVAL) in deltas
    assert timedelta(seconds=2 * INTERVAL) in deltas   # 采集失败留下的一次缺采

    # 分钟汇总与写时空洞：与从样本整体重算的结果一致；每条序列的写时空洞 == 休眠次数
    assert assert_rollups_consistent(db.db_path) > 0
    with db._factory.connect() as conn:
        per_series = conn.execute(
            'SELECT COUNT(*) FROM series_gaps GROUP BY series_id').fetchall()
    assert [r[0] for r in per_series] == [len(clock.sleeps)] * len(METRICS)
    raw = AnalyticsRepo(db._factory, path='raw')
    rolled = AnalyticsRepo(db._factory, path='rollup')
    for metric in METRICS:
        assert close(raw.aggregates(task_id, metric), rolled.aggregates(task_id, metric))
        assert raw.gaps(task_id, INTERVAL, metric=metric) ==             rolled.gaps(task_id, INTERVAL, metric=metric)
        assert close(raw.buckets(task_id, metric, 50, interval_seconds=INTERVAL),
                     rolled.buckets(task_id, metric, 50, interval_seconds=INTERVAL))
        assert raw.legacy_buckets(task_id, metric, 2000) ==             rolled.legacy_buckets(task_id, metric, 2000)

    # 分析页整批查询能算出结果
    for metric in METRICS:
        snap = run_history_query(db, build_request(task_id, metric, 'all', analysis=True))
        assert snap.ok, f"{metric} 查询失败: {snap.error}"
        assert snap.total_count == expected_samples
        assert len(snap.gaps) == len(clock.sleeps)
        assert snap.stats is not None and snap.stats['count'] == expected_samples
    leak = run_history_query(db, build_request(task_id, 'memory_private', 'all', analysis=True))
    assert leak.judgement is not None

    assert elapsed < MAX_REAL_SECONDS, f"加速浸泡耗时 {elapsed:.1f} s 超过 {MAX_REAL_SECONDS} s"


# ========== 被测进程崩溃重启、工具被强杀后续采 ==========

class _DelayedFinder:
    """重启查找替身：进入等待后过 delay 秒（假时钟）才找到下一个实例。"""

    def __init__(self, clock, delay, pids):
        self.clock = clock
        self.delay = delay
        self.pids = list(pids)
        self._since = None
        self.found = []

    def __call__(self, target):
        from core.restart_finder import RestartHit
        now = self.clock.monotonic()
        if self._since is None:
            self._since = now
        if now - self._since < self.delay or not self.pids:
            return None
        self._since = None
        pid = self.pids.pop(0)
        self.found.append(pid)
        return RestartHit(pid, None, 'soak.exe')


def _segment_samples(cycles, level, rng):
    """一段平稳的样本：水平 level 加小噪声。"""
    return [level + rng.uniform(-20, 20) for _ in range(cycles)]


def test_seven_days_with_target_crashes_and_restarts(db, qapp):
    """7 天里被测进程崩溃 / 退出三次、每次五分钟后重新启动：同一个任务一路记下来。

    各段水平不同（阶梯上升）：整体看像在涨，只统计最后一段则平稳。"""
    from core.exit_forensics import is_abnormal_exit
    from tests.soak_doubles import StepClock, build_rig
    from tests.test_rollup import assert_rollups_consistent

    rng = random.Random(7)
    day = CYCLES_PER_DAY
    plan = [(100, 2 * day, 100_000.0, 0xC0000005), (200, 2 * day, 150_000.0, 0),
            (300, 2 * day, 200_000.0, 0xC0000409), (400, day, 250_000.0, None)]
    segments = {pid: _segment_samples(cycles, level, rng) for pid, cycles, level, _c in plan}
    codes = {pid: code for pid, _c, _l, code in plan}
    clock = StepClock()
    finder = _DelayedFinder(clock, 300, [200, 300, 400])
    rig = build_rig(db, segments={pid: s for pid, s in segments.items()},
                    codes=codes, finder=finder, metrics=METRICS[:1], interval=INTERVAL,
                    resume=True, limit=3, clock=clock)
    total = sum(len(s) for s in segments.values())
    # 最后一段采满后由用户停止（不让它因退出而等待重启）
    last = rig.collectors[400]
    last.on_collect = lambda n: rig.worker.stop() if n == len(segments[400]) else None

    begin = time.perf_counter()
    rig.worker.start()
    assert rig.worker.wait(int(MAX_REAL_SECONDS * 2 * 1000)), "加速浸泡未在时限内结束"
    elapsed = time.perf_counter() - begin
    tid = rig.worker.task_id
    print(f"\n崩溃重启浸泡：{total} 个周期，真实耗时 {elapsed:.1f} s")

    assert rig.stops == [StopReason.USER]
    assert rig.restarts == [(200, 1), (300, 2), (400, 3)]
    assert rig.waiting_changes == [True, False] * 3
    task = db.tasks.get(tid)
    assert task.pid == 400 and task.status == 'stopped'
    assert task.stop_reason == 'user' and task.exit_code is None

    markers = db.markers.list(tid)
    crashes = [m.label for m in markers if m.kind == 'crash']
    assert len(crashes) == 2 and all("异常退出" in label for label in crashes)
    assert [m.label for m in markers if m.kind == 'restart'] == [
        "第 2 次启动 PID 200", "第 3 次启动 PID 300", "第 4 次启动 PID 400"]
    assert is_abnormal_exit(0xC0000409)

    # 数据：每个周期一行，无重复、单调；三次空档各 >= 5 分钟，是仅有的空洞
    stamps = [p.timestamp for p in db.points.fetch(PointQuery(tid, METRICS[0]))]
    assert len(stamps) == total
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    gaps = db.analytics.gaps(tid, INTERVAL)
    assert len(gaps) == 3
    assert all(end - start >= timedelta(seconds=300) for start, end in gaps)
    assert task.sample_count == total
    assert assert_rollups_consistent(db.db_path) > 0

    # 等待期间不算计划周期：没有"漏采"；发现进程退出的那三次采集照常算完成周期
    quality = db.tasks.load_quality(tid)
    assert quality.missed_cycles == 0
    assert quality.planned_cycles == quality.done_cycles == total + 3

    # 判定只用最后一段（第 4 次启动之后）
    snap = run_history_query(db, build_request(tid, METRICS[0], 'all', analysis=True))
    assert (snap.segment_number, snap.segment_count) == (4, 4)
    assert snap.stats['count'] == day
    assert snap.judgement is not None
    assert snap.judgement.verdict.value != 'rising'
    assert elapsed < MAX_REAL_SECONDS


def test_seven_days_with_tool_killed_and_recovered(db, qapp):
    """第 3.5 天工具被强杀（任务行仍是 running、没有收尾），20 分钟后重启并续采到第 7 天：
    沿用 task_id 与开始时间，数据只有一个 20 分钟的空洞，写一个 recover 打点。"""
    from tests.soak_doubles import SegmentCollector, StepClock
    from tests.test_rollup import assert_rollups_consistent
    from core.exit_forensics import NullExitCodeProbe
    from core.monitor_task import MonitorWorker

    rng = random.Random(11)
    half = int(3.5 * CYCLES_PER_DAY)
    clock_a = StepClock()
    first = SegmentCollector(METRICS[:1], _segment_samples(half + 5, 100_000.0, rng))
    worker_a = MonitorWorker(
        pid=os.getpid(), process_name='soak.exe', metric_types=METRICS[:1], interval=INTERVAL,
        db=db, clock=clock_a, collector_factory=lambda pid, spec: first,
        exit_probe_factory=lambda pid: NullExitCodeProbe())
    first.on_collect = lambda n: worker_a.stop() if n == half else None
    worker_a.start()
    assert worker_a.wait(int(MAX_REAL_SECONDS * 1000))
    tid = worker_a.task_id
    start_time = db.tasks.get(tid).start_time
    killed_at = clock_a.now()

    # 强杀：线程没来得及收尾，库里仍是 running（用收尾前的状态还原）
    with db._factory.connect() as conn:
        conn.execute("UPDATE tasks SET status = 'running', end_time = NULL WHERE task_id = ?",
                     (tid,))
    row = db.tasks.get(tid)
    assert row.status == 'running'

    clock_b = StepClock(start=killed_at + timedelta(minutes=20))
    second = SegmentCollector(METRICS[:1], _segment_samples(half + 5, 100_000.0, rng))
    worker_b = MonitorWorker(
        pid=os.getpid(), process_name='soak.exe', metric_types=METRICS[:1], interval=INTERVAL,
        task_id=tid, db=db, clock=clock_b, collector_factory=lambda pid, spec: second,
        exit_probe_factory=lambda pid: NullExitCodeProbe(), recovering=True,
        start_time=row.start_time)
    second.on_collect = lambda n: worker_b.stop() if n == half else None
    begin = time.perf_counter()
    worker_b.start()
    assert worker_b.wait(int(MAX_REAL_SECONDS * 1000))
    elapsed = time.perf_counter() - begin

    task = db.tasks.get(tid)
    assert task.task_id == tid and task.start_time == start_time
    assert task.status == 'stopped'
    assert [m.kind for m in db.markers.list(tid)] == ['recover']
    stamps = [p.timestamp for p in db.points.fetch(PointQuery(tid, METRICS[0]))]
    assert len(stamps) == 2 * half
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    gaps = db.analytics.gaps(tid, INTERVAL)
    assert len(gaps) == 1
    assert gaps[0][1] - gaps[0][0] >= timedelta(minutes=20)
    assert task.sample_count == 2 * half
    assert assert_rollups_consistent(db.db_path) > 0
    # 续采没有制造重启分段：recover 不切段
    snap = run_history_query(db, build_request(tid, METRICS[0], 'all', analysis=True))
    assert snap.segment_count == 1 and snap.stats['count'] == 2 * half
    assert elapsed < MAX_REAL_SECONDS
