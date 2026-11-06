"""
v3 -> v4 迁移（版本号由迁移运行器统一写入，本函数不写 user_version）

1. tasks 增加 create_time REAL（被监控进程的创建时间，epoch 秒；旧任务未知，保持 NULL）
2. task_markers 增加 kind TEXT NOT NULL DEFAULT 'user'（旧打点都是用户打点）

均先查列是否存在，保证幂等。列顺序与 schema.create_current 一致。
"""
import sqlite3

VERSION = 4
NAME = '进程创建时间与打点类型'


def apply(cursor: sqlite3.Cursor) -> None:
    cursor.execute('PRAGMA table_info(tasks)')
    task_columns = {row[1] for row in cursor.fetchall()}
    if 'create_time' not in task_columns:
        cursor.execute('ALTER TABLE tasks ADD COLUMN create_time REAL')

    cursor.execute('PRAGMA table_info(task_markers)')
    marker_columns = {row[1] for row in cursor.fetchall()}
    if 'kind' not in marker_columns:
        cursor.execute(
            "ALTER TABLE task_markers ADD COLUMN kind TEXT NOT NULL DEFAULT 'user'")
