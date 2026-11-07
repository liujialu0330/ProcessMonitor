"""
重建式迁移（v0–v4 → v5）：

- 6 份 golden 样本直接重建，与"冻结就地链迁到 v4 再重建"的结果逐行一致；
- 旧文件：回滚日志模式逐字节不变，WAL 模式只做一次 checkpoint；原文件改名为 .bak_v<n>；
- 换库：硬链接备份 + os.replace，换入失败旧库原样；不支持硬链接时退回改名式（带换库标记），
  改名失败注入：旧库留在原位、无 .migrating 残留；双重失败报 FAILED_KEPT；
- 换库中断（两步之间崩溃、只剩备份、只剩 .migrating）下次启动自动恢复，不建空库；
- 重试只针对锁/IO/磁盘满，程序性异常只跑一次；时间戳无效的行跳过并计数；
- 迁移期间目录里只有新旧两个库文件（磁盘峰值）；on_progress 回调次数；
- 时间换算：纯 SQL 快路径、逐行 Python 路径与 data.mappers.to_ms 逐行一致；
- 中文路径；100 万行耗时（perf 标记，默认不跑）。
"""
import gzip
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import data.db.migrations.rebuild as rebuild_module
import data.db.migrations.runner as runner_module
from data.database import Database
from data.db.connection import ConnectionFactory
from data.db.migrations.rebuild import CHUNK_ROWS, STAGE_ROLLUPS, STAGE_SAMPLES, MsConverter
from data.db.migrations.runner import MigrationOutcome, MigrationRunner
from data.db.schema import SCHEMA_VERSION
from data.mappers import to_ms
from tests.fixtures.legacy_db import FIXTURE_DIR
from tests.test_migration import INPLACE_TARGET, _create_v0_db, _get_user_version
from tests.test_migration_golden import SAMPLES, _dump, _ids, _read_bytes
from tests.test_migration_v2 import _create_v1_db


def _unpack(sample, dest_dir, name='monitor.db') -> str:
    os.makedirs(str(dest_dir), exist_ok=True)
    db_path = os.path.join(str(dest_dir), name)
    with gzip.open(os.path.join(FIXTURE_DIR, sample['file']), 'rb') as src, \
            open(db_path, 'wb') as dst:
        shutil.copyfileobj(src, dst)
    return db_path


def _migrate(db_path, target=SCHEMA_VERSION, on_progress=None):
    factory = ConnectionFactory(db_path, keep_alive=False)
    try:
        return MigrationRunner(factory, target_version=target, on_progress=on_progress).migrate()
    finally:
        factory.close()


def _names(directory):
    return sorted(os.listdir(str(directory)))


def _v1_rows(n, task_id='t1', metrics=('memory_rss', 'memory_private')):
    base = datetime(2026, 1, 1, 8, 0, 0)
    rows = []
    for i in range(n):
        ts = (base + timedelta(seconds=i // len(metrics))).isoformat()
        rows.append((task_id, ts, float(i), metrics[i % len(metrics)]))
    return rows


# ========== golden：直接重建 == 就地链到 v4 再重建 ==========

@pytest.mark.parametrize('sample', [pytest.param(s, id=_ids(s)) for s in SAMPLES])
def test_direct_rebuild_equals_inplace_v4_then_rebuild(sample, tmp_path):
    direct = _unpack(sample, tmp_path / 'direct')
    assert _migrate(direct).outcome is MigrationOutcome.MIGRATED

    via_v4 = _unpack(sample, tmp_path / 'via_v4')
    if sample['schema_version'] < INPLACE_TARGET:
        report = _migrate(via_v4, target=INPLACE_TARGET)
        assert report.outcome is MigrationOutcome.MIGRATED
        assert _get_user_version(via_v4) == INPLACE_TARGET
    report = _migrate(via_v4)
    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.from_version == INPLACE_TARGET

    assert _get_user_version(direct) == _get_user_version(via_v4) == SCHEMA_VERSION
    assert _dump(direct) == _dump(via_v4)


@pytest.mark.parametrize('sample', [pytest.param(s, id=_ids(s)) for s in SAMPLES])
def test_rebuild_generates_rollups_matching_samples(sample, tmp_path):
    """重建式迁移搬完样本后一次性生成的分钟汇总与空洞 == 按样本整体重算的结果"""
    from tests.test_rollup import assert_rollups_consistent
    path = _unpack(sample, tmp_path / 'db')
    assert _migrate(path).outcome is MigrationOutcome.MIGRATED
    conn = sqlite3.connect(path)
    try:
        samples = conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        counted = conn.execute('SELECT COALESCE(SUM(count), 0) FROM rollup_1m').fetchone()[0]
    finally:
        conn.close()
    assert counted == samples
    assert (assert_rollups_consistent(path) > 0) == (samples > 0)


# ========== 旧文件不被改动 ==========

def test_rollback_journal_source_is_byte_identical_and_renamed(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(10, metrics=('memory_rss',)))
    original = _read_bytes(db_path)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.backup_path == db_path + '.bak_v1'
    assert _read_bytes(report.backup_path) == original
    assert 'monitor.db.migrating' not in _names(tmp_path)


def _wal_db_with_pending_frames(src_dir, dest_dir):
    """
    造一个"主文件 + 未合并 -wal"的 v1 库：持有写连接时把两份文件拷走（相当于进程崩溃时
    留下的现场）。返回 dest_dir 里的库路径。
    """
    os.makedirs(str(src_dir), exist_ok=True)
    src = os.path.join(str(src_dir), 'monitor.db')
    _create_v1_db(src, {'t1': ['memory_rss']}, _v1_rows(5, metrics=('memory_rss',)))
    conn = sqlite3.connect(src, isolation_level=None)
    try:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.executemany(
            "INSERT INTO data_points (task_id, timestamp, value, metric_type) "
            "VALUES ('t1', ?, ?, 'memory_rss')",
            [(f'2026-01-02T00:00:{i:02d}', float(i)) for i in range(20)])
        assert os.path.getsize(src + '-wal') > 0
        os.makedirs(str(dest_dir), exist_ok=True)
        dest = os.path.join(str(dest_dir), 'monitor.db')
        shutil.copyfile(src, dest)
        shutil.copyfile(src + '-wal', dest + '-wal')
    finally:
        conn.close()
    return dest


def test_wal_source_is_only_checkpointed(tmp_path):
    """WAL 库：备份 == 原库只做一次 wal_checkpoint(TRUNCATE) 的结果（逐字节），且含 -wal 里的数据"""
    db_path = _wal_db_with_pending_frames(tmp_path / 'src', tmp_path / 'mig')
    reference = _wal_db_with_pending_frames(tmp_path / 'src2', tmp_path / 'ref')
    conn = sqlite3.connect(reference)
    try:
        assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    finally:
        conn.close()
    expected = _read_bytes(reference)
    expected_dump = _dump(reference)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert _read_bytes(report.backup_path) == expected
    assert _dump(report.backup_path) == expected_dump
    assert len(expected_dump['data_points']) == 25
    # 备份没有边车文件；-wal 里的 20 行进了新库
    names = _names(tmp_path / 'mig')
    assert 'monitor.db.bak_v1-wal' not in names and 'monitor.db.bak_v1-shm' not in names
    assert 'monitor.db.migrating' not in names
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 25
    finally:
        conn.close()


def test_backup_name_never_overwrites_existing_file(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    with open(db_path + '.bak_v0', 'wb') as f:
        f.write(b'earlier backup')

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.backup_path != db_path + '.bak_v0'
    assert report.backup_path.startswith(db_path + '.bak_v0')
    assert _read_bytes(db_path + '.bak_v0') == b'earlier backup'


# ========== 失败注入 ==========

def _failing(real, fail_on):
    """包装 os.rename / os.replace：第 fail_on 个调用（从 1 计）抛 OSError"""
    calls = {'n': 0}

    def wrapper(src, dst, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] in fail_on:
            raise OSError(f"模拟改名失败: {src} -> {dst}")
        return real(src, dst, *args, **kwargs)
    return wrapper


def _no_link(src, dst, *args, **kwargs):
    """模拟不支持硬链接的文件系统"""
    raise OSError(f"模拟不支持硬链接: {src} -> {dst}")


def _patch_os(monkeypatch, *, rename=None, replace=None, link=None):
    fake = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)
                              if not name.startswith('__')})
    fake.path = os.path
    if rename is not None:
        fake.rename = rename
    if replace is not None:
        fake.replace = replace
    if link is not None:
        fake.link = link
    monkeypatch.setattr(runner_module, 'os', fake)


