"""
v0 -> v1 数据库迁移用例
用裸 sqlite3 构造旧库（tasks.metric_type 单值文本、data_points 无 metric_type 列、
user_version=0），再实例化 Database 触发迁移，断言回填/幂等/备份等行为
"""
import glob
import json
import os
import shutil
import sqlite3

import data.database as database_module
from data.database import Database, SCHEMA_VERSION


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


def test_migration_v0_to_v1(db_path):
    """迁移完成后：user_version=1、data_points 回填、tasks.metric_type 变 JSON 数组、生成备份"""
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


def test_migration_idempotent_on_second_instantiation(db_path):
    """二次实例化不重复迁移：数据不受影响，version 保持不变"""
    _create_v0_db(db_path)

    db1 = Database(db_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION

    backup_path = db_path + '.bak_v0'
    backup_mtime_after_first = os.path.getmtime(backup_path)

    # 第二次实例化：is_new_db 判定为 False（tasks 表已存在），
    # _migrate_if_needed 因 version >= SCHEMA_VERSION 直接返回，不重复迁移
    db2 = Database(db_path)
    assert _get_user_version(db_path) == SCHEMA_VERSION

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


# ========== 批2新增：迁移失败三态 + 进程级 guard ==========

def test_migration_failed_restores_backup_and_sets_flag(db_path, monkeypatch):
    """迁移两次尝试均失败但还原备份成功：置 migration_failed，旧数据（未迁移）保留"""
    _create_v0_db(db_path)

    def _boom(cursor):
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(Database, '_migrate_v0_to_v1', staticmethod(_boom))

    db = Database(db_path)

    assert db.migration_failed is True
    assert db.backup_aborted is False
    assert db.data_reset is False

    # 还原后仍是 v0（未迁移），原始数据完整
    assert _get_user_version(db_path) == 0
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT metric_type FROM tasks WHERE task_id = 'task-1'")
        raw_value = cursor.fetchone()[0]
    finally:
        conn.close()
    assert raw_value == 'memory_rss'  # 未被转换为 JSON 数组，说明确实回滚了

    # 备份文件存在（迁移前已生成）
    assert os.path.exists(db_path + '.bak_v0')


def test_backup_failure_aborts_migration_and_leaves_db_untouched(db_path, monkeypatch):
    """迁移前备份失败：置 backup_aborted，旧库原样保留，不生成备份文件、不做任何写入"""
    _create_v0_db(db_path)

    def _boom_copy2(*args, **kwargs):
        raise OSError("模拟磁盘已满")

    monkeypatch.setattr(database_module.shutil, 'copy2', _boom_copy2)

    db = Database(db_path)

    assert db.backup_aborted is True
    assert db.migration_failed is False
    assert db.data_reset is False

    # 旧库未被迁移、未生成备份文件
    assert _get_user_version(db_path) == 0
    assert not os.path.exists(db_path + '.bak_v0')

    # 原始数据依然完整可读（v0 结构）
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT metric_type FROM tasks WHERE task_id = 'task-1'")
        raw_value = cursor.fetchone()[0]
    finally:
        conn.close()
    assert raw_value == 'memory_rss'


def test_migration_guard_prevents_repeated_backup_after_abort(db_path, monkeypatch):
    """backup_aborted 后再次实例化 Database（同进程）：不重复尝试备份，直接复用既有状态"""
    _create_v0_db(db_path)

    call_count = {'n': 0}

    def _boom_copy2(*args, **kwargs):
        call_count['n'] += 1
        raise OSError("模拟磁盘已满")

    monkeypatch.setattr(database_module.shutil, 'copy2', _boom_copy2)

    db1 = Database(db_path)
    assert db1.backup_aborted is True
    assert call_count['n'] == 1

    # 第二次实例化：guard 生效，不应再调用 shutil.copy2
    db2 = Database(db_path)
    assert db2.backup_aborted is True
    assert call_count['n'] == 1

    # 第三次同样如此
    db3 = Database(db_path)
    assert db3.backup_aborted is True
    assert call_count['n'] == 1


def test_restore_from_backup_also_fails_resets_to_empty_db(db_path, monkeypatch):
    """
    迁移失败，且从备份还原也失败：损坏库改名保留（.broken_*），db_path 处新建空 v1 库，
    置 data_reset。用 shutil.copy2 的调用序号区分：第一次（迁移前备份 db_path->backup_path）
    放行，之后（还原 backup_path->db_path）全部失败，从而落到 _restore_from_backup 内层
    "还原也失败"分支。
    """
    _create_v0_db(db_path)

    def _boom_migrate(cursor):
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(Database, '_migrate_v0_to_v1', staticmethod(_boom_migrate))

    original_copy2 = shutil.copy2
    call_count = {'n': 0}

    def _copy2_side_effect(src, dst, *args, **kwargs):
        call_count['n'] += 1
        if call_count['n'] == 1:
            # 迁移前备份：正常放行，保证备份文件里留有原始数据
            return original_copy2(src, dst, *args, **kwargs)
        # 之后都是"从备份还原"的调用：模拟磁盘满/文件被占用等还原失败
        raise OSError("模拟还原失败")

    monkeypatch.setattr(database_module.shutil, 'copy2', _copy2_side_effect)

    db = Database(db_path)

    assert db.data_reset is True
    assert db.migration_failed is False
    assert db.backup_aborted is False

    # db_path 处最终应是全新的空 v1 库
    assert _get_user_version(db_path) == SCHEMA_VERSION
    assert db.get_all_tasks() == []

    # 原始数据的权威留存位置是迁移前只写一次、之后只读不写的 .bak_v0 备份文件——
    # 这是数据"没有真正丢失"的可靠断言依据。
    # 注：_migrate_v0_to_v1 的 for attempt in (1, 2) 循环里，每次失败都会触发一次
    # "还原失败->改名保留->新建空库"，磁盘上可能出现多个 .broken_* 文件；但改名用的
    # 时间戳只精确到秒，同一秒内的第二次 os.replace 可能与第一次同名而相互覆盖——
    # 这是 _restore_from_backup 里预先存在、超出本轮 M1/M3 范围的边界情况（发现后未
    # 修复，已在验收报告中注明留给后续处理），因此这里不依赖 .broken_* 的具体份数与
    # 内容做强断言，只确认至少留下了改名痕迹。
    backup_path = db_path + '.bak_v0'
    assert os.path.exists(backup_path), "迁移前的 .bak_v0 备份应始终存在，不受还原失败影响"
    conn = sqlite3.connect(backup_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT metric_type FROM tasks WHERE task_id = 'task-1'")
        row = cursor.fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == 'memory_rss', (
        ".bak_v0 备份应完整保留原始任务数据（证明数据从未真正丢失）")

    broken_files = glob.glob(db_path + '.broken_*')
    assert len(broken_files) >= 1, "应至少留下一个 .broken_* 改名痕迹"
