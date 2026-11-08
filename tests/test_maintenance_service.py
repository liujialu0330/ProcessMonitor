"""MaintenanceService：启动校正顺序、清理压缩与重建汇总时维护锁的持有/释放（一段式与两段式）、
忙碌原因与失败上抛"""
from datetime import datetime, timedelta

import pytest

from core.maintenance_service import (
    BUSY_EXPORT_RUNNING, BUSY_MAINTENANCE_RUNNING, BUSY_TASKS_RUNNING, CleanupResult,
    MaintenanceBusy,
    MaintenanceService, ReconcileResult,
)
from data.errors import StorageError
from data.models import DataPoint, MonitorTask


class FakeManager:
    """只实现维护锁相关接口的管理器替身"""

    def __init__(self, tasks_running=False, exports_running=False):
        self.tasks_running = tasks_running
        self.exports_running = exports_running
        self.maintenance = False
        self.calls = []

    def has_running_tasks(self):
        return self.tasks_running

    def has_running_exports(self):
        return self.exports_running

    def begin_maintenance(self):
        self.calls.append('begin')
        if self.tasks_running or self.exports_running:
            return False
        self.maintenance = True
        return True

    def end_maintenance(self):
        self.calls.append('end')
        self.maintenance = False

    def is_maintenance_running(self):
        return self.maintenance


class RecordingDb:
    """记录启动校正的调用顺序"""

    def __init__(self):
        self.calls = []

    def touch_writer(self, version):
        self.calls.append('touch')
        return True

    def import_legacy_points(self):
        self.calls.append('import')
        return 5

    def purge_orphan_series(self):
        self.calls.append('purge')
        return 6

    def reconcile_orphan_tasks(self):
        self.calls.append('orphans')
        return 2

    def reconcile_sample_counts(self):
        self.calls.append('recount')
        return 3

    def cleanup_old_tasks(self, days):
        self.calls.append(('cleanup', days))
        return 4


def _old_stopped_task(db, days_ago=40):
    start = datetime.now() - timedelta(days=days_ago)
    task = MonitorTask(task_id=f"old-{days_ago}", pid=1, process_name="old.exe",
                       metric_types=["memory_rss"], interval=1.0, start_time=start,
                       end_time=start + timedelta(minutes=1), status="stopped")
    db.save_task(task)
    return task


# ========== startup_reconcile ==========

def test_startup_reconcile_order_and_result():
    db = RecordingDb()
    result = MaintenanceService(db, FakeManager()).startup_reconcile(30)
    # v5 起先记写入版本、导回 1.4.1 写的 data_points、清孤儿序列，再走原有三步
    assert db.calls == ['touch', 'import', 'purge', 'orphans', 'recount', ('cleanup', 30)]
    assert result == ReconcileResult(orphans=2, recounted=3, cleaned=4, imported=5, purged=6)


@pytest.mark.parametrize("days", [0, -1])
def test_startup_reconcile_skips_cleanup_when_disabled(days):
    db = RecordingDb()
    result = MaintenanceService(db, FakeManager()).startup_reconcile(days)
    assert db.calls == ['touch', 'import', 'purge', 'orphans', 'recount']
    assert result.cleaned == 0


def test_startup_reconcile_skipped_for_read_only_db():
    """只读打开的库（由更高版本写入）不做任何校正：每一步都要写库"""
    db = RecordingDb()
    db.read_only = True
    result = MaintenanceService(db, FakeManager()).startup_reconcile(30)
    assert db.calls == []
    assert result == ReconcileResult(orphans=0, recounted=0, cleaned=0)


def test_startup_reconcile_on_real_db(db):
    """真实库：遗留 running 任务被校正为 stopped，过期任务被清理"""
    running = MonitorTask(task_id="orphan", pid=1, process_name="a.exe",
                          metric_types=["memory_rss"], interval=1.0,
                          start_time=datetime.now(), end_time=None, status="running")
    db.save_task(running)
    _old_stopped_task(db)

    result = MaintenanceService(db, FakeManager()).startup_reconcile(30)

    assert result.orphans == 1
    assert result.cleaned == 1
    assert db.get_task("orphan").status == "stopped"
    assert db.get_task("old-40") is None