class _Crash(BaseException):
    """模拟进程在两步之间崩溃：不是 Exception，任何 except 都拦不住，现场原样留下"""


def _crashing(real, crash_on):
    calls = {'n': 0}

    def wrapper(src, dst, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] in crash_on:
            raise _Crash(f"{src} -> {dst}")
        return real(src, dst, *args, **kwargs)
    return wrapper


def _same_file(a, b):
    return os.path.samefile(a, b)


# ---------- 硬链接式（默认） ----------

def test_swap_uses_hard_link_backup(tmp_path):
    """备份是旧库的硬链接（同一份文件），换库期间不需要第二份磁盘空间"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    before = os.stat(db_path)
    links = []
    real_link = os.link

    def link(src, dst):
        links.append((os.path.basename(src), os.path.basename(dst)))
        return real_link(src, dst)

    import data.db.migrations.runner as rm
    rm.os.link, saved = link, rm.os.link
    try:
        report = _migrate(db_path)
    finally:
        rm.os.link = saved

    assert report.outcome is MigrationOutcome.MIGRATED
    assert links == [('monitor.db', 'monitor.db.bak_v0')]
    after = os.stat(report.backup_path)
    assert (after.st_ino, after.st_dev) == (before.st_ino, before.st_dev)
    assert _read_bytes(report.backup_path) == original
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v0']


def test_link_swap_replace_fails_keeps_old_in_place(tmp_path, monkeypatch):
    """硬链接建好后新库换不进来：原路径一直是旧库，多出来的硬链接删掉"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, replace=_failing(os.replace, {1}))

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert report.reason and "新数据库无法换到原位置" in report.reason
    assert _read_bytes(db_path) == original
    assert _names(tmp_path) == ['monitor.db']


def test_crash_between_link_and_replace_leaves_old_db_in_place(tmp_path, monkeypatch):
    """
    硬链接式两步之间崩溃：原路径仍是完整旧库；下次启动照常迁移成功，上次留下的多余硬链接
    （与本次备份是同一份文件）被删掉，内容不同的更早备份保留
    """
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    with open(db_path + '.bak_v1', 'wb') as f:
        f.write(b'earlier backup')
    _patch_os(monkeypatch, replace=_crashing(os.replace, {1}))
    with pytest.raises(_Crash):
        _migrate(db_path)
    assert _read_bytes(db_path) == original
    assert _same_file(db_path, db_path + '.bak_v0')
    monkeypatch.undo()
    runner_module.reset_guard_for_tests()

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert _read_bytes(report.backup_path) == original
    assert report.backup_path != db_path + '.bak_v0'
    assert _names(tmp_path) == sorted(['monitor.db', 'monitor.db.bak_v1',
                                       os.path.basename(report.backup_path)])
    assert _read_bytes(db_path + '.bak_v1') == b'earlier backup'


