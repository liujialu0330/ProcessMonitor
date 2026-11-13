"""
分析页整批加载：5 个指标各跑一次 run_history_query(analysis=True)，5 线程并行。

与 AnalysisViewModel 的做法一致——每个指标一个后台查询线程，整批完成才显示；这里
用 ThreadPoolExecutor(5) 代替 HistoryRefreshWorker 线程外壳，测的是同一组查询。
锚点不预先缓存（build_request 不给 anchor），与首次进入分析页相同。
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.history_query import build_request, run_history_query
from data.database import Database
from tests.perf import synth

pytestmark = pytest.mark.perf

SPEC_3D = synth.days(3, '3d')
SPEC_7D = synth.days(7, '7d')


def _load_batch(db, spec: synth.TaskSpec, range_key: str, auto_refresh: bool = False):
    def one(metric):
        return run_history_query(db, build_request(
            spec.task_id, metric, range_key, analysis=True, auto_refresh=auto_refresh))
    with ThreadPoolExecutor(len(spec.metrics)) as pool:
        return list(pool.map(one, spec.metrics))


def _assert_complete(snaps, spec: synth.TaskSpec, range_key: str) -> None:
    assert len(snaps) == len(spec.metrics)
    for snap in snaps:
        assert snap.ok, snap.error
        assert snap.total_count > 0
    if range_key == 'all':
        leak = snaps[spec.metrics.index('memory_private')]
        assert leak.judgement is not None
        assert len(leak.gaps) == synth.expected_holes(spec)
        assert leak.baseline is not None and len(leak.rounds) == spec.rounds


@pytest.mark.parametrize('spec', [SPEC_3D, SPEC_7D], ids=['3d', '7d'])
def test_analysis_batch_all(spec, synth_cache, perf):
    path, info = synth_cache.task_db(spec)
    db = Database(path)
    try:
        snaps = []
        samples = perf.time_ms(lambda: snaps.append(_load_batch(db, spec, 'all')))
        _assert_complete(snaps[-1], spec, 'all')
    finally:
        db.close()
    perf.check_median(f'analysis_all_{spec.name}', samples,
                      desc=f"分析页“全部”整批 5 指标并行（{info['rows']} 行）")


def test_analysis_batch_last_hour(synth_cache, perf):
    """运行中任务的自动刷新路径：最近 1 小时范围（3 天库）"""
    spec = SPEC_3D
    path, info = synth_cache.task_db(spec)
    db = Database(path)
    try:
        snaps = []
        samples = perf.time_ms(
            lambda: snaps.append(_load_batch(db, spec, '1h', auto_refresh=True)))
        _assert_complete(snaps[-1], spec, '1h')
    finally:
        db.close()
    perf.check_median('analysis_1h_3d', samples,
                      desc=f"自动刷新“最近 1 小时”整批 5 指标并行（{info['rows']} 行库）")
