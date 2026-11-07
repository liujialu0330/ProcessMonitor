"""
数据库表结构（当前版本）与版本号读写

版本演进：
- v1：多指标支持，tasks.metric_type 存 JSON 数组，data_points 新增 metric_type 列
- v2：合并重复内存指标（memory_vms / memory_pagefile 并入 memory_private，表结构不变）
- v3：tasks 增加 note / sample_count / last_sample_at；新增 task_markers 表；
      清理孤儿数据点并回填 NULL 的 metric_type；索引收敛为 (task_id, metric_type, timestamp)
- v4：tasks 增加 create_time（被监控进程的创建时间，epoch 秒，可空）；
      task_markers 增加 kind（user / baseline / pause / resume，默认 user）
- v5：样本改存紧凑表 series + samples（UTC 毫秒整数时间戳，WITHOUT ROWID）；
      新增 meta、rollup_1m、series_gaps、task_members、handle_snapshots；tasks 增加 2.x 预留列；
      data_points 保留 v4 原样定义但为空表（见下）。v5 为 2.x 的最终结构，2.x 期间不再迁移，
      后续阶段只往预留的表与列里写数据。

旧库升到 v5 用重建式迁移（data.db.migrations.rebuild）：在新文件里用 create_current 建好结构
再把数据搬进来，因此不再有"迁移链得到的结构必须与新建库一致"的列顺序约束；结构一致性
仍由 golden 测试比对新建库与迁移结果守护。
"""
import sqlite3
from typing import Optional, Union

from data.db import meta as meta_store
from data.models import MARKER_KINDS  # noqa: F401  兼容旧的 from data.db.schema import MARKER_KINDS

SCHEMA_VERSION = 5

_Executor = Union[sqlite3.Connection, sqlite3.Cursor]


def create_current(cursor: sqlite3.Cursor, app_version: Optional[str] = None) -> None:
    """创建当前版本的表结构与索引，写入 meta 四键，并置 user_version=SCHEMA_VERSION。

    结构与版本号必须一起写入：只建表不写版本号的库下次启动会被当成 v0 重新走迁移。
    调用方负责事务（迁移运行器在 BEGIN IMMEDIATE 事务内调用）。

    Args:
        app_version: 写进 meta 的 created_by / last_writer 版本；None 取 config.APP_VERSION
    """
    # tasks.metric_type 存 JSON 数组文本，如 ["memory_rss","cpu_percent"]。
    # 前 12 列与 v4 相同；其后为 2.x 预留列，一律可空或带默认值：1.4.1 回退使用时按 8 个旧列
    # INSERT OR REPLACE 仍能写入；TaskRepo.save 的 UPSERT 只更新模型携带的列，不会清掉它们
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
            create_time REAL,
            target_kind TEXT NOT NULL DEFAULT 'pid',
            target_spec TEXT,
            group_id TEXT,
            member_key TEXT,
            exit_code INTEGER,
            resume_on_restart INTEGER NOT NULL DEFAULT 0,
            restart_limit INTEGER NOT NULL DEFAULT 0,
            plan_json TEXT,
            meta_json TEXT,
            scenario TEXT NOT NULL DEFAULT '',
            build TEXT NOT NULL DEFAULT '',
            is_reference INTEGER NOT NULL DEFAULT 0,
            source_task_id TEXT,
            origin TEXT,
            planned_cycles INTEGER,
            done_cycles INTEGER,
            missed_cycles INTEGER,
            lag_p95_ms INTEGER,
            collect_p95_ms INTEGER
        )
    ''')

    # 一个 (任务, 指标) 一条序列；series_id 是 samples 等紧凑表里的短键
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS series (
            series_id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            UNIQUE(task_id, metric)
        )
    ''')

    # 样本：ts_ms 为 UTC 毫秒整数。主键即聚簇顺序，同一序列同一毫秒只有一行（后写覆盖）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS samples (
            series_id INTEGER NOT NULL,
            ts_ms INTEGER NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY(series_id, ts_ms)
        ) WITHOUT ROWID
    ''')

    # 分钟汇总（规则与维护见 data.repos.rollup）：bucket_ms 为桶起点（UTC 毫秒）=
    # max(所在自然分钟起点, 所在段的段起点)，一个分钟内出现空洞时拆成两行，任何行都不跨空洞；
    # first/last 为桶内首末样本，min_ms/max_ms 为最小/最大值最早出现的时间（图表取真实点用）。
    # 拟合用的各阶和以 x = (ts_ms - bucket_ms) / 1000.0（相对桶起点的秒数）计算，
    # 合并多个桶时由使用方平移到统一原点。与 samples 同一事务维护
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rollup_1m (
            series_id INTEGER NOT NULL,
            bucket_ms INTEGER NOT NULL,
            count INTEGER NOT NULL,
            first_ms INTEGER NOT NULL,
            first_value REAL NOT NULL,
            last_ms INTEGER NOT NULL,
            last_value REAL NOT NULL,
            min_value REAL NOT NULL,
            min_ms INTEGER NOT NULL,
            max_value REAL NOT NULL,
            max_ms INTEGER NOT NULL,
            sum_y REAL NOT NULL,
            sum_yy REAL NOT NULL,
            sum_x REAL NOT NULL,
            sum_xx REAL NOT NULL,
            sum_xy REAL NOT NULL,
            PRIMARY KEY(series_id, bucket_ms)
        ) WITHOUT ROWID
    ''')

    # 写入时记录的空洞：[start_ms, end_ms] 为空洞前后两个样本的时间（相邻间隔 >
    # tasks.interval × 2.5 秒，与分析查询的默认空洞口径相同）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS series_gaps (
            series_id INTEGER NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            PRIMARY KEY(series_id, start_ms)
        ) WITHOUT ROWID
    ''')

    # 进程组任务的成员留痕：(pid, create_time) 唯一识别一个进程实例
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_members (
            task_id TEXT NOT NULL,
            pid INTEGER NOT NULL,
            create_time REAL NOT NULL,
            name TEXT NOT NULL,
            first_ms INTEGER NOT NULL,
            last_ms INTEGER NOT NULL,
            PRIMARY KEY(task_id, pid, create_time)
        ) WITHOUT ROWID
    ''')

    # 句柄按类型的计数快照
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS handle_snapshots (
            task_id TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            type_name TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY(task_id, ts_ms, type_name)
        ) WITHOUT ROWID
    ''')

    # v4 原样定义，v5 中恒为空表：用户回退到 1.4.1 时它只认识这张表，降级期间的样本写在
    # 这里；再用 2.x 启动时由启动校正导回 samples 并清空（Maintenance.import_legacy_points）。
    # metric_type 列保持可空（与 v4 一致）
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

    # data_points 保留 v4 的索引：1.4.1 降级期间的查询与写入照旧走它
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_data_points_task_metric_ts
        ON data_points(task_id, metric_type, timestamp)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_task_markers_task_ts
        ON task_markers(task_id, timestamp)
    ''')

    meta_store.create_table(cursor)
    meta_store.write_initial(cursor, SCHEMA_VERSION, app_version)
    write_version(cursor, SCHEMA_VERSION)


def read_version(conn: _Executor) -> int:
    """读取库的 user_version"""
    return int(conn.execute('PRAGMA user_version').fetchone()[0])


def write_version(cursor: _Executor, version: int) -> None:
    """写入 user_version（PRAGMA 不能参数化，先强制转 int 再拼接）"""
    cursor.execute(f'PRAGMA user_version = {int(version)}')


def table_exists(conn: _Executor, name: str) -> bool:
    """库中是否存在名为 name 的表（只查主库，不含 ATTACH 的库）"""
    row = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None