# ---------- 改名式（不支持硬链接时） ----------

def test_rename_swap_writes_and_removes_marker(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    seen = []
    real_rename = os.rename

    def rename(src, dst):
        seen.append(os.path.exists(db_path + '.swap_pending'))
        return real_rename(src, dst)

    _patch_os(monkeypatch, link=_no_link, rename=rename)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert seen == [True]   # 改名前标记已落盘
    assert _read_bytes(report.backup_path) == original
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v0']


def test_rename_old_to_backup_fails_keeps_old_in_place(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, link=_no_link, rename=_failing(os.rename, {1}))

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert (report.from_version, report.to_version) == (0, 0)
    assert _read_bytes(db_path) == original
    assert _names(tmp_path) == ['monitor.db']


def test_rename_new_into_place_fails_restores_old_name(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, link=_no_link, replace=_failing(os.replace, {1}))

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert _read_bytes(db_path) == original
    assert _get_user_version(db_path) == 0
    assert _names(tmp_path) == ['monitor.db']


def test_both_renames_fail_reports_failed_kept(tmp_path, monkeypatch):
    """
    改名式：新库换不进来、旧库也改不回原名：旧数据完整留在 .bak 里，报告指向它；
    换库标记与完整的新库留给下次启动恢复（下一个用例）
    """
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, link=_no_link, rename=_failing(os.rename, {2}),
              replace=_failing(os.replace, {1}))

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert report.backup_path == report.broken_path == db_path + '.bak_v0'
    assert _read_bytes(report.backup_path) == original
    assert _names(tmp_path) == ['monitor.db.bak_v0', 'monitor.db.migrating',
                                'monitor.db.swap_pending']


def test_failed_kept_is_recovered_on_next_start(tmp_path, monkeypatch):
    """上一个用例的现场：下次启动把完整的新库换进来，按 MIGRATED 报告，备份仍在"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, link=_no_link, rename=_failing(os.rename, {2}),
              replace=_failing(os.replace, {1}))
    assert _migrate(db_path).outcome is MigrationOutcome.FAILED_KEPT
    monkeypatch.undo()
    runner_module.reset_guard_for_tests()

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert (report.from_version, report.to_version) == (0, SCHEMA_VERSION)
    assert report.backup_path == db_path + '.bak_v0'
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert _read_bytes(report.backup_path) == original
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v0']


def test_crash_between_renames_is_recovered_on_next_start(tmp_path, monkeypatch):
    """改名式两步之间崩溃（os.replace 时进程没了）：下次启动不建空库，把完整的新库换进来"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(30, metrics=('memory_rss',)))
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, link=_no_link, replace=_crashing(os.replace, {1}))
    with pytest.raises(_Crash):
        _migrate(db_path)
    assert not os.path.exists(db_path)
    assert _names(tmp_path) == ['monitor.db.bak_v1', 'monitor.db.migrating',
                                'monitor.db.swap_pending']
    monkeypatch.undo()
    runner_module.reset_guard_for_tests()

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.created is False
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v1']
    assert _read_bytes(db_path + '.bak_v1') == original
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 30
    finally:
        conn.close()


def _write_marker(db_path, backup, from_version):
    """模拟改名式换库写下的标记（落盘后才改名）"""
    with open(db_path + '.swap_pending', 'w', encoding='utf-8') as f:
        json.dump({'old': db_path, 'new': db_path + '.migrating', 'backup': backup,
                   'from_version': from_version, 'time': '2026-01-01T00:00:00'}, f)


def _orphan_warning(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == runner_module.__name__ and r.levelno == logging.WARNING
            and '不自动放回备份' in r.getMessage()]


def test_only_backup_left_without_marker_starts_fresh(tmp_path, caplog):
    """
    原路径没有库、只有裸 .bak_v1（没有标记）：只会来自用户删库，按全新库启动、不放回备份，
    WARNING 写明备份文件名（有意的行为变更：原先会自动放回重新迁移）
    """
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(12, metrics=('memory_rss',)))
    original = _read_bytes(db_path)
    os.rename(db_path, db_path + '.bak_v1')

    probe = _probe(db_path)
    assert not probe.needs_migration and probe.estimated_rows == 0

    with caplog.at_level(logging.WARNING, logger=runner_module.__name__):
        report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.UP_TO_DATE and report.created is True
    assert _read_bytes(db_path + '.bak_v1') == original
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v1']
    assert _get_user_version(db_path) == SCHEMA_VERSION
    warnings = _orphan_warning(caplog)
    assert len(warnings) == 1 and 'monitor.db.bak_v1' in warnings[0]


