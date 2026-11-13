"""续采必须合并分布；元数据探针后写不能冲掉已保存的质量依据。"""
import json

from core.cycle_stats import CycleStats, Histogram, merge_histograms
from core.monitor_task import MonitorWorker
from tests.test_repos_task import _task


def _stats(count, seconds):
    stats = CycleStats(1)
    for _ in range(count):
        stats.lag(seconds)
        stats.collected(seconds)
    return stats


def test_merged_percentile_is_not_max_of_segment_percentiles(db):
    task = _task()
    db.save_task(task)
    prior = _stats(1, .5)
    current = _stats(99, .001)
    db.tasks.save_quality(task.task_id, prior.summary(), histograms=prior.histograms())
    worker = MonitorWorker(task.pid, task.process_name, task.metric_types, db=db)
    worker.task_id = task.task_id
    worker.cycle_stats = current
    worker._load_prior_quality()
    quality = worker._with_prior_quality(current.summary())
    assert quality.done_cycles == 100
    assert quality.lag_p95_ms == 1
    assert quality.collect_p95_ms == 1
    merged = worker._combined_histograms()
    assert Histogram.restore(merged['lag']).total == 100


def test_target_metadata_and_stop_do_not_erase_histograms(db):
    task = _task()
    db.save_task(task)
    stats = _stats(5, .01)
    db.tasks.save_quality(task.task_id, stats.summary(), histograms=stats.histograms())
    db.tasks.save_meta(task.task_id, '{"name":"测试程序"}')
    db.tasks.save_stop(task.task_id, 'user', None)
    assert db.tasks.load_quality_histograms(task.task_id) == stats.histograms()
    assert json.loads(db.tasks.load_meta(task.task_id))['name'] == '测试程序'


def test_legacy_or_corrupt_histograms_do_not_invent_distribution():
    current = _stats(3, .001).histograms()
    assert merge_histograms(current, {'version': 99}) is None
    corrupt = _stats(1, .01).histograms()
    corrupt['lag']['counts'][0] = -1
    assert merge_histograms(current, corrupt) is None
