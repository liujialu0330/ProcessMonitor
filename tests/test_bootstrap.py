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

    def reconcile(self, days, **kwargs):
        events.append(('reconcile', days))
        return original_reconcile(self, days, **kwargs)

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
                        lambda self, days, **kw: seen.append(days) or ReconcileResult(0, 0, 0))
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
    def create_application(argv):
        state['qt_argv'] = list(argv)
        events.append('qapplication')
        return _FakeApp(events)

    monkeypatch.setattr(bootstrap, '_create_application', create_application)

    class Guard(_FakeGuard):
        def notify_existing_instance(self):
            self.events.append('single_instance')
            return state['existing']

    def make_guard(*args, **kwargs):
        state['guard_args'] = (args, kwargs)
        return Guard(events)

    monkeypatch.setattr(single_instance, 'SingleInstanceGuard', make_guard)
    monkeypatch.setattr(bootstrap, '_apply_preferences',
                        lambda app, paths: events.append('preferences'))

    def fake_migrate(paths):
        events.append('migrate')
        if state.get('real_migrate'):
            return original_migrate_database(paths)
        return state.get('report') or MigrationReport(MigrationOutcome.UP_TO_DATE, 5, 5)

    original_migrate_database = bootstrap._migrate_database
    monkeypatch.setattr(bootstrap, '_migrate_database', fake_migrate)

    def fake_build_context(paths, *, report=None, legacy_importer=None):
        events.append('build_context')
        state['build_report'] = report
        state['legacy_importer'] = legacy_importer
        return AppContext(paths=paths, db=_FakeDb(events), manager='manager',
                          services='services', registry=None, maintenance=None,
                          reconcile=ReconcileResult(0, 0, 0))

    monkeypatch.setattr(bootstrap, 'build_context', fake_build_context)
    # 绝不在测试机桌面上注册真实全局热键
    monkeypatch.setattr(bootstrap, '_create_marker_hotkey',
                        lambda app, services: events.append('hotkey') or 'hotkey')

    class FakeWindow:
        def __init__(self, db, manager, services, *, hotkey=None, diagnostics_provider=None):
            events.append(('window', manager, services))
            assert hotkey == 'hotkey'
            state['diagnostics_provider'] = diagnostics_provider

        def show(self):
            events.append('window.show')

        def show_sync_dir_warning(self, text):
            events.append(('sync_dir_warning', text))

    monkeypatch.setattr(main_window_module, 'MainWindow', FakeWindow)
    # 看门狗与自身监控另有用例覆盖；这里只记下调用时机，不进 events（固定顺序断言不变）
    monkeypatch.setattr(bootstrap, 'install_watchdog',
                        lambda app, ctx, window: state.setdefault('after_show', []).append(
                            ('watchdog', 'window.show' in events)))
    monkeypatch.setattr(bootstrap, 'start_self_monitor',
                        lambda ctx: state.setdefault('after_show', []).append(
                            ('self_monitor', 'window.show' in events)))
    exits = []
    return events, state, exits, app_paths


def test_run_fixed_order(run_env):
    events, state, exits, paths = run_env
    code = bootstrap.run(['prog'], exit_process=lambda c: (events.append(('exit', c)),
                                                           exits.append(c)))
    assert code == 3
    assert events == [
        ('logging', paths.log_dir), 'excepthook', 'qapplication', 'single_instance', 'listen',
        'preferences', 'migrate', 'build_context', 'hotkey', ('window', 'manager', 'services'),
        'window.show',
        'activation_connected', 'exec', ('exit', 3),
    ]
    # 数据库只由主窗口的退出协调器关闭，组合根不再重复关闭
    assert 'db.close' not in events
    # 路径解析后先建目录（日志初始化要写文件）
    assert os.path.isdir(paths.data_dir) and os.path.isdir(paths.log_dir)


def test_run_keys_single_instance_by_data_dir(run_env):
    """单实例按数据目录区分：组合根必须把 AppPaths.data_dir 交给守卫"""
    events, state, exits, paths = run_env
    bootstrap.run(['prog'], exit_process=exits.append)
    assert state['guard_args'] == ((), {'data_dir': paths.data_dir})


