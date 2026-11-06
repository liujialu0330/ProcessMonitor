"""
数据库维护：过期任务清理、压缩、占用统计

与仓储一样，sqlite3 异常包成 StorageError 抛出；尤其 vacuum 失败必须让调用方知道，
否则设置页会在压缩失败时仍提示"完成"。
"""
import logging
import os
from datetime import datetime, timedelta

from data.errors import StorageError
from data.mappers import to_iso
from data.repos._guard import storage_errors
from data.repos.task_repo import delete_task_rows

logger = logging.getLogger(__name__)


class Maintenance:
    """数据库维护操作，持有与仓储相同的连接工厂。"""

    def __init__(self, factory):
        self._factory = factory

    def cleanup_older_than(self, retention_days: int) -> int:
        """
        删除已停止且早于保留期限的任务及其全部数据点、打点（一个事务）。

        retention_days <= 0 表示禁用清理，直接返回 0、不碰库。调用方必须保证在孤儿任务
        校正之后调用：先把上次崩溃遗留的 running 任务校正为 stopped，否则"刚崩溃、其实
        还新鲜"的任务可能在同一次启动里被当成过期任务误删。
        判断口径 COALESCE(end_time, start_time) < 截止时间，兜底 end_time 为空的老数据。

        Returns:
            int: 被删除的任务数
        """
        if retention_days <= 0:
            return 0
        cutoff = to_iso(datetime.now() - timedelta(days=retention_days))
        with storage_errors("清理过期任务"):
            with self._factory.transaction() as cursor:
                cursor.execute('''
                    SELECT task_id FROM tasks
                    WHERE status = 'stopped' AND COALESCE(end_time, start_time) < ?
                ''', (cutoff,))
                task_ids = [row[0] for row in cursor.fetchall()]
                for task_id in task_ids:
                    point_count = delete_task_rows(cursor, task_id)
                    logger.info("自动清理: 删除过期任务 task_id=%s 数据点=%d 条",
                                task_id, point_count)
        if task_ids:
            # 大批量删除会把 -wal 撑大；常驻连接下 -wal 不会随连接关闭而删除，主动截断
            self._checkpoint_truncate()
        return len(task_ids)

    def _checkpoint_truncate(self) -> None:
        """
        PRAGMA wal_checkpoint(TRUNCATE)：把 -wal 合并进主文件并截断为 0 字节。
        必须在事务外执行（connect() 的隐式事务只在 DML 前开启，PRAGMA 不会触发）。
        """
        with storage_errors("截断 WAL"):
            with self._factory.connect() as conn:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def vacuum(self) -> None:
        """
        先 wal_checkpoint(TRUNCATE) 把 -wal 合并进主文件，再 VACUUM 重建主文件回收空间，
        最后再 TRUNCATE 一次：WAL 模式下 VACUUM 重写的整库页面先写进 -wal，常驻连接开着时
        -wal 不会随连接关闭而删除，会停在接近整库的大小（"当前占用"反而变大）。
        VACUUM 需要较独占的访问，调用方应只在没有运行中任务时调用（这里不越层查任务状态）。
        失败抛 StorageError。
        """
        with storage_errors("压缩数据库"):
            with self._factory.connect() as conn:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                conn.execute('VACUUM')
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def size_bytes(self) -> int:
        """
        数据库占用的磁盘字节数：主库 + -wal + -shm（存在的部分求和）。
        checkpoint 之前最近写入的数据还在 -wal 里，只算主库会低估。
        文件在检查与取大小之间被删掉（连接关闭时 -wal 可能被移除）按 0 计；
        其余文件系统错误抛 StorageError。
        """
        total = 0
        db_path = self._factory.db_path
        for suffix in ('', '-wal', '-shm'):
            path = db_path + suffix
            try:
                total += os.path.getsize(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                raise StorageError(f"获取数据库文件大小失败: {path}: {e}") from e
        return total
