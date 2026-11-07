"""
TaskRepo：UPSERT 语义（不清 note/sample_count/create_time）、查询、状态更新、
三表级联删除、孤儿任务校正、datetime 强校验与 StorageError 包装；plan_json 读写且 UPSERT 不清掉它
"""
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from core.contracts import TaskStatus
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from data.repos.marker_repo import MarkerRepo
from data.repos.point_repo import PointRepo
from data.repos.task_repo import TaskRepo
from tests.helpers import ConnectionFactoryStub, make_broken_factories, make_v4_db

BASE = datetime(2026, 3, 1, 9, 0, 0)


@pytest.fixture
def factory(tmp_path):
    return ConnectionFactoryStub(make_v4_db(str(tmp_path / 'repo.db')))


@pytest.fixture
def repo(factory):
    return TaskRepo(factory)


def _task(**kw) -> MonitorTask:
    fields = dict(task_id=str(uuid.uuid4()), pid=100, process_name='a.exe',
                  metric_types=['memory_rss', 'cpu_percent'], interval=1.0,
                  start_time=BASE, end_time=None, status='running')
    fields.update(kw)
    return MonitorTask(**fields)


def _points(task_id, n, start=BASE, metrics=('memory_rss',)):
    return [DataPoint(task_id, start + timedelta(seconds=i), float(i), m)
            for i in range(n) for m in metrics]


def _count(factory, sql, params=()):
    with factory.connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_save_and_get_roundtrip(repo):
    task = _task(status=TaskStatus.RUNNING, note='备注', create_time=1767196800.5)
    repo.save(task)
    got = repo.get(task.task_id)
    assert got == replace(task, status='running')
    assert repo.get('no-such-task') is None


def test_upsert_keeps_counts_and_create_time(repo, factory):
    task = _task(note='first', create_time=123.5)
    repo.save(task)
    PointRepo(factory).append(_points(task.task_id, 3, metrics=('memory_rss', 'cpu_percent')))

    # 内存里的对象 sample_count=0、create_time=None，再保存不能把库里的值清掉
    updated = replace(task, note='second', status='stopped', end_time=BASE + timedelta(hours=1),
                      sample_count=0, last_sample_at=None, create_time=None)
    repo.save(updated)

    got = repo.get(task.task_id)
    assert got.note == 'second'
    assert got.status == 'stopped'
    assert got.end_time == BASE + timedelta(hours=1)
    assert got.sample_count == 3
    assert got.last_sample_at == BASE + timedelta(seconds=2)
    assert got.create_time == 123.5
    # 重复保存不新增行
    assert _count(factory, 'SELECT COUNT(*) FROM tasks') == 1


def test_upsert_keeps_note_when_resaved_unchanged(repo):
    task = _task(note='保留我')
    repo.save(task)
    repo.save(task)
    assert repo.get(task.task_id).note == '保留我'


def test_save_rejects_non_datetime_and_writes_nothing(repo, factory):
    with pytest.raises(TypeError):
        repo.save(_task(start_time=BASE.timestamp()))
    with pytest.raises(TypeError):
        repo.save(_task(end_time=1.0))
    assert _count(factory, 'SELECT COUNT(*) FROM tasks') == 0


def test_save_without_start_time_is_storage_error(repo):
    # 库约束 start_time NOT NULL：未启动（pending）的任务不应落库
    with pytest.raises(StorageError) as info:
        repo.save(_task(start_time=None, status='pending'))
    assert isinstance(info.value.__cause__, sqlite3.IntegrityError)


def test_list_all_orders_by_start_time_desc(repo):
    older = _task(start_time=BASE)
    newer = _task(start_time=BASE + timedelta(days=1))
    repo.save(older)
    repo.save(newer)
    assert [t.task_id for t in repo.list_all()] == [newer.task_id, older.task_id]


def test_list_with_data_filters_by_sample_count(repo, factory):
    with_data = _task(start_time=BASE)
    newer_with_data = _task(start_time=BASE + timedelta(hours=1))
    without = _task(start_time=BASE + timedelta(hours=2))
    for t in (with_data, newer_with_data, without):
        repo.save(t)
    PointRepo(factory).append(_points(with_data.task_id, 2) + _points(newer_with_data.task_id, 1))
    assert [t.task_id for t in repo.list_with_data()] == [
        newer_with_data.task_id, with_data.task_id]


def test_list_with_data_falls_back_when_sample_count_missing(repo, factory):
    with_data, without = _task(), _task()
    repo.save(with_data)
    repo.save(without)
    PointRepo(factory).append(_points(with_data.task_id, 2))
    with factory.connect() as conn:
        conn.execute('ALTER TABLE tasks DROP COLUMN sample_count')
    tasks = repo.list_with_data()
    assert [t.task_id for t in tasks] == [with_data.task_id]
    assert tasks[0].sample_count == 0


