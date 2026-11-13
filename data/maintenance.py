"""
数据库维护：过期任务清理、压缩、占用统计，以及回退 1.4.1 后再升级时的两步启动校正
（导回降级期间写入 data_points 的样本、清理已删任务留下的孤儿序列）

与仓储一样，sqlite3 异常包成 StorageError 抛出；尤其 vacuum 失败必须让调用方知道，
否则设置页会在压缩失败时仍提示"完成"。
"""
import logging
import os
from datetime import datetime, timedelta

from data.db.meta import MetaInfo, read_meta, touch_writer
from data.db.migrations import rebuild
from data.errors import StorageError
from data.mappers import to_iso
from data.repos import rollup, series_cache
from data.repos._guard import storage_errors
from data.repos._samples import delete_series_rows
from data.repos.task_repo import delete_task_rows

logger = logging.getLogger(__name__)


class Maintenance:
    """数据库维护操作，持有与仓储相同的连接工厂。"""

    def __init__(self, factory):
        self._factory = factory

    def cleanup_older_than(self, retention_days: int) -> int:
        """
        删除已停止且早于保留期限的任务及其全部样本、打点与附属行（一个事务）。

        retention_days <= 0 表示禁用清理，直接返回 0、不碰库。调用方必须保证在孤儿任务
        校正之后调用：先把上次崩溃遗留的 running 任务校正为 stopped，否则"刚崩溃、其实
        还新鲜"的任务可能在同一次启动里被当成过期任务误删。
        判断口径 COALESCE(end_time, start_time) < 截止时间，兜底 end_time 为空的老数据。
        场景参考不参与自动清理，只能由用户显式确认删除。

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
                    WHERE status = 'stopped' AND COALESCE(is_reference, 0) = 0
                        AND COALESCE(end_time, start_time) < ?
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

    def legacy_points_pending(self) -> int:
        """data_points 里待导回的行数（1.4.1 降级期间写入；组合根据此决定是否显示进度窗口）"""
        with storage_errors("检查降级期间写入的数据点"):
            with self._factory.connect() as conn:
                return conn.execute('SELECT COUNT(*) FROM data_points').fetchone()[0]

    def import_legacy_points(self, on_progress=None) -> int:
        """
        启动校正：把 data_points 中的行（用户回退 1.4.1 期间由它写入）导回 samples，
        清空 data_points，并重算受影响任务的采样计数（一个事务）。

        口径同重建式迁移对 v1 源库的处理：旧内存指标键（memory_vms / memory_pagefile）并入
        memory_private、同一时刻的重复内存指标只留一行，NULL 指标回填为任务首指标，
        任务已不存在的孤儿行丢弃，时间戳无效的行跳过；任务指标列表里的旧键同样改名。
        data_points 为空时只做一次只读检查，不开写事务。

        Args:
            on_progress: 进度回调 (阶段名, 已完成, 总数)，阶段同重建式迁移
                （rebuild.STAGE_SAMPLES / STAGE_ROLLUPS）；在调用线程里同步调用

        Returns:
            int: 导回前 data_points 的行数（0 表示无事可做）
        """
        with storage_errors("检查降级期间写入的数据点"):
            with self._factory.connect() as conn:
                pending = conn.execute(
                    'SELECT EXISTS (SELECT 1 FROM data_points)').fetchone()[0]
        if not pending:
            return 0
        with storage_errors("导回降级期间写入的数据点"):
            with self._factory.transaction() as cursor:
                total, tasks, skipped = rebuild.import_legacy_points(
                    cursor.connection, on_progress=on_progress)
        logger.warning("已导回回退旧版本期间写入的数据点: %d 行，涉及任务 %d 个，"
                       "跳过时间戳无效的行 %d", total, len(tasks), skipped)
        return total

    def rebuild_rollups(self) -> int:
        """
        按 samples 整体重算全部序列的分钟汇总（rollup_1m）与空洞（series_gaps），一个事务。

        正常写入路径在同一事务内维护二者，这里是兜底（设置页"重建汇总"）：手工改库、
        异常中断留下的不一致都以样本为准修正。调用方应持有维护锁（与运行中任务的写入互斥）。

        Returns:
            int: 重算的序列数
        """
        with storage_errors("重建分钟汇总"):
            with self._factory.transaction() as cursor:
                count = rollup.rebuild_rollups(cursor.connection)
        logger.info("重建分钟汇总完成: %d 条序列", count)
        return count

    def purge_orphan_series(self) -> int:
        """
        启动校正：删除任务已不存在的序列及其样本、汇总、空洞，以及孤儿的成员、句柄快照与
        打点行（1.4.1 删除任务时只删它认识的 tasks / data_points，其余表留下孤儿行）。

        Returns:
            int: 被删除的孤儿序列数
        """
        with storage_errors("清理孤儿序列"):
            with self._factory.transaction() as cursor:
                conn = cursor.connection
                orphans = [row[0] for row in conn.execute(
                    'SELECT series_id FROM series '
                    'WHERE task_id NOT IN (SELECT task_id FROM tasks)')]
                if orphans:
                    series_cache.invalidate()
                    samples = delete_series_rows(conn, orphans)
                else:
                    samples = 0
                others = 0
                for table in ('task_members', 'handle_snapshots', 'task_markers'):
                    others += conn.execute(
                        f'DELETE FROM {table} WHERE task_id NOT IN '
                        f'(SELECT task_id FROM tasks)').rowcount
        if orphans or others:
            logger.warning("孤儿清理: 序列 %d 条（样本 %d 行），其他附属行 %d 行",
                           len(orphans), samples, others)
        return len(orphans)

    def read_meta(self) -> MetaInfo:
        """读取 meta 表（旧库没有该表时各字段为 None）"""
        with storage_errors("读取数据库元数据"):
            with self._factory.connect() as conn:
                return read_meta(conn)

    def touch_writer(self, app_version: str) -> None:
        """记录本次以可写方式启动的应用版本（meta.last_writer_version）"""
        with storage_errors("记录数据库写入版本"):
            with self._factory.transaction() as cursor:
                touch_writer(cursor, app_version)

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
