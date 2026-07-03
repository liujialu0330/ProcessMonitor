"""
导出工作线程
在后台 QThread 内用游标 fetchmany 分批读取数据点，配合 core/export.py 的
pivot_rows 生成器流式写 CSV（同一时间戳跨批次由生成器天然处理），避免大数据量
导出时一次性 fetchall 占用大量内存、也避免长时间同步写文件阻塞 GUI 主线程。
"""
import csv
import logging
import os
import sqlite3
from datetime import datetime

from PyQt5.QtCore import QThread, pyqtSignal

from core.export import build_csv_header, pivot_rows
from data.models import MonitorTask, DataPoint

logger = logging.getLogger(__name__)

# 游标 fetchmany 每批读取行数：足够大摊薄往返开销，又不至于一次性把大数据量全部载入内存
FETCH_BATCH_SIZE = 5000


class ExportWorker(QThread):
    """CSV 导出后台线程（持引用防GC，用法参照 core/update_checker.py 的 UpdateDownloader）"""

    # 导出进度信号：已处理的数据点行数（非CSV行数，一次采集多个指标算多条数据点）
    export_progress = pyqtSignal(int)
    # 导出完成信号：(保存路径, 写出的CSV数据行数/采集次数, 处理的数据点行数)
    export_finished = pyqtSignal(str, int, int)
    # 导出失败信号，携带错误描述
    error_occurred = pyqtSignal(str)

    def __init__(self, db_path: str, task: MonitorTask, save_path: str,
                 metric_type: str = None, parent=None):
        """
        Args:
            db_path: 数据库文件路径，用于 run() 内自建专用连接（跨批次存活，
                     database.py 现为每操作独立连接，这里游标 fetchmany 需要一个
                     贯穿整个导出过程的连接，故不复用 Database._get_connection）
            task: 待导出的任务（提供表头与透视所需的 process_name/pid/metric_types）
            save_path: CSV 保存路径
            metric_type: 指标类型过滤（None 表示导出任务全部指标，与现有一次性
                         导出行为一致：宽表每次采集一行，各指标一列）
            parent: 父对象
        """
        super().__init__(parent)
        self.db_path = db_path
        self.task = task
        self.save_path = save_path
        self.metric_type = metric_type
        self._cancelled = False

    def cancel(self):
        """请求取消导出（导出循环内轮询检查，closeEvent 按 shutdown_thread 模式接入）"""
        self._cancelled = True

    def _iter_data_points(self, conn: sqlite3.Connection):
        """
        用游标 fetchmany 流式读取数据点（避免一次性 fetchall 占用大量内存）。
        按 timestamp 升序读取，与 pivot_rows 生成器要求的"同组行相邻"一致。
        取消标志在每行/每批之间检查，保证取消请求能及时生效。
        """
        cursor = conn.cursor()

        where = 'task_id = ?'
        params: list = [self.task.task_id]
        if self.metric_type is not None:
            first_metric = self.task.metric_types[0] if self.task.metric_types else None
            if self.metric_type == first_metric:
                where += ' AND (metric_type = ? OR metric_type IS NULL)'
            else:
                where += ' AND metric_type = ?'
            params.append(self.metric_type)

        cursor.execute(f'''
            SELECT task_id, timestamp, value, metric_type FROM data_points
            WHERE {where}
            ORDER BY timestamp ASC
        ''', params)

        while True:
            if self._cancelled:
                return
            rows = cursor.fetchmany(FETCH_BATCH_SIZE)
            if not rows:
                return
            for row in rows:
                if self._cancelled:
                    return
                yield DataPoint(
                    task_id=row['task_id'],
                    timestamp=datetime.fromisoformat(row['timestamp']),
                    value=row['value'],
                    metric_type=row['metric_type'] or '',
                )

    def run(self):
        conn = None
        try:
            # 自建专用连接，参数与 database.py 的 _get_connection 一致
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
            conn.execute('PRAGMA synchronous=NORMAL')

            header = build_csv_header(self.task)

            processed = 0

            def _counted_iter():
                nonlocal processed
                for dp in self._iter_data_points(conn):
                    processed += 1
                    if processed % FETCH_BATCH_SIZE == 0:
                        self.export_progress.emit(processed)
                    yield dp

            row_count = 0
            with open(self.save_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                for row in pivot_rows(self.task, _counted_iter()):
                    if self._cancelled:
                        break
                    writer.writerow(row)
                    row_count += 1

            if self._cancelled:
                logger.info("导出已取消: task_id=%s，删除未完成文件", self.task.task_id)
                self._cleanup()
                return

            self.export_progress.emit(processed)
            self.export_finished.emit(self.save_path, row_count, processed)
        except Exception as e:
            logger.error("导出失败: task_id=%s", self.task.task_id, exc_info=True)
            self._cleanup()
            self.error_occurred.emit(f"导出失败：{e}")
        finally:
            if conn is not None:
                conn.close()

    def _cleanup(self):
        """取消或失败时删除写了一半的 CSV 文件"""
        try:
            if os.path.exists(self.save_path):
                os.remove(self.save_path)
        except Exception:
            logger.error("清理未完成的导出文件失败: %s", self.save_path, exc_info=True)
