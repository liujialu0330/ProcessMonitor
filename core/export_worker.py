"""
导出工作线程
在后台 QThread 内经 db.points.iter_batches 分批读取数据点，配合 core/export.py 的
pivot_rows 生成器流式写 CSV（同一时间戳跨批次由生成器天然处理），避免大数据量
导出时一次性 fetchall 占用大量内存、也避免长时间同步写文件阻塞 GUI 主线程。

写入先落到同目录的 <目标>.tmp，成功后 os.replace 到目标；取消或失败只删除临时文件，
绝不触碰已存在的目标文件。
"""
import csv
import logging
import os
from typing import Iterator

from PyQt5.QtCore import QThread, pyqtSignal

from core.export import build_csv_header, pivot_rows
from data.database import Database
from data.models import MonitorTask, DataPoint

logger = logging.getLogger(__name__)

# 每批读取行数：足够大摊薄往返开销，又不至于一次性把大数据量全部载入内存
FETCH_BATCH_SIZE = 5000


class ExportWorker(QThread):
    """
    CSV 导出后台线程（持引用防GC，用法参照 core/update_checker.py 的 UpdateDownloader）

    信号约定：完成/失败信号是 run() 的最后一个动作（数据库连接、临时文件等收尾均
    已先完成），UI 侧应把 QThread.finished 接到 deleteLater，而不是在这些信号的槽里
    释放线程对象。
    """

    # 导出进度信号：已处理的数据点行数（非CSV行数，一次采集多个指标算多条数据点）
    export_progress = pyqtSignal(int)
    # 导出完成信号：(保存路径, 写出的CSV数据行数/采集次数, 处理的数据点行数)
    export_finished = pyqtSignal(str, int, int)
    # 导出失败信号，携带错误描述
    error_occurred = pyqtSignal(str)

    def __init__(self, db: Database, task: MonitorTask, save_path: str,
                 metric_type: str = None, parent=None):
        """
        Args:
            db: 调用方已有的 Database 实例。线程内不再新建实例：Database 构造时会开
                BEGIN IMMEDIATE 写事务，会和"清理并压缩数据库"抢写锁；而这里用到的
                points.iter_batches / markers.list 每次调用都自建连接，跨线程共用是安全的
            task: 待导出的任务（提供表头与透视所需的 process_name/pid/metric_types）
            save_path: CSV 保存路径
            metric_type: 指标类型过滤（None 表示导出任务全部指标：宽表每次采集一行，
                         各指标一列）
            parent: 父对象
        """
        super().__init__(parent)
        self.db = db
        self.task = task
        self.save_path = save_path
        self.metric_type = metric_type
        self._cancelled = False

    def cancel(self):
        """请求取消导出（导出循环内轮询检查，closeEvent 按 shutdown_thread 模式接入）"""
        self._cancelled = True

    @property
    def tmp_path(self) -> str:
        """临时文件路径（与目标同目录，保证 os.replace 在同一卷内原子替换）"""
        return self.save_path + '.tmp'

    def _iter_data_points(self, batches: Iterator[list]) -> Iterator[DataPoint]:
        """把分批数据拍平为逐点迭代；取消标志在每个点之间检查，保证取消及时生效"""
        for batch in batches:
            for dp in batch:
                if self._cancelled:
                    return
                if self.metric_type is not None and dp.metric_type != self.metric_type:
                    continue
                yield dp

    def run(self):
        error_message = None
        row_count = 0
        processed = 0
        batches = None
        try:
            # 仓储接口：读取失败抛 StorageError，由下面的 except 走导出失败路径
            batches = self.db.points.iter_batches(self.task.task_id, FETCH_BATCH_SIZE)
            # 打点随宽表导出：写在"时间不早于打点时刻的第一行"的"打点"列
            markers = self.db.markers.list(self.task.task_id)
            header = build_csv_header(self.task, include_markers=True)

            def _counted_iter():
                nonlocal processed
                for dp in self._iter_data_points(batches):
                    processed += 1
                    if processed % FETCH_BATCH_SIZE == 0:
                        self.export_progress.emit(processed)
                    yield dp

            with open(self.tmp_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                for row in pivot_rows(self.task, _counted_iter(), markers):
                    if self._cancelled:
                        break
                    writer.writerow(row)
                    row_count += 1

            if self._cancelled:
                logger.info("导出已取消: task_id=%s，删除临时文件", self.task.task_id)
                self._cleanup()
            else:
                os.replace(self.tmp_path, self.save_path)
        except Exception as e:
            logger.error("导出失败: task_id=%s", self.task.task_id, exc_info=True)
            self._cleanup()
            error_message = f"导出失败：{e}"
        finally:
            if batches is not None:
                batches.close()

        # 收尾（连接已关闭、临时文件已处理）完成后才发信号，信号是 run() 的最后动作；
        # 取消时不发任何信号
        if error_message is not None:
            self.error_occurred.emit(error_message)
        elif not self._cancelled:
            self.export_progress.emit(processed)
            self.export_finished.emit(self.save_path, row_count, processed)

    def _cleanup(self):
        """取消或失败时只删除临时文件，不删除已存在的目标文件"""
        try:
            if os.path.exists(self.tmp_path):
                os.remove(self.tmp_path)
        except Exception:
            logger.error("清理临时导出文件失败: %s", self.tmp_path, exc_info=True)
