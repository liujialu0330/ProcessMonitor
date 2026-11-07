"""
阶段 1 存储层的若干契约（各用例去掉对应实现即失败）：

- 升级失败（库仍是旧结构）时组合根只读打开、跳过启动校正、不写库，主窗口横幅写明原因、
  禁止开始监控与打点；
- 分析查询在同一读事务里完成：读到一半时另一个连接追加样本，汇总路径的结果与追加前一致；
- 回退期间写入的数据较多时，启动校正的导回经进度窗口在工作线程执行；
- 改采集周期后按新周期重算汇总与空洞；
- 整包读取：超过 SQLite 长度上限退回逐行、-0.0 两条路径都规范为 0.0；
- 打点时刻作为样本边界时与样本同一取整规则；
- 进度窗口显示"正在生成汇总 N / M"；
- 连接工厂只在首次打开时切 WAL，只读工厂不切。
"""
import gzip
import math
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import bootstrap
from app.bootstrap import build_context
from core import history_query
from core.maintenance_service import ReconcileResult
from data.database import Database
from data.db import connection as connection_module
from data.db.connection import ConnectionFactory
from data.db.migrations.runner import MigrationOutcome, MigrationRunner
from data.mappers import from_ms, to_ms
from data.models import DataPoint, Marker, MonitorTask
from data.repos import _samples
from data.repos.analytics_repo import AnalyticsRepo
from tests.fixtures.legacy_db import FIXTURE_DIR
from tests.test_analytics_rollup_path import close
from tests.test_migration_golden import SAMPLES, _read_bytes
from tests.test_rollup import assert_rollups_consistent

T0 = datetime(2026, 3, 1, 10, 0, 0)


@pytest.fixture
def paths(app_paths):
    app_paths.ensure_dirs()
    return app_paths


def _task(db, task_id='t1', interval=1.0, metrics=('m1', 'm2'), status='stopped'):
    db.save_task(MonitorTask(task_id=task_id, pid=1, process_name='a.exe',
                             metric_types=list(metrics), interval=interval, start_time=T0,
                             end_time=None, status=status))


# ========== 升级失败：只读打开旧结构库 ==========

