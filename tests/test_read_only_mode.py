"""
只读模式：库由更高版本创建（meta.min_reader_version 高于本版本）时，组合根以只读方式打开，
仓储写入失败、启动校正跳过、主窗口常驻横幅、开始监控与打点被拒并就近提示
"""
import sqlite3
from datetime import datetime

import pytest

from app.bootstrap import build_context
from core.maintenance_service import ReconcileResult
from data.database import Database
from data.db import meta as meta_store
from data.errors import StorageError
from data.models import MonitorTask
from tests.doubles import RecordingNotifier
from tests.factories import make_main_window, make_services
from tests.helpers import dispose_widget, process_events_until, stop_and_join
from ui.formatting import READ_ONLY_START_BLOCKED, READ_ONLY_TITLE
from ui.presenters.monitor_presenter import QUICK_MARKER_FAILED
from ui.services import ManagerTaskStatus
from ui.widgets.monitor.new_task_form import TaskRequest

NEWER = '9.1.0'


@pytest.fixture
def paths(app_paths):
    app_paths.ensure_dirs()
    return app_paths


def _seed(path, min_reader='9.0.0', writer=NEWER):
    """建一个当前结构的库，并把 meta 改成"由更高版本写入"；顺带放一个遗留的运行中任务"""
    db = Database(path)
    db.save_task(MonitorTask(task_id='orphan', pid=1, process_name='a.exe',
                             metric_types=['memory_rss'], interval=1.0,
                             start_time=datetime(2026, 1, 1, 8), end_time=None,
                             status='running'))
    db.close()
    conn = sqlite3.connect(path)
    try:
        conn.executemany('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', [
            (meta_store.KEY_MIN_READER, min_reader),
            (meta_store.KEY_LAST_WRITER, writer),
        ])
        conn.commit()
    finally:
        conn.close()


def _read_meta(path):
    return meta_store.read_meta_file(path)


def test_build_context_opens_newer_db_read_only(qapp, paths):
    _seed(paths.db_path)
    ctx = build_context(paths, retention_days=0)
    try:
        db = ctx.db
        assert db.read_only is True
        assert db.newer_version == NEWER
        # 仓储写入抛 StorageError；门面旧写方法按原语义返回 False
        with pytest.raises(StorageError):
            db.tasks.save(MonitorTask(task_id='new', pid=2, process_name='b.exe',
                                      metric_types=['memory_rss'], interval=1.0,
                                      start_time=datetime(2026, 1, 2), end_time=None,
                                      status='running'))
        assert db.add_marker('orphan', datetime(2026, 1, 1, 9), 'x') is False
        # 读取照常
        assert [t.task_id for t in db.get_all_tasks()] == ['orphan']
        # 启动校正整体跳过：遗留任务仍是 running，最近写入版本没被改成本版本
        assert ctx.reconcile == ReconcileResult(orphans=0, recounted=0, cleaned=0)
        assert db.get_task('orphan').status == 'running'
    finally:
        ctx.db.close()
    assert _read_meta(paths.db_path).last_writer_version == NEWER


def test_build_context_normal_db_is_writable(qapp, paths):
    Database(paths.db_path).close()
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.db.read_only is False and ctx.db.newer_version is None
    finally:
        ctx.db.close()


def test_build_context_same_release_line_is_writable(qapp, paths):
    """min_reader 2.0.0 的库对 2.0.0-beta.1 可写（预发布按其正式版比较）"""
    _seed(paths.db_path, min_reader='2.0.0', writer='2.0.0')
    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.db.read_only is False
        assert ctx.reconcile.orphans == 1
    finally:
        ctx.db.close()


