"""
min/max 带的分桶在强制断点（restart / recover 打点）处切开

分桶原本只在自然空洞处切开：重启后新进程的第一个样本与旧进程的最后一个样本只隔一个周期时，
同一个桶会把两次启动连起来，带与中心线跨过重启画成一段。现在与 gaps 的强制断点口径一致。
原始路径与汇总路径的结果必须逐项相同，且 series_gaps / rollup_1m 不受影响（它们恒等于
"从样本按空洞规则重算"的结果）。
"""
from datetime import timedelta

import pytest

from core.history_query import build_request, run_history_query
from data.repos.analytics_repo import AnalyticsRepo
from tests.test_segment_verdict import BASE, FLAT, MEM, _seed

INTERVAL = 1.0


def _repos(db):
    return (AnalyticsRepo(db._factory, path='raw'), AnalyticsRepo(db._factory, path='rollup'))


def _same(left, right):
    assert len(left) == len(right)
    for a, b in zip(left, right):
        assert (a.start, a.end, a.count) == (b.start, b.end, b.count)
        for field in ('min', 'max', 'avg', 'last'):
            assert getattr(a, field) == pytest.approx(getattr(b, field))


def _spans_break(buckets, before, after) -> bool:
    return any(b.start <= before and after <= b.end for b in buckets)


def test_bucket_is_cut_at_a_restart_without_a_natural_gap(db):
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0)
    before, after = starts[1] - timedelta(seconds=1), starts[1]
    restart = [starts[1] - timedelta(milliseconds=500)]
    raw, rolled = _repos(db)
    for repo in (raw, rolled):
        merged = repo.buckets(task.task_id, MEM, 1, interval_seconds=INTERVAL)
        assert len(merged) == 1 and _spans_break(merged, before, after)
        cut = repo.buckets(task.task_id, MEM, 1, interval_seconds=INTERVAL,
                           break_instants=restart)
        assert len(cut) == 2 and not _spans_break(cut, before, after)
        assert cut[0].end == before and cut[1].start == after


@pytest.mark.parametrize('gap', [1.0, 5.0])
def test_raw_and_rollup_paths_agree_with_forced_breaks(db, gap):
    """gap=1：断点在一个自然分钟的汇总行内部；gap=5：断点本来就是自然空洞（并入后不重复）"""
    task, starts = _seed(db, [FLAT, FLAT, FLAT], gap=gap)
    marks = [starts[1] - timedelta(milliseconds=500), starts[2] - timedelta(milliseconds=500),
             starts[0] + timedelta(seconds=100, milliseconds=300)]      # 最后一个：续采打点
    raw, rolled = _repos(db)
    ranges = [(None, None), (starts[0] + timedelta(seconds=30), None),
              (None, starts[2] + timedelta(seconds=200)),
              (starts[0] + timedelta(seconds=90), starts[2] + timedelta(seconds=300))]
    for since, until in ranges:
        for max_buckets in (1, 2, 3, 7, 40):
            for interval in (INTERVAL, None):
                args = (task.task_id, MEM, max_buckets, since, until, interval)
                _same(raw.buckets(*args, break_instants=marks),
                      rolled.buckets(*args, break_instants=marks))


def test_recover_inside_a_segment_cuts_the_band_too(db):
    task, starts = _seed(db, [FLAT], gap=1.0)
    at = starts[0] + timedelta(seconds=100, milliseconds=300)
    raw, rolled = _repos(db)
    for repo in (raw, rolled):
        cut = repo.buckets(task.task_id, MEM, 1, interval_seconds=INTERVAL, break_instants=[at])
        assert [b.count for b in cut] == [101, 599]
    # 不给断点时行为不变
    assert len(raw.buckets(task.task_id, MEM, 1, interval_seconds=INTERVAL)) == 1


def test_break_outside_the_range_is_ignored(db):
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0)
    restart = [starts[1] - timedelta(milliseconds=500)]
    raw, rolled = _repos(db)
    for repo in (raw, rolled):
        # 范围只含第二段：两端不全在范围内的断点不算
        only_second = repo.buckets(task.task_id, MEM, 1, since=starts[1],
                                   interval_seconds=INTERVAL, break_instants=restart)
        assert len(only_second) == 1


def test_forced_breaks_leave_the_summary_tables_untouched(db):
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0)
    restart = [starts[1] - timedelta(milliseconds=500)]
    raw, rolled = _repos(db)
    rolled.buckets(task.task_id, MEM, 1, interval_seconds=INTERVAL, break_instants=restart)
    assert db.analytics.gaps(task.task_id, INTERVAL) == raw.gaps(task.task_id, INTERVAL) == []
    with db._factory.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM series_gaps').fetchone()[0] == 0


def test_analysis_band_is_cut_at_restart_in_the_snapshot(db):
    """分析页的 min/max 带（run_history_query 的 band）在重启处切开"""
    task, starts = _seed(db, [FLAT, FLAT], gap=1.0)
    request = build_request(task.task_id, MEM, 'all', analysis=True, max_buckets=1)
    for path in ('raw', 'rollup'):
        snapshot = run_history_query(AnalysisDb(db, path), request)
        before, after = starts[1] - timedelta(seconds=1), starts[1]
        assert len(snapshot.band) == 2 and not _spans_break(snapshot.band, before, after)


class AnalysisDb:
    """只把 analytics 换成指定路径的仓储，其余委托给真实库（让 run_history_query 走某条路径）"""

    def __init__(self, db, path):
        self._db = db
        self.analytics = AnalyticsRepo(db._factory, path=path)

    def __getattr__(self, name):
        return getattr(self._db, name)