def _golden_v1(db_path):
    sample = next(s for s in SAMPLES if s['file'] == 'v1.4.1_v1.db.gz')
    with gzip.open(os.path.join(FIXTURE_DIR, sample['file']), 'rb') as src, \
            open(db_path, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    # 样本里的 golden-b 是"上次没正常退出"的运行中任务：启动校正若照常执行就会改写它


def _disk_full_report(db_path):
    factory = ConnectionFactory(db_path, keep_alive=False)
    try:
        return MigrationRunner(
            factory, disk_usage=lambda _d: SimpleNamespace(free=1024)).migrate()
    finally:
        factory.close()


@pytest.fixture
def stale_ctx(qapp, paths):
    """真实的旧库（golden v1）+ 磁盘不足 → BACKUP_ABORTED，再按组合根的路径组装"""
    _golden_v1(paths.db_path)
    before = _read_bytes(paths.db_path)
    report = _disk_full_report(paths.db_path)
    assert report.outcome is MigrationOutcome.BACKUP_ABORTED
    ctx = build_context(paths, retention_days=1, report=report)
    yield ctx, before
    ctx.db.close()


def test_failed_upgrade_opens_old_schema_read_only_without_writes(stale_ctx, paths):
    ctx, before = stale_ctx
    db = ctx.db
    assert db.schema_mismatch is True and db.read_only is True
    assert db.backup_aborted is True
    assert ctx.reconcile == ReconcileResult(orphans=0, recounted=0, cleaned=0)
    # 写入一律被拒（门面按原语义返回 False），旧库逐字节不变
    assert db.save_task(MonitorTask(task_id='new', pid=1, process_name='b.exe',
                                    metric_types=['memory_rss'], interval=1.0,
                                    start_time=T0, end_time=None, status='running')) is False
    db.close()
    assert _read_bytes(paths.db_path) == before
    conn = sqlite3.connect(paths.db_path)
    try:
        assert conn.execute("SELECT status FROM tasks WHERE task_id = 'golden-b'"
                            ).fetchone()[0] == 'running'
    finally:
        conn.close()


def test_failed_upgrade_analysis_degrades_to_empty(stale_ctx):
    """旧结构里没有 samples：门面读取降级为空，不抛异常（横幅解释原因）"""
    ctx, _before = stale_ctx
    db = ctx.db
    task = db.get_all_tasks()[0]
    assert db.get_task_data_points(task.task_id) == []
    assert db.get_metric_stats(task.task_id, task.metric_types[0]) is None


def test_failed_upgrade_banner_and_blocked_start(stale_ctx):
    from tests.doubles import RecordingNotifier
    from tests.factories import make_main_window, make_services
    from tests.helpers import dispose_widget, process_events_until, stop_and_join
    from ui.formatting import READ_ONLY_TITLE, SCHEMA_STALE_START_BLOCKED
    from ui.services import ManagerTaskStatus

    ctx, _before = stale_ctx
    services = make_services(notifier=RecordingNotifier(),
                             task_status=ManagerTaskStatus(ctx.manager))
    win = make_main_window(ctx.db, ctx.manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    try:
        notifier = services.notifier
        assert process_events_until(lambda: notifier.messages("warning"))
        process_events_until(lambda: False, timeout=0.3)
        [banner] = notifier.messages("warning")   # 只有横幅，不再另弹"升级已中止"
        assert banner.title == READ_ONLY_TITLE and banner.duration == -1
        assert banner.content.startswith("数据库尚未升级（上次升级失败：磁盘空间不足")
        assert "本次以只读方式打开" in banner.content
        assert "历史数据要等升级完成后才能查看与分析" in banner.content
        assert "请释放磁盘空间后重启" in banner.content
        presenter = win.monitor_page.presenter
        assert presenter.read_only
        assert presenter._read_only_reason == SCHEMA_STALE_START_BLOCKED
    finally:
        stop_and_join(win.monitor_manager)
        dispose_widget(win)


@pytest.mark.parametrize('outcome, stale', [
    (MigrationOutcome.BACKUP_ABORTED, True), (MigrationOutcome.FAILED_RESTORED, True),
    (MigrationOutcome.FAILED_KEPT, True), (MigrationOutcome.RESET, False),
    (MigrationOutcome.MIGRATED, False), (MigrationOutcome.UP_TO_DATE, False),
])
def test_schema_not_upgraded_outcomes(outcome, stale):
    from data.db.migrations.runner import MigrationReport
    assert bootstrap.schema_not_upgraded(MigrationReport(outcome, 1, 1)) is stale


def test_failed_rebuild_reason_reaches_banner(qapp, paths, monkeypatch):
    """重建失败（程序性异常）：报告带原因，横幅照抄"""
    import data.db.migrations.rebuild as rebuild_module
    from tests.test_migration_v2 import _create_v1_db
    _create_v1_db(paths.db_path, {'t1': ['memory_rss']},
                  [('t1', '2026-01-01T00:00:00', 1.0, 'memory_rss')])

    def boom(*args, **kwargs):
        raise ValueError("坏数据")

    monkeypatch.setattr(rebuild_module, 'copy_points', boom)
    ctx = build_context(paths, retention_days=0)
    try:
        report = ctx.db.migration_report
        assert report.outcome is MigrationOutcome.FAILED_RESTORED
        assert report.reason == "旧数据转换出错（坏数据）"
        assert ctx.db.schema_mismatch
        from ui.formatting import schema_stale_banner_text
        text = schema_stale_banner_text(report.reason, report.disk_shortage)
        assert "上次升级失败：旧数据转换出错（坏数据）" in text
        assert "请关闭可能占用数据库的程序后重启重试" in text
    finally:
        ctx.db.close()


# ========== 分析查询在同一读事务里 ==========

class _InjectingFactory:
    """包装连接工厂：第 after 条查询语句执行完、下一条开始前，调用一次 inject（另一个连接写入）"""

    def __init__(self, inner, inject, after=1):
        self.inner = inner
        self.inject = inject
        self.after = after
        self.injected = False

    @contextmanager
    def connect(self):
        with self.inner.connect() as conn:
            seen = [0]

            def trace(sql):
                head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ''
                if head in ('BEGIN', 'COMMIT', 'ROLLBACK', 'PRAGMA'):
                    return
                seen[0] += 1
                if seen[0] == self.after + 1 and not self.injected:
                    self.injected = True
                    self.inject()

            conn.set_trace_callback(trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)


def _snapshot_db(tmp_path):
    path = str(tmp_path / 'monitor.db')
    db = Database(path)
    _task(db, metrics=('m1', 'm2'))
    points = []
    for i in range(900):
        ts = T0 + timedelta(seconds=i)
        if 300 <= i < 320:
            continue    # 一个空洞
        points.append(DataPoint('t1', ts, float((i * 7) % 50), 'm1'))
        points.append(DataPoint('t1', ts, float((i * 11) % 30), 'm2'))
    db.save_data_points(points)
    return path, db


def _appender(db):
    """追加在已有样本之后（隔一个空洞）的新样本：改变末端、计数与空洞"""
    def inject():
        later = T0 + timedelta(seconds=2000)
        db.save_data_points(
            [DataPoint('t1', later + timedelta(seconds=k), 1000.0 + k, m)
             for k in range(120) for m in ('m1', 'm2')])
    return inject


@pytest.mark.parametrize('call', [
    lambda repo: repo.legacy_buckets('t1', None, 5),
    lambda repo: repo.legacy_buckets('t1', 'm1', 5),
    lambda repo: repo.aggregates('t1', 'm1'),
    lambda repo: repo.gaps('t1', 1.0),
    lambda repo: repo.point_count('t1'),
    lambda repo: repo.buckets('t1', 'm1', 20, interval_seconds=1.0),
], ids=['legacy_buckets_all', 'legacy_buckets_m1', 'aggregates', 'gaps_multi', 'point_count',
        'buckets'])
def test_rollup_path_reads_one_snapshot(tmp_path, call):
    path, db = _snapshot_db(tmp_path)
    try:
        factory = ConnectionFactory(path)
        expected = call(AnalyticsRepo(factory, path='rollup'))
        raw = call(AnalyticsRepo(factory, path='raw'))

        injecting = _InjectingFactory(factory, _appender(db))
        got = call(AnalyticsRepo(injecting, path='rollup'))

        assert injecting.injected
        assert got == expected
        assert close(got, raw)
        # 注入确实生效：之后的新查询能看到追加的样本
        assert call(AnalyticsRepo(factory, path='rollup')) != expected
        factory.close()
    finally:
        db.close()


def test_read_transaction_allowed_on_read_only_connection(tmp_path):
    path, db = _snapshot_db(tmp_path)
    db.close()
    factory = ConnectionFactory(path, read_only=True)
    try:
        assert AnalyticsRepo(factory).point_count('t1') == 1760
    finally:
        factory.close()


# ========== 回退期间数据较多时导回经进度窗口 ==========

def _seed_legacy_points(path, n):
    db = Database(path)
    _task(db, metrics=('memory_rss',))
    db.close()
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            'INSERT INTO data_points (task_id, timestamp, value, metric_type) VALUES (?, ?, ?, ?)',
            [('t1', (T0 + timedelta(seconds=i)).isoformat(), float(i), 'memory_rss')
             for i in range(n)])
        conn.commit()
    finally:
        conn.close()


def test_legacy_import_goes_through_progress_window(qapp, paths, monkeypatch):
    from ui.windows import migration_window
    _seed_legacy_points(paths.db_path, 30)
    monkeypatch.setattr(bootstrap, 'MIGRATION_PROGRESS_THRESHOLD_ROWS', 1)
    calls = []
    real = migration_window.run_with_progress_window

    def spy(migrate, **kwargs):
        win = migration_window.MigrationWindow(kwargs['backup_path'], kwargs['estimated_rows'])
        calls.append((kwargs, win))
        return real(migrate, window=win, **kwargs)

    monkeypatch.setattr(migration_window, 'run_with_progress_window', spy)
    ctx = build_context(paths, retention_days=0,
                        legacy_importer=bootstrap.import_legacy_with_progress)
    try:
        [(kwargs, win)] = calls
        assert kwargs['backup_path'] is None and kwargs['estimated_rows'] == 30
        assert win.title_label.text() == "正在合并回退期间的数据…"
        assert "备份" not in win.status_label.text()
        assert win.is_finished and win.progress_bar.value() == migration_window.PROGRESS_SCALE
        assert ctx.reconcile.imported == 30
        assert ctx.db.get_data_point_count('t1') == 30
        assert ctx.db.get_task('t1').sample_count == 30
        conn = sqlite3.connect(paths.db_path)
        try:
            assert conn.execute('SELECT COUNT(*) FROM data_points').fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        ctx.db.close()
        from tests.helpers import dispose_widget
        for _kwargs, win in calls:
            dispose_widget(win)
    assert_rollups_consistent(paths.db_path)


def test_small_legacy_import_stays_synchronous(qapp, paths, monkeypatch):
    from ui.windows import migration_window
    _seed_legacy_points(paths.db_path, 30)
    monkeypatch.setattr(migration_window, 'run_with_progress_window',
                        lambda *a, **k: pytest.fail("行数未超阈值不应显示进度窗口"))
    ctx = build_context(paths, retention_days=0,
                        legacy_importer=bootstrap.import_legacy_with_progress)
    try:
        assert ctx.reconcile.imported == 30
    finally:
        ctx.db.close()


# ========== 改采集周期后重算汇总 ==========

def test_interval_change_rebuilds_rollups(tmp_path):
    path = str(tmp_path / 'monitor.db')
    db = Database(path)
    try:
        _task(db, interval=1.0, metrics=('m1',))
        # 第 10 秒之后隔 4 秒：1 秒周期下是空洞（> 2.5 秒），2 秒周期下不是（≤ 5 秒）
        stamps = [T0 + timedelta(seconds=s) for s in [*range(0, 11), *range(14, 30)]]
        db.save_data_points([DataPoint('t1', ts, float(i), 'm1') for i, ts in enumerate(stamps)])
        assert len(db.analytics.gaps('t1', 1.0)) == 1
        assert_rollups_consistent(path)

        task = db.get_task('t1')
        task.interval = 2.0
        assert db.save_task(task) is True

        assert_rollups_consistent(path)
        conn = sqlite3.connect(path)
        try:
            assert conn.execute('SELECT COUNT(*) FROM series_gaps').fetchone()[0] == 0
        finally:
            conn.close()
        assert db.analytics.gaps('t1', 2.0) == []
    finally:
        db.close()


def test_saving_same_interval_does_not_rebuild(tmp_path, monkeypatch):
    from data.repos import task_repo
    path = str(tmp_path / 'monitor.db')
    db = Database(path)
    try:
        _task(db, interval=1.0, metrics=('m1',))
        db.save_data_points([DataPoint('t1', T0, 1.0, 'm1')])
        monkeypatch.setattr(task_repo.rollup, 'rebuild_rollups',
                            lambda *a, **k: pytest.fail("周期未变不应重算"))
        task = db.get_task('t1')
        task.status = 'stopped'
        assert db.save_task(task) is True
    finally:
        db.close()


# ========== 整包读取 ==========

def test_fetch_packed_falls_back_when_result_exceeds_length_limit():
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v REAL)')
        conn.executemany('INSERT INTO t (v) VALUES (?)', [(i / 7,) for i in range(200)])
        sql = 'SELECT id AS c0, v AS c1 FROM t'
        expected = [tuple(r) for r in conn.execute(sql)]
        assert sorted(_samples.fetch_packed(conn, sql, (), 'if')) == expected
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 500)
        with pytest.raises(sqlite3.DataError):
            conn.execute("SELECT group_concat(printf('%!.17g', v), ',') FROM t").fetchone()
        assert sorted(_samples.fetch_packed(conn, sql, (), 'if')) == expected
    finally:
        conn.close()