def test_unreadable_meta_falls_back_to_writable(qapp, paths, monkeypatch, caplog):
    import logging
    Database(paths.db_path).close()

    def locked(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(meta_store, 'read_meta_file', locked)
    with caplog.at_level(logging.ERROR, logger='app.bootstrap'):
        ctx = build_context(paths, retention_days=0)
    ctx.db.close()
    assert ctx.db.read_only is False
    assert any("读取数据库元数据失败" in r.getMessage() for r in caplog.records)


def test_read_meta_file_does_not_switch_journal_mode(tmp_path):
    """读 meta 不能借连接工厂：可写连接会把回滚日志模式的库切成 WAL（本身就是一次写）"""
    path = str(tmp_path / 'rollback.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute("INSERT INTO meta VALUES ('min_reader_version', '9.0.0')")
    conn.commit()
    conn.close()
    with open(path, 'rb') as f:
        before = f.read()

    assert _read_meta(path).min_reader_version == '9.0.0'
    with open(path, 'rb') as f:
        assert f.read() == before
    assert meta_store.read_meta_file(str(tmp_path / 'missing.db')) == meta_store.MetaInfo()
    assert not (tmp_path / 'missing.db').exists()


# ========== 主窗口与监控页 ==========

def _window(db):
    from core.monitor_manager import MonitorManager
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


@pytest.fixture
def read_only_window(qapp, tmp_path):
    path = str(tmp_path / 'monitor.db')
    _seed(path)
    win = _window(Database(path, read_only=True, newer_version=NEWER))
    yield win
    _close(win)


def _request():
    return TaskRequest(pid=4321, name='target.exe', create_time=None,
                       metrics=('memory_rss',), interval=1.0)


def test_main_window_shows_persistent_read_only_banner(read_only_window):
    notifier = read_only_window.services.notifier
    assert process_events_until(lambda: notifier.messages("warning"))
    notice = notifier.last("warning")
    assert notice.title == READ_ONLY_TITLE
    assert notice.content == ("数据库由更高版本（9.1.0）创建，本版本以只读方式打开："
                              "可以查看与导出，不能开始监控")
    assert notice.duration == -1
    assert read_only_window.read_only_banner is notice.bar


def test_start_button_disabled_with_reason(read_only_window):
    form = read_only_window.monitor_page.presenter.form
    assert not form.start_button.isEnabled()
    assert form.start_button.toolTip() == READ_ONLY_START_BLOCKED
    # 勾选"等待进程启动"会改按钮文案，但不能把原因 tooltip 冲掉
    form.set_wait_enabled(True)
    assert form.start_button.toolTip() == READ_ONLY_START_BLOCKED
    assert not form.start_button.isEnabled()


def test_start_monitoring_is_refused_with_nearby_hint(read_only_window):
    presenter = read_only_window.monitor_page.presenter
    notifier = read_only_window.services.notifier
    notifier.clear()

    assert presenter.on_start_requested(_request()) is None
    assert presenter.start_monitoring(_request()) is None

    notice = notifier.last("warning")
    assert notice.title == READ_ONLY_TITLE and notice.content == READ_ONLY_START_BLOCKED
    assert notice.parent is presenter.host
    assert read_only_window.monitor_manager.running_task_count() == 0
    assert [t.task_id for t in read_only_window.db.get_all_tasks()] == ['orphan']


def test_wait_request_is_refused(read_only_window):
    from ui.widgets.monitor.new_task_form import WaitRequest
    presenter = read_only_window.monitor_page.presenter
    request = WaitRequest(name='target.exe', metrics=('memory_rss',), interval=1.0)
    assert presenter.on_wait_requested(request) is None
    assert presenter.wait_ids() == []


def test_quick_marker_does_not_write(read_only_window):
    """托盘与全局快捷键的打点同样被拒（反馈交给主窗口弹提示）"""
    presenter = read_only_window.monitor_page.presenter
    feedback = []
    presenter.quick_marker_feedback.connect(lambda *args: feedback.append(args))
    assert presenter.quick_marker() == []
    assert feedback and feedback[-1][0] == QUICK_MARKER_FAILED
    assert feedback[-1][1] == READ_ONLY_TITLE


def test_normal_db_has_no_banner_and_start_enabled(qapp, tmp_path):
    win = _window(Database(str(tmp_path / 'monitor.db')))
    try:
        process_events_until(lambda: False, timeout=0.8)
        assert not [n for n in win.services.notifier.messages() if n.title == READ_ONLY_TITLE]
        assert win.read_only_banner is None
        presenter = win.monitor_page.presenter
        assert not presenter.read_only
        assert presenter.form.start_button.isEnabled()
    finally:
        _close(win)
