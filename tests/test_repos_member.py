"""
MemberRepo 与进程组落库：task_members 留痕的写入语义（INSERT OR IGNORE 保留最早 first_ms、
last_ms 只增不减）、成员概况归并、成员序列发现；tasks.target_kind / target_spec 往返；
成员序列（指标@成员）走现有取数与汇总路径；任务删除级联清掉留痕；CSV 明细迭代可排除成员序列。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from data.errors import StorageError
from data.mappers import from_ms, to_ms
from data.models import DataPoint, MemberRecord, MonitorTask
from data.repos.member_repo import summarize_members
from data.repos.point_repo import PointQuery
from utils.metrics import member_series_key

BASE = datetime(2026, 3, 1, 9, 0, 0)


def ms(seconds):
    return to_ms(BASE + timedelta(seconds=seconds))


def make_task(**kw):
    fields = dict(task_id=str(uuid.uuid4()), pid=100, process_name='app.exe',
                  metric_types=['memory_private', 'process_count'], interval=1.0,
                  start_time=BASE, end_time=None, status='running')
    fields.update(kw)
    return MonitorTask(**fields)


def rec(task_id, pid, ct, name, first, last):
    return MemberRecord(task_id, pid, ct, name, ms(first), ms(last))


# ========== tasks.target_kind / target_spec ==========

def test_task_target_kind_round_trip(db):
    task = make_task(target_kind='tree', target_spec='{"root_pid": 100, "root_create_time": 1.5}')
    assert db.save_task(task)
    loaded = db.get_task(task.task_id)
    assert loaded.target_kind == 'tree'
    assert loaded.target_spec == '{"root_pid": 100, "root_create_time": 1.5}'


def test_task_defaults_to_pid_kind(db):
    task = make_task()
    assert db.save_task(task)
    loaded = db.get_task(task.task_id)
    assert loaded.target_kind == 'pid' and loaded.target_spec is None


def test_task_upsert_does_not_rewrite_target(db):
    """目标在创建时定下，之后的状态更新不改它"""
    task = make_task(target_kind='name', target_spec='{"name": "a.exe"}')
    db.save_task(task)
    db.save_task(make_task(task_id=task.task_id, target_kind='pid', target_spec=None,
                           status='stopped'))
    assert db.get_task(task.task_id).target_kind == 'name'


# ========== apply ==========

def test_apply_inserts_and_lists_in_first_seen_order(db):
    t = 'task-1'
    db.members.apply([rec(t, 20, 2.0, 'b.exe', 5, 5), rec(t, 10, 1.0, 'a.exe', 0, 0)])
    rows = db.members.list_instances(t)
    assert [(r.pid, r.first_ms, r.last_ms) for r in rows] == [(10, ms(0), ms(0)),
                                                              (20, ms(5), ms(5))]


def test_apply_ignores_duplicate_appearance_keeping_earliest_first_ms(db):
    t = 'task-1'
    db.members.apply([rec(t, 10, 1.0, 'a.exe', 3, 3)])
    db.members.apply([rec(t, 10, 1.0, 'a.exe', 9, 9)])
    (row,) = db.members.list_instances(t)
    assert (row.first_ms, row.last_ms) == (ms(3), ms(3))


def test_last_seen_only_increases_and_is_safe_out_of_order(db):
    t = 'task-1'
    db.members.apply([rec(t, 10, 1.0, 'a.exe', 0, 0)], [(t, 10, 1.0, ms(8))])
    db.members.apply([], [(t, 10, 1.0, ms(4))])   # 乱序到达的旧值不回退
    (row,) = db.members.list_instances(t)
    assert row.last_ms == ms(8)


def test_pid_reuse_is_a_separate_row(db):
    t = 'task-1'
    db.members.apply([rec(t, 7, 1.0, 'a.exe', 0, 0), rec(t, 7, 99.0, 'a.exe', 5, 5)],
                     [(t, 7, 1.0, ms(3)), (t, 7, 99.0, ms(9))])
    rows = db.members.list_instances(t)
    assert [(r.create_time, r.first_ms, r.last_ms) for r in rows] == [
        (1.0, ms(0), ms(3)), (99.0, ms(5), ms(9))]


def test_apply_empty_is_a_no_op(db):
    db.members.apply([], [])
    assert db.members.list_instances('x') == []


def test_apply_wraps_sqlite_errors_as_storage_error(db, monkeypatch):
    def boom(*a, **k):
        raise __import__('sqlite3').OperationalError('boom')
    monkeypatch.setattr(db.members._factory, 'transaction', boom)
    with pytest.raises(StorageError):
        db.members.apply([rec('t', 1, 1.0, 'a.exe', 0, 0)])


def test_database_facade_save_and_list(db):
    assert db.save_member_changes([rec('t', 1, 1.0, 'A.exe', 0, 0)], []) is True
    (member,) = db.list_members('t')
    assert member.member_key == 'a.exe' and member.name == 'A.exe'


def test_facade_save_returns_false_on_storage_error(db, monkeypatch):
    def boom(*a, **k):
        raise StorageError('x')
    monkeypatch.setattr(db.members, 'apply', boom)
    assert db.save_member_changes([rec('t', 1, 1.0, 'a.exe', 0, 0)], []) is False


# ========== 成员概况 ==========

def test_summarize_members_merges_same_name_case_insensitively():
    rows = [rec('t', 1, 1.0, 'Worker.exe', 2, 6), rec('t', 2, 2.0, 'worker.exe', 4, 9),
            rec('t', 3, 3.0, 'root.exe', 0, 9)]
    summaries = summarize_members(rows)
    assert [s.member_key for s in summaries] == ['root.exe', 'worker.exe']
    worker = summaries[1]
    assert worker.name == 'Worker.exe'   # 取最早出现的实例的原名
    assert worker.instance_count == 2
    assert worker.first_at == from_ms(ms(2)) and worker.last_at == from_ms(ms(9))


def test_list_members_of_pid_task_is_empty(db):
    assert db.list_members('no-such-task') == []


# ========== 成员序列 ==========

def test_member_series_and_keys_come_from_series_table(db):
    task = make_task(target_kind='tree', target_spec='{}')
    db.save_task(task)
    points = []
    for i in range(3):
        stamp = BASE + timedelta(seconds=i)
        points += [DataPoint(task.task_id, stamp, 10.0 + i, 'memory_private'),
                   DataPoint(task.task_id, stamp, 6.0, member_series_key('memory_private', 'a.exe')),
                   DataPoint(task.task_id, stamp, 4.0, member_series_key('memory_private', 'b.exe')),
                   DataPoint(task.task_id, stamp, 2.0, member_series_key('process_count', 'a.exe'))]
    assert db.save_data_points(points)
    assert db.members.member_keys(task.task_id) == ['a.exe', 'b.exe']
    assert db.members.member_series(task.task_id) == [
        ('memory_private', 'a.exe'), ('memory_private', 'b.exe'), ('process_count', 'a.exe')]
    got = db.points.fetch(PointQuery(task.task_id, member_series_key('memory_private', 'a.exe')))
    assert [p.value for p in got] == [6.0, 6.0, 6.0]


def test_iter_batches_can_leave_member_series_out(db):
    task = make_task()
    db.save_task(task)
    stamp = BASE
    db.save_data_points([DataPoint(task.task_id, stamp, 1.0, 'memory_private'),
                         DataPoint(task.task_id, stamp, 1.0, 'memory_private@a.exe')])

    def metrics(**kw):
        return {p.metric_type for batch in db.points.iter_batches(task.task_id, 100, **kw)
                for p in batch}
    assert metrics() == {'memory_private', 'memory_private@a.exe'}
    assert metrics(include_members=False) == {'memory_private'}


def test_deleting_task_cascades_to_members(db):
    task = make_task()
    db.save_task(task)
    db.members.apply([rec(task.task_id, 1, 1.0, 'a.exe', 0, 0)])
    assert db.delete_task(task.task_id)
    assert db.members.list_instances(task.task_id) == []