def test_run_returns_zero_when_instance_exists(run_env):
    events, state, exits, paths = run_env
    state['existing'] = True
    code = bootstrap.run(['prog'], exit_process=exits.append)
    assert code == 0
    assert events[-1] == 'single_instance'
    assert 'build_context' not in events and 'preferences' not in events
    assert exits == []


# ========== run()：数据目录 ==========

def test_run_uses_cli_data_dir(run_env, tmp_path):
    """--data-dir 优先于环境变量：日志、单实例与建目录都跟随它，QApplication 拿不到该参数"""
    events, state, exits, paths = run_env
    target = tmp_path / "cli_data"
    code = bootstrap.run(['prog', '--data-dir', str(target), '-style', 'fusion'],
                         exit_process=exits.append)
    assert code == 3
    assert events[0] == ('logging', str(target / "logs"))
    assert state['guard_args'] == ((), {'data_dir': str(target)})
    assert state['qt_argv'] == ['prog', '-style', 'fusion']
    assert target.is_dir() and (target / "logs").is_dir()
    # 环境变量所指的目录没有被当作日志目录
    assert ('logging', paths.log_dir) not in events


def test_run_accepts_equals_form(run_env, tmp_path):
    events, state, exits, paths = run_env
    target = tmp_path / "eq"
    bootstrap.run(['prog', f'--data-dir={target}'], exit_process=exits.append)
    assert state['guard_args'] == ((), {'data_dir': str(target)})
    assert state['qt_argv'] == ['prog']


def test_resolve_matches_what_run_uses(tmp_path):
    """run() 与 resolve() 用同一份 argv 解析（只测解析，不走启动流程）"""
    from app.paths import AppPaths
    import config
    target = str(tmp_path / "x")
    resolved = AppPaths.resolve(argv=['prog', '--data-dir', target])
    assert resolved.data_dir == target
    assert resolved.source == config.DATA_DIR_SOURCE_CLI


@pytest.mark.parametrize("make_argv", [
    lambda tmp: ['prog', '--data-dir'],
    lambda tmp: ['prog', '--data-dir='],
    # 同名文件挡路，makedirs 失败
    lambda tmp: ['prog', '--data-dir', str(tmp / "blocker" / "sub")],
])
def test_run_fails_instead_of_falling_back(run_env, tmp_path, make_argv):
    """数据目录不可用：只在默认日志目录记录原因并返回非 0，不建 QApplication、不回退到别的目录"""
    from app.paths import AppPaths
    events, state, exits, paths = run_env
    (tmp_path / "blocker").write_text("x", encoding="utf-8")

    code = bootstrap.run(make_argv(tmp_path), exit_process=exits.append)

    assert code == bootstrap.EXIT_DATA_DIR_UNAVAILABLE != 0
    assert events == [('logging', AppPaths.default().log_dir)]
    assert 'guard_args' not in state and exits == []
    # 环境变量所指的数据目录也没有被悄悄启用
    assert not os.path.exists(paths.log_dir)


def test_run_logs_failure_reason(run_env, tmp_path, caplog):
    import logging
    events, state, exits, paths = run_env
    with caplog.at_level(logging.ERROR, logger="app.bootstrap"):
        bootstrap.run(['prog', '--data-dir'], exit_process=exits.append)
    assert any("数据目录不可用" in r.getMessage() and "--data-dir" in r.getMessage()
               for r in caplog.records)


def test_run_hands_sync_dir_hint_to_window(run_env, monkeypatch):
    events, state, exits, paths = run_env
    seen = []
    monkeypatch.setattr(bootstrap, 'sync_dir_hint',
                        lambda data_dir: seen.append(data_dir) or "同步盘提示")
    bootstrap.run(['prog'], exit_process=exits.append)
    assert seen == [paths.data_dir]
    show = events.index('window.show')
    assert events[show + 1:show + 3] == ['activation_connected',
                                         ('sync_dir_warning', "同步盘提示")]
    assert events.index('exec') > events.index(('sync_dir_warning', "同步盘提示"))


