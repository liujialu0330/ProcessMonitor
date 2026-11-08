"""
打点撤销（MonitorManager.undo_marker / undo_last_marker）与打点时刻取值：
撤销后仓储里没有该记录、"第 N 轮"编号回退；30 秒时限（假时钟）；撤销基线恢复被降级的旧基线；
同一批（undo_group）一起撤销；系统打点不进撤销列表；撤销发 markers_changed。
"""
import os
from datetime import datetime, timedelta

import pytest

from core.monitor_manager import (
    UNDO_EXPIRED, UNDO_NOTHING, UNDO_OK, UNDO_WINDOW_SECONDS, MonitorManager,
)
from data.errors import StorageError
from data.models import DataPoint, Marker
from tests.doubles import FakeClock
from tests.helpers import stop_and_join
from ui.formatting import marker_echo_text, marker_tick_text
from utils.metrics import MetricType

T0 = datetime(2026, 11, 6, 9, 0, 0)


@pytest.fixture
def clock():
    return FakeClock(T0)


@pytest.fixture
def manager(db, qapp, clock):
    mgr = MonitorManager(db=db, clock=clock)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def task_id(manager):
    return manager.create_task(os.getpid(), "x.exe", ["memory_rss"], 1.0)


def _labels(manager, task_id):
    return [(m.label, m.kind) for m in manager.list_markers(task_id)]


def _changes(manager):
    seen = []
    manager.markers_changed.connect(seen.append)
    return seen


def test_undo_removes_the_record_and_rolls_the_label_back(manager, task_id):
    manager.add_marker(task_id, "基线", T0, kind="baseline")
    manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=10))
    last = manager.add_marker(task_id, "第 2 轮", T0 + timedelta(seconds=20))
    assert manager.next_marker_label(task_id) == "第 3 轮"

    outcome = manager.undo_marker(last.id)

    assert outcome.status == UNDO_OK and outcome.markers == (last,)
    assert manager.db.markers.list(task_id) == manager.list_markers(task_id)
    assert all(m.id != last.id for m in manager.db.markers.list(task_id))
    assert manager.next_marker_label(task_id) == "第 2 轮"
    # 再来一个：仍是"第 2 轮"，而不是跳号
    again = manager.add_marker(task_id, manager.next_marker_label(task_id),
                               T0 + timedelta(seconds=25))
    assert again.label == "第 2 轮"


def test_undo_emits_markers_changed_once_per_task(manager, task_id):
    marker = manager.add_marker(task_id, "基线", T0, kind="baseline")
    seen = _changes(manager)

    manager.undo_marker(marker.id)
    assert seen == [task_id]
    # 已经撤销过的不再重复发
    assert manager.undo_marker(marker.id).status == UNDO_NOTHING
    assert seen == [task_id]


def test_undo_expires_after_thirty_seconds(manager, task_id, clock):
    first = manager.add_marker(task_id, "基线", T0, kind="baseline")
    clock.advance(UNDO_WINDOW_SECONDS)          # 恰好 30 秒仍有效
    second = manager.add_marker(task_id, "第 1 轮", clock.now())
    assert manager.undo_marker(first.id).status == UNDO_OK

    clock.advance(UNDO_WINDOW_SECONDS + 1)
    seen = _changes(manager)
    outcome = manager.undo_marker(second.id)

    assert outcome.status == UNDO_EXPIRED and outcome.markers == ()
    assert seen == []
    assert [m.id for m in manager.list_markers(task_id)] == [second.id]
    assert manager.undo_last_marker().status == UNDO_NOTHING   # 过期的登记已丢弃


def test_undo_last_marker_takes_the_most_recent_one(manager, task_id, clock):
    manager.add_marker(task_id, "基线", T0, kind="baseline")
    clock.advance(5)
    second = manager.add_marker(task_id, "第 1 轮", clock.now())

    assert manager.undo_last_marker().markers == (second,)
    assert [m.label for m in manager.list_markers(task_id)] == ["基线"]
    assert manager.undo_last_marker().markers[0].label == "基线"
    assert manager.undo_last_marker().status == UNDO_NOTHING


