"""
v2 -> v3 迁移（版本号由迁移运行器统一写入，本函数不写 user_version）

1. tasks 增加 note / sample_count / last_sample_at（幂等：先查列是否存在）
2. 新建 task_markers 表与 (task_id, timestamp) 索引（v3 时的结构，v4 再加 kind 列）
3. 删除孤儿数据点（task_id 不在 tasks 中）
4. 仍为 NULL 的 data_points.metric_type 回填为所属任务的首指标
5. 索引收敛为 (task_id, metric_type, timestamp)，删除三个旧索引
6. 回填 sample_count = COUNT(DISTINCT timestamp)、last_sample_at = MAX(timestamp)
"""
import sqlite3

from data.db.migrations._frozen import parse_metric_types

VERSION = 3
NAME = '采样计数、备注与打点表'


def apply(cursor: sqlite3.Cursor) -> None:
    # 步骤1
    cursor.execute('PRAGMA table_info(tasks)')
    task_columns = {row[1] for row in cursor.fetchall()}
    if 'note' not in task_columns:
        cursor.execute("ALTER TABLE tasks ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if 'sample_count' not in task_columns:
        cursor.execute(
            'ALTER TABLE tasks ADD COLUMN sample_count INTEGER NOT NULL DEFAULT 0')
    if 'last_sample_at' not in task_columns:
        cursor.execute('ALTER TABLE tasks ADD COLUMN last_sample_at TEXT')

    # 步骤2
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_markers (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_task_markers_task_ts
        ON task_markers(task_id, timestamp)
    ''')

    # 步骤3
    cursor.execute(
        'DELETE FROM data_points WHERE task_id NOT IN (SELECT task_id FROM tasks)')

    # 步骤4：按任务回填（首指标取自 tasks.metric_type，兼容 JSON 数组与裸指标名）。
    # 指标列表为空数组的任务没有可回填的首指标，其 NULL 行保持原样
    cursor.execute('SELECT task_id, metric_type FROM tasks')
    for task_id, text in cursor.fetchall():
        metrics = parse_metric_types(text)
        if not metrics:
            continue
        cursor.execute(
            'UPDATE data_points SET metric_type = ? '
            'WHERE task_id = ? AND metric_type IS NULL',
            (metrics[0], task_id))

    # 步骤5：先建新索引再删旧索引
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_data_points_task_metric_ts
        ON data_points(task_id, metric_type, timestamp)
    ''')
    for old_index in ('idx_data_points_timestamp', 'idx_data_points_task_id',
                      'idx_data_points_task_metric'):
        cursor.execute(f'DROP INDEX IF EXISTS {old_index}')

    # 步骤6：一次扫描按任务聚合后逐任务回写
    cursor.execute('''
        SELECT task_id, COUNT(DISTINCT timestamp), MAX(timestamp)
        FROM data_points GROUP BY task_id
    ''')
    for task_id, sample_count, last_sample_at in cursor.fetchall():
        cursor.execute(
            'UPDATE tasks SET sample_count = ?, last_sample_at = ? WHERE task_id = ?',
            (sample_count, last_sample_at, task_id))
