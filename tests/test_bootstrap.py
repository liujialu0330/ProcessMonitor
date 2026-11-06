"""组合根：build_context 的组装顺序与迁移/校正，run() 的固定启动顺序（全部替身，不建真实窗口）"""
import os
from datetime import datetime

import pytest

from app import bootstrap
from app.bootstrap import AppContext, build_context
from core.maintenance_service import MaintenanceService, ReconcileResult
from data.database import Database
from data.db.migrations.runner import MigrationOutcome, MigrationReport, MigrationRunner
from data.models import MonitorTask


@pytest.fixture
def paths(app_paths):
    app_paths.ensure_dirs()
    return app_paths


def _close(ctx):
    ctx.db.close()


# ========== build_context ==========

def test_build_context_assembles_everything(qapp, paths):
    ctx = build_context(paths, retention_days=0)
    try:
        assert isinstance(ctx, AppContext)
        assert ctx.paths is paths
        assert isinstance(ctx.db, Database)
        assert ctx.db.db_path == paths.db_path
        assert ctx.db.migration_report.outcome is MigrationOutcome.UP_TO_DATE
        assert ctx.db.migration_report.created is True
        assert (ctx.db.migration_failed, ctx.db.data_reset, ctx.db.backup_aborted) == \
            (False, False, False)
        assert ctx.manager is not None
        assert isinstance(ctx.maintenance, MaintenanceService)
        assert ctx.reconcile == ReconcileResult(orphans=0, recounted=0, cleaned=0)
        assert ctx.services.task_status is not None
        assert ctx.window is None
    finally:
        _close(ctx)


def test_build_context_order_and_explicit_migration(qapp, paths, monkeypatch):
    """顺序：显式迁移 → Database(migrate=False) → 启动校正 → 界面服务；构造函数不再迁移"""
    events = []

    original_migrate = MigrationRunner.migrate

    def migrate(self):
        events.append('migrate')
        return original_migrate(self)

    original_init = Database.__init__

    def db_init(self, *args, **kwargs):
        events.append(('database', kwargs.get('migrate', True)))
        original_init(self, *args, **kwargs)

    def init_database(self):
        events.append('database._init_database')

    original_reconcile = MaintenanceService.startup_reconcile

    def reconcile(self, days):
        events.append(('reconcile', days))
        return original_reconcile(self, days)

    import ui.services as services_module
    original_services = services_module.production_services

    def services(*args):
        events.append('services')
        return original_services(*args)

    monkeypatch.setattr(MigrationRunner, 'migrate', migrate)
    monkeypatch.setattr(Database, '__init__', db_init)
    monkeypatch.setattr(Database, '_init_database', init_database)
    monkeypatch.setattr(MaintenanceService, 'startup_reconcile', reconcile)
    monkeypatch.setattr(services_module, 'production_services', services)

    ctx = build_context(paths, retention_days=7)
    _close(ctx)
    assert events == ['migrate', ('database', False), ('reconcile', 7), 'services']


@pytest.mark.parametrize("outcome, flags", [
    (MigrationOutcome.BACKUP_ABORTED, (False, False, True)),
    (MigrationOutcome.RESET, (False, True, False)),
    (MigrationOutcome.FAILED_RESTORED, (True, False, False)),
    (MigrationOutcome.FAILED_KEPT, (True, False, False)),
    (MigrationOutcome.MIGRATED, (False, False, False)),
])
def test_build_context_maps_migration_report(qapp, paths, monkeypatch, outcome, flags):
    report = MigrationReport(outcome, 2, 2)
    original_migrate = MigrationRunner.migrate

    def migrate(self):
        # 仍建出当前结构，保证随后的启动校正能正常读写
        original_migrate(self)
        return report

    monkeypatch.setattr(MigrationRunner, 'migrate', migrate)
    ctx = build_context(paths, retention_days=0)
    _close(ctx)
    assert ctx.db.migration_report is report
    assert (ctx.db.migration_failed, ctx.db.data_reset, ctx.db.backup_aborted) == flags


def test_build_context_reconciles_orphans_before_returning(qapp, paths):
    """上次运行遗留的 running 任务在组装完成时已校正为 stopped"""
    seed = Database(paths.db_path)
    seed.save_task(MonitorTask(task_id="orphan", pid=1, process_name="a.exe",
                               metric_types=["memory_rss"], interval=1.0,
                               start_time=datetime.now(), end_time=None,
                               status="running"))
    seed.close()

    ctx = build_context(paths, retention_days=0)
    try:
        assert ctx.reconcile.orphans == 1
        assert ctx.db.get_task("orphan").status == "stopped"
    finally:
        _close(ctx)