def test_update_status(repo):
    task = _task(end_time=BASE + timedelta(minutes=5))
    repo.save(task)

    # 不给 end_time：只改状态，原结束时间不动
    assert repo.update_status(task.task_id, 'stopped', None) is True
    got = repo.get(task.task_id)
    assert (got.status, got.end_time) == ('stopped', BASE + timedelta(minutes=5))

    end = BASE + timedelta(hours=2)
    assert repo.update_status(task.task_id, TaskStatus.STOPPED, end) is True
    got = repo.get(task.task_id)
    assert (got.status, got.end_time) == ('stopped', end)

    assert repo.update_status('no-such-task', 'stopped', end) is False
    with pytest.raises(TypeError):
        repo.update_status(task.task_id, 'stopped', end.timestamp())


def test_delete_removes_task_points_and_markers(repo, factory):
    victim, other = _task(), _task()
    repo.save(victim)
    repo.save(other)
    points = PointRepo(factory)
    points.append(_points(victim.task_id, 3) + _points(other.task_id, 2))
    markers = MarkerRepo(factory)
    markers.add(victim.task_id, BASE, 'a')
    markers.add(other.task_id, BASE, 'b')

    assert repo.delete(victim.task_id) is True

    assert repo.get(victim.task_id) is None
    # v5 样本在 series/samples 里（samples 不带 task_id，按序列统计）
    samples_sql = ('SELECT COUNT(*) FROM samples WHERE series_id IN '
                   '(SELECT series_id FROM series WHERE task_id = ?)')
    for sql in (samples_sql, 'SELECT COUNT(*) FROM series WHERE task_id = ?',
                'SELECT COUNT(*) FROM task_markers WHERE task_id = ?'):
        assert _count(factory, sql, (victim.task_id,)) == 0
        assert _count(factory, sql, (other.task_id,)) > 0
    # 没有留下失去序列的样本：剩下的恰好是 other 的 2 行
    assert _count(factory, 'SELECT COUNT(*) FROM samples', ()) == 2
    assert repo.get(other.task_id) is not None
    assert repo.delete(victim.task_id) is False


def test_reconcile_orphans(repo, factory):
    sampled = _task(status='running')
    unsampled = _task(status='running')
    stopped = _task(status='stopped', end_time=BASE + timedelta(minutes=1))
    for t in (sampled, unsampled, stopped):
        repo.save(t)
    PointRepo(factory).append(_points(sampled.task_id, 5))

    before = datetime.now()
    assert repo.reconcile_orphans() == 2
    after = datetime.now()

    got = repo.get(sampled.task_id)
    assert (got.status, got.end_time) == ('stopped', BASE + timedelta(seconds=4))
    got = repo.get(unsampled.task_id)
    assert got.status == 'stopped' and before <= got.end_time <= after
    assert repo.get(stopped.task_id).end_time == BASE + timedelta(minutes=1)
    assert repo.reconcile_orphans() == 0