def test_bare_backups_without_marker_are_not_restored(tmp_path, caplog):
    """多份裸备份加不完整的临时文件（没有标记）：备份都不动并列进日志，临时文件删掉，按新库启动"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'old': ['memory_rss']}, _v1_rows(3, task_id='old',
                                                             metrics=('memory_rss',)))
    os.rename(db_path, db_path + '.bak_v1')
    _create_v1_db(db_path, {'new': ['memory_rss']}, _v1_rows(5, task_id='new',
                                                             metrics=('memory_rss',)))
    newer = db_path + '.bak_v1_20260101_120000'
    os.rename(db_path, newer)
    with open(db_path + '.migrating', 'wb') as f:
        f.write(b'half written')

    with caplog.at_level(logging.WARNING, logger=runner_module.__name__):
        report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.UP_TO_DATE and report.created is True
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v1',
                                'monitor.db.bak_v1_20260101_120000']
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    finally:
        conn.close()
    warnings = _orphan_warning(caplog)
    assert len(warnings) == 1
    assert 'monitor.db.bak_v1, ' in warnings[0] or 'monitor.db.bak_v1；' in warnings[0]
    assert 'monitor.db.bak_v1_20260101_120000' in warnings[0]


def test_marker_backup_is_restored_even_if_not_newest(tmp_path):
    """有标记时放回标记里记的那份备份（即使另有更新的备份文件）"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'marked': ['memory_rss']}, _v1_rows(3, task_id='marked',
                                                                metrics=('memory_rss',)))
    os.rename(db_path, db_path + '.bak_v1')
    os.utime(db_path + '.bak_v1', (1_000_000, 1_000_000))
    _create_v1_db(db_path, {'other': ['memory_rss']}, _v1_rows(5, task_id='other',
                                                               metrics=('memory_rss',)))
    os.rename(db_path, db_path + '.bak_v1_20260101_120000')
    _write_marker(db_path, db_path + '.bak_v1', 1)

    probe = _probe(db_path)
    assert probe.needs_migration and probe.version == 1 and probe.estimated_rows == 3

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT task_id FROM tasks').fetchall() == [('marked',)]
    finally:
        conn.close()
    assert 'monitor.db.swap_pending' not in _names(tmp_path)


def test_only_complete_migrating_left_is_swapped_in(tmp_path):
    """原路径没有库、只剩完整的 .migrating：换进来按 MIGRATED 报告，不建空库"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(8, metrics=('memory_rss',)))
    rebuild_module.rebuild_into(db_path + '.migrating', db_path, 1)
    os.remove(db_path)

    probe = _probe(db_path)
    assert not probe.needs_migration

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.created is False and report.backup_path is None
    assert _names(tmp_path) == ['monitor.db']
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 8
    finally:
        conn.close()


def test_incomplete_migrating_is_not_swapped_in(tmp_path, monkeypatch):
    """换库中断（有标记）但 .migrating 不完整（user_version 不是 5）：不换入，放回备份重新迁移"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(8, metrics=('memory_rss',)))
    real = rebuild_module.rollup.rebuild_rollups

    def crash(conn, *args, **kwargs):
        real(conn, *args, **kwargs)
        raise _Crash("建汇总时崩溃")

    monkeypatch.setattr(rebuild_module.rollup, 'rebuild_rollups', crash)
    with pytest.raises(_Crash):
        rebuild_module.rebuild_into(db_path + '.migrating', db_path, 1)
    monkeypatch.undo()
    assert _get_user_version(db_path + '.migrating') == 0
    _write_marker(db_path, db_path + '.bak_v1', 1)
    os.rename(db_path, db_path + '.bak_v1')

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert _names(tmp_path) == ['monitor.db', 'monitor.db.bak_v1']
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 8
    finally:
        conn.close()


def test_completion_marker_written_only_after_data_fsync(tmp_path, monkeypatch):
    """
    完成标记的顺序：数据提交后第一次 fsync 时，文件里的 user_version 必须还是 0；
    create_current 建表时就会置 5，若不先改回 0，提交即带上 5，提交后、fsync 前断电
    就可能留下"标记完整、数据未落盘"的 .migrating，下次启动被当作完整新库换入
    """
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(8, metrics=('memory_rss',)))
    new_path = db_path + '.migrating'
    real = rebuild_module._fsync_path
    seen = []

    def recording(path):
        with open(path, 'rb') as f:
            header = f.read(100)
        seen.append(int.from_bytes(header[60:64], 'big'))
        real(path)

    monkeypatch.setattr(rebuild_module, '_fsync_path', recording)
    rebuild_module.rebuild_into(new_path, db_path, 1)

    assert seen[0] == 0
    assert seen[-1] == SCHEMA_VERSION
    assert _get_user_version(new_path) == SCHEMA_VERSION


def test_only_incomplete_migrating_without_backup_starts_fresh(tmp_path):
    """没有任何备份与标记、只有不完整的临时文件：其中没有可恢复的数据，删掉后按全新库处理"""
    db_path = str(tmp_path / 'monitor.db')
    with open(db_path + '.migrating', 'wb') as f:
        f.write(b'half written')

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.UP_TO_DATE and report.created is True
    assert _names(tmp_path) == ['monitor.db']


def test_unrecoverable_interrupted_swap_reports_failed_kept(tmp_path, monkeypatch):
    """备份放不回原路径：FAILED_KEPT（组合根据此退出），原路径不建空库"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    _write_marker(db_path, db_path + '.bak_v0', 0)
    os.rename(db_path, db_path + '.bak_v0')
    _patch_os(monkeypatch, rename=_failing(os.rename, {1}))

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_KEPT
    assert report.broken_path == db_path + '.bak_v0'
    assert not os.path.exists(db_path)


def test_backup_names_with_other_versions_are_not_recovery_traces(tmp_path):
    """.bak_v5 及无关文件不算换库痕迹：照常新建空库"""
    db_path = str(tmp_path / 'monitor.db')
    for name in ('monitor.db.bak_v5', 'monitor.db.broken_20260101_000000', 'other.db.bak_v1'):
        with open(str(tmp_path / name), 'wb') as f:
            f.write(b'x')

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.UP_TO_DATE and report.created is True


@pytest.mark.parametrize('error', [
    sqlite3.OperationalError("database or disk is full"),
    OSError(28, "No space left on device"),
])
def test_transient_rebuild_failure_retries_then_leaves_old_untouched(tmp_path, monkeypatch,
                                                                     error):
    """锁、IO、磁盘满这类瞬时故障重试一次，仍失败则旧库原样、报告带原因"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(10, metrics=('memory_rss',)))
    original = _read_bytes(db_path)
    attempts = []
    real = rebuild_module.copy_points

    def _boom(*args, **kwargs):
        attempts.append(1)
        real(*args, **kwargs)
        raise error

    monkeypatch.setattr(rebuild_module, 'copy_points', _boom)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert len(attempts) == 2
    assert report.reason == runner_module.REASON_IO_ERROR.format(error=error)
    assert _read_bytes(db_path) == original
    assert _names(tmp_path) == ['monitor.db']


