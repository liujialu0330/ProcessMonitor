"""
反向兼容：用户从当前版本回退到已发布的 1.4.1 后，1.4.1 仍能使用当前版本建的库

1.4.1 的 Database（tests/fixtures/legacy_db/frozen_v1_4_1/ 冻结副本）只认 user_version
是否 >= 1：版本更高的库直接按原样使用，不迁移、不备份。本文件锁住这条回退路径：
当前版本建库写数 → 1.4.1 打开后能列任务、读点、新增任务写点、删除任务，且不改
user_version、不动表结构、不生成 .bak → 当前版本再打开，降级期间写入的数据仍在。

v5 起样本改存 series / samples 紧凑表，data_points 保留 v4 定义但为空表，因此：
- 1.4.1 打开 v5 库能列任务（tasks 新增列都有默认值），但看不到 2.0 写入的历史
  （它只读 data_points）；
- 1.4.1 降级期间新写入的点落在 data_points 里；
- 再升回 2.0 时启动校正把 data_points 中的点导回 samples（旧内存键名合并、重算计数）并清空
  data_points，降级期间写入的任务与点在 2.0 中可见；
- 1.4.1 删除任务只删 tasks 与 data_points，留下的孤儿 series / samples（以及打点）由启动校正清掉。
"""
import os
import sqlite3
from datetime import datetime, timedelta

from core.maintenance_service import MaintenanceService
from data.database import Database
from data.db.migrations.runner import MigrationOutcome
from data.db.schema import SCHEMA_VERSION
from data.models import DataPoint, MonitorTask
from tests.fixtures.legacy_db.frozen_v1_4_1.database import Database as LegacyDatabase
from tests.fixtures.legacy_db.frozen_v1_4_1.models import (
    DataPoint as LegacyDataPoint, MonitorTask as LegacyMonitorTask,
)

BASE = datetime(2026, 3, 1, 9, 0, 0, 250000)


def _schema(path):
    conn = sqlite3.connect(path)
    try:
        return (conn.execute('PRAGMA user_version').fetchone()[0],
                sorted(conn.execute('SELECT type, name, sql FROM sqlite_master').fetchall()))
    finally:
        conn.close()


def _files(directory):
    return sorted(os.listdir(directory))


def _seed_current(db_path):
    """当前版本建库：两个已结束任务（一个带备注与打点），各若干周期的多指标数据点"""
    db = Database(db_path)
    try:
        assert db.migration_report.created
        for task_id, name, note in (('cur-keep', '当前版本_保留.exe', '回退前的备注'),
                                    ('cur-drop', '当前版本_删除.exe', '')):
            task = MonitorTask(
                task_id=task_id, pid=100, process_name=name,
                metric_types=['memory_private', 'num_handles'], interval=1.0,
                start_time=BASE, end_time=BASE + timedelta(seconds=30),
                status='stopped', note=note, create_time=1700000000.5)
            assert db.save_task(task)
            points = []
            for i in range(30):
                ts = BASE + timedelta(seconds=i)
                points.append(DataPoint(task_id, ts, 1000.0 + i, 'memory_private'))
                points.append(DataPoint(task_id, ts, 50.0 + i, 'num_handles'))
            assert db.save_data_points(points)
            assert db.add_marker(task_id, BASE + timedelta(seconds=10), '第1轮')
    finally:
        db.close()


