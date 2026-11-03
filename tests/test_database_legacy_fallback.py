"""
库仍停留在旧版本（迁移失败、tasks 缺 sample_count 列）时，get_tasks_with_data /
get_sample_count 要回退到按 data_points 计算，而不是返回空。
"""
import sqlite3
import uuid
from datetime import datetime, timedelta

from data.models import DataPoint, MonitorTask


def _task(name="legacy.exe") -> MonitorTask:
    return MonitorTask(
        task_id=str(uuid.uuid4()), pid=100, process_name=name,
        metric_types=["memory_private", "num_handles"], interval=1.0,
        start_time=datetime(2026, 1, 1, 0, 0, 0), end_time=None, status="stopped")


def _seed(db, task, samples):
    base = datetime(2026, 1, 1, 0, 0, 0)
    points = []
    for i in range(samples):
        ts = base + timedelta(seconds=i)
        points.append(DataPoint(task.task_id, ts, 1.0 + i, "memory_private"))
        points.append(DataPoint(task.task_id, ts, 10.0 + i, "num_handles"))
    db.save_data_points(points)


def _drop_sample_count_column(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE tasks DROP COLUMN sample_count")
        conn.commit()
    finally:
        conn.close()


def test_current_schema_filters_by_sample_count(db):
    """基线：新库按 sample_count 过滤，没有数据的任务不出现"""
    with_data, without_data = _task("a.exe"), _task("b.exe")
    for task in (with_data, without_data):
        db.save_task(task)
    _seed(db, with_data, 3)

    assert [t.task_id for t in db.get_tasks_with_data()] == [with_data.task_id]
    assert db.get_sample_count(with_data.task_id) == 3


def test_get_tasks_with_data_falls_back_when_sample_count_column_missing(db, db_path):
    with_data, without_data = _task("a.exe"), _task("b.exe")
    for task in (with_data, without_data):
        db.save_task(task)
    _seed(db, with_data, 4)
    _drop_sample_count_column(db_path)

    tasks = db.get_tasks_with_data()

    assert [t.task_id for t in tasks] == [with_data.task_id]
    # 旧结构下新增列按默认值处理，不报错
    assert tasks[0].sample_count == 0
    assert tasks[0].process_name == "a.exe"


def test_get_sample_count_falls_back_to_distinct_timestamps(db, db_path):
    task = _task()
    db.save_task(task)
    _seed(db, task, 5)  # 5 个周期 x 2 个指标 = 10 条数据点
    _drop_sample_count_column(db_path)

    assert db.get_sample_count(task.task_id) == 5
    assert db.get_sample_count("no-such-task") == 0


def test_fallback_keeps_start_time_descending_order(db, db_path):
    older, newer = _task("old.exe"), _task("new.exe")
    older.start_time = datetime(2026, 1, 1, 0, 0, 0)
    newer.start_time = datetime(2026, 1, 2, 0, 0, 0)
    for task in (older, newer):
        db.save_task(task)
        _seed(db, task, 2)
    _drop_sample_count_column(db_path)

    assert [t.process_name for t in db.get_tasks_with_data()] == ["new.exe", "old.exe"]
