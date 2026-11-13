"""迁移备份删除边界：只删明确选中的独立备份，维护锁与失败路径可恢复。"""
import os
from pathlib import Path

import pytest

from core.maintenance_service import MaintenanceBusy, MaintenanceService
from tests.test_maintenance_service import FakeManager


def test_delete_backup_preserves_database_and_other_files(db):
    manager = FakeManager()
    service = MaintenanceService(db, manager)
    database = Path(db.db_path)
    backup = Path(str(database) + '.bak_v1')
    backup.write_bytes(b'old database')
    db.backup_path = str(backup)
    before = database.read_bytes()
    service.delete_migration_backup(str(backup))
    assert not backup.exists() and database.read_bytes() == before
    assert db.backup_path is None and not manager.maintenance


def test_outside_backup_and_database_itself_are_rejected(db, tmp_path):
    service = MaintenanceService(db, FakeManager())
    other = tmp_path / 'outside' / (Path(db.db_path).name + '.bak_v1')
    other.parent.mkdir()
    other.write_bytes(b'keep')
    for path in (db.db_path, str(other)):
        with pytest.raises(ValueError):
            service.delete_migration_backup(path)
    assert other.read_bytes() == b'keep'


def test_hardlink_to_current_database_is_rejected(db):
    backup = Path(db.db_path + '.bak_v1')
    os.link(db.db_path, backup)
    try:
        with pytest.raises(ValueError):
            MaintenanceService(db, FakeManager()).delete_migration_backup(str(backup))
        assert backup.exists()
    finally:
        backup.unlink()


def test_running_task_blocks_backup_delete(db):
    manager = FakeManager()
    manager.tasks_running = True
    backup = Path(db.db_path + '.bak_v1')
    backup.write_bytes(b'keep')
    with pytest.raises(MaintenanceBusy):
        MaintenanceService(db, manager).delete_migration_backup(str(backup))
    assert backup.exists()


def test_failed_delete_releases_maintenance_lock(db, monkeypatch):
    manager = FakeManager()
    backup = Path(db.db_path + '.bak_v1')
    backup.write_bytes(b'keep')
    def denied(*args):
        raise PermissionError('备份正被占用')
    monkeypatch.setattr(Path, 'unlink', denied)
    with pytest.raises(PermissionError):
        MaintenanceService(db, manager).delete_migration_backup(str(backup))
    assert not manager.maintenance and backup.exists()


def test_storage_job_blocks_every_maintenance_service_and_batch_deletion(db, qapp):
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    manager = MonitorManager(db)
    health = MaintenanceService(db, manager)
    settings = MaintenanceService(db, manager)
    assert health.begin_storage_job()
    try:
        with pytest.raises(MaintenanceBusy):
            settings.begin()
        with pytest.raises(ValueError):
            manager.start_task_deletion(['any'], ThreadRegistry())
        assert not manager.is_maintenance_running()
    finally:
        health.end_storage_job()
    settings.begin()
    try:
        assert not health.begin_storage_job()
    finally:
        settings.end()


def test_backup_discovery_survives_restart_and_ignores_unrelated_files(db):
    database = Path(db.db_path)
    older = Path(str(database) + '.bak_v1')
    newer = Path(str(database) + '.bak_v1_20261112_100000')
    older.write_bytes(b'older')
    newer.write_bytes(b'newer')
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    Path(str(database) + '.bak_v1_directory').mkdir()
    Path(str(database) + '.bak_v1.unrelated').write_bytes(b'keep')
    db.backup_path = None
    assert MaintenanceService(db, FakeManager()).list_migration_backups() == [str(newer), str(older)]


def test_quick_check_blocks_other_service_until_cancelled(db, qapp, monkeypatch):
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    from core.storage_guard import StorageGuard
    from tests.test_storage_guard import FakeQuickCheck
    from tests.helpers import process_events_until
    manager, registry = MonitorManager(db), ThreadRegistry()
    health = MaintenanceService(db, manager)
    other = MaintenanceService(db, manager)
    check = FakeQuickCheck(block=True)
    monkeypatch.setattr(health, 'new_quick_check', lambda: check)
    guard = StorageGuard(manager, health, registry, db.db_path)
    try:
        assert guard._maybe_start_quick_check(0)
        assert check.started.wait(2)
        with pytest.raises(MaintenanceBusy):
            other.begin()
        with pytest.raises(ValueError):
            manager.start_task_deletion(['any'], registry)
        guard.stop()
        assert process_events_until(lambda: not guard.quick_check_running, 5)
        other.begin()
        other.end()
    finally:
        guard.stop()
        registry.cancel_all()
        registry.shutdown_all()


def test_quick_check_construction_failure_releases_shared_count(db, qapp, monkeypatch):
    from core.monitor_manager import MonitorManager
    from core.shutdown import ThreadRegistry
    from core.storage_guard import StorageGuard
    manager = MonitorManager(db)
    service = MaintenanceService(db, manager)
    def fail():
        raise OSError('库无法打开')
    monkeypatch.setattr(service, 'new_quick_check', fail)
    guard = StorageGuard(manager, service, ThreadRegistry(), db.db_path)
    with pytest.raises(OSError):
        guard._maybe_start_quick_check(0)
    assert not manager.has_storage_jobs()
    service.begin()
    service.end()
