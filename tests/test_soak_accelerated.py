"""
加速浸泡：假时钟 + 假采集器驱动单个 MonitorWorker 模拟连续运行 7 天。

周期 60 秒（约 1 万个周期），真实 QThread 运行、真实落库（临时目录）。期间：
- 每 6 小时注入一次 5 分钟"系统休眠"：等待结束时单调时钟与墙钟一起向前跳 300 秒，
  调度应当重置截止时间、不补采，库里留下一个空洞；
- 每天一次采集失败：采集器返回空字典（全部指标不可用），该周期不落库、不算采样。

结束后检查不变式：sample_count == 实际行数；last_sample_at == 最后一行时间；
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