@pytest.mark.parametrize('force_rows', [False, True])
def test_fetch_packed_negative_zero_normalized_on_both_paths(force_rows):
    conn = sqlite3.connect(':memory:')
    try:
        # 第二行的 NULL 让整包读取判为不安全、退回逐行
        extra = ' UNION ALL SELECT 2, NULL' if force_rows else ''
        rows = _samples.fetch_packed(
            conn, f'SELECT 1 AS c0, 0.0 * -1 AS c1{extra}', (), 'if')
        first = sorted(rows, key=lambda r: r[0])[0]
        assert first == (1, 0.0)
        assert math.copysign(1.0, first[1]) == 1.0
    finally:
        conn.close()


# ========== 打点边界与样本同一取整规则 ==========

X = T0 + timedelta(seconds=10)   # 整毫秒时刻


def _us(dt, micro):
    return dt + timedelta(microseconds=micro)


@pytest.mark.parametrize('marker_us, sample_us, after', [
    (400, 400, True),      # 与打点同一时刻采集：一定算打点及之后（原先向上取整会算到之前）
    (300, 700, True),      # 晚 0.4 ms
    (700, 300, False),     # 早 0.4 ms，取整后不在同一毫秒：之前
    (400, 0, True),        # 早 0.4 ms 但取整后同一毫秒：按同一毫秒算之后
    (-400, -800, False),   # 早 0.4 ms、跨到前一毫秒：之前
])
def test_value_near_marker_rounding(tmp_path, marker_us, sample_us, after):
    path = str(tmp_path / 'monitor.db')
    db = Database(path)
    try:
        _task(db, metrics=('m1',))
        sample = _us(X, sample_us)
        db.save_data_points([DataPoint('t1', X - timedelta(seconds=1), 1.0, 'm1'),
                             DataPoint('t1', sample, 2.0, 'm1'),
                             DataPoint('t1', X + timedelta(seconds=1), 3.0, 'm1')])
        assert (to_ms(sample) >= to_ms(_us(X, marker_us))) is after
        value = db.analytics.value_near('t1', 'm1', _us(X, marker_us), n=1, after_only=True)
        assert value == (2.0 if after else 3.0)
    finally:
        db.close()