def test_run_without_sync_dir_hint_shows_nothing(run_env, monkeypatch):
    events, state, exits, paths = run_env
    monkeypatch.setattr(bootstrap, 'sync_dir_hint', lambda data_dir: None)
    bootstrap.run(['prog'], exit_process=exits.append)
    assert not any(isinstance(e, tuple) and e[0] == 'sync_dir_warning' for e in events)


# ========== run()：迁移失败与启动失败的原生错误框 ==========

def _boxes(state):
    """注入 run() 的原生错误框替身：记录 (标题, 正文)"""
    shown = state.setdefault('boxes', [])
    return lambda title, text: shown.append((title, text))


def test_run_passes_progress_importer_to_build_context(run_env):
    """启动校正的导回一步经组合根给出的进度窗口方式执行（行数多时不在界面线程同步跑）"""
    events, state, exits, paths = run_env
    bootstrap.run(['prog'], exit_process=exits.append)
    assert state['legacy_importer'] is bootstrap.import_legacy_with_progress


def test_run_passes_migration_report_to_build_context(run_env):
    """迁移只跑一次：run() 跑出的报告原样交给 build_context"""
    events, state, exits, paths = run_env
    state['report'] = MigrationReport(MigrationOutcome.MIGRATED, 1, 5,
                                      backup_path=paths.db_path + '.bak_v1')
    bootstrap.run(['prog'], exit_process=exits.append)
    assert state['build_report'] is state['report']


def test_run_exits_with_native_box_when_old_db_not_restored(run_env):
    """FAILED_KEPT：弹原生错误框写明原文件位置，返回 EXIT_MIGRATION_FAILED，不组装、不建窗口"""
    events, state, exits, paths = run_env
    kept = paths.db_path + '.bak_v1'
    state['report'] = MigrationReport(MigrationOutcome.FAILED_KEPT, 1, 1,
                                      backup_path=kept, broken_path=kept)

    code = bootstrap.run(['prog'], exit_process=exits.append, message_box=_boxes(state))

    assert code == bootstrap.EXIT_MIGRATION_FAILED == 3
    assert events[-1] == 'migrate'
    assert 'build_context' not in events and exits == []
    [(title, text)] = state['boxes']
    assert title == "进程监控助手"
    assert "数据库升级失败" in text and kept in text and "程序将退出" in text


@pytest.mark.parametrize("outcome", [
    MigrationOutcome.FAILED_RESTORED, MigrationOutcome.BACKUP_ABORTED, MigrationOutcome.RESET,
])
def test_run_continues_on_other_migration_failures(run_env, outcome):
    """其余失败照常启动（主窗口按三态标志提示），不弹原生错误框"""
    events, state, exits, paths = run_env
    state['report'] = MigrationReport(outcome, 1, 1)
    code = bootstrap.run(['prog'], exit_process=exits.append, message_box=_boxes(state))
    assert code == 3 and 'build_context' in events   # 3 是 _FakeApp 事件循环的退出码
    assert state.get('boxes', []) == []


def test_run_failed_swap_end_to_end_never_creates_empty_db(run_env, monkeypatch):
    """
    真实迁移（不支持硬链接、退回改名式）：新库换不进来（os.replace 第一次失败）、旧库也改不回
    原名（os.rename 第二次失败）→ 原路径没有库：组合根必须退出，不能让后续步骤在原路径建出空库
    """
    from tests.test_migration import _create_v0_db
    from tests.test_rebuild_migration import _failing, _no_link, _patch_os
    events, state, exits, paths = run_env
    state['real_migrate'] = True
    _create_v0_db(paths.db_path)
    _patch_os(monkeypatch, link=_no_link, rename=_failing(os.rename, {2}),
              replace=_failing(os.replace, {1}))

    code = bootstrap.run(['prog'], exit_process=exits.append, message_box=_boxes(state))

    assert code == bootstrap.EXIT_MIGRATION_FAILED
    assert 'build_context' not in events
    assert not os.path.exists(paths.db_path)
    backup = paths.db_path + '.bak_v0'
    assert os.path.exists(backup)
    [(_title, text)] = state['boxes']
    assert backup in text


