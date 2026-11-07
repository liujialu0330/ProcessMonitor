"""数据库升级进度窗口，以及主窗口对升级结果的提示（备份位置、磁盘空间不足）"""
import threading

import pytest
from PyQt5.QtGui import QCloseEvent

from core.monitor_manager import MonitorManager
from data.database import Database
from data.db.migrations.runner import MigrationOutcome, MigrationReport
from tests.doubles import RecordingNotifier
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget, process_events_until, stop_and_join
from ui.services import ManagerTaskStatus
from ui.windows import migration_window
from ui.windows.migration_window import (
    MIGRATION_WINDOW_TITLE, PROGRESS_SCALE, MigrationWindow, run_with_progress_window,
)

BACKUP = r"C:\数据\monitor.db.bak_v1"


@pytest.fixture
def window(qapp):
    win = MigrationWindow(BACKUP, estimated_rows=1_234_567)
    yield win
    win.finish()
    dispose_widget(win)


def test_window_texts(window):
    assert window.title_label.text() == MIGRATION_WINDOW_TITLE == "正在升级数据库…"
    assert "约 1,234,567 行" in window.status_label.text()
    assert BACKUP in window.status_label.text()
    assert window.progress_bar.value() == 0


def test_close_is_ignored_until_finished(window):
    window.show()
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    window.close()
    assert window.isVisible()

    window.finish()
    assert window.is_finished and not window.isVisible()


def test_set_progress_updates_bar_and_text(window):
    window.set_progress(250_000, 1_000_000)
    assert window.progress_bar.value() == PROGRESS_SCALE // 4
    assert window.status_label.text() == \
        f"已处理 250,000 / 1,000,000 行 · 备份保存在 {BACKUP}"

    window.set_progress(1_000_000, 1_000_000)
    assert window.progress_bar.value() == PROGRESS_SCALE
    assert window.status_label.text().startswith("正在收尾")


def test_set_progress_handles_rows_beyond_32_bits(window):
    total = 6_000_000_000
    window.set_progress(total // 2, total)
    assert window.progress_bar.value() == PROGRESS_SCALE // 2


def test_set_progress_ignores_zero_total(window):
    before = window.status_label.text()
    window.set_progress(0, 0)
    assert window.status_label.text() == before


def test_run_executes_in_worker_thread_and_reports_progress(qapp):
    """迁移在工作线程执行，进度回到界面线程更新窗口；结束后窗口关闭并返回结果"""
    win = MigrationWindow(BACKUP)
    main_thread = threading.get_ident()
    seen = {}
    shown = []
    original_show = win.show
    win.show = lambda: (shown.append(True), original_show())

    def migrate(on_progress):
        seen['thread'] = threading.get_ident()
        for done in (50_000, 100_000, 120_000):
            on_progress('samples', done, 120_000)
        return 'report'

    try:
        result = run_with_progress_window(migrate, backup_path=BACKUP, window=win)
        assert result == 'report'
        assert seen['thread'] != main_thread
        assert shown == [True]
        assert win.is_finished and not win.isVisible()
        assert win.progress_bar.value() == PROGRESS_SCALE
    finally:
        dispose_widget(win)


def test_progress_reaches_window_while_migration_is_running(qapp):
    """进度在迁移进行中就回到界面线程（不是等线程结束后才补上）"""
    from PyQt5.QtCore import pyqtSlot

    delivered = threading.Event()
    main_thread = threading.get_ident()

    class SpyWindow(MigrationWindow):
        @pyqtSlot(object, object)
        def set_progress(self, done, total):
            if threading.get_ident() == main_thread:
                delivered.set()
            super().set_progress(done, total)

    win = SpyWindow(BACKUP)

    def migrate(on_progress):
        on_progress('samples', 10, 100)
        return delivered.wait(5)   # 迁移线程等界面线程收到进度

    try:
        assert run_with_progress_window(migrate, backup_path=BACKUP, window=win) is True
    finally:
        dispose_widget(win)


def test_run_reraises_migration_exception(qapp):
    """迁移在工作线程里抛出的异常原样交还组合根（与同步迁移一致），窗口也已关闭"""
    win = MigrationWindow(BACKUP)
    boom = RuntimeError("库被锁")

    def migrate(on_progress):
        raise boom

    try:
        with pytest.raises(RuntimeError) as info:
            run_with_progress_window(migrate, backup_path=BACKUP, window=win)
        assert info.value is boom
        assert win.is_finished and not win.isVisible()
    finally:
        dispose_widget(win)


def test_run_creates_window_when_not_given(qapp, monkeypatch):
    created = []
    real = migration_window.MigrationWindow

    def factory(backup_path, estimated_rows):
        win = real(backup_path, estimated_rows)
        created.append((win, backup_path, estimated_rows))
        return win

    monkeypatch.setattr(migration_window, 'MigrationWindow', factory)
    assert run_with_progress_window(lambda cb: 7, backup_path=BACKUP, estimated_rows=9) == 7
    [(win, path, rows)] = created
    assert (path, rows) == (BACKUP, 9) and win.is_finished
    dispose_widget(win)


# ========== 主窗口：升级结果提示 ==========

def _main_window(db):
    manager = MonitorManager(db=db)
    services = make_services(notifier=RecordingNotifier(),
                             task_status=ManagerTaskStatus(manager))
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    return win


def _close(win):
    stop_and_join(win.monitor_manager)
    dispose_widget(win)
    win.db.close()


def test_main_window_shows_backup_location_after_upgrade(qapp, tmp_path):
    path = str(tmp_path / 'monitor.db')
    Database(path).close()
    report = MigrationReport(MigrationOutcome.MIGRATED, 1, 5, backup_path=path + '.bak_v1')
    win = _main_window(Database(path, migrate=False, migration_report=report))
    try:
        notifier = win.services.notifier
        assert process_events_until(lambda: notifier.messages("success"))
        notice = notifier.last("success")
        assert notice.title == "数据库已升级"
        assert notice.content == \
            "数据库已升级到 v5，旧文件已保留为 monitor.db.bak_v1（可用于回退到 1.4.1）"
        assert notice.duration == 6000
    finally:
        _close(win)


def test_main_window_no_upgrade_tip_for_fresh_db(qapp, tmp_path):
    win = _main_window(Database(str(tmp_path / 'monitor.db')))
    try:
        process_events_until(lambda: False, timeout=0.8)
        assert win.services.notifier.messages() == []
    finally:
        _close(win)


def test_backup_aborted_tip_names_disk_space(qapp, tmp_path):
    path = str(tmp_path / 'monitor.db')
    Database(path).close()
    mb = 1024 * 1024
    report = MigrationReport(MigrationOutcome.BACKUP_ABORTED, 1, 1,
                             required_bytes=300 * mb + 1, free_bytes=50 * mb + 5)
    win = _main_window(Database(path, migrate=False, migration_report=report))
    try:
        win._show_backup_aborted_tip()
        notice = win.services.notifier.last("warning")
        assert (notice.title, notice.duration) == ("数据库升级已中止", -1)
        assert "需要约 301 MB 可用空间（当前 50 MB），数据库未改动" in notice.content
    finally:
        _close(win)


def test_backup_aborted_tip_without_disk_info_keeps_generic_text(qapp, tmp_path):
    path = str(tmp_path / 'monitor.db')
    Database(path).close()
    report = MigrationReport(MigrationOutcome.BACKUP_ABORTED, 1, 1)
    win = _main_window(Database(path, migrate=False, migration_report=report))
    try:
        win._show_backup_aborted_tip()
        assert "请释放磁盘空间后重启" in win.services.notifier.last("warning").content
    finally:
        _close(win)