def test_undo_last_marker_with_expired_latest_reports_expired(manager, task_id, clock):
    manager.add_marker(task_id, "基线", T0, kind="baseline")
    clock.advance(UNDO_WINDOW_SECONDS + 1)
    assert manager.undo_last_marker().status == UNDO_EXPIRED
    assert len(manager.list_markers(task_id)) == 1


def test_undoing_a_baseline_restores_the_previous_baseline(manager, task_id):
    old = manager.add_marker(task_id, "基线", T0, kind="baseline")
    manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=10))
    new = manager.add_marker(task_id, "重新预热", T0 + timedelta(seconds=20), kind="baseline")
    # 旧基线降为普通打点后，它算一个用户打点：默认名是"第 3 轮"
    assert manager.get_baseline(task_id).id == new.id
    assert manager.next_marker_label(task_id) == "第 3 轮"

    assert manager.undo_marker(new.id).status == UNDO_OK

    assert manager.get_baseline(task_id).id == old.id
    assert _labels(manager, task_id) == [("基线", "baseline"), ("第 1 轮", "user")]
    assert manager.next_marker_label(task_id) == "第 2 轮"


def test_undoing_the_first_baseline_leaves_no_baseline(manager, task_id):
    marker = manager.add_marker(task_id, "基线", T0, kind="baseline")
    assert manager.undo_marker(marker.id).status == UNDO_OK
    assert manager.get_baseline(task_id) is None
    assert manager.next_marker_label(task_id) == "基线"


def test_undoing_a_baseline_keeps_a_baseline_set_in_the_meantime(manager, task_id):
    manager.add_marker(task_id, "基线", T0, kind="baseline")
    new = manager.add_marker(task_id, "重新预热", T0 + timedelta(seconds=5), kind="baseline")
    other = manager.add_marker(task_id, "第 2 轮", T0 + timedelta(seconds=9))
    # 用户随后把另一个打点设为基线，再撤销"重新预热"：不能冒出两个基线
    assert manager.set_baseline(task_id, other.id)
    assert manager.undo_marker(new.id).status == UNDO_OK
    baselines = [m for m in manager.list_markers(task_id) if m.kind == "baseline"]
    assert [m.id for m in baselines] == [other.id]


def test_group_is_undone_together(manager, db, task_id):
    other = manager.create_task(os.getpid(), "y.exe", ["memory_rss"], 1.0)
    group = object()
    a = manager.add_marker(task_id, "基线", T0, kind="baseline", undo_group=group)
    b = manager.add_marker(other, "基线", T0, kind="baseline", undo_group=group)
    solo = manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=3))
    seen = _changes(manager)

    assert manager.undo_marker(solo.id).markers == (solo,)
    outcome = manager.undo_marker(a.id)

    assert {m.id for m in outcome.markers} == {a.id, b.id}
    assert manager.list_markers(task_id) == [] and manager.list_markers(other) == []
    assert sorted(seen) == sorted([task_id, task_id, other])


def test_system_markers_and_deleted_markers_are_not_undoable(manager, task_id):
    pause = manager.add_marker(task_id, "暂停", T0, kind="pause")
    assert manager.undo_marker(pause.id).status == UNDO_NOTHING
    assert manager.undo_last_marker().status == UNDO_NOTHING

    marker = manager.add_marker(task_id, "基线", T0, kind="baseline")
    assert manager.delete_marker(marker.id)
    assert manager.undo_marker(marker.id).status == UNDO_NOTHING


def test_undo_last_skips_a_marker_the_user_already_deleted(manager, task_id):
    first = manager.add_marker(task_id, "基线", T0, kind="baseline")
    second = manager.add_marker(task_id, "第 1 轮", T0 + timedelta(seconds=5))
    assert manager.delete_marker(second.id)                  # 手动删了最近的一个
    assert manager.undo_last_marker().markers == (first,)    # Ctrl+Z 轮到再前一个