def test_build_context_reads_retention_from_config(qapp, paths, monkeypatch):
    from app_config import cfg
    cfg.retention_days.value = 30
    seen = []
    monkeypatch.setattr(MaintenanceService, 'startup_reconcile',
                        lambda self, days: seen.append(days) or ReconcileResult(0, 0, 0))
    _close(build_context(paths))
    assert seen == [30]


def test_build_context_uses_given_registry(qapp, paths):
    marker = object()
    ctx = build_context(paths, registry=marker, retention_days=0)
    _close(ctx)
    assert ctx.registry is marker


def test_build_context_creates_thread_registry(qapp, paths):
    from core.shutdown import ThreadRegistry
    ctx = build_context(paths, retention_days=0)
    _close(ctx)
    assert isinstance(ctx.registry, ThreadRegistry)
    assert ctx.services.threads is ctx.registry


# ========== run() ==========

class _FakeApp:
    def __init__(self, events, exit_code=3):
        self.events = events
        self.exit_code = exit_code

    def exec_(self):
        self.events.append('exec')
        return self.exit_code


class _FakeGuard:
    def __init__(self, events):
        self.events = events
        self.activation_requested = _Signal(events)

    def listen(self):
        self.events.append('listen')
        return True


class _Signal:
    def __init__(self, events):
        self.events = events

    def connect(self, slot):
        self.events.append('activation_connected')


class _FakeDb:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append('db.close')


@pytest.fixture
def run_env(monkeypatch, app_paths):
    """把 run() 的每一步替换为记录事件的替身"""
    import app.single_instance as single_instance
    import ui.main_window as main_window_module
    import utils.crash_handler as crash_handler
    import utils.logger as logger_module

    events = []
    state = {'existing': False}

    monkeypatch.setattr(logger_module, 'setup_logging',
                        lambda log_dir=None: events.append(('logging', log_dir)) or log_dir)
    monkeypatch.setattr(crash_handler, 'install_excepthook',
                        lambda log_dir: events.append('excepthook'))
    monkeypatch.setattr(bootstrap, '_create_application',
                        lambda argv: events.append('qapplication') or _FakeApp(events))

    class Guard(_FakeGuard):
        def notify_existing_instance(self):
            self.events.append('single_instance')
            return state['existing']

    monkeypatch.setattr(single_instance, 'SingleInstanceGuard', lambda: Guard(events))
    monkeypatch.setattr(bootstrap, '_apply_preferences',
                        lambda app, paths: events.append('preferences'))

    def fake_build_context(paths):
        events.append('build_context')
        return AppContext(paths=paths, db=_FakeDb(events), manager='manager',
                          services='services', registry=None, maintenance=None,
                          reconcile=ReconcileResult(0, 0, 0))

    monkeypatch.setattr(bootstrap, 'build_context', fake_build_context)

    class FakeWindow:
        def __init__(self, db, manager, services):
            events.append(('window', manager, services))

        def show(self):
            events.append('window.show')

    monkeypatch.setattr(main_window_module, 'MainWindow', FakeWindow)
    exits = []
    return events, state, exits, app_paths


def test_run_fixed_order(run_env):
    events, state, exits, paths = run_env
    code = bootstrap.run(['prog'], exit_process=lambda c: (events.append(('exit', c)),
                                                           exits.append(c)))
    assert code == 3
    assert events == [
        ('logging', paths.log_dir), 'excepthook', 'qapplication', 'single_instance', 'listen',
        'preferences', 'build_context', ('window', 'manager', 'services'), 'window.show',
        'activation_connected', 'exec', ('exit', 3),
    ]
    # 数据库只由主窗口的退出协调器关闭，组合根不再重复关闭
    assert 'db.close' not in events
    # 路径解析后先建目录（日志初始化要写文件）
    assert os.path.isdir(paths.data_dir) and os.path.isdir(paths.log_dir)


def test_run_returns_zero_when_instance_exists(run_env):
    events, state, exits, paths = run_env
    state['existing'] = True
    code = bootstrap.run(['prog'], exit_process=exits.append)
    assert code == 0
    assert events[-1] == 'single_instance'
    assert 'build_context' not in events and 'preferences' not in events
    assert exits == []