def test_run_shows_native_box_for_data_dir_failure_when_frozen(run_env, monkeypatch):
    """打包的窗口程序没有控制台：数据目录不可用时弹原生错误框（含路径与原因）"""
    import sys
    events, state, exits, paths = run_env
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    code = bootstrap.run(['prog', '--data-dir'], exit_process=exits.append,
                         message_box=_boxes(state))
    assert code == bootstrap.EXIT_DATA_DIR_UNAVAILABLE
    [(title, text)] = state['boxes']
    assert title == "进程监控助手"
    assert "数据目录不可用" in text and "--data-dir" in text
    assert bootstrap.DATA_DIR_UNKNOWN_PATH in text


def test_data_dir_failure_box_names_the_path(run_env, monkeypatch, tmp_path):
    import sys
    events, state, exits, paths = run_env
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    (tmp_path / "blocker").write_text("x", encoding="utf-8")
    target = str(tmp_path / "blocker" / "sub")
    bootstrap.run(['prog', '--data-dir', target], exit_process=exits.append,
                  message_box=_boxes(state))
    [(_title, text)] = state['boxes']
    assert "blocker" in text


def test_run_has_no_native_box_for_data_dir_failure_in_dev(run_env):
    """开发环境有控制台（stderr 已输出原因），不弹框打断"""
    events, state, exits, paths = run_env
    code = bootstrap.run(['prog', '--data-dir'], exit_process=exits.append,
                         message_box=_boxes(state))
    assert code == bootstrap.EXIT_DATA_DIR_UNAVAILABLE
    assert state.get('boxes', []) == []


# ========== 第 6 步：探测 → 同步 / 进度窗口 ==========

def _probe(**kwargs):
    from data.db.migrations.runner import ProbeResult
    values = dict(has_tasks=True, version=1, target_version=5, file_bytes=0, estimated_rows=0)
    values.update(kwargs)
    return ProbeResult(**values)


@pytest.mark.parametrize("probe, expected", [
    (dict(estimated_rows=500_000), False),
    (dict(estimated_rows=500_001), True),
    (dict(file_bytes=100 * 1024 * 1024), False),
    (dict(file_bytes=100 * 1024 * 1024 + 1), True),
    # 不需要迁移的库再大也不显示
    (dict(estimated_rows=10 ** 7, version=5), False),
    (dict(estimated_rows=10 ** 7, has_tasks=False, version=0), False),
])
def test_needs_progress_window_thresholds(probe, expected):
    assert bootstrap.needs_progress_window(_probe(**probe)) is expected


def _v1_db(paths, rows=10):
    from tests.test_migration_v2 import _create_v1_db
    from tests.test_rebuild_migration import _v1_rows
    _create_v1_db(paths.db_path, {'t1': ['memory_rss', 'memory_private']}, _v1_rows(rows))


def test_migrate_database_small_db_runs_synchronously(paths, monkeypatch):
    from ui.windows import migration_window
    _v1_db(paths)

    def no_window(*args, **kwargs):
        pytest.fail("小库不应显示进度窗口")

    monkeypatch.setattr(migration_window, 'run_with_progress_window', no_window)
    report = bootstrap._migrate_database(paths)
    assert report.outcome is MigrationOutcome.MIGRATED


def test_migrate_database_large_db_uses_progress_window(paths, monkeypatch):
    """超过阈值：经进度窗口执行同一个迁移，备份路径提前算好交给窗口"""
    from ui.windows import migration_window
    _v1_db(paths, rows=20)
    monkeypatch.setattr(bootstrap, 'MIGRATION_PROGRESS_THRESHOLD_ROWS', 5)
    seen = {'progress': []}

    def fake_window(migrate, *, backup_path, estimated_rows):
        seen.update(backup_path=backup_path, estimated_rows=estimated_rows)
        return migrate(lambda stage, done, total: stage == 'samples'
                       and seen['progress'].append((done, total)))

    monkeypatch.setattr(migration_window, 'run_with_progress_window', fake_window)
    report = bootstrap._migrate_database(paths)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert seen['backup_path'] == report.backup_path == paths.db_path + '.bak_v1'
    assert seen['estimated_rows'] == 20
    assert seen['progress'] and seen['progress'][-1] == (20, 20)


