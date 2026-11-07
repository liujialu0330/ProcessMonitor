"""
数据库元数据（meta 表）：结构版本、建库版本、最近写入版本、最低可读版本

user_version 只说明"库是什么结构"；meta 另外记录"谁建的、谁最后写过、至少什么版本才能
安全地读写"。以后的版本若写入了旧版本无法正确理解的数据（如汇总表口径变化），就提高
min_reader_version，旧版本据此以只读方式打开（reader_blocked 给出原因），而不是照常写入
把数据写坏。

键：
- schema_version：与 user_version 相同（文本）；
- created_by_version：建库（或重建迁移）的应用版本；
- last_writer_version：最近一次以可写方式启动的应用版本（启动校正时 touch_writer 更新）；
- min_reader_version：能安全读写本库的最低应用版本。
"""
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional, Union

from utils.version import compare_version, release_part

# v5 库要求的最低读取版本
MIN_READER_VERSION = '2.0.0'

KEY_SCHEMA = 'schema_version'
KEY_CREATED_BY = 'created_by_version'
KEY_LAST_WRITER = 'last_writer_version'
KEY_MIN_READER = 'min_reader_version'

_Executor = Union[sqlite3.Connection, sqlite3.Cursor]


@dataclass(frozen=True)
class MetaInfo:
    """meta 表内容；表或键不存在时对应字段为 None"""
    schema_version: Optional[int] = None
    created_by_version: Optional[str] = None
    last_writer_version: Optional[str] = None
    min_reader_version: Optional[str] = None


def _app_version(app_version: Optional[str]) -> str:
    if app_version:
        return app_version
    import config   # 延迟导入：只在调用方没给版本号时才需要
    return config.APP_VERSION


def create_table(cursor: _Executor) -> None:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')


def write_initial(cursor: _Executor, schema_version: int,
                  app_version: Optional[str] = None) -> None:
    """新建库或重建迁移时写入四个键（已存在则覆盖）"""
    version = _app_version(app_version)
    cursor.executemany('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', [
        (KEY_SCHEMA, str(int(schema_version))),
        (KEY_CREATED_BY, version),
        (KEY_LAST_WRITER, version),
        (KEY_MIN_READER, MIN_READER_VERSION),
    ])


def read_meta(conn: _Executor) -> MetaInfo:
    """读取 meta 表；没有该表（旧库）时返回全 None 的 MetaInfo"""
    exists = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if exists is None:
        return MetaInfo()
    values = dict(conn.execute('SELECT key, value FROM meta').fetchall())
    schema = values.get(KEY_SCHEMA)
    try:
        schema_version = int(schema) if schema is not None else None
    except ValueError:
        schema_version = None
    return MetaInfo(
        schema_version=schema_version,
        created_by_version=values.get(KEY_CREATED_BY),
        last_writer_version=values.get(KEY_LAST_WRITER),
        min_reader_version=values.get(KEY_MIN_READER),
    )


def touch_writer(conn: _Executor, app_version: Optional[str] = None) -> None:
    """记录本次以可写方式启动的应用版本（调用方负责提交）"""
    conn.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
                 (KEY_LAST_WRITER, _app_version(app_version)))


def reader_blocked(meta: MetaInfo, app_version: str,
                   compare: Optional[Callable[[str, str], int]] = None) -> Optional[str]:
    """
    当前版本是否不能安全写本库：min_reader_version 高于 app_version 时返回原因文案，否则 None。

    比较时 app_version 只取发布号部分（2.0.0-beta.1 视为 2.0.0）：库结构在某个发布线的
    第一个预发布版就已定稿，同一发布线的预发布版能正确读写；若按 SemVer 严格比较，
    2.0.0-beta.1 会因"低于 2.0.0"把 v5 库判为只读。

    Args:
        compare: 版本比较函数（a < b 返回负数）；默认用 utils.version.compare_version
    """
    required = meta.min_reader_version
    if not required:
        return None
    if compare is None:
        compare = compare_version
    if compare(release_part(app_version), required) < 0:
        return (f"数据库由 {meta.last_writer_version or meta.created_by_version or '更新的版本'} "
                f"写入，需要 {required} 或更高版本才能修改；当前版本 {app_version} "
                f"只能以只读方式打开")
    return None


def newer_writer_version(meta: MetaInfo) -> str:
    """只读提示里显示的"更高版本"：最近写入者优先，其次建库者，最后是最低可读版本"""
    return (meta.last_writer_version or meta.created_by_version
            or meta.min_reader_version or '')


# 读取 meta 时等待其他连接让路的秒数（与迁移探测一致）
READ_TIMEOUT = 5.0


def read_meta_file(db_path: str) -> MetaInfo:
    """
    用不设日志模式的裸连接读取库文件的 meta（query_only，不做任何写入）。

    组合根在迁移之后、构造 Database 之前调用，据此决定是否只读打开：不能用连接工厂，
    工厂的可写连接会把库切成 WAL，对更高版本写出的库这本身就是一次写入。
    文件不存在时返回全 None 的 MetaInfo（不让 sqlite3 建出空文件）。

    Raises:
        sqlite3.Error: 库被锁或损坏（调用方决定如何降级）
    """
    if not os.path.exists(db_path):
        return MetaInfo()
    conn = sqlite3.connect(db_path, timeout=READ_TIMEOUT)
    try:
        conn.execute('PRAGMA query_only=1')
        return read_meta(conn)
    finally:
        conn.close()
