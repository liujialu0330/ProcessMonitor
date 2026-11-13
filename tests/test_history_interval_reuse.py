"""区间统计只在本次查询的完整闭区间内复用聚合。"""
from datetime import timedelta

import pytest

from core.history_query import build_request, run_history_query
from data.models import DataPoint
from tests.test_segment_verdict import BASE, FLAT, MEM, RISING, _seed


def _spy(db, monkeypatch):
    calls = []
    original = db.analytics.aggregates

    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(db.analytics, 'aggregates', counted)
    return calls


@pytest.mark.parametrize('range_key', ['all', '1h', 'custom'])
def test_single_interval_reuses_only_this_requests_complete_aggregate(db, monkeypatch, range_key):
    task, _ = _seed(db, [RISING])
    calls = _spy(db, monkeypatch)
    bounds = (dict(since=BASE + timedelta(seconds=11, microseconds=500),
                   until=BASE + timedelta(seconds=610, microseconds=800))
              if range_key == 'custom' else {})
    result = run_history_query(db, build_request(task.task_id, MEM, range_key,
                                                analysis=True, **bounds))
    assert result.ok
    assert len(calls) == 1
    [interval] = result.intervals
    assert interval.sample_count == result.total_count
    assert interval.delta == result.stats['last'] - result.stats['first']
    assert interval.peak == result.stats['max']

    # 新请求必须重新取数，不能沿用上一次的样本数或末值。
    db.save_data_points([DataPoint(task.task_id, BASE + timedelta(seconds=700), 999999, MEM)])
    calls.clear()
    refreshed = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    assert refreshed.ok and len(calls) == 1
    assert refreshed.intervals[0].sample_count == 701
    assert refreshed.intervals[0].end_value == 999999


def test_restart_trim_does_not_reuse_whole_display_range(db, monkeypatch):
    task, starts = _seed(db, [RISING, FLAT], gap=1.0)
    calls = _spy(db, monkeypatch)
    result = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    assert result.ok and len(calls) == 3
    [interval] = result.intervals
    assert interval.sample_count == 700
    assert result.total_count == 1400
    assert interval.delta == 0
    assert interval.start == starts[1] - timedelta(milliseconds=500)


def test_marker_split_retains_right_open_end_semantics(db, monkeypatch):
    task, _ = _seed(db, [RISING])
    db.markers.add(task.task_id, BASE + timedelta(seconds=300), '第一轮', 'user')
    calls = _spy(db, monkeypatch)
    result = run_history_query(db, build_request(task.task_id, MEM, 'all', analysis=True))
    assert result.ok and len(calls) == 3
    assert [interval.sample_count for interval in result.intervals] == [300, 400]
    assert [interval.delta for interval in result.intervals] == [2990, 3990]