def test_build_context_with_report_does_not_migrate_again(qapp, paths, monkeypatch):
    first = bootstrap.new_migration_runner(paths).migrate()

    def no_migrate(self):
        pytest.fail("已传入报告时不应再迁移")

    monkeypatch.setattr(MigrationRunner, 'migrate', no_migrate)
    ctx = build_context(paths, retention_days=0, report=first)
    _close(ctx)
    assert ctx.db.migration_report is first


def test_build_context_logs_backup_path_after_upgrade(qapp, paths, caplog):
    import logging
    _v1_db(paths)
    with caplog.at_level(logging.INFO, logger="app.bootstrap"):
        ctx = build_context(paths, retention_days=0)
    _close(ctx)
    assert ctx.db.backup_path == paths.db_path + '.bak_v1'
    assert any("monitor.db.bak_v1" in r.getMessage() and "1.4.1" in r.getMessage()
               for r in caplog.records)


# ========== 无窗口模式（--background）与 IPC 接线 ==========

class _FakeDispatcher:
    def __init__(self, events):
        self.events = events
        self.context = None

    def has_pending(self):
        return False

    def set_settings(self, settings):
        self.events.append('dispatcher.settings')

    def set_context(self, context):
        self.events.append('dispatcher.context')
        self.context = context


@pytest.fixture
def bg_env(run_env, monkeypatch):
    """run_env 之上：守卫接受 activate 参数并带命令处理器，无窗口路径的各步换成替身"""
    import app.single_instance as single_instance
    import app_config
    import core.headless as headless_module
    from utils import crash_handler
    events, state, exits, paths = run_env
    # 无窗口模式会把异常处理切到"不弹框"：测试结束后还原，不影响后续用例
    monkeypatch.setattr(crash_handler, '_headless', False)

    class Guard(_FakeGuard):
        def __init__(self, events):
            super().__init__(events)
            self.dispatcher = _FakeDispatcher(events)
            self.activation_error = state.get('activation_error')

        def notify_existing_instance(self, activate=True):
            self.events.append(('single_instance', activate))
            return state['existing']

    def make_guard(*args, **kwargs):
        state['guard_args'] = (args, kwargs)
        guard = Guard(events)
        state['guard'] = guard
        return guard

    monkeypatch.setattr(single_instance, 'SingleInstanceGuard', make_guard)
    monkeypatch.setattr(app_config, 'load_app_config',
                        lambda paths=None: events.append('load_config'))

    class Runner:
        def migrate(self):
            events.append('headless_migrate')
            return state.get('report') or MigrationReport(MigrationOutcome.UP_TO_DATE, 5, 5)

    monkeypatch.setattr(bootstrap, 'new_migration_runner', lambda paths, on_progress=None: Runner())

    class FakeServices:
        clock = 'clock'

    def fake_build_context(paths, *, report=None, legacy_importer=None):
        events.append('build_context')
        return AppContext(paths=paths, db=_FakeDb(events), manager='manager',
                          services=FakeServices(), registry='registry', maintenance=None,
                          reconcile=ReconcileResult(0, 0, 0))

    monkeypatch.setattr(bootstrap, 'build_context', fake_build_context)

    class FakeRuntime:
        def __init__(self, manager, registry, db, *, idle_exit_s=0.0, busy=None,
                     stage_renderer=None, quit_hooks=()):
            events.append(('runtime', manager, registry, idle_exit_s))
            self.auto_marker = 'auto_marker'
            self.busy = busy
            self.stage_renderer = stage_renderer
            self.quit_hooks = list(quit_hooks)
            state['runtime'] = self

        def quit(self):
            events.append('runtime.quit')

        def start(self):
            events.append('runtime.start')

    monkeypatch.setattr(headless_module, 'HeadlessRuntime', FakeRuntime)
    return events, state, exits, paths