def test_undo_storage_error_keeps_the_entry_for_retry(manager, task_id, monkeypatch):
    marker = manager.add_marker(task_id, "基线", T0, kind="baseline")
    real_delete = manager.db.markers.delete

    def boom(_id):
        raise StorageError("database is locked")

    monkeypatch.setattr(manager.db.markers, "delete", boom)
    assert manager.undo_marker(marker.id).status == "failed"
    assert len(manager.list_markers(task_id)) == 1

    monkeypatch.setattr(manager.db.markers, "delete", real_delete)
    assert manager.undo_marker(marker.id).status == UNDO_OK


def test_sample_values_at_picks_the_latest_sample_not_after_the_time(manager, db, task_id):
    for offset, value in ((0, 10.0), (1, 20.0), (2, 30.0)):
        db.save_data_points([DataPoint(task_id, T0 + timedelta(seconds=offset), value,
                                       "memory_rss")])

    assert manager.sample_values_at(
        task_id, ["memory_rss", "num_handles"], T0 + timedelta(seconds=1, milliseconds=500)
    ) == {"memory_rss": 20.0}
    assert manager.sample_values_at(task_id, ["memory_rss"], T0 - timedelta(seconds=1)) == {}


def test_sample_values_at_returns_empty_on_storage_error(manager, task_id, monkeypatch):
    def boom(*a, **k):
        raise StorageError("x")

    monkeypatch.setattr(manager.db.points, "fetch", boom)
    assert manager.sample_values_at(task_id, ["memory_rss"], T0) == {}


# ---------- 回显文案的纯函数（增量计算与缺值） ----------

_PRIVATE, _GDI, _HANDLES, _THREADS = (
    MetricType.MEMORY_PRIVATE, MetricType.NUM_GDI_OBJECTS, MetricType.NUM_HANDLES,
    MetricType.NUM_THREADS)


def test_echo_text_formats_deltas_with_signs_and_units():
    text = marker_echo_text(
        "第 7 轮", [_HANDLES, _GDI, _PRIVATE],
        {_HANDLES: 118, _GDI: 5, _PRIVATE: 101229.0},
        {_HANDLES: 100, _GDI: 5, _PRIVATE: 100000.0})
    assert text == "第 7 轮 · 句柄数 +18 个 · GDI对象数 ±0 · 私有字节 +1.2 MB（较上轮）"


def test_echo_text_negative_delta_and_baseline_basis():
    text = marker_echo_text("第 1 轮", [_GDI], {_GDI: 3}, {_GDI: 10},
                            previous_is_baseline=True)
    assert text == "第 1 轮 · GDI对象数 -7 个（较基线）"


@pytest.mark.parametrize("current, previous", [
    (None, {_GDI: 1}), ({_GDI: 1}, None), ({}, {}), ({_GDI: 1}, {_HANDLES: 1}),
])
def test_echo_text_without_comparable_values_is_just_the_label(current, previous):
    assert marker_echo_text("第 2 轮", [_GDI], current, previous) == "第 2 轮"


def test_echo_text_lists_at_most_three_metrics_and_skips_the_missing_one():
    current = {_PRIVATE: 2.0, _GDI: 2.0, _HANDLES: 2.0, _THREADS: 9.0}
    previous = {_PRIVATE: 1.0, _HANDLES: 1.0, _THREADS: 1.0}      # GDI 缺值
    text = marker_echo_text("第 2 轮", [_PRIVATE, _GDI, _HANDLES, _THREADS], current, previous)
    assert "GDI" not in text and "线程" not in text
    assert text.count(" · ") == 2          # 名称 + 私有字节 + 句柄数


def test_tick_text_variants():
    when = datetime(2026, 11, 6, 10, 21, 5)
    user = Marker("t", when, "第 3 轮", 1, "user")
    assert marker_tick_text(user, 3) == "第 3 轮 · 10:21:05"
    assert marker_tick_text(Marker("t", when, "建模", 1, "user"), 3) == "第 3 轮 · 10:21:05 · 建模"
    assert marker_tick_text(Marker("t", when, "预热完成", 1, "baseline"), None) == (
        "基线 · 10:21:05 · 预热完成")
    assert marker_tick_text(Marker("t", when, "基线", 1, "baseline"), None) == "基线 · 10:21:05"
    assert marker_tick_text(Marker("t", when, "重启 PID 7", 1, "restart"), None) == (
        "重启 PID 7 · 10:21:05")