@pytest.mark.parametrize('error', [ValueError("坏数据"), RuntimeError("程序错误"),
                                   sqlite3.IntegrityError("约束失败")])
def test_deterministic_rebuild_failure_is_not_retried(tmp_path, monkeypatch, error):
    """程序性异常重试必然再失败：只跑一次，报告带原因文案"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(10, metrics=('memory_rss',)))
    original = _read_bytes(db_path)
    attempts = []

    def _boom(*args, **kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr(rebuild_module, 'copy_points', _boom)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert len(attempts) == 1
    assert report.reason == runner_module.REASON_DATA_ERROR.format(error=error)
    assert _read_bytes(db_path) == original
    assert _names(tmp_path) == ['monitor.db']


def test_null_and_invalid_timestamps_are_skipped_and_counted(tmp_path, caplog, monkeypatch):
    """源库含 NULL 与非法文本的时间戳：迁移成功，这些行跳过并计数（快路径与 Python 路径相同）"""
    import logging
    rows = _v1_rows(6, metrics=('memory_rss',))
    bad = [('t1', None, 100.0, 'memory_rss'), ('t1', 'not-a-time', 101.0, 'memory_rss'),
           ('t1', '2026-13-45T00:00:00', 102.0, 'memory_rss'),
           ('t1', '2026-01-01T09:00:00.12345x', 103.0, 'memory_rss'),
           ('t1', 12.5, 104.0, 'memory_rss')]
    for fast in (True, False):
        directory = tmp_path / ('fast' if fast else 'slow')
        directory.mkdir()
        db_path = str(directory / 'monitor.db')
        _create_v1_db(db_path, {'t1': ['memory_rss']}, rows)
        # v1 的 timestamp 列有 NOT NULL；更早的库或手工改过的库没有，去掉约束后再塞坏数据
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript('''
                ALTER TABLE data_points RENAME TO dp_old;
                CREATE TABLE data_points (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL, timestamp TEXT, value REAL NOT NULL, metric_type TEXT);
                INSERT INTO data_points SELECT * FROM dp_old;
                DROP TABLE dp_old;
            ''')
            conn.executemany('INSERT INTO data_points (task_id, timestamp, value, metric_type) '
                             'VALUES (?, ?, ?, ?)', bad)
            conn.commit()
        finally:
            conn.close()
        if not fast:
            monkeypatch.setattr(rebuild_module, 'time', SimpleNamespace(daylight=1))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            report = _migrate(db_path)
        runner_module.reset_guard_for_tests()

        assert report.outcome is MigrationOutcome.MIGRATED
        assert report.skipped_rows == 5
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == 6
            assert conn.execute('SELECT sample_count FROM tasks').fetchone()[0] == 6
        finally:
            conn.close()
        assert any('跳过时间戳为 NULL 或无法解析的数据点 5 行' in r.getMessage()
                   for r in caplog.records)


def test_stale_migrating_file_is_replaced(tmp_path):
    """上次迁移中途断电留下的 .migrating 被删除重建，不影响结果"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    with open(db_path + '.migrating', 'wb') as f:
        f.write(b'half written')

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert 'monitor.db.migrating' not in _names(tmp_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION


def test_database_maps_failed_rebuild_to_migration_failed(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    _patch_os(monkeypatch, replace=_failing(os.replace, {1}))
    db = Database(db_path)
    try:
        assert (db.migration_failed, db.data_reset, db.backup_aborted) == (True, False, False)
    finally:
        db.close()


def test_sidecar_check_failure_keeps_old_in_place(tmp_path, monkeypatch):
    """旧库残留日志、不能换库：删掉新文件，旧库原样留在原路径"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    monkeypatch.setattr(MigrationRunner, '_clear_source_sidecars', lambda self: False)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert _read_bytes(db_path) == original
    assert _names(tmp_path) == ['monitor.db']


def test_failed_swap_puts_old_back_before_cleaning_new_file(tmp_path, monkeypatch):
    """新文件换不进来时先把旧库改回原名，再删新文件：删新文件失败也不影响旧库归位"""
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    original = _read_bytes(db_path)
    _patch_os(monkeypatch, link=_no_link, replace=_failing(os.replace, {1}))
    order = []
    real_rename = runner_module.os.rename
    real_remove = runner_module._remove_with_sidecars

    def rename(src, dst):
        order.append(('rename', os.path.basename(src), os.path.basename(dst)))
        return real_rename(src, dst)

    def remove(path):
        order.append(('remove', os.path.basename(path)))
        if not any(item[0] == 'rename' for item in order):
            return real_remove(path)   # 重建前清理残留临时文件：照常
        return False   # 换库失败后：模拟新文件删不掉

    runner_module.os.rename = rename
    monkeypatch.setattr(runner_module, '_remove_with_sidecars', remove)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.FAILED_RESTORED
    assert _read_bytes(db_path) == original
    put_back = order.index(('rename', 'monitor.db.bak_v0', 'monitor.db'))
    assert order[put_back + 1:] == [('remove', 'monitor.db.migrating')]
    real_remove(db_path + '.migrating')


# ========== 磁盘预检 ==========

class _Usage:
    def __init__(self, free):
        self.free = free


def _migrate_with_disk(db_path, disk_usage):
    factory = ConnectionFactory(db_path, keep_alive=False)
    try:
        return MigrationRunner(factory, disk_usage=disk_usage).migrate()
    finally:
        factory.close()


def test_disk_precheck_aborts_without_touching_old_db(tmp_path, caplog):
    """可用空间 < 旧库 × 0.3 + 200 MB：BACKUP_ABORTED，旧库逐字节不变，没有 .migrating"""
    import logging
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(10, metrics=('memory_rss',)))
    original = _read_bytes(db_path)
    size = os.path.getsize(db_path)
    required = int(size * runner_module.DISK_HEADROOM_RATIO) + runner_module.DISK_HEADROOM_BYTES
    asked = []

    def disk_usage(path):
        asked.append(path)
        return _Usage(required - 1)

    with caplog.at_level(logging.ERROR, logger=runner_module.__name__):
        report = _migrate_with_disk(db_path, disk_usage)

    assert report.outcome is MigrationOutcome.BACKUP_ABORTED
    assert (report.required_bytes, report.free_bytes) == (required, required - 1)
    assert report.disk_shortage and report.backup_path is None
    assert (report.from_version, report.to_version) == (1, 1)
    assert asked == [str(tmp_path)]
    assert _read_bytes(db_path) == original
    assert _names(tmp_path) == ['monitor.db']
    assert _get_user_version(db_path) == 1
    assert any("需要约" in r.getMessage() and "数据库未改动" in r.getMessage()
               for r in caplog.records)
    db = Database(db_path, migrate=False, migration_report=report)
    assert (db.migration_failed, db.data_reset, db.backup_aborted) == (False, False, True)


def test_disk_precheck_passes_when_free_equals_required(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(10, metrics=('memory_rss',)))
    size = os.path.getsize(db_path)
    with open(db_path + '-wal', 'wb'):
        pass   # 空 -wal 不计入大小，也不妨碍换库
    required = runner_module.required_free_bytes(size)

    report = _migrate_with_disk(db_path, lambda path: _Usage(required))

    assert report.outcome is MigrationOutcome.MIGRATED
    assert report.required_bytes is None and report.free_bytes is None


def test_disk_precheck_size_includes_wal(tmp_path):
    """-wal 里未合并的提交 checkpoint 后会并入主文件，预检按主文件 + -wal 计算"""
    db_path = str(tmp_path / 'monitor.db')
    with open(db_path, 'wb') as f:
        f.write(b'x' * 1000)
    with open(db_path + '-wal', 'wb') as f:
        f.write(b'y' * 234)
    runner = MigrationRunner(ConnectionFactory(db_path, keep_alive=False))
    assert runner._source_bytes() == 1234


def test_disk_usage_failure_does_not_block_migration(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)

    def broken(path):
        raise OSError("模拟查询失败")

    assert _migrate_with_disk(db_path, broken).outcome is MigrationOutcome.MIGRATED


def test_disk_precheck_skipped_for_fresh_and_current_db(tmp_path):
    """全新库与已是 v5 的库不迁移，也不查询磁盘"""
    db_path = str(tmp_path / 'monitor.db')

    def never(path):
        pytest.fail("不迁移时不应查询磁盘空间")

    assert _migrate_with_disk(db_path, never).created
    runner_module.reset_guard_for_tests()
    assert _migrate_with_disk(db_path, never).outcome is MigrationOutcome.UP_TO_DATE


@pytest.mark.parametrize('required, free, text', [
    (300 * 1024 * 1024 + 1, 50 * 1024 * 1024 + 5, "需要约 301 MB 可用空间（当前 50 MB），数据库未改动"),
    (200 * 1024 * 1024, 0, "需要约 200 MB 可用空间（当前 0 MB），数据库未改动"),
])
def test_disk_shortage_message(required, free, text):
    assert runner_module.disk_shortage_message(required, free) == text


def test_required_free_bytes_formula():
    assert runner_module.required_free_bytes(0) == 200 * 1024 * 1024
    assert runner_module.required_free_bytes(1000 * 1024 * 1024) == \
        300 * 1024 * 1024 + 200 * 1024 * 1024


# ========== 探测 ==========

def _probe(db_path):
    factory = ConnectionFactory(db_path, keep_alive=False)
    return MigrationRunner(factory).probe()


def test_probe_legacy_db_reports_rows_size_and_backup_name(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss', 'memory_private']}, _v1_rows(30))
    original = _read_bytes(db_path)

    probe = _probe(db_path)

    assert (probe.has_tasks, probe.version, probe.target_version) == (True, 1, SCHEMA_VERSION)
    assert probe.needs_migration
    assert probe.estimated_rows == 30
    assert probe.file_bytes == os.path.getsize(db_path)
    assert _read_bytes(db_path) == original and _names(tmp_path) == ['monitor.db']
    runner = MigrationRunner(ConnectionFactory(db_path, keep_alive=False))
    assert runner.backup_path_for(1) == db_path + '.bak_v1'


def test_probe_fresh_path_creates_nothing(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    probe = _probe(db_path)
    assert (probe.has_tasks, probe.needs_migration, probe.estimated_rows) == (False, False, 0)
    assert _names(tmp_path) == []


def test_probe_current_db_needs_no_migration(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    Database(db_path).close()
    probe = _probe(db_path)
    assert probe.has_tasks and probe.version == SCHEMA_VERSION
    assert not probe.needs_migration and probe.estimated_rows == 0


# ========== 进度与磁盘峰值 ==========

def test_progress_callbacks_and_disk_peak(tmp_path):
    """
    每 CHUNK_ROWS 行回调一次、最后一次 done == total；回调时目录里只有旧库与 .migrating
    （旧库以 immutable 只读挂载，不产生 -wal/-shm；新库关闭日志，不产生 -journal）。
    """
    db_path = str(tmp_path / 'monitor.db')
    rows = 2 * CHUNK_ROWS + 1
    _create_v1_db(db_path, {'t1': ['memory_rss', 'memory_private']}, _v1_rows(rows))
    calls = []

    def _progress(stage, done, total):
        calls.append((stage, done, total, _names(tmp_path)))

    report = _migrate(db_path, on_progress=_progress)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert [(s, d, t) for s, d, t, _n in calls] == [
        (STAGE_SAMPLES, CHUNK_ROWS, rows),
        (STAGE_SAMPLES, 2 * CHUNK_ROWS, rows),
        (STAGE_SAMPLES, rows, rows),
        (STAGE_ROLLUPS, 1, 2),
        (STAGE_ROLLUPS, 2, 2),
    ]
    for *_rest, names in calls:
        assert names == ['monitor.db', 'monitor.db.migrating']
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM samples').fetchone()[0] == rows
    finally:
        conn.close()


def test_empty_source_reports_no_progress(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, [])
    calls = []
    report = _migrate(db_path, on_progress=lambda *a: calls.append(a))
    assert report.outcome is MigrationOutcome.MIGRATED
    assert calls == []


def test_chinese_path(tmp_path):
    directory = tmp_path / '中文 目录#%20'
    directory.mkdir()
    db_path = str(directory / '监控 数据.db')
    _create_v1_db(db_path, {'t1': ['memory_rss']}, _v1_rows(10, metrics=('memory_rss',)))
    original = _read_bytes(db_path)

    report = _migrate(db_path)

    assert report.outcome is MigrationOutcome.MIGRATED
    assert _read_bytes(report.backup_path) == original
    db = Database(db_path)
    try:
        assert len(db.get_task_data_points('t1', 'memory_rss')) == 10
    finally:
        db.close()


# ========== 迁移规则 ==========

def test_v1_merge_null_fill_orphans_and_same_ms(tmp_path):
    """
    v1 源库：memory_vms / memory_pagefile 并入 memory_private（同一时刻保留 id 最小的一行）、
    NULL 指标回填为任务首指标、孤儿行丢弃、同一毫秒的两行按 id 后写者胜。
    """
    db_path = str(tmp_path / 'monitor.db')
    rows = [
        ('t1', '2026-01-01T00:00:01', 1.0, 'memory_vms'),        # id 1：三者同刻，留它
        ('t1', '2026-01-01T00:00:01', 2.0, 'memory_private'),
        ('t1', '2026-01-01T00:00:01', 3.0, 'memory_pagefile'),
        ('t1', '2026-01-01T00:00:02', 4.0, 'memory_pagefile'),   # 单独一行：改名保留
        ('t1', '2026-01-01T00:00:03', 5.0, None),                # NULL → 首指标 memory_rss
        ('ghost', '2026-01-01T00:00:03', 6.0, 'memory_rss'),     # 孤儿
        ('t1', '2026-01-01T00:00:04.000400', 7.0, 'memory_rss'),  # 与下一行同一毫秒
        ('t1', '2026-01-01T00:00:04.000100', 8.0, 'memory_rss'),  # id 更大：后写者胜
    ]
    _create_v1_db(db_path, {'t1': ['memory_rss', 'memory_vms', 'memory_pagefile']}, rows)

    assert _migrate(db_path).outcome is MigrationOutcome.MIGRATED

    db = Database(db_path)
    try:
        task = db.get_task('t1')
        assert task.metric_types == ['memory_rss', 'memory_private']
        assert task.sample_count == 4
        private = [(p.timestamp.second, p.value)
                   for p in db.get_task_data_points('t1', 'memory_private')]
        assert private == [(1, 1.0), (2, 4.0)]
        rss = [(p.timestamp, p.value) for p in db.get_task_data_points('t1', 'memory_rss')]
        assert rss == [(datetime(2026, 1, 1, 0, 0, 3), 5.0),
                       (datetime(2026, 1, 1, 0, 0, 4), 8.0)]
        assert db.get_task('ghost') is None
    finally:
        db.close()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM series WHERE task_id = 'ghost'").fetchone() \
            == (0,)
        # series 按任务指标列表顺序建立
        assert conn.execute(
            "SELECT metric FROM series WHERE task_id = 't1' ORDER BY series_id").fetchall() == \
            [('memory_rss',), ('memory_private',)]
    finally:
        conn.close()


def test_v0_metric_comes_from_task(tmp_path):
    db_path = str(tmp_path / 'monitor.db')
    _create_v0_db(db_path)
    assert _migrate(db_path).outcome is MigrationOutcome.MIGRATED
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute('SELECT task_id, metric FROM series').fetchall() == \
            [('task-1', 'memory_rss')]
        assert conn.execute('SELECT metric_type FROM tasks').fetchone() == ('["memory_rss"]',)
    finally:
        conn.close()


# ========== 时间换算：快路径 / Python 路径 / to_ms ==========

_EDGE_TEXTS = [
    '2026-01-01T00:00:00', '2026-01-01T00:00:00.000000', '2026-01-01T00:00:00.000499',
    '2026-01-01T00:00:00.000500', '2026-01-01T00:00:00.999499', '2026-01-01T00:00:00.999500',
    '2026-01-01T23:59:59.999999', '2024-02-29T12:34:56.123456', '1999-12-31T23:59:59.999500',
    '2038-01-19T03:14:08.000001', '2026-06-15 08:00:00',         # 空格分隔（长度 19）
    '2026-06-15T08:00:00.5',                                     # 非 6 位小数 → Python
    '2026-06-15T08:00:00+08:00',                                 # 带时区 → Python
    '2026-06-15T08:00:00.123456+00:00',
]


def _expected_ms(text):
    return to_ms(datetime.fromisoformat(text))


def _converted(texts, fast_enabled, monkeypatch):
    if not fast_enabled:
        monkeypatch.setattr(rebuild_module, 'time', SimpleNamespace(daylight=1))
    conn = sqlite3.connect(':memory:')
    try:
        conv = MsConverter(conn, ['2026-01-01T00:00:00'])
        assert conv.fast is fast_enabled
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)')
        conn.executemany('INSERT INTO t (ts) VALUES (?)', [(t,) for t in texts])
        return [row[0] for row in conn.execute(f"SELECT {conv.expr('ts')} FROM t ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.skipif(time.daylight != 0, reason='本机时区有夏令时，快路径不启用')
def test_fast_sql_path_matches_to_ms(monkeypatch):
    texts = list(_EDGE_TEXTS)
    base = datetime(2025, 1, 1)
    texts += [(base + timedelta(seconds=7919 * i, microseconds=(i * 7777) % 10 ** 6)).isoformat()
              for i in range(5000)]
    assert _converted(texts, True, monkeypatch) == [_expected_ms(t) for t in texts]


def test_python_path_matches_to_ms(monkeypatch):
    texts = list(_EDGE_TEXTS)
    assert _converted(texts, False, monkeypatch) == [_expected_ms(t) for t in texts]


@pytest.mark.parametrize('sample', [pytest.param(s, id=_ids(s)) for s in SAMPLES])
def test_golden_timestamps_match_to_ms_on_both_paths(sample, tmp_path, monkeypatch):
    """golden 样本的每一行：两条路径重建出的 samples 完全相同，且与逐行 to_ms 一致"""
    source = _unpack(sample, tmp_path / 'src')
    conn = sqlite3.connect(source)
    try:
        texts = [row[0] for row in conn.execute('SELECT timestamp FROM data_points ORDER BY id')]
    finally:
        conn.close()
    if time.daylight == 0:
        assert _converted(texts, True, monkeypatch) == [_expected_ms(t) for t in texts]

    fast_db = _unpack(sample, tmp_path / 'fast')
    assert _migrate(fast_db).outcome is MigrationOutcome.MIGRATED
    slow_db = _unpack(sample, tmp_path / 'slow')
    monkeypatch.setattr(rebuild_module, 'time', SimpleNamespace(daylight=1))
    assert _migrate(slow_db).outcome is MigrationOutcome.MIGRATED
    assert _dump(fast_db) == _dump(slow_db)


def test_fast_path_probe_samples_every_year(monkeypatch):
    """
    快路径核对按自然年抽样：首末两行都在偏移 +8h 的年份，中间一年偏移不同（模拟该年时区规则
    变了）时，只看首末两行会误开快路径；逐年抽样能发现。用假的 to_ms 与 daylight=0 隔离本机时区
    """
    epoch = datetime(1970, 1, 1)

    def fake_to_ms(dt):
        shift = 9 if dt.year == 2025 else 8
        return int((dt - epoch).total_seconds() * 1000) - shift * 3600 * 1000

    monkeypatch.setattr(rebuild_module, 'time', SimpleNamespace(daylight=0))
    monkeypatch.setattr(rebuild_module, 'to_ms', fake_to_ms)
    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE data_points (id INTEGER PRIMARY KEY, timestamp TEXT)')
        texts = [f'{year}-{month:02d}-15T08:00:00' for year in (2024, 2025, 2026)
                 for month in (1, 4, 7, 10)]
        conn.executemany('INSERT INTO data_points (timestamp) VALUES (?)', [(t,) for t in texts])

        probes = rebuild_module.probe_texts(conn, 'data_points')

        assert probes[:2] == [texts[0], texts[-1]]
        assert '2025-01-15T08:00:00' in probes
        assert MsConverter(conn, probes).fast is False
        assert MsConverter(conn, probes[:2]).fast is True     # 对照：只看首末会误开快路径
    finally:
        conn.close()


_INNER_RUN = 'PM_REBUILD_FIXED_TZ_RUN'


@pytest.mark.skipif(time.daylight == 0 or bool(os.environ.get(_INNER_RUN)),
                    reason='本机时区无夏令时，快路径用例已在本进程内执行')
def test_fast_path_cases_in_fixed_offset_zone():
    """
    本机时区有夏令时（如环境变量 TZ=America/New_York）时快路径不启用，上面两个快路径断言会被
    跳过；这里在 TZ=CST-8（UTC+8、无夏令时）的子进程里重跑它们，保证快路径始终被验证。
    """
    import subprocess
    import sys
    env = dict(os.environ, TZ='CST-8', **{_INNER_RUN: '1'})
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-rs',
         os.path.abspath(__file__), '-k', 'fast_sql_path or golden_timestamps'],
        cwd=root, env=env, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert 'skipped' not in result.stdout, result.stdout[-3000:]


# ========== 100 万行（perf，默认不跑） ==========

@pytest.mark.perf
def test_rebuild_v1_one_million_rows(tmp_path, capsys):
    from tests.perf import synth
    db_path = str(tmp_path / 'monitor.db')
    info = synth.build_v1_db(db_path, synth.LegacySpec())
    calls = []
    started = time.perf_counter()
    report = _migrate(db_path, on_progress=lambda *a: calls.append(a))
    elapsed = time.perf_counter() - started
    assert report.outcome is MigrationOutcome.MIGRATED
    samples_calls = [c for c in calls if c[0] == STAGE_SAMPLES]
    total = samples_calls[-1][2]
    assert len(samples_calls) == -(-total // CHUNK_ROWS)
    size = os.path.getsize(db_path)
    with capsys.disabled():
        print(f"\n[重建式迁移] v1 {info} → v5：{elapsed:.2f} 秒，回调 {len(calls)} 次，"
              f"新库 {size / 1e6:.1f} MB")
