"""
存储路径：单周期写入延迟、旧库迁移吞吐。

- 写入：照采集线程的落库写法——每周期 5 个指标共用一个时间戳组成一批，经门面
  db.save_data_points（内部 points.append：同一事务写数据点并维护 sample_count /
  last_sample_at），库为已有 10 万行的当前版本库，常驻连接与生产一致；
- 迁移：用 MigrationRunner 把 100 万行的 v1（1.4.1）库迁到当前版本，计时包含迁移前
  的文件级备份（用户首次启动新版时实际经历的全部耗时）。
"""
import math
import os
import shutil
import statistics
import time
from datetime import timedelta

import pytest

from data.database import Database
from data.db.connection import ConnectionFactory
from data.db.migrations.runner import MigrationOutcome, MigrationRunner
from data.db.schema import SCHEMA_VERSION
from data.models import DataPoint
from tests.perf import synth

pytestmark = pytest.mark.perf

# 10 万行 = 2 万个周期 × 5 指标；不开空洞、不打点
SPEC_WRITE_BASE = synth.TaskSpec(name='write100k', duration_s=20_000, hole_every_s=0,
                                 rounds=0)
WRITE_CYCLES = 200
SPEC_V1 = synth.LegacySpec()


def _percentile(samples, pct: float) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _remove_db_files(path: str) -> None:
    folder = os.path.dirname(path)
    shutil.rmtree(folder, ignore_errors=True)


def test_write_latency_per_cycle(synth_cache, tmp_path, perf):
    src, info = synth_cache.task_db(SPEC_WRITE_BASE)
    task_id = SPEC_WRITE_BASE.task_id
    start = synth.T0 + timedelta(seconds=SPEC_WRITE_BASE.duration_s)
    p50s, p95s = [], []
    for run in range(perf.repeat):
        run_dir = tmp_path / f'run{run}'
        run_dir.mkdir()
        path = str(run_dir / 'monitor.db')
        shutil.copyfile(src, path)
        db = Database(path)
        try:
            latencies = []
            for k in range(WRITE_CYCLES):
                ts = start + timedelta(seconds=k)
                batch = [DataPoint(task_id, ts, 1000.0 + k, metric)
                         for metric in SPEC_WRITE_BASE.metrics]
                begin = time.perf_counter()
                ok = db.save_data_points(batch)
                latencies.append((time.perf_counter() - begin) * 1000)
                assert ok
            task = db.tasks.get(task_id)
            assert task.sample_count == info['cycles'] + WRITE_CYCLES
        finally:
            db.close()
            _remove_db_files(path)
        p50s.append(statistics.median(latencies))
        p95s.append(_percentile(latencies, 95))
    perf.check_many([
        ('write_cycle_p50', p50s,
         f'单周期写入 5 指标 p50（{info["rows"]} 行库，{WRITE_CYCLES} 次）'),
        ('write_cycle_p95', p95s,
         f'单周期写入 5 指标 p95（{info["rows"]} 行库，{WRITE_CYCLES} 次）'),
    ])


def test_migration_v1_to_current(synth_cache, tmp_path, perf):
    src, info = synth_cache.v1_db(SPEC_V1)
    samples = []
    for run in range(perf.repeat):
        run_dir = tmp_path / f'mig{run}'
        run_dir.mkdir()
        path = str(run_dir / 'monitor.db')
        shutil.copyfile(src, path)
        try:
            begin = time.perf_counter()
            report = MigrationRunner(ConnectionFactory(path, keep_alive=False)).migrate()
            samples.append((time.perf_counter() - begin) * 1000)
            assert report.outcome is MigrationOutcome.MIGRATED
            assert (report.from_version, report.to_version) == (1, SCHEMA_VERSION)
        finally:
            _remove_db_files(path)
    perf.check_median('migration_v1_1m', samples,
                      desc=f'v1 → v{SCHEMA_VERSION} 迁移（{info["rows"]} 行，含备份）')