def test_run_background_order(bg_env):
    events, state, exits, paths = bg_env
    code = bootstrap.run(['prog', '--background', '--idle-exit-s', '7'],
                         exit_process=lambda c: events.append(('exit', c)))
    assert code == 3
    assert events == [
        ('logging', paths.log_dir), 'excepthook', 'qapplication', ('single_instance', False),
        'listen', 'load_config', 'headless_migrate', 'build_context',
        ('runtime', 'manager', 'registry', 7.0), 'dispatcher.settings', 'dispatcher.context',
        'runtime.start', 'exec', ('exit', 3),
    ]
    # 不建主窗口、托盘、全局热键，也不走带进度窗口的迁移
    assert not any(isinstance(e, tuple) and e[0] == 'window' for e in events)
    assert 'hotkey' not in events and 'preferences' not in events and 'migrate' not in events
    # 交给 QApplication 的参数里没有无窗口模式的私有参数
    assert state['qt_argv'] == ['prog']
    assert state['guard_args'] == ((), {'data_dir': paths.data_dir, 'background': True})
    context = state['guard'].dispatcher.context
    runtime = state['runtime']
    assert context.auto_marker == 'auto_marker' and context.handle_auto_export is True
    assert context.quit == runtime.quit and context.clock == 'clock'
    assert context.report_renderer is bootstrap.render_report_image_for_ipc
    assert runtime.busy == state['guard'].dispatcher.has_pending
    # 无窗口模式也写阶段快照：报告图片的渲染函数由组合根注入
    assert runtime.stage_renderer is bootstrap.render_stage_snapshot_image


def test_run_background_starts_storage_guard_and_stops_it_on_quit(bg_env, monkeypatch):
    """无窗口模式：守卫启动（不连任何界面），退出收尾之前由运行期的钩子停掉"""
    events, state, exits, paths = bg_env

    class Guard:
        started = False

        def start(self):
            self.started = True

        def stop(self):
            pass

    guard = Guard()
    fake_build = bootstrap.build_context

    def build(paths, *, report=None, legacy_importer=None):
        ctx = fake_build(paths, report=report, legacy_importer=legacy_importer)
        ctx.storage_guard = guard
        return ctx

    monkeypatch.setattr(bootstrap, 'build_context', build)
    bootstrap.run(['prog', '--background'], exit_process=exits.append)
    assert guard.started
    assert state['runtime'].quit_hooks == [guard.stop]


def test_run_background_secondary_does_not_activate(bg_env):
    events, state, exits, paths = bg_env
    state['existing'] = True
    code = bootstrap.run(['prog', '--background'], exit_process=exits.append)
    assert code == 0
    assert events[-1] == ('single_instance', False)
    assert 'listen' not in events and exits == []


def test_run_background_migration_failure_exits(bg_env):
    events, state, exits, paths = bg_env
    state['report'] = MigrationReport(MigrationOutcome.FAILED_KEPT, 3, 5)
    code = bootstrap.run(['prog', '--background'], exit_process=exits.append)
    assert code == bootstrap.EXIT_MIGRATION_FAILED
    assert 'build_context' not in events and 'exec' not in events


def test_run_background_exits_when_ipc_cannot_listen(bg_env, monkeypatch):
    """没有管道就收不到 quit：无窗口实例不能留成关不掉的后台进程"""
    events, state, exits, paths = bg_env
    monkeypatch.setattr(_FakeGuard, 'listen', lambda self: self.events.append('listen') or False)
    code = bootstrap.run(['prog', '--background'], exit_process=exits.append)
    assert code == bootstrap.EXIT_IPC_UNAVAILABLE
    assert events[-1] == 'listen'
    assert 'build_context' not in events and exits == []


def test_run_gui_tells_user_when_background_instance_runs(bg_env):
    events, state, exits, paths = bg_env
    state['existing'] = True
    state['activation_error'] = 'invalid_state'
    boxes = []
    code = bootstrap.run(['prog'], exit_process=exits.append,
                         message_box=lambda title, text: boxes.append(text))
    assert code == 0
    assert events[-1] == ('single_instance', True)
    assert boxes == [bootstrap.BACKGROUND_RUNNING_TEXT]


