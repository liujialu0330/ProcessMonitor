"""
reconcile_sample_counts：用户回退旧版本运行后再升级回来，旧版写入的任务
sample_count=0（只写数据点、不维护计数），启动时需按数据点重算，
否则这些任务会在历史页/导出页永久消失。
"""
import sqlite3
from datetime import datetime, timedelta

from data.models import DataPoint
from tests.test_schema_v3 import _make_task, _points


def _insert_points_like_old_version(db_path, task_id, base, n, metric="memory_rss"):
    """模拟旧版写入：直接插数据点，不更新 tasks.sample_count / last_sample_at"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            'INSERT INTO data_points (task_id, timestamp, value, metric_type) '
            'VALUES (?, ?, ?, ?)',
            [(task_id, (base + timedelta(seconds=i)).isoformat(), float(i), metric)
             for i in range(n)])
        conn.commit()
    finally:
        conn.close()


def test_reconcile_sample_counts_restores_old_version_tasks(db, db_path):
    base = datetime(2026, 3, 1, 9, 0, 0)
    legacy = _make_task(status='stopped')
    healthy = _make_task(status='stopped')
    empty = _make_task(status='stopped')
    for t in (legacy, healthy, empty):
        db.save_task(t)
    _insert_points_like_old_version(db_path, legacy.task_id, base, 5)
    db.save_data_points(_points(healthy.task_id, base, 3))

    # 校正前：旧版任务在历史页/导出页口径下不可见
    assert legacy.task_id not in {t.task_id for t in db.get_tasks_with_data()}

    assert db.reconcile_sample_counts() == 1

    fixed = db.get_task(legacy.task_id)
    assert fixed.sample_count == 5
    assert fixed.last_sample_at == base + timedelta(seconds=4)
    assert legacy.task_id in {t.task_id for t in db.get_tasks_with_data()}
    # 正常任务与无数据任务不受影响
    assert db.get_task(healthy.task_id).sample_count == 3
    assert db.get_task(empty.task_id).sample_count == 0
    assert db.get_task(empty.task_id).last_sample_at is None
    # 幂等
    assert db.reconcile_sample_counts() == 0


def test_reconcile_sample_counts_counts_distinct_timestamps(db, db_path):
    """同一周期多指标只算一次采样"""
    base = datetime(2026, 3, 1, 9, 0, 0)
    task = _make_task(status='stopped')
    db.save_task(task)
    _insert_points_like_old_version(db_path, task.task_id, base, 4, metric="memory_rss")
    _insert_points_like_old_version(db_path, task.task_id, base, 4, metric="cpu_percent")

    assert db.reconcile_sample_counts() == 1
    assert db.get_task(task.task_id).sample_count == 4


def test_startup_reconcile_restores_sample_counts(qapp, tmp_path):
    """启动校正由组合根经 MaintenanceService.startup_reconcile 执行（主窗口不再调用，
    启动顺序见 tests/test_bootstrap.py）：旧版写入的任务在启动后重新可见"""
    from core.maintenance_service import MaintenanceService
    from core.monitor_manager import MonitorManager
    from data.database import Database

    db_file = str(tmp_path / "startup.db")
    db = Database(db_file)
    task = _make_task(status='stopped')
    db.save_task(task)
    _insert_points_like_old_version(db_file, task.task_id, datetime(2026, 3, 1), 3)

    MaintenanceService(db, MonitorManager(db=db)).startup_reconcile(0)

    assert db.get_task(task.task_id).sample_count == 3
