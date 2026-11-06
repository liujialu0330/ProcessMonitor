"""
迁移注册表

每个迁移把库从 version-1 升到 version；迁移函数只改结构与数据，不写 user_version
（由 MigrationRunner 在同一事务末尾统一写入）。已发布的迁移冻结不改，
需要的辅助函数用 _frozen 中的副本，不引用会演进的运行时代码。
"""
import sqlite3
from dataclasses import dataclass
from typing import Callable, List

from data.db.migrations import v1, v2, v3, v4


@dataclass(frozen=True)
class Migration:
    """一步迁移：把库从 version-1 升到 version"""
    version: int
    name: str
    apply: Callable[[sqlite3.Cursor], None]


MIGRATIONS: List[Migration] = sorted(
    (Migration(m.VERSION, m.NAME, m.apply) for m in (v1, v2, v3, v4)),
    key=lambda m: m.version,
)

__all__ = ['Migration', 'MIGRATIONS']