def test_run_gui_attaches_ipc_after_window(bg_env, monkeypatch):
    import ui.main_window as main_window_module
    events, state, exits, paths = bg_env

    class Presenter:
        auto_marker = 'presenter_auto_marker'

    class Page:
        presenter = Presenter()

    class Window:
        monitor_page = Page()

        def __init__(self, *args, **kwargs):
            events.append('window')

        def show(self):
            events.append('window.show')

        def request_quit(self):
            pass

    monkeypatch.setattr(main_window_module, 'MainWindow', Window)
    monkeypatch.setattr(bootstrap, 'sync_dir_hint', lambda data_dir: None)
    bootstrap.run(['prog'], exit_process=exits.append)
    assert events.index('listen') < events.index('window') < events.index('dispatcher.context')
    assert events.index('activation_connected') < events.index('dispatcher.context')
    context = state['guard'].dispatcher.context
    # 自动打点服务全程只有一份：用监控页 Presenter 的那一份
    assert context.auto_marker == 'presenter_auto_marker'
    assert context.handle_auto_export is False
    assert context.quit.__func__ is Window.request_quit


@pytest.mark.parametrize('argv, expected', [
    (['p'], 0.0), (['p', '--idle-exit-s', '30'], 30.0), (['p', '--idle-exit-s=2.5'], 2.5),
    (['p', '--idle-exit-s', 'abc'], 0.0), (['p', '--idle-exit-s', '-5'], 0.0),
    (['p', '--idle-exit-s'], 0.0),
])
def test_parse_idle_exit(argv, expected):
    assert bootstrap._parse_idle_exit(argv) == expected


def test_strip_background_args():
    argv = ['p', '--background', '--idle-exit-s', '5', '-x', '--idle-exit-s=3', 'y']
    assert bootstrap.strip_background_args(argv) == ['p', '-x', 'y']
    # argv[0] 恰好与参数同名时不剥
    assert bootstrap.strip_background_args(['--background']) == ['--background']


def test_command_settings_reads_config_live():
    from app_config import cfg
    settings = bootstrap.command_settings()
    original = cfg.automation_enabled.value
    try:
        cfg.automation_enabled.value = False
        assert settings.automation_enabled() is False
        cfg.automation_enabled.value = True
        assert settings.automation_enabled() is True
    finally:
        cfg.automation_enabled.value = original


# ========== run()：自身监控、看门狗、启动计时 ==========

def test_run_installs_watchdog_and_self_monitor_after_show(run_env):
    events, state, exits, paths = run_env
    bootstrap.run(['prog'], exit_process=exits.append)
    # 两者都在窗口显示之后（看门狗不能在首屏渲染期间就计时）
    assert state['after_show'] == [('watchdog', True), ('self_monitor', True)]


def test_run_starts_storage_guard_after_show_with_the_window(run_env, monkeypatch):
    """存储守卫在窗口显示之后启动，并把主窗口交给它接信号（有窗口模式的提示都经窗口）"""
    events, state, exits, paths = run_env
    seen = []
    monkeypatch.setattr(bootstrap, 'start_storage_guard',
                        lambda ctx, window=None: seen.append(
                            ('window.show' in events, window is not None)))
    bootstrap.run(['prog'], exit_process=exits.append)
    assert seen == [(True, True)]


def test_run_gives_window_the_context_as_diagnostics_provider(run_env):
    events, state, exits, paths = run_env
    bootstrap.run(['prog'], exit_process=exits.append)
    ctx = state['diagnostics_provider']()
    assert isinstance(ctx, AppContext) and ctx.paths == paths


def test_startup_trace_flag_is_stripped_and_logs_stages(run_env, caplog):
    events, state, exits, paths = run_env
    with caplog.at_level('INFO', logger='app.bootstrap'):
        bootstrap.run(['prog', '--startup-trace'], exit_process=exits.append)
    assert '--startup-trace' not in state['qt_argv']
    report = "\n".join(r.getMessage() for r in caplog.records if r.levelname == 'INFO')
    for stage in ("QApplication", "迁移", "主窗口", "就绪"):
        assert stage in report


def test_no_startup_trace_without_flag(run_env, caplog):
    events, state, exits, paths = run_env
    with caplog.at_level('INFO', logger='app.bootstrap'):
        bootstrap.run(['prog'], exit_process=exits.append)
    assert not any("就绪" in r.getMessage() for r in caplog.records)


def test_strip_startup_trace_arg_keeps_program_name():
    assert bootstrap.strip_startup_trace_arg(['prog', '--startup-trace', '-x']) == ['prog', '-x']
    assert bootstrap.strip_startup_trace_arg(['--startup-trace']) == ['--startup-trace']