def test_startup_reconcile_purges_markers_of_deleted_tasks(db):
    """任务已不存在的打点（1.4.1 删任务只删它认识的表）由启动校正清掉，仍存在任务的打点不动；
    再校正一次无事可做"""
    now = datetime.now()
    for task_id in ("keep", "gone"):
        db.save_task(MonitorTask(task_id=task_id, pid=1, process_name="a.exe",
                                 metric_types=["memory_rss"], interval=1.0, start_time=now,
                                 end_time=now + timedelta(minutes=1), status="stopped"))
        db.markers.add(task_id, now, f"{task_id}-基线", kind='baseline')
        db.markers.add(task_id, now + timedelta(seconds=30), f"{task_id}-打点")
    with db._factory.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE task_id = 'gone'")   # 1.4.1 的删除：附属表留下孤儿行
        assert conn.execute("SELECT COUNT(*) FROM task_markers WHERE task_id = 'gone'"
                            ).fetchone()[0] == 2

    MaintenanceService(db, FakeManager()).startup_reconcile(0)

    assert db.markers.list("gone") == []
    assert [m.label for m in db.markers.list("keep")] == ["keep-基线", "keep-打点"]
    with db._factory.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_markers").fetchone()[0] == 2
    # 再校正一次：没有新的孤儿，保留的打点原样
    MaintenanceService(db, FakeManager()).startup_reconcile(0)
    assert len(db.markers.list("keep")) == 2


# ========== cleanup_and_vacuum ==========

def test_cleanup_holds_and_releases_lock(db):
    manager = FakeManager()
    seen = []
    original_vacuum = db.maintenance.vacuum

    def vacuum():
        seen.append(manager.is_maintenance_running())
        original_vacuum()

    db.maintenance.vacuum = vacuum
    _old_stopped_task(db)

    result = MaintenanceService(db, manager).cleanup_and_vacuum(30)

    assert isinstance(result, CleanupResult)
    assert result.deleted == 1
    assert result.size_before > 0 and result.size_after > 0
    assert seen == [True]                     # 压缩期间持有维护锁
    assert manager.calls == ['begin', 'end']
    assert manager.is_maintenance_running() is False


def test_cleanup_without_retention_only_vacuums(db):
    _old_stopped_task(db)
    result = MaintenanceService(db, FakeManager()).cleanup_and_vacuum(0)
    assert result.deleted == 0
    assert db.get_task("old-40") is not None


@pytest.mark.parametrize("tasks, exports, reason", [
    (True, False, BUSY_TASKS_RUNNING),
    (True, True, BUSY_TASKS_RUNNING),
    (False, True, BUSY_EXPORT_RUNNING),
])
def test_cleanup_rejected_when_busy(db, tasks, exports, reason):
    manager = FakeManager(tasks_running=tasks, exports_running=exports)
    _old_stopped_task(db)
    with pytest.raises(MaintenanceBusy) as info:
        MaintenanceService(db, manager).cleanup_and_vacuum(30)
    assert info.value.reason == reason
    # 被拒时不做任何改动，也不调用 end_maintenance（锁本来就没拿到）
    assert manager.calls == ['begin']
    assert db.get_task("old-40") is not None


@pytest.mark.parametrize("failing", ["cleanup_older_than", "vacuum", "size_bytes"])
def test_storage_error_propagates_and_lock_released(db, failing):
    manager = FakeManager()

    def boom(*args, **kwargs):
        raise StorageError(f"{failing} 失败")

    setattr(db.maintenance, failing, boom)
    with pytest.raises(StorageError):
        MaintenanceService(db, manager).cleanup_and_vacuum(30)
    assert manager.calls == ['begin', 'end']
    assert manager.is_maintenance_running() is False


def test_works_with_real_monitor_manager(db, qapp):
    """与真实 MonitorManager 的维护锁接口对接：执行后锁已释放"""
    from core.monitor_manager import MonitorManager
    manager = MonitorManager(db=db)
    MaintenanceService(db, manager).cleanup_and_vacuum(0)
    assert manager.is_maintenance_running() is False


# ========== 两段式：begin / cleanup_and_vacuum(already_locked=True) / end ==========

def test_begin_and_end_hold_lock():
    manager = FakeManager()
    service = MaintenanceService(None, manager)
    service.begin()
    assert manager.is_maintenance_running() is True
    service.end()
    assert manager.is_maintenance_running() is False
    assert manager.calls == ['begin', 'end']


@pytest.mark.parametrize("tasks, exports, reason", [
    (True, False, BUSY_TASKS_RUNNING),
    (False, True, BUSY_EXPORT_RUNNING),
])
def test_begin_rejected_when_busy(tasks, exports, reason):
    manager = FakeManager(tasks_running=tasks, exports_running=exports)
    with pytest.raises(MaintenanceBusy) as info:
        MaintenanceService(None, manager).begin()
    assert info.value.reason == reason
    assert manager.is_maintenance_running() is False


def test_begin_rejected_while_maintenance_running():
    """已有维护在进行：不重复占锁（否则先结束的一方会替另一方释放锁）"""
    manager = FakeManager()
    service = MaintenanceService(None, manager)
    service.begin()
    with pytest.raises(MaintenanceBusy) as info:
        service.begin()
    assert info.value.reason == BUSY_MAINTENANCE_RUNNING
    assert manager.calls == ['begin']
    assert manager.is_maintenance_running() is True


