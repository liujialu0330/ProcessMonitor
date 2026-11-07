"""
v0 -> v1 数据库迁移用例，以及迁移运行器的失败分支（备份中止 / 还原 / 重置 / 原样保留）
用裸 sqlite3 构造旧库（tasks.metric_type 单值文本、data_points 无 metric_type 列、
user_version=0），再实例化 Database 或直接运行 MigrationRunner 触发迁移
"""
import glob
import json
import os
import shutil
import sqlite3

import pytest

import data.database as database_module
import data.db.connection as connection_module
import data.db.migrations.runner as runner_module
from data.database import Database
from data.db.connection import ConnectionFactory
from data.db.migrations import MIGRATIONS, Migration
from data.db.migrations.runner import MigrationOutcome, MigrationRunner
from data.db.schema import SCHEMA_VERSION


def _create_v0_db(path: str):
    """构造一个 v0 版本的旧库：单值 metric_type、data_points 无 metric_type 列"""
    conn = sqlite3.connect(path)
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                process_name TEXT NOT NULL,
                metric_type TEXT NOT NULL,
                interval REAL NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                status TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE data_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                value REAL NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            )
        ''')
        cursor.execute('''
            INSERT INTO tasks (task_id, pid, process_name, metric_type, interval,
                                start_time, end_time, status)
            VALUES ('task-1', 1111, 'old.exe', 'memory_rss', 1.0,
                    '2026-01-01T00:00:00', NULL, 'stopped')
        ''')
        cursor.execute('''
            INSERT INTO data_points (task_id, timestamp, value)
            VALUES ('task-1', '2026-01-01T00:00:01', 100.0)
        ''')
        cursor.execute('''
            INSERT INTO data_points (task_id, timestamp, value)
            VALUES ('task-1', '2026-01-01T00:00:02', 101.0)
        ''')
        conn.commit()
        cursor.execute('PRAGMA user_version = 0')
        conn.commit()
    finally:
        conn.close()


def _get_user_version(path: str) -> int:
    conn = sqlite3.connect(path)
    try:
        cursor = conn.cursor()
        cursor.execute('PRAGMA user_version')
        return cursor.fetchone()[0]
    finally:
        conn.close()


def patched_migrations(version: int, fn):
    """把第 version 步迁移替换为 fn 的完整迁移链"""
    return [Migration(m.version, m.name, fn) if m.version == version else m
            for m in MIGRATIONS]


# 冻结的就地迁移链的终点。备份、单事务回滚、从备份还原、重置为空库这些失败分支只存在于
# 就地迁移路径（MigrationRunner(target_version=4)，测试与样本生成器仍在用）；
# 生产用的重建式迁移（目标 v5）的失败分支见 tests/test_rebuild_migration.py
INPLACE_TARGET = 4


def patch_database_migration(monkeypatch, version: int = None, fn=None):
    """让 Database() 内部改用就地迁移链（version/fn 给出时把第 version 步替换为 fn）"""
    patched = patched_migrations(version, fn) if version is not None else MIGRATIONS
    monkeypatch.setattr(
        database_module, 'MigrationRunner',
        lambda factory: MigrationRunner(factory, migrations=patched,
                                        target_version=INPLACE_TARGET))


def run_migration(db_path: str, migrations=MIGRATIONS):
    """不经 Database 直接运行迁移（目标为当前版本），返回 MigrationReport"""
    return MigrationRunner(ConnectionFactory(db_path), migrations=migrations).migrate()


def run_inplace(db_path: str, migrations=MIGRATIONS):
    """不经 Database 直接运行冻结的就地迁移链（目标 v4），返回 MigrationReport"""
    return MigrationRunner(ConnectionFactory(db_path), migrations=migrations,
                           target_version=INPLACE_TARGET).migrate()


def _raise(exc):
    """返回一个调用即抛 exc 的替身函数"""
    def _fn(*args, **kwargs):
        raise exc
    return _fn


def test_migration_v0_to_v1(db_path):
    """迁移完成后：user_version 升到当前版本、data_points 回填、tasks.metric_type 变 JSON 数组、生成备份"""
    _create_v0_db(db_path)
    assert _get_user_version(db_path) == 0

    db = Database(db_path)

    # user_version 已升级
    assert _get_user_version(db_path) == SCHEMA_VERSION

    # tasks.metric_type 已转换为 JSON 数组文本
    task = db.get_task('task-1')
    assert task is not None
    assert task.metric_types == ['memory_rss']

    # data_points.metric_type 已回填为所属任务的旧单值
    data_points = db.get_task_data_points('task-1')
    assert len(data_points) == 2
    for dp in data_points:
        assert dp.metric_type == 'memory_rss'

    # 迁移前备份存在
    backup_path = db_path + '.bak_v0'
    assert os.path.exists(backup_path)

    # 未标记迁移失败
    assert db.migration_failed is False
    report = db.migration_report
    assert report.outcome is MigrationOutcome.MIGRATED
    assert (report.from_version, report.to_version) == (0, SCHEMA_VERSION)
    assert report.backup_path == backup_path and report.broken_path is None


def test_migration_idempotent_on_second_instantiation(db_path):
    """二次实例化不重复迁移：数据不受影响，version 保持不变"""
    _create_v0_db(db_path)

    Database(db_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION

    backup_path = db_path + '.bak_v0'
    backup_mtime_after_first = os.path.getmtime(backup_path)

    # 第二次实例化：tasks 表已存在且版本已是当前版本，直接返回，不重复迁移
    db2 = Database(db_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert db2.migration_report.outcome is MigrationOutcome.UP_TO_DATE
    assert db2.migration_report.created is False

    # 备份文件未被二次覆盖（迁移逻辑在第二次实例化时未执行到备份语句）
    assert os.path.getmtime(backup_path) == backup_mtime_after_first

    # 数据仍然完整、正确
    task = db2.get_task('task-1')
    assert task.metric_types == ['memory_rss']
    data_points = db2.get_task_data_points('task-1')
    assert len(data_points) == 2


def test_migrated_tasks_metric_type_is_json_array_in_raw_db(db_path):
    """底层存储确实是 JSON 数组文本（而不仅仅是 Python 端解析兼容）"""
    _create_v0_db(db_path)
    Database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT metric_type FROM tasks WHERE task_id = 'task-1'")
        raw_value = cursor.fetchone()[0]
    finally:
        conn.close()

    assert raw_value.startswith('[')
    assert json.loads(raw_value) == ['memory_rss']


# ========== 迁移失败分支 + 进程级 guard ==========

def _boom(cursor):
    raise RuntimeError("模拟迁移失败")


# 写进库文件模拟损坏的垃圾字节（SQLite 打开时报 file is not a database）
_GARBAGE = b'not a sqlite database' * 200


def _copy2_backup_only(monkeypatch, corrupt_db=None, corrupt_with=_GARBAGE):
    """
    shutil.copy2 第一次（迁移前备份）放行，之后（从备份还原）全部失败。
    corrupt_db 给出时，还原失败前先把该库文件改写为 corrupt_with（模拟旧库已损坏、不可读）
    """
    original_copy2 = shutil.copy2
    call_count = {'n': 0}

    def _copy2_side_effect(src, dst, *args, **kwargs):
        call_count['n'] += 1
        if call_count['n'] == 1:
            # 迁移前备份：正常放行，保证备份文件里留有原始数据
            return original_copy2(src, dst, *args, **kwargs)
        if corrupt_db is not None:
            with open(corrupt_db, 'wb') as f:
                f.write(corrupt_with)
        # 之后都是"从备份还原"的调用：模拟磁盘满/文件被占用等还原失败
        raise OSError("模拟还原失败")

    monkeypatch.setattr(runner_module.shutil, 'copy2', _copy2_side_effect)
    return call_count


def test_migration_failed_restores_backup(db_path):
    """迁移两次尝试均失败但还原备份成功：FAILED_RESTORED，旧数据（未迁移）保留"""
    _create_v0_db(db_path)

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert (report.from_version, report.to_version) == (0, 0)
    assert report.backup_path == db_path + '.bak_v0' and report.broken_path is None

    # 还原后仍是 v0（未迁移），原始数据完整
    assert _get_user_version(db_path) == 0
    conn = sqlite3.connect(db_path)
    try:
        raw_value = conn.execute(
            "SELECT metric_type FROM tasks WHERE task_id = 'task-1'").fetchone()[0]
    finally:
        conn.close()
    assert raw_value == 'memory_rss'  # 未被转换为 JSON 数组，说明确实回滚了
    assert os.path.exists(db_path + '.bak_v0')


def test_migration_retries_once_after_restore(db_path):
    """首次失败还原后重试一次，第二次成功即为 MIGRATED"""
    _create_v0_db(db_path)
    original = MIGRATIONS[0].apply
    calls = {'n': 0}

    def _flaky(cursor):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError("瞬时故障")
        original(cursor)

    report = run_inplace(db_path, patched_migrations(1, _flaky))

    assert calls['n'] == 2
    assert report.outcome is MigrationOutcome.MIGRATED
    assert _get_user_version(db_path) == INPLACE_TARGET


def test_backup_failure_aborts_migration_and_leaves_db_untouched(db_path, monkeypatch):
    """迁移前备份失败：BACKUP_ABORTED，旧库原样保留，不生成备份文件、不做任何写入"""
    _create_v0_db(db_path)
    monkeypatch.setattr(runner_module.shutil, 'copy2', _raise(OSError("模拟磁盘已满")))

    report = run_inplace(db_path)

    assert report.outcome is MigrationOutcome.BACKUP_ABORTED
    assert (report.from_version, report.to_version) == (0, 0)
    assert report.backup_path is None

    # 旧库未被迁移、未生成备份文件
    assert _get_user_version(db_path) == 0
    assert not os.path.exists(db_path + '.bak_v0')

    # 原始数据依然完整可读（v0 结构）
    conn = sqlite3.connect(db_path)
    try:
        raw_value = conn.execute(
            "SELECT metric_type FROM tasks WHERE task_id = 'task-1'").fetchone()[0]
    finally:
        conn.close()
    assert raw_value == 'memory_rss'


def test_migration_guard_prevents_repeated_backup_after_abort(db_path, monkeypatch):
    """backup_aborted 后再次实例化 Database（同进程同路径）：不重复尝试备份，直接复用既有结果"""
    _create_v0_db(db_path)

    call_count = {'n': 0}

    def _boom_copy2(*args, **kwargs):
        call_count['n'] += 1
        raise OSError("模拟磁盘已满")

    monkeypatch.setattr(runner_module.shutil, 'copy2', _boom_copy2)
    patch_database_migration(monkeypatch)

    db1 = Database(db_path)
    assert db1.backup_aborted is True
    assert call_count['n'] == 1

    # 第二次、第三次实例化：guard 生效，不应再调用 shutil.copy2
    for _ in range(2):
        again = Database(db_path)
        assert again.backup_aborted is True
        assert again.migration_report is db1.migration_report
        assert call_count['n'] == 1


def test_migration_guard_is_per_path(tmp_path, monkeypatch):
    """guard 按库路径区分：一个库备份失败不影响另一个库照常迁移"""
    first, second = str(tmp_path / 'a.db'), str(tmp_path / 'b.db')
    _create_v0_db(first)
    _create_v0_db(second)

    real_copy2 = shutil.copy2

    def _copy2(src, dst, *args, **kwargs):
        if src == first:
            raise OSError("模拟磁盘已满")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(runner_module.shutil, 'copy2', _copy2)

    assert run_inplace(first).outcome is MigrationOutcome.BACKUP_ABORTED
    assert run_inplace(second).outcome is MigrationOutcome.MIGRATED
    assert _get_user_version(first) == 0
    assert _get_user_version(second) == INPLACE_TARGET


def test_reset_guard_for_tests_allows_retry(db_path, monkeypatch):
    """reset_guard_for_tests 清空记录后同一路径会重新尝试迁移"""
    _create_v0_db(db_path)
    with monkeypatch.context() as m:
        m.setattr(runner_module.shutil, 'copy2', _raise(OSError("模拟磁盘已满")))
        assert run_inplace(db_path).outcome is MigrationOutcome.BACKUP_ABORTED

    assert run_inplace(db_path).outcome is MigrationOutcome.BACKUP_ABORTED  # 仍复用
    runner_module.reset_guard_for_tests()
    assert run_inplace(db_path).outcome is MigrationOutcome.MIGRATED


@pytest.mark.parametrize('corrupt_with', [_GARBAGE, b''], ids=['garbage', 'truncated'])
def test_restore_from_backup_also_fails_resets_to_empty_db(db_path, monkeypatch, corrupt_with):
    """
    迁移失败、从备份还原也失败，且旧库已不可读（写入垃圾字节 / 截断为 0 字节、没有表）：
    损坏库改名保留（.broken_*），db_path 处新建当前版本空库，结果为 RESET。
    """
    _create_v0_db(db_path)
    _copy2_backup_only(monkeypatch, corrupt_db=db_path, corrupt_with=corrupt_with)

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.RESET
    assert (report.from_version, report.to_version) == (0, INPLACE_TARGET)

    # db_path 处最终应是全新的空库
    assert _get_user_version(db_path) == INPLACE_TARGET
    assert Database(db_path).get_all_tasks() == []

    # 原始数据的权威留存位置是迁移前只写一次、之后只读不写的 .bak_v0 备份文件
    backup_path = db_path + '.bak_v0'
    assert report.backup_path == backup_path
    conn = sqlite3.connect(backup_path)
    try:
        row = conn.execute(
            "SELECT metric_type FROM tasks WHERE task_id = 'task-1'").fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == 'memory_rss', (
        ".bak_v0 备份应完整保留原始任务数据（证明数据从未真正丢失）")

    # 重置为空库后立即停止重试：只改名一次，不会拿旧版本号在新空库上再跑迁移链
    broken_files = glob.glob(db_path + '.broken_*')
    assert broken_files == [report.broken_path]

    # 损坏库原样保留在 .broken_* 中
    assert _read_bytes(broken_files[0]) == corrupt_with


def test_restore_and_rename_both_fail_never_touches_broken_db(db_path, monkeypatch):
    """
    迁移失败 + 备份还原失败 + 旧库已损坏 + 改名也失败：不得在原路径建库或写 user_version，
    原样留下旧库文件，结果为 FAILED_KEPT
    """
    _create_v0_db(db_path)
    _copy2_backup_only(monkeypatch, corrupt_db=db_path)
    monkeypatch.setattr(runner_module.os, 'replace',
                        _raise(PermissionError("模拟文件被占用，无法改名")))

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert (report.from_version, report.to_version) == (0, 0)
    assert report.broken_path is None
    assert _read_bytes(db_path) == _GARBAGE
    assert glob.glob(db_path + '.broken_*') == []


def test_restore_fails_but_old_db_intact_is_kept(db_path, monkeypatch):
    """
    迁移失败、从备份还原也失败，但旧库仍可读：不改名、不建空库，原样保留为 FAILED_KEPT；
    绝不在原路径建表或写 user_version，否则旧表会被标成当前版本、永不再迁移
    """
    _create_v0_db(db_path)
    _copy2_backup_only(monkeypatch)

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert (report.from_version, report.to_version) == (0, 0)
    assert report.broken_path is None

    # 原路径仍是 v0 旧库：版本号未被改写，没有新建当前版本的表或列
    assert _get_user_version(db_path) == 0
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        task_columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
        dp_columns = {row[1] for row in conn.execute('PRAGMA table_info(data_points)')}
        raw_value = conn.execute(
            "SELECT metric_type FROM tasks WHERE task_id = 'task-1'").fetchone()[0]
    finally:
        conn.close()
    assert 'task_markers' not in tables
    assert 'note' not in task_columns
    assert 'metric_type' not in dp_columns
    assert raw_value == 'memory_rss'
    assert glob.glob(db_path + '.broken_*') == []


def test_reset_works_while_caller_holds_keep_alive(db_path, monkeypatch):
    """调用方工厂的常驻连接已打开时，运行器先释放它，改名损坏库不会被文件句柄挡住"""
    _create_v0_db(db_path)
    factory = ConnectionFactory(db_path)
    with factory.connect() as conn:
        conn.execute('SELECT 1').fetchall()
    assert factory.keep_alive_open is True
    _copy2_backup_only(monkeypatch, corrupt_db=db_path)

    report = MigrationRunner(factory, migrations=patched_migrations(1, _boom),
                             target_version=INPLACE_TARGET).migrate()

    assert report.outcome is MigrationOutcome.RESET
    assert factory.keep_alive_open is False
    assert _get_user_version(db_path) == INPLACE_TARGET


@pytest.mark.parametrize('scenario, expected', [
    ('ok', (False, False, False)),
    ('restored', (True, False, False)),
    ('reset', (False, True, False)),
    ('kept', (True, False, False)),
    ('backup', (False, False, True)),
])
def test_database_maps_report_to_flags(db_path, monkeypatch, scenario, expected):
    """Database 把 MigrationReport 映射为 (migration_failed, data_reset, backup_aborted)"""
    _create_v0_db(db_path)
    if scenario == 'backup':
        monkeypatch.setattr(runner_module.shutil, 'copy2', _raise(OSError("模拟磁盘已满")))
        patch_database_migration(monkeypatch)
    elif scenario != 'ok':
        patch_database_migration(monkeypatch, 1, _boom)
        if scenario == 'reset':
            _copy2_backup_only(monkeypatch, corrupt_db=db_path)
        if scenario == 'kept':
            _copy2_backup_only(monkeypatch)

    db = Database(db_path)

    assert (db.migration_failed, db.data_reset, db.backup_aborted) == expected
    assert db.migration_report is not None


def test_runner_rejects_incomplete_chain(db_path):
    """迁移链必须从 v1 连续到目标版本"""
    with pytest.raises(ValueError):
        MigrationRunner(ConnectionFactory(db_path), migrations=MIGRATIONS[1:],
                        target_version=INPLACE_TARGET)


def test_new_db_report_is_up_to_date_and_created(db_path):
    """全新库：建当前结构，结果 UP_TO_DATE 且 created=True，不产生备份"""
    report = run_migration(db_path)
    assert report.outcome is MigrationOutcome.UP_TO_DATE and report.created is True
    assert (report.from_version, report.to_version) == (SCHEMA_VERSION, SCHEMA_VERSION)
    assert report.backup_path is None
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert glob.glob(db_path + '.bak_*') == []


def test_newer_db_is_left_untouched(db_path):
    """库版本比程序支持的还新：原样使用，不迁移不备份"""
    run_migration(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION + 1}')
        conn.commit()
    finally:
        conn.close()

    report = run_migration(db_path)

    assert report.outcome is MigrationOutcome.UP_TO_DATE and report.created is False
    assert report.from_version == SCHEMA_VERSION + 1
    assert _get_user_version(db_path) == SCHEMA_VERSION + 1
    assert glob.glob(db_path + '.bak_*') == []


def _runner_for(db_path):
    run_migration(db_path)   # 先建出当前版本库
    return MigrationRunner(ConnectionFactory(db_path, keep_alive=False))


def test_move_aside_broken_db_moves_wal_and_shm_together(db_path):
    """损坏库改名时连同 -wal/-shm 一起挪走，原路径不残留旧日志"""
    runner = _runner_for(db_path)
    for suffix, content in (('-wal', b'old wal'), ('-shm', b'old shm')):
        with open(db_path + suffix, 'wb') as f:
            f.write(content)

    assert runner._move_aside_broken_db() is True

    for suffix in ('', '-wal', '-shm'):
        assert not os.path.exists(db_path + suffix), suffix
    broken_main = [p for p in glob.glob(db_path + '.broken_*')
                   if not p.endswith(('-wal', '-shm'))]
    assert broken_main == [runner._broken_path]
    with open(broken_main[0] + '-wal', 'rb') as f:
        assert f.read() == b'old wal'
    with open(broken_main[0] + '-shm', 'rb') as f:
        assert f.read() == b'old shm'


def test_move_aside_broken_db_puts_sidecars_back_when_main_fails(db_path, monkeypatch):
    """主库挪不动时把已挪走的伴随文件放回，旧库保持原样"""
    runner = _runner_for(db_path)
    with open(db_path + '-wal', 'wb') as f:
        f.write(b'old wal')

    real_replace = os.replace

    def _replace(src, dst):
        if src == db_path:
            raise PermissionError("主库被占用")
        return real_replace(src, dst)

    monkeypatch.setattr(runner_module.os, 'replace', _replace)

    assert runner._move_aside_broken_db() is False
    assert os.path.exists(db_path)
    with open(db_path + '-wal', 'rb') as f:
        assert f.read() == b'old wal'
    assert glob.glob(db_path + '.broken_*') == []


# ========== 备份命名、原子还原、checkpoint 阻塞 ==========

def _read_bytes(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


def _v0_task_value(path: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT metric_type FROM tasks WHERE task_id = 'task-1'").fetchone()[0]
    finally:
        conn.close()


def _copy2_half_then_fail(src, dst, *args, **kwargs):
    """替身 copy2：只写入源文件前一半就抛错（模拟写到一半磁盘满）"""
    data = _read_bytes(src)
    with open(dst, 'wb') as f:
        f.write(data[:len(data) // 2])
    raise OSError("模拟写到一半磁盘已满")


def test_existing_backup_is_not_overwritten(db_path, monkeypatch):
    """.bak_v0 已存在时不覆盖，本次备份改用带时间戳的名字；时间戳名也冲突时追加序号"""
    _create_v0_db(db_path)
    with open(db_path + '.bak_v0', 'wb') as f:
        f.write(b'older backup')
    monkeypatch.setattr(runner_module, '_timestamp', lambda: '20260101_000000')
    with open(db_path + '.bak_v0_20260101_000000', 'wb') as f:
        f.write(b'same second backup')

    report = run_inplace(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.backup_path == db_path + '.bak_v0_20260101_000000_2'
    assert _read_bytes(db_path + '.bak_v0') == b'older backup'
    assert _read_bytes(db_path + '.bak_v0_20260101_000000') == b'same second backup'
    assert _get_user_version(report.backup_path) == 0
    assert _v0_task_value(report.backup_path) == 'memory_rss'


def test_restore_uses_backup_made_by_this_run(db_path):
    """已有旧 .bak_v0（内容不可用）时，失败还原用的是本次新建的备份"""
    _create_v0_db(db_path)
    with open(db_path + '.bak_v0', 'wb') as f:
        f.write(b'not a database')

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert report.backup_path.startswith(db_path + '.bak_v0_')
    assert _get_user_version(db_path) == 0
    assert _v0_task_value(db_path) == 'memory_rss'
    assert _read_bytes(db_path + '.bak_v0') == b'not a database'


def test_partial_backup_is_removed_and_aborts(db_path, monkeypatch):
    """备份写到一半失败：BACKUP_ABORTED，删掉不完整的备份文件，旧库不动"""
    _create_v0_db(db_path)
    monkeypatch.setattr(runner_module.shutil, 'copy2', _copy2_half_then_fail)

    report = run_inplace(db_path)

    assert report.outcome is MigrationOutcome.BACKUP_ABORTED
    assert glob.glob(db_path + '.bak_*') == []
    assert _get_user_version(db_path) == 0


def test_checkpoint_busy_aborts_backup(db_path, monkeypatch):
    """
    迁移前 checkpoint 被其他连接挡住（PRAGMA 返回 busy=1，不抛错）：-wal 里可能还有未合并的
    提交，旧文件不自包含，不能作为备份，必须按 BACKUP_ABORTED 处理，不备份、不迁移
    """
    _create_v0_db(db_path)
    real_apply = connection_module._apply_pragmas

    def _short_timeout(conn, *args):
        real_apply(conn, *args)
        conn.execute('PRAGMA busy_timeout=50')   # 避免用例等满 5 秒

    monkeypatch.setattr(connection_module, '_apply_pragmas', _short_timeout)
    monkeypatch.setattr(runner_module, 'PROBE_TIMEOUT', 0.05)

    writer = sqlite3.connect(db_path, isolation_level=None)
    reader = sqlite3.connect(db_path, isolation_level=None)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        reader.execute('BEGIN')
        reader.execute('SELECT COUNT(*) FROM tasks').fetchone()   # 持有旧快照
        writer.execute("INSERT INTO data_points (task_id, timestamp, value) "
                       "VALUES ('task-1', '2026-01-01T00:00:03', 102.0)")

        report = run_migration(db_path)

        assert report.outcome is MigrationOutcome.BACKUP_ABORTED
        assert report.backup_path is None
        assert glob.glob(db_path + '.bak_*') == []
        assert _get_user_version(db_path) == 0
        reader.execute('COMMIT')
    finally:
        reader.close()
        writer.close()

    # 最后一个连接关闭后 -wal 已合并：写入的数据没丢
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM data_points').fetchone()[0] == 3
    finally:
        conn.close()


@pytest.mark.parametrize('failure', ['copy_half', 'replace_fails'])
def test_replace_from_backup_failure_leaves_db_untouched(db_path, monkeypatch, failure):
    """还原中途失败（临时文件写一半 / os.replace 失败）：原库字节不变、版本仍是旧值、临时文件已删"""
    _create_v0_db(db_path)
    backup = db_path + '.other'
    run_migration(backup)            # 一个内容不同的当前版本库充当备份
    before = _read_bytes(db_path)
    runner = MigrationRunner(ConnectionFactory(db_path, keep_alive=False))

    if failure == 'copy_half':
        monkeypatch.setattr(runner_module.shutil, 'copy2', _copy2_half_then_fail)
    else:
        monkeypatch.setattr(runner_module.os, 'replace',
                            _raise(PermissionError("模拟目标被占用")))

    assert runner._replace_from_backup(backup) is False

    assert _read_bytes(db_path) == before
    assert _get_user_version(db_path) == 0
    assert not os.path.exists(db_path + '.restore_tmp')


def test_failed_restore_keeps_intact_db_byte_for_byte(db_path, monkeypatch):
    """
    迁移失败后还原写到一半失败：已回滚的旧库没被截断/写坏（直接 copy2 覆盖原路径会留下
    半个文件），与迁移前备份逐字节一致，原样保留为 FAILED_KEPT 并记入 guard；
    清空 guard 后再迁移即可成功
    """
    _create_v0_db(db_path)
    real_copy2 = shutil.copy2
    calls = {'n': 0}

    def _copy2(src, dst, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            return real_copy2(src, dst, *args, **kwargs)
        return _copy2_half_then_fail(src, dst)

    with monkeypatch.context() as m:
        m.setattr(runner_module.shutil, 'copy2', _copy2)

        report = run_inplace(db_path, patched_migrations(1, _boom))

        assert report.outcome is MigrationOutcome.FAILED_KEPT
        assert report.broken_path is None
        assert _read_bytes(db_path) == _read_bytes(report.backup_path)
        assert _get_user_version(db_path) == 0
        assert _v0_task_value(db_path) == 'memory_rss'
        assert glob.glob(db_path + '.broken_*') == []
        assert not os.path.exists(db_path + '.restore_tmp')
        # 结果已记入 guard：同进程再次迁移直接复用
        assert run_inplace(db_path) is report

    runner_module.reset_guard_for_tests()
    again = run_migration(db_path)
    assert again.outcome is MigrationOutcome.MIGRATED
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert again.backup_path != report.backup_path   # 首份备份未被覆盖
    assert _get_user_version(report.backup_path) == 0


def test_second_attempt_restore_failure_keeps_intact_db(db_path, monkeypatch):
    """第 1 次失败还原成功、第 2 次失败还原失败：旧库仍完好，原样保留为 FAILED_KEPT"""
    _create_v0_db(db_path)
    real_copy2 = shutil.copy2
    calls = {'n': 0}

    def _copy2(src, dst, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] <= 2:          # 迁移前备份 + 第 1 次还原
            return real_copy2(src, dst, *args, **kwargs)
        raise OSError("模拟第 2 次还原失败")

    monkeypatch.setattr(runner_module.shutil, 'copy2', _copy2)
    attempts = {'n': 0}

    def _always_fail(cursor):
        attempts['n'] += 1
        raise RuntimeError("模拟迁移失败")

    report = run_inplace(db_path, patched_migrations(1, _always_fail))

    assert attempts['n'] == 2 and calls['n'] == 3
    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert report.broken_path is None
    assert _get_user_version(db_path) == 0
    assert _v0_task_value(db_path) == 'memory_rss'
    assert glob.glob(db_path + '.broken_*') == []
    assert not os.path.exists(db_path + '.restore_tmp')


# ========== 重置时新建空库失败 ==========

def test_reset_create_failure_puts_old_db_back(db_path, monkeypatch):
    """
    还原失败、旧库不可读已改名，但新建空库也失败（磁盘满）：不抛出，删掉建了一半的空库并把
    旧库放回原路径（下次启动不会被当成全新安装），结果 FAILED_KEPT 并记入 guard
    """
    _create_v0_db(db_path)
    _copy2_backup_only(monkeypatch, corrupt_db=db_path)
    monkeypatch.setattr(runner_module, 'create_schema_v4',
                        _raise(sqlite3.OperationalError("database or disk is full")))

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert (report.from_version, report.to_version) == (0, 0)
    assert report.broken_path is None
    assert _v0_task_value(report.backup_path) == 'memory_rss'
    # 原路径是放回来的旧库文件，没有残留空库或改名文件
    assert _read_bytes(db_path) == _GARBAGE
    for suffix in ('-wal', '-shm'):
        assert not os.path.exists(db_path + suffix), suffix
    assert glob.glob(db_path + '.broken_*') == []
    assert not os.path.exists(db_path + '.restore_tmp')
    # 结果已记入 guard（库不可读，再次 migrate() 会在检测阶段抛出，故直接查记录）
    key = runner_module._guard_key(db_path, INPLACE_TARGET)
    assert runner_module._ATTEMPTS[key] is report


def test_reset_create_failure_and_move_back_failure(db_path, monkeypatch):
    """
    新建空库失败且旧库也放不回去：FAILED_KEPT，broken_path 指向保留的旧库，
    原路径不残留空库（否则下次启动会被当成全新安装悄悄建出空库）
    """
    _create_v0_db(db_path)
    _copy2_backup_only(monkeypatch, corrupt_db=db_path)
    monkeypatch.setattr(runner_module, 'create_schema_v4',
                        _raise(sqlite3.OperationalError("database or disk is full")))
    real_replace = os.replace

    def _replace(src, dst):
        if '.broken_' in src:
            raise PermissionError("模拟放回失败")
        return real_replace(src, dst)

    monkeypatch.setattr(runner_module.os, 'replace', _replace)

    report = run_inplace(db_path, patched_migrations(1, _boom))

    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert report.broken_path is not None
    assert glob.glob(db_path + '.broken_*') == [report.broken_path]
    assert _read_bytes(report.broken_path) == _GARBAGE
    assert _v0_task_value(report.backup_path) == 'memory_rss'
    for suffix in ('', '-wal', '-shm'):
        assert not os.path.exists(db_path + suffix), suffix


def test_discard_new_db_keeps_db_with_tables(db_path):
    """清理新建失败的空库时只删无表的库，原路径上已有表的库不删"""
    runner = _runner_for(db_path)
    assert runner._discard_new_db() is False
    assert os.path.exists(db_path)


def test_move_aside_main_and_put_back_both_fail_only_logs(db_path, monkeypatch, caplog):
    """主库改名失败、放回伴随文件也失败：只记日志不抛出，返回 False，主库仍在原路径"""
    runner = _runner_for(db_path)
    with open(db_path + '-wal', 'wb') as f:
        f.write(b'old wal')
    real_replace = os.replace

    def _replace(src, dst):
        if src == db_path or '.broken_' in src:
            raise PermissionError("模拟文件被占用")
        return real_replace(src, dst)

    monkeypatch.setattr(runner_module.os, 'replace', _replace)

    assert runner._move_aside_broken_db() is False
    assert runner._broken_path is None
    assert os.path.exists(db_path)
    assert '放回数据库伴随文件失败' in caplog.text


# ========== 检测库状态时出错 ==========

@pytest.mark.parametrize('scenario', ['corrupt', 'locked_table_exists', 'locked_read_version'])
def test_probe_error_propagates_without_guard(db_path, monkeypatch, scenario):
    """
    库文件损坏或被锁，导致检测阶段（table_exists / read_version）抛异常：migrate() 原样抛出
    （启动即失败，由上层处理），不记入 guard，调用方的常驻连接已释放，库文件没有残留句柄
    """
    if scenario == 'corrupt':
        with open(db_path, 'wb') as f:
            f.write(b'not a sqlite database' * 100)
        factory = ConnectionFactory(db_path)
        expected = sqlite3.DatabaseError
    else:
        _create_v0_db(db_path)
        factory = ConnectionFactory(db_path)
        with factory.connect() as conn:
            conn.execute('SELECT 1').fetchall()
        assert factory.keep_alive_open is True
        name = scenario.replace('locked_', '')
        monkeypatch.setattr(runner_module, name,
                            _raise(sqlite3.OperationalError("database is locked")))
        expected = sqlite3.OperationalError
    before = _read_bytes(db_path)

    with pytest.raises(expected):
        MigrationRunner(factory).migrate()

    assert runner_module._ATTEMPTS == {}
    assert factory.keep_alive_open is False
    assert _read_bytes(db_path) == before
    assert glob.glob(db_path + '.bak_*') == []
    # 没有残留句柄：Windows 上可改名
    os.replace(db_path, db_path + '.moved')
    os.replace(db_path + '.moved', db_path)