def test_all_methods_wrap_sqlite_errors(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        repo = TaskRepo(factory)
        calls = [
            lambda: repo.save(_task()),
            lambda: repo.get('x'),
            lambda: repo.list_all(),
            lambda: repo.list_with_data(),
            lambda: repo.update_status('x', 'stopped', BASE),
            lambda: repo.delete('x'),
            lambda: repo.reconcile_orphans(),
        ]
        for call in calls:
            with pytest.raises(StorageError) as info:
                call()
            assert isinstance(info.value.__cause__, sqlite3.Error)


def test_read_failure_on_missing_table_is_storage_error(repo, factory):
    with factory.connect() as conn:
        conn.execute('DROP TABLE tasks')
    with pytest.raises(StorageError):
        repo.list_all()
    with pytest.raises(StorageError):
        repo.get('x')


# ---------- plan_json（自动化方案） ----------

PLAN_JSON = '{"schema_version": 1, "timed_marker_seconds": 600, "log_path": "D:\\\\日志\\\\a.log"}'


def test_save_and_load_plan(repo):
    task = _task()
    repo.save(task)
    assert repo.load_plan(task.task_id) is None
    assert repo.get(task.task_id).plan_json is None

    assert repo.save_plan(task.task_id, PLAN_JSON) is True
    assert repo.load_plan(task.task_id) == PLAN_JSON
    assert repo.get(task.task_id).plan_json == PLAN_JSON
    assert [t.plan_json for t in repo.list_all()] == [PLAN_JSON]

    assert repo.save_plan(task.task_id, None) is True
    assert repo.load_plan(task.task_id) is None


def test_save_plan_before_task_row_exists_reports_miss(repo):
    """任务行由采集线程启动时写入：之前调用 save_plan 不新建残缺行，只返回 False"""
    assert repo.save_plan('not-yet', PLAN_JSON) is False
    assert repo.get('not-yet') is None
    assert repo.load_plan('not-yet') is None


def test_upsert_does_not_clear_plan(repo):
    task = _task()
    repo.save(task)
    repo.save_plan(task.task_id, PLAN_JSON)
    # 采集线程收尾时再次保存（模型里 plan_json 为 None）不能把方案清掉
    repo.save(replace(task, status='stopped', end_time=BASE + timedelta(hours=1)))
    assert repo.load_plan(task.task_id) == PLAN_JSON
    # 模型带着别的值也不会经 save 覆盖：方案只经 save_plan 写
    repo.save(replace(task, plan_json='{"timed_marker_seconds": 1}'))
    assert repo.load_plan(task.task_id) == PLAN_JSON


def test_plan_methods_wrap_storage_errors(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        repo = TaskRepo(factory)
        with pytest.raises(StorageError):
            repo.save_plan('x', PLAN_JSON)
        with pytest.raises(StorageError):
            repo.load_plan('x')


# ---------- 数据质量列（planned_cycles 等） ----------

def test_save_and_load_quality(repo):
    from data.models import QualityStats
    task = _task()
    repo.save(task)
    assert repo.load_quality(task.task_id) is None
    assert repo.get(task.task_id).quality is None

    quality = QualityStats(12370, 12345, 20, 12, 5, write_p95_ms=2)
    assert repo.save_quality(task.task_id, quality) is True
    # write_p95_ms 没有列：读回为 None，其余五列原样
    expected = QualityStats(12370, 12345, 20, 12, 5, None)
    assert repo.load_quality(task.task_id) == expected
    assert repo.get(task.task_id).quality == expected
    assert [t.quality for t in repo.list_all()] == [expected]

    # 重复写以最后一次为准；p95 为空照样可存
    assert repo.save_quality(task.task_id, QualityStats(3, 3, 0)) is True
    assert repo.load_quality(task.task_id) == QualityStats(3, 3, 0, None, None)


def test_save_quality_before_task_row_exists_reports_miss(repo):
    from data.models import QualityStats
    assert repo.save_quality('missing', QualityStats(1, 1, 0)) is False
    assert repo.load_quality('missing') is None


def test_upsert_does_not_clear_quality(repo):
    from data.models import QualityStats
    task = _task()
    repo.save(task)
    repo.save_quality(task.task_id, QualityStats(10, 9, 1, 2, 3))
    repo.save(replace(task, status='stopped', end_time=BASE + timedelta(hours=1)))
    assert repo.load_quality(task.task_id) == QualityStats(10, 9, 1, 2, 3)


def test_quality_methods_wrap_storage_errors(tmp_path):
    from data.models import QualityStats
    for factory in make_broken_factories(str(tmp_path)):
        repo = TaskRepo(factory)
        with pytest.raises(StorageError):
            repo.save_quality('x', QualityStats(1, 1, 0))
        with pytest.raises(StorageError):
            repo.load_quality('x')


# ---------- meta_json（被测目标元数据） ----------

META_JSON = '{"schema_version": 1, "exe_path": "C:\\\\演示\\\\app.exe", "file_version": "1.2.3.4"}'


def test_save_and_load_meta(repo):
    task = _task()
    repo.save(task)
    assert repo.load_meta(task.task_id) is None
    assert repo.get(task.task_id).meta_json is None

    assert repo.save_meta(task.task_id, META_JSON) is True
    assert repo.load_meta(task.task_id) == META_JSON
    assert repo.get(task.task_id).meta_json == META_JSON
    assert [t.meta_json for t in repo.list_all()] == [META_JSON]

    assert repo.save_meta(task.task_id, None) is True
    assert repo.load_meta(task.task_id) is None


def test_save_meta_before_task_row_exists_reports_miss(repo):
    assert repo.save_meta('not-yet', META_JSON) is False
    assert repo.get('not-yet') is None
    assert repo.load_meta('not-yet') is None


def test_upsert_does_not_clear_meta(repo):
    task = _task()
    repo.save(task)
    repo.save_meta(task.task_id, META_JSON)
    repo.save(replace(task, status='stopped', end_time=BASE + timedelta(hours=1)))
    assert repo.load_meta(task.task_id) == META_JSON
    # 模型带着别的值也不会经 save 覆盖：元数据只经 save_meta 写
    repo.save(replace(task, meta_json='{"exe_path": "x"}'))
    assert repo.load_meta(task.task_id) == META_JSON


def test_meta_methods_wrap_storage_errors(tmp_path):
    for factory in make_broken_factories(str(tmp_path)):
        repo = TaskRepo(factory)
        with pytest.raises(StorageError):
            repo.save_meta('x', META_JSON)
        with pytest.raises(StorageError):
            repo.load_meta('x')
