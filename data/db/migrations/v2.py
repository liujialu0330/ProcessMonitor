"""
v1 -> v2 迁移：合并重复内存指标（Windows 上 vms/pagefile/private 数值完全相同）
（版本号由迁移运行器统一写入，本函数不写 user_version）

1. tasks：memory_vms / memory_pagefile 映射为 memory_private，保持原顺序去重
2. data_points：同一 (task_id, timestamp) 下三者只保留一行，再统一改名为 memory_private

别名映射用字面量写死，迁移代码不依赖会演进的常量。metric_type 为 NULL 的旧行不动。
"""
import json
import sqlite3
from typing import List

from data.db.migrations._frozen import parse_metric_types

VERSION = 2
NAME = '合并重复内存指标'


def apply(cursor: sqlite3.Cursor) -> None:
    legacy = {'memory_vms': 'memory_private', 'memory_pagefile': 'memory_private'}

    # 步骤1: 任务指标列表改名并去重（保持原顺序）
    cursor.execute('SELECT task_id, metric_type FROM tasks')
    for task_id, text in cursor.fetchall():
        old_list = parse_metric_types(text)
        new_list: List[str] = []
        for metric in old_list:
            metric = legacy.get(metric, metric)
            if metric not in new_list:
                new_list.append(metric)
        if new_list != old_list:
            cursor.execute(
                'UPDATE tasks SET metric_type = ? WHERE task_id = ?',
                (json.dumps(new_list), task_id)
            )

    # 步骤2: 数据点去重（同一采集周期在 psutil oneshot 内取值，三者数值相同，
    # 保留哪行都正确），再统一改名
    cursor.execute('''
        DELETE FROM data_points
        WHERE metric_type IN ('memory_private', 'memory_vms', 'memory_pagefile')
          AND id NOT IN (
            SELECT MIN(id) FROM data_points
            WHERE metric_type IN ('memory_private', 'memory_vms', 'memory_pagefile')
            GROUP BY task_id, timestamp
          )
    ''')
    cursor.execute('''
        UPDATE data_points SET metric_type = 'memory_private'
        WHERE metric_type IN ('memory_vms', 'memory_pagefile')
    ''')
