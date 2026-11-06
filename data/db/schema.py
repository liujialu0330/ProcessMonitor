"""
数据库表结构（当前版本）与版本号读写

版本演进：
- v1：多指标支持，tasks.metric_type 存 JSON 数组，data_points 新增 metric_type 列
- v2：合并重复内存指标（memory_vms / memory_pagefile 并入 memory_private，表结构不变）
- v3：tasks 增加 note / sample_count / last_sample_at；新增 task_markers 表；
      清理孤儿数据点并回填 NULL 的 metric_type；索引收敛为 (task_id, metric_type, timestamp)
- v4：tasks 增加 create_time（被监控进程的创建时间，epoch 秒，可空）；
      task_markers 增加 kind（user / baseline / pause / resume，默认 user）

新库直接用 create_current 建成当前结构；迁移链得到的库必须与之结构一致
（列顺序与各迁移的 ALTER TABLE ADD COLUMN 顺序一致），由测试守护。
"""
import sqlite3
from typing import Union

SCHEMA_VERSION = 4

# 打点类型的合法取值（列本身不加 CHECK 约束，由写入方校验）
MARKER_KINDS = ('user', 'baseline', 'pause', 'resume')

_Executor = Union[sqlite3.Connection, sqlite3.Cursor]


def create_current(cursor: sqlite3.Cursor) -> None:
    """创建当前版本的表结构与索引，并置 user_version=SCHEMA_VERSION。

    结构与版本号必须一起写入：只建表不写版本号的库下次启动会被当成 v0 重新走迁移链。
    调用方负责事务（迁移运行器在 BEGIN IMMEDIATE 事务内调用）。
    """
    # tasks.metric_type 存 JSON 数组文本，如 ["memory_rss","cpu_percent"]
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            process_name TEXT NOT NULL,
            metric_type TEXT NOT NULL,
            interval REAL NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            sample_count INTEGER NOT NULL DEFAULT 0,
            last_sample_at TEXT,
            create_time REAL
        )
    ''')

    # metric_type 列保持可空：SQLite 无法原地改列约束，迁移库与新库须结构一致
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS data_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            value REAL NOT NULL,
            metric_type TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_markers (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'user'
        )
    ''')

    # data_points 唯一的索引：覆盖按任务/指标/时间范围的过滤、排序与首尾点查询
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_data_points_task_metric_ts
        ON data_points(task_id, metric_type, timestamp)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_task_markers_task_ts
        ON task_markers(task_id, timestamp)
    ''')

    write_version(cursor, SCHEMA_VERSION)


def read_version(conn: _Executor) -> int:
    """读取库的 user_version"""
    return int(conn.execute('PRAGMA user_version').fetchone()[0])


def write_version(cursor: _Executor, version: int) -> None:
    """写入 user_version（PRAGMA 不能参数化，先强制转 int 再拼接）"""
    cursor.execute(f'PRAGMA user_version = {int(version)}')


def table_exists(conn: _Executor, name: str) -> bool:
    """库中是否存在名为 name 的表"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None
