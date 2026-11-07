"""
设置页"重建汇总"按钮：与"清理数据库"同样的两段式维护锁——界面线程占锁、后台线程按样本重算、
线程结束后界面线程释放；有运行中任务或导出时直接提示、不启动线程；重算期间两个维护按钮都禁用；
失败时提示且原汇总不变。
"""
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest
from PyQt5.QtWidgets import QApplication

from core.monitor_manager import MonitorManager
from data.errors import StorageError
from data.models import DataPoint, MonitorTask
from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
from tests.factories import make_services, make_setting_page
from tests.helpers import poll_until
from ui.services import ManagerTaskStatus

T0 = datetime(2026, 3, 1, 9, 0, 0)


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def setting(qapp, db, manager, notifier):
    services = make_services(notifier=notifier, dialogs=ScriptedDialogs(),
                             preferences=FakePreferences(retention_days=0),
                             task_status=ManagerTaskStatus(manager))
    page = make_setting_page(db, manager, services)
    yield page
    if page._rebuild_worker is not None:
        page._rebuild_worker.wait(10000)
        QApplication.processEvents()
    page.close()


def _seed(db):
    db.save_task(MonitorTask(task_id='t', pid=1, process_name='a.exe', metric_types=['m'],
                             interval=1.0, start_time=T0, end_time=None, status='stopped'))
    db.save_data_points([DataPoint('t', T0 + timedelta(seconds=s), float(s % 5), 'm')
                         for s in range(0, 400, 3)])


def _rollup_rows(db):
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute('SELECT * FROM rollup_1m ORDER BY 1, 2').fetchall()
    finally:
        conn.close()


def _wait_done(setting):
    assert poll_until(
        lambda: (QApplication.processEvents() or True) and setting._rebuild_worker is None,
        timeout=20)


def test_rebuild_locks_on_ui_thread_and_restores_rollups(setting, db, manager, monkeypatch):
    _seed(db)
    good = _rollup_rows(db)
    conn = sqlite3.connect(db.db_path)
    conn.execute('UPDATE rollup_1m SET count = 0')
    conn.commit()
    conn.close()

    callers = []
    real_begin, real_end = manager.begin_maintenance, manager.end_maintenance

    def begin():
        callers.append(('begin', threading.current_thread() is threading.main_thread()))
        return real_begin()

    def end():
        callers.append(('end', threading.current_thread() is threading.main_thread()))
        return real_end()

    monkeypatch.setattr(manager, 'begin_maintenance', begin)
    monkeypatch.setattr(manager, 'end_maintenance', end)

    setting._on_rebuild_clicked()
    assert callers == [('begin', True)]
    assert not setting.rebuild_card.button.isEnabled()
    assert not setting.cleanup_card.button.isEnabled()
    _wait_done(setting)
    assert callers == [('begin', True), ('end', True)]
    assert manager.is_maintenance_running() is False
    assert _rollup_rows(db) == good
    assert setting.rebuild_card.button.isEnabled()
    assert setting.cleanup_card.button.isEnabled()


def test_rebuild_refused_while_tasks_running(setting, manager, notifier, monkeypatch):
    monkeypatch.setattr(manager, 'has_running_tasks', lambda: True)
    setting._on_rebuild_clicked()
    assert setting._rebuild_worker is None
    assert manager.is_maintenance_running() is False
    notice = notifier.last("warning")
    assert notice.title == "无法重建汇总" and "监控任务" in notice.content


def test_rebuild_refused_while_exporting(setting, manager, notifier):
    assert manager.begin_export()
    try:
        setting._on_rebuild_clicked()
        assert setting._rebuild_worker is None
        assert manager.is_maintenance_running() is False
        notice = notifier.last("warning")
        assert notice.title == "无法重建汇总" and "正在导出" in notice.content
    finally:
        manager.end_export()


def test_rebuild_failure_reports_and_releases_lock(setting, db, manager, notifier, monkeypatch):
    _seed(db)
    good = _rollup_rows(db)

    def boom(*a, **k):
        raise StorageError('磁盘已满')

    monkeypatch.setattr(db.maintenance, 'rebuild_rollups', boom)
    setting._on_rebuild_clicked()
    _wait_done(setting)
    assert manager.is_maintenance_running() is False
    error = notifier.last("error")
    assert error.title == "重建汇总失败" and "磁盘已满" in error.content
    assert setting._rebuild_tooltip is None
    assert setting.rebuild_card.button.isEnabled()
    assert _rollup_rows(db) == good