def test_intervals_put_sample_taken_at_marker_after_it(tmp_path):
    """打点时刻带亚毫秒（X + 0.3 ms），同一时刻采集的样本归入打点之后的区间"""
    path = str(tmp_path / 'monitor.db')
    db = Database(path)
    try:
        _task(db, metrics=('m1',))
        marker_ts = _us(X, 300)
        db.save_data_points([DataPoint('t1', X - timedelta(seconds=1), 1.0, 'm1'),
                             DataPoint('t1', marker_ts, 2.0, 'm1'),
                             DataPoint('t1', X + timedelta(seconds=1), 3.0, 'm1')])
        markers = [Marker(task_id='t1', timestamp=marker_ts, label='第1轮', kind='user')]
        first, last = X - timedelta(seconds=1), X + timedelta(seconds=1)

        intervals = history_query._intervals(db, 't1', 'm1', markers, first, last)

        assert [i.sample_count for i in intervals] == [1, 2]
        assert intervals[1].start_value == 2.0
        assert intervals[0].end == marker_ts   # 显示用的仍是打点原时刻
    finally:
        db.close()


def test_intervals_marker_within_first_sample_ms_has_no_empty_head(tmp_path):
    """打点取整后与首个样本同一毫秒：不再补出一个空的"开始"段"""
    path = str(tmp_path / 'monitor.db')
    db = Database(path)
    try:
        _task(db, metrics=('m1',))
        db.save_data_points([DataPoint('t1', X, 1.0, 'm1'),
                             DataPoint('t1', X + timedelta(seconds=1), 2.0, 'm1')])
        markers = [Marker(task_id='t1', timestamp=_us(X, 300), label='基线', kind='baseline')]
        intervals = history_query._intervals(db, 't1', 'm1', markers, X,
                                             X + timedelta(seconds=1))
        assert [i.sample_count for i in intervals] == [2]
    finally:
        db.close()