def test_already_locked_neither_takes_nor_releases_lock(db):
    manager = FakeManager()
    service = MaintenanceService(db, manager)
    service.begin()
    manager.calls.clear()
    _old_stopped_task(db)

    result = service.cleanup_and_vacuum(30, already_locked=True)

    assert result.deleted == 1
    assert manager.calls == []                    # 不占也不放
    assert manager.is_maintenance_running() is True
    service.end()
    assert manager.is_maintenance_running() is False


def test_already_locked_failure_keeps_lock_for_caller(db):
    """already_locked 下失败：异常上抛，锁仍由调用方持有、由调用方 end()"""
    manager = FakeManager()
    service = MaintenanceService(db, manager)
    service.begin()

    def boom():
        raise StorageError("vacuum 失败")

    db.maintenance.vacuum = boom
    with pytest.raises(StorageError):
        service.cleanup_and_vacuum(0, already_locked=True)
    assert manager.is_maintenance_running() is True
    service.end()
    assert manager.calls == ['begin', 'end']


def test_two_phase_with_real_manager_blocks_task_and_export(db, qapp):
    """占锁后（界面线程）立即拒绝新任务与导出，end() 后恢复"""
    import os
    from core.monitor_manager import MonitorManager
    manager = MonitorManager(db=db)
    service = MaintenanceService(db, manager)
    service.begin()
    try:
        assert manager.begin_export() is False
        assert manager.create_task(os.getpid(), "a.exe", ["memory_rss"], 1.0) is None
    finally:
        service.end()
    assert manager.begin_export() is True
    manager.end_export()


@pytest.mark.parametrize("already_locked", [False, True])
def test_cleanup_worker_passes_already_locked(db, qapp, already_locked):
    """CleanupWorker 把 already_locked 原样交给维护服务；结果经 result 发出"""
    from PyQt5.QtCore import Qt
    from core.workers.cleanup_worker import CleanupWorker

    class Service:
        def __init__(self):
            self.calls = []

        def cleanup_and_vacuum(self, days, *, already_locked=False):
            self.calls.append((days, already_locked))
            return CleanupResult(0, 1, 1)

    service = Service()
    worker = CleanupWorker(service, 7, already_locked=already_locked)
    results = []
    worker.result.connect(results.append, Qt.DirectConnection)
    worker.start()
    assert worker.wait(5000)
    assert service.calls == [(7, already_locked)]
    assert results == [CleanupResult(0, 1, 1)]


# ========== rebuild_rollups ==========

def _seed_rollups(db):
    db.save_task(MonitorTask(task_id='r', pid=1, process_name='a.exe', metric_types=['m'],
                             interval=1.0, start_time=datetime(2026, 3, 1, 9),
                             end_time=None, status='stopped'))
    db.save_data_points([DataPoint('r', datetime(2026, 3, 1, 9) + timedelta(seconds=s), 1.0, 'm')
                         for s in range(0, 300, 2)])


def test_rebuild_rollups_holds_and_releases_lock(db):
    from core.maintenance_service import RebuildRollupsResult
    manager = FakeManager()
    seen = []
    original = db.maintenance.rebuild_rollups

    def rebuild():
        seen.append(manager.is_maintenance_running())
        return original()

    db.maintenance.rebuild_rollups = rebuild
    _seed_rollups(db)
    result = MaintenanceService(db, manager).rebuild_rollups()
    assert result == RebuildRollupsResult(series=1)
    assert seen == [True]
    assert manager.calls == ['begin', 'end']


@pytest.mark.parametrize("tasks, exports, reason", [
    (True, False, BUSY_TASKS_RUNNING),
    (False, True, BUSY_EXPORT_RUNNING),
])
def test_rebuild_rollups_rejected_when_busy(db, tasks, exports, reason):
    manager = FakeManager(tasks_running=tasks, exports_running=exports)
    with pytest.raises(MaintenanceBusy) as info:
        MaintenanceService(db, manager).rebuild_rollups()
    assert info.value.reason == reason
    assert manager.calls == ['begin']


def test_rebuild_rollups_already_locked_and_failure(db):
    manager = FakeManager()
    service = MaintenanceService(db, manager)
    service.begin()
    assert service.rebuild_rollups(already_locked=True).series == 0
    assert manager.calls == ['begin'] and manager.is_maintenance_running()

    def boom():
        raise StorageError("重建失败")

    db.maintenance.rebuild_rollups = boom
    with pytest.raises(StorageError):
        service.rebuild_rollups(already_locked=True)
    assert manager.is_maintenance_running()       # 已占锁时失败也不释放，交给调用方 end()
    service.end()
    with pytest.raises(StorageError):
        service.rebuild_rollups()
    assert manager.calls[-2:] == ['begin', 'end']
    assert manager.is_maintenance_running() is False
