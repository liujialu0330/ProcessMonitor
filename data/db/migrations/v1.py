"""
v0 -> v1 迁移：多指标支持（版本号由迁移运行器统一写入，本函数不写 user_version）

顺序敏感：先回填 data_points 再转换 tasks。
1. data_points 增加 metric_type 列（幂等检查）
2. 用任务当前的裸指标名回填数据点的 metric_type
3. tasks.metric_type 裸字符串转换为 JSON 数组文本
4. 建复合索引（v3 会删掉它，换成三列索引）
"""
import json
import sqlite3

VERSION = 1
NAME = '多指标支持'


def apply(cursor: sqlite3.Cursor) -> None:
    # 步骤1: 增加列（先检查列是否已存在，保证幂等）
    cursor.execute('PRAGMA table_info(data_points)')
    columns = [row[1] for row in cursor.fetchall()]
    if 'metric_type' not in columns:
        cursor.execute('ALTER TABLE data_points ADD COLUMN metric_type TEXT')

    # 步骤2: 先回填数据点（此时 tasks.metric_type 还是裸字符串）
    cursor.execute('''
        UPDATE data_points
        SET metric_type = (
            SELECT t.metric_type FROM tasks t WHERE t.task_id = data_points.task_id
        )
        WHERE metric_type IS NULL
    ''')

    # 步骤3: 后转换任务表（Python 侧 json.dumps，不依赖 json1 扩展）
    cursor.execute("SELECT task_id, metric_type FROM tasks WHERE metric_type NOT LIKE '[%'")
    rows = cursor.fetchall()
    for row in rows:
        cursor.execute(
            'UPDATE tasks SET metric_type = ? WHERE task_id = ?',
            (json.dumps([row[1]]), row[0])
        )

    # 步骤4: 建复合索引
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_data_points_task_metric
        ON data_points(task_id, metric_type)
    ''')
