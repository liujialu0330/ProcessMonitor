"""
成员仓储：task_members 表（进程组任务的实例留痕）与成员序列的发现

一个进程组任务（tree / name）里的每个进程实例由 (pid, create_time) 唯一识别：实例首次
出现时写一行（first_ms = last_ms = 出现时刻），实例消失或任务结束时把 last_ms 更新为最后
一次见到的时刻。PID 被复用时 create_time 不同，是另一行，不会与旧实例混淆。

成员 = 同名实例的合并（成员键为进程名小写）。成员的样本是普通序列，指标键为
"指标@成员键"（见 utils.metrics.member_series_key），取数直接用 PointRepo / AnalyticsRepo
传这个键即可，不需要专门的取数方法；这里只负责回答"这个任务有哪些成员、各有哪些序列"。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Sequence, Tuple

from data.mappers import from_ms
from data.models import MemberRecord
from data.repos._guard import storage_errors
from data.repos._samples import task_series
from utils.metrics import is_member_series, normalize_member_key, split_member_key


@dataclass(frozen=True)
class MemberSummary:
    """一个成员（同名实例的合并）的概况。

    instance_count 为留痕里出现过的不同实例数（含已退出的）；first_at / last_at 为该
    成员全部实例里最早的首次出现与最晚的最后一次见到（本地时间）。name 取最早出现的
    实例的原名（保留大小写）。
    """
    member_key: str
    name: str
    instance_count: int
    first_at: datetime
    last_at: datetime


def summarize_members(rows: Iterable[MemberRecord]) -> List[MemberSummary]:
    """实例行 → 成员概况（纯函数）：按成员键归并，按首次出现的先后排列。"""
    grouped: Dict[str, List[MemberRecord]] = {}
    for row in rows:
        grouped.setdefault(normalize_member_key(row.name), []).append(row)
    summaries = []
    for key, items in grouped.items():
        earliest = min(items, key=lambda r: (r.first_ms, r.pid))
        summaries.append(MemberSummary(
            member_key=key, name=earliest.name, instance_count=len(items),
            first_at=from_ms(min(r.first_ms for r in items)),
            last_at=from_ms(max(r.last_ms for r in items))))
    summaries.sort(key=lambda m: (m.first_at, m.member_key))
    return summaries


class MemberRepo:
    """task_members 表仓储。所有方法遇 sqlite3.Error 抛 StorageError。"""

    def __init__(self, factory):
        self._factory = factory

    def apply(self, appeared: Sequence[MemberRecord] = (),
              last_seen: Sequence[Tuple[str, int, float, int]] = ()) -> None:
        """
        一个事务内写入一批实例变化（采集线程按缓冲批量调用，不为每个周期单独开事务）：
        - appeared：新出现的实例，first_ms = last_ms；已存在（同 task_id / pid /
          create_time）的行不覆盖，保留最早的 first_ms；
        - last_seen：(task_id, pid, create_time, last_ms)，把 last_ms 更新为较大者
          （只增不减，重复或乱序提交都安全）。
        两者都为空时不开事务。
        """
        if not appeared and not last_seen:
            return
        with storage_errors("写入进程组成员留痕 "):
            with self._factory.transaction() as cursor:
                if appeared:
                    cursor.executemany(
                        'INSERT OR IGNORE INTO task_members '
                        '(task_id, pid, create_time, name, first_ms, last_ms) '
                        'VALUES (?, ?, ?, ?, ?, ?)',
                        [(m.task_id, m.pid, m.create_time, m.name, m.first_ms, m.last_ms)
                         for m in appeared])
                if last_seen:
                    cursor.executemany(
                        'UPDATE task_members SET last_ms = MAX(last_ms, ?) '
                        'WHERE task_id = ? AND pid = ? AND create_time = ?',
                        [(ms, task_id, pid, ct) for task_id, pid, ct, ms in last_seen])

    def list_instances(self, task_id: str) -> List[MemberRecord]:
        """任务的全部实例留痕，按首次出现时间排列。"""
        with storage_errors(f"读取进程组实例 {task_id} "):
            with self._factory.connect() as conn:
                rows = conn.execute(
                    'SELECT task_id, pid, create_time, name, first_ms, last_ms '
                    'FROM task_members WHERE task_id = ? ORDER BY first_ms, pid, create_time',
                    (task_id,)).fetchall()
        return [MemberRecord(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def list_members(self, task_id: str) -> List[MemberSummary]:
        """任务的成员概况（同名实例合并）：成员键、显示名、出现过的实例数、首末时间；
        按首次出现的先后排列。pid 任务（没有留痕）返回空列表。"""
        return summarize_members(self.list_instances(task_id))

    def member_series(self, task_id: str) -> List[Tuple[str, str]]:
        """任务里已有样本序列的成员序列 [(指标键, 成员键)]（按序列建立顺序）：
        来自 series 表，不依赖留痕，所以实例留痕写失败时分析仍能找到成员曲线。"""
        with storage_errors(f"读取成员序列 {task_id} "):
            with self._factory.connect() as conn:
                series = task_series(conn, task_id)
        found = []
        for _sid, metric in series:
            if is_member_series(metric):
                base, member = split_member_key(metric)
                found.append((base, member))
        return found

    def member_keys(self, task_id: str) -> List[str]:
        """任务的成员键（按成员序列首次建立的先后，去重）。"""
        keys: List[str] = []
        for _metric, member in self.member_series(task_id):
            if member not in keys:
                keys.append(member)
        return keys
