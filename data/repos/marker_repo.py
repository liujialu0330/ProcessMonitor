"""
打点仓储：task_markers 表

kind 区分用户手动打点与系统写入的基线/暂停/恢复打点，区间分析按它切分；
合法取值见 data.models.MARKER_KINDS，写入前校验，避免脏值进库后无法归类。
"""
from datetime import datetime
from typing import List, Optional

from data.mappers import row_to_marker, to_iso
from data.models import MARKER_KINDS, Marker
from data.repos._guard import storage_errors


def _check_kind(kind: str) -> str:
    if kind not in MARKER_KINDS:
        raise ValueError(f"未知的打点类型 {kind!r}，可选：{', '.join(MARKER_KINDS)}")
    return kind


class MarkerRepo:
    """task_markers 表仓储。所有方法遇 sqlite3.Error 抛 StorageError。"""

    def __init__(self, factory):
        self._factory = factory

    def add(self, task_id: str, timestamp: datetime, label: str,
            kind: str = 'user') -> int:
        """新增一个打点，返回其自增 ID（供界面随后改名/删除）。"""
        params = (task_id, to_iso(timestamp), label, _check_kind(kind),
                  to_iso(datetime.now()))
        with storage_errors(f"添加打点 {task_id} "):
            with self._factory.connect() as conn:
                cursor = conn.execute('''
                    INSERT INTO task_markers (task_id, timestamp, label, kind, created_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', params)
                return cursor.lastrowid

    def list(self, task_id: str, since: Optional[datetime] = None,
             until: Optional[datetime] = None) -> List[Marker]:
        """任务的打点，按时间升序（同时刻按 ID）；since/until 为闭区间，None 表示不限。"""
        clauses = ['task_id = ?']
        params: list = [task_id]
        if since is not None:
            clauses.append('timestamp >= ?')
            params.append(to_iso(since))
        if until is not None:
            clauses.append('timestamp <= ?')
            params.append(to_iso(until))
        where = ' AND '.join(clauses)
        with storage_errors(f"读取打点 {task_id} "):
            with self._factory.connect() as conn:
                rows = conn.execute(f'''
                    SELECT * FROM task_markers
                    WHERE {where} ORDER BY timestamp ASC, id ASC
                ''', params).fetchall()
                return [row_to_marker(row) for row in rows]

    def update_label(self, marker_id: int, label: str) -> bool:
        """改打点文字；返回是否命中。"""
        with storage_errors(f"修改打点 {marker_id} "):
            with self._factory.connect() as conn:
                return conn.execute(
                    'UPDATE task_markers SET label = ? WHERE id = ?',
                    (label, marker_id)).rowcount > 0

    def delete(self, marker_id: int) -> bool:
        """删除打点；返回是否命中。"""
        with storage_errors(f"删除打点 {marker_id} "):
            with self._factory.connect() as conn:
                return conn.execute(
                    'DELETE FROM task_markers WHERE id = ?', (marker_id,)).rowcount > 0

    def set_kind(self, marker_id: int, kind: str) -> bool:
        """改打点类型；返回是否命中。"""
        _check_kind(kind)
        with storage_errors(f"修改打点类型 {marker_id} "):
            with self._factory.connect() as conn:
                return conn.execute(
                    'UPDATE task_markers SET kind = ? WHERE id = ?',
                    (kind, marker_id)).rowcount > 0