def test_start_self_monitor_ticks_now_and_every_interval(qapp):
    from tests.helpers import process_events_until

    class Monitor:
        def __init__(self):
            self.ticks = 0

        def tick(self):
            self.ticks += 1

    ctx = AppContext(paths=None, db=None, manager=None, services=None, registry=None,
                     maintenance=None, reconcile=None, self_monitor=Monitor())
    timer = bootstrap.start_self_monitor(ctx, interval_ms=60_000)
    try:
        assert ctx.self_monitor_timer is timer and timer.interval() == 60_000
        assert timer.isActive()
        # 立即采一次：诊断信息里马上有值
        assert process_events_until(lambda: ctx.self_monitor.ticks >= 1, timeout=2)
    finally:
        timer.stop()
    assert bootstrap.SELF_MONITOR_INTERVAL_MS == 60_000


def test_start_self_monitor_without_monitor_does_nothing(qapp):
    ctx = AppContext(paths=None, db=None, manager=None, services=None, registry=None,
                     maintenance=None, reconcile=None)
    assert bootstrap.start_self_monitor(ctx) is None


def test_build_context_creates_self_monitor(qapp, paths):
    ctx = build_context(paths)
    try:
        assert ctx.self_monitor is not None
        assert ctx.hub is ctx.manager.snapshot_hub
    finally:
        _close(ctx)


def test_install_watchdog_hands_it_to_window(qapp, monkeypatch):
    import utils.crash_handler as crash_handler
    sentinel = object()
    monkeypatch.setattr(crash_handler, 'install_ui_watchdog', lambda app: sentinel)
    attached = []

    class Window:
        def attach_watchdog(self, wd):
            attached.append(wd)

    ctx = AppContext(paths=None, db=None, manager=None, services=None, registry=None,
                     maintenance=None, reconcile=None)
    bootstrap.install_watchdog(qapp, ctx, Window())
    assert ctx.watchdog is sentinel and attached == [sentinel]

    monkeypatch.setattr(crash_handler, 'install_ui_watchdog', lambda app: None)
    attached.clear()
    bootstrap.install_watchdog(qapp, ctx, Window())
    assert ctx.watchdog is None and attached == []



# ========== 评审修复：无窗口模式不弹异常框、关闭命令行控制时直接退出 ==========

def test_run_background_suppresses_crash_message_box(bg_env):
    from utils import crash_handler
    events, state, exits, paths = bg_env
    bootstrap.run(['prog', '--background'], exit_process=exits.append)
    assert crash_handler.is_headless() is True


def test_run_gui_keeps_crash_message_box(bg_env, monkeypatch):
    from utils import crash_handler
    events, state, exits, paths = bg_env
    state['existing'] = True
    bootstrap.run(['prog'], exit_process=exits.append,
                  message_box=lambda title, text: None)
    assert crash_handler.is_headless() is False


def test_run_background_exits_zero_when_automation_disabled(bg_env, monkeypatch, caplog):
    import logging
    events, state, exits, paths = bg_env
    monkeypatch.setattr(bootstrap, '_automation_enabled', lambda: False)
    with caplog.at_level(logging.WARNING):
        code = bootstrap.run(['prog', '--background'], exit_process=exits.append)
    assert code == bootstrap.EXIT_AUTOMATION_DISABLED == 0
    # 读完配置就退出：不升级数据库、不组装、不进事件循环
    assert events[-1] == 'load_config'
    assert 'headless_migrate' not in events and 'build_context' not in events
    assert 'exec' not in events and exits == []
    assert "允许命令行与脚本控制" in caplog.text


def test_automation_enabled_reads_setting_and_fails_open(monkeypatch):
    class Settings:
        def __init__(self, value):
            self.value = value

        def automation_enabled(self):
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    monkeypatch.setattr(bootstrap, 'command_settings', lambda: Settings(False))
    assert bootstrap._automation_enabled() is False
    monkeypatch.setattr(bootstrap, 'command_settings', lambda: Settings(True))
    assert bootstrap._automation_enabled() is True
    monkeypatch.setattr(bootstrap, 'command_settings', lambda: Settings(RuntimeError("坏")))
    assert bootstrap._automation_enabled() is True