# ========== 进度窗口：生成汇总阶段 ==========

def test_window_shows_rollup_stage(qapp):
    from tests.helpers import dispose_widget
    from ui.windows.migration_window import PROGRESS_SCALE, MigrationWindow
    win = MigrationWindow(r"C:\数据\monitor.db.bak_v1")
    try:
        win.set_stage_progress('samples', 5, 10)
        assert win.status_label.text().startswith("已处理 5 / 10 行")
        win.set_stage_progress('rollups', 3, 1200)
        assert win.status_label.text() == \
            r"正在生成汇总 3 / 1,200 · 备份保存在 C:\数据\monitor.db.bak_v1"
        assert win.progress_bar.value() == 3 * PROGRESS_SCALE // 1200
    finally:
        win.finish()
        dispose_widget(win)


def test_run_with_progress_window_relays_rollup_stage(qapp):
    from tests.helpers import dispose_widget
    from ui.windows.migration_window import MigrationWindow, run_with_progress_window
    win = MigrationWindow(None)

    def migrate(on_progress):
        on_progress('samples', 10, 10)
        on_progress('rollups', 2, 2)
        return 'ok'

    try:
        assert run_with_progress_window(migrate, backup_path=None, window=win) == 'ok'
        assert win.status_label.text() == "正在生成汇总 2 / 2"
    finally:
        dispose_widget(win)


# ========== 连接工厂只切一次 WAL ==========

def test_factory_sets_wal_once(tmp_path, monkeypatch):
    calls = []
    real = connection_module._apply_pragmas

    def spy(conn, read_only=False, set_wal=True):
        calls.append((read_only, set_wal))
        return real(conn, read_only, set_wal)

    monkeypatch.setattr(connection_module, '_apply_pragmas', spy)
    path = str(tmp_path / 'monitor.db')
    factory = ConnectionFactory(path)
    try:
        with factory.connect() as conn:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        with factory.transaction() as cursor:
            cursor.execute('CREATE TABLE t (x)')
        with factory.connect() as conn:
            conn.execute('SELECT 1').fetchone()
    finally:
        factory.close()
    assert calls == [(False, True), (False, False), (False, False)]

    calls.clear()
    ro = ConnectionFactory(path, read_only=True)
    try:
        with ro.connect() as conn:
            conn.execute('SELECT COUNT(*) FROM t').fetchone()
    finally:
        ro.close()
    assert calls == [(True, False)]


def test_time_bounds_helper_roundtrip():
    """from_ms(to_ms(x)) 与 normalize_ms 一致（_bound 的依据）"""
    from data.mappers import normalize_ms
    for micro in (0, 400, 499, 500, 501, 999_499, 999_500):
        dt = _us(X, micro)
        assert normalize_ms(dt) == from_ms(to_ms(dt))