def test_v141_uses_current_db_without_migrating(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _seed_current(db_path)
    schema_before = _schema(db_path)
    assert schema_before[0] == SCHEMA_VERSION

    legacy = LegacyDatabase(db_path)

    # 打开即用：不迁移、不备份、不报任何迁移状态
    assert (legacy.migration_failed, legacy.data_reset, legacy.backup_aborted) == \
        (False, False, False)
    assert not any('.bak' in name for name in _files(tmp_path))

    # 列任务；2.0 写入的样本在 samples 里，1.4.1 只读 data_points，看不到历史
    assert {t.task_id for t in legacy.get_all_tasks()} == {'cur-keep', 'cur-drop'}
    keep = legacy.get_task('cur-keep')
    assert keep.process_name == '当前版本_保留.exe'
    assert keep.metric_types == ['memory_private', 'num_handles']
    assert legacy.get_task_data_points('cur-keep', 'memory_private') == []
    assert legacy.get_sample_count('cur-keep') == 0
    assert legacy.get_metric_stats('cur-keep', 'num_handles') is None

    # 新增任务并写点（1.4.1 的采集流程：save_task → 每周期 save_data_points → 停止时更新状态）
    start = BASE + timedelta(hours=1)
    assert legacy.save_task(LegacyMonitorTask(
        task_id='legacy-new', pid=200, process_name='回退期间_新任务.exe',
        metric_types=['memory_private', 'num_handles'], interval=1.0,
        start_time=start, end_time=None, status='running'))
    for i in range(20):
        ts = start + timedelta(seconds=i)
        assert legacy.save_data_points([
            LegacyDataPoint('legacy-new', ts, 2000.0 + i, 'memory_private'),
            LegacyDataPoint('legacy-new', ts, 80.0 + i, 'num_handles'),
        ])
    # 旧版键名：1.4.1 自己不会再写 memory_vms，这里直接插几行，锁住"导回时合并旧键名"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO data_points (task_id, timestamp, value, metric_type) "
            "VALUES ('legacy-new', ?, 1.0, 'memory_vms')",
            ((start + timedelta(seconds=30)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    assert legacy.update_task_status('legacy-new', 'stopped', start + timedelta(seconds=20))
    assert len(legacy.get_task_data_points('legacy-new', 'num_handles')) == 20

    # 删除任务
    assert legacy.delete_task('cur-drop')
    assert legacy.get_task('cur-drop') is None
    del legacy

    # 版本号与结构原样，没有备份文件
    assert _schema(db_path) == schema_before
    assert not any('.bak' in name for name in _files(tmp_path))

    # 当前版本再打开：不迁移；启动校正前降级期间写的点还在 data_points 里，2.0 看不到
    db = Database(db_path)
    try:
        assert db.migration_report.outcome is MigrationOutcome.UP_TO_DATE
        assert not db.migration_report.created
        assert db.get_task('legacy-new').sample_count == 0
        assert db.get_task_data_points('legacy-new') == []

        result = MaintenanceService(db, manager=None).startup_reconcile(0)
        # 41 行 = 20 周期 × 2 指标 + 1 行旧键名；导回时已重算计数，recount 无事可做
        assert result.imported == 41
        assert result.recounted == 0
        # cur-drop 的两条序列
        assert result.purged == 2

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute('SELECT COUNT(*) FROM data_points').fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM series WHERE task_id = 'cur-drop'").fetchone()[0] == 0
            assert conn.execute(
                'SELECT COUNT(*) FROM samples WHERE series_id NOT IN '
                '(SELECT series_id FROM series)').fetchone()[0] == 0
        finally:
            conn.close()

        new = db.get_task('legacy-new')
        assert new.status == 'stopped'
        assert new.sample_count == 21
        assert new.last_sample_at == start + timedelta(seconds=30)
        assert new.note == '' and new.create_time is None
        assert {t.task_id for t in db.get_tasks_with_data()} == {'cur-keep', 'legacy-new'}
        assert len(db.get_task_data_points('legacy-new', 'memory_private')) == 21
        assert len(db.get_task_data_points('legacy-new', 'num_handles')) == 20
        assert db.get_task_data_points('legacy-new', 'memory_vms') == []
        # 分析页可见：统计与分桶都能算出
        stats = db.get_metric_stats('legacy-new', 'num_handles')
        assert stats['count'] == 20 and stats['first'] == 80.0 and stats['last'] == 99.0

        # 回退期间没碰过的任务：备注、计数、进程创建时间、打点都在
        keep = db.get_task('cur-keep')
        assert keep.note == '回退前的备注'
        assert keep.sample_count == 30
        assert keep.create_time == 1700000000.5
        assert [m.label for m in db.markers.list('cur-keep')] == ['第1轮']

        # 1.4.1 删除任务只删 data_points 与 tasks，不知道 series / samples / task_markers；
        # 启动校正把这些孤儿行一并清掉（v4 时打点会作为孤儿行一直留在库里）
        assert db.get_task('cur-drop') is None
        assert db.get_task_data_points('cur-drop') == []
        assert db.markers.list('cur-drop') == []
    finally:
        db.close()


def test_v141_save_task_on_existing_row_resets_new_columns(tmp_path):
    """
    1.4.1 的 save_task 是 INSERT OR REPLACE 且只写 8 个旧列：若它对已有任务再调用一次，
    当前版本的 note / sample_count / last_sample_at / create_time 会被重置为默认值。
    1.4.1 只在创建任务时调用 save_task，正常回退不会触发；此用例锁住这一行为，
    供后续评估"降级写入"风险时参考。sample_count 可由启动校正恢复，其余三列不可恢复。
    """
    db_path = str(tmp_path / 'monitor.db')
    _seed_current(db_path)

    legacy = LegacyDatabase(db_path)
    task = legacy.get_task('cur-keep')
    assert legacy.save_task(task)
    del legacy

    db = Database(db_path)
    try:
        replaced = db.get_task('cur-keep')
        assert replaced.note == ''
        assert replaced.sample_count == 0
        assert replaced.last_sample_at is None
        assert replaced.create_time is None
        MaintenanceService(db, manager=None).startup_reconcile(0)
        assert db.get_task('cur-keep').sample_count == 30
    finally:
        db.close()
