"""
迁移专用的辅助函数冻结副本

迁移代码描述的是"某个历史版本的数据长什么样"，不能随运行时代码演进而改变行为，
所以这里的函数一经发布不再修改；运行时的同名逻辑另行维护。
"""
import json
from typing import List


def parse_metric_types(text: str) -> List[str]:
    """
    解析 tasks.metric_type 列内容为指标列表

    - JSON 数组文本 → 元素转字符串后的列表；空数组 '[]' → []
    - 旧版裸指标名、或以 '[' 开头但不是合法 JSON 数组的文本 → [原文本]
    """
    if text and text.startswith('['):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(m) for m in parsed]
        except (json.JSONDecodeError, TypeError):
            pass  # 解析失败回退为单值
    return [text]


def create_schema_v4(cursor) -> None:
    """
    v4 结构的冻结副本（不写 user_version，由迁移运行器写入）。

    生产路径不再建 v4 库；只供 MigrationRunner(target_version=4) 在全新库上建出 v4 结构，
    让测试能构造"就地迁移链的终点"作为重建式迁移的对照。
    """
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
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_data_points_task_metric_ts
        ON data_points(task_id, metric_type, timestamp)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_task_markers_task_ts
        ON task_markers(task_id, timestamp)
    ''')
