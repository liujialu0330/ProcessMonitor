"""
分析报告导出工作线程
ReportWorker 是分析页"导出报告"的线程：在后台 QThread 内经 db.points.iter_batches
分批读取数据点，配合 core/report.py 的 report_rows 生成器流式写明细 CSV，另写报告图片
与摘要 CSV；避免大数据量导出时一次性 fetchall 占用大量内存、也避免长时间同步写文件
阻塞 GUI 主线程。

每个文件先落到同目录的 <目标>.tmp，全部成功后逐个 os.replace 到目标；取消或失败只删除
临时文件，绝不触碰已存在的目标文件。
"""
import csv
import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from core.report import SUMMARY_HEADER, build_report_csv_header, report_rows
from core.workers.base import BackgroundTask, CancelToken
from data.database import Database
from data.models import MonitorTask

logger = logging.getLogger(__name__)

# 每批读取行数：足够大摊薄往返开销，又不至于一次性把大数据量全部载入内存
FETCH_BATCH_SIZE = 5000


@dataclass(frozen=True)
class ReportJob:
    """
    一次报告导出的全部输入（主线程准备好、线程内只读）。

    三个输出路径任一为 None 表示不写该文件（导出菜单的"仅 CSV / 仅图片"复用同一线程）。
    image 是界面层离屏渲染好的 QImage（QImage 可跨线程使用，QPixmap 不行）；
    summary_rows 是已算好的摘要行（数据在分析快照里，主线程拼装很便宜）。
    """
    task: MonitorTask
    csv_path: Optional[str] = None
    summary_path: Optional[str] = None
    image_path: Optional[str] = None
    image: Any = None
    metadata: Tuple[str, ...] = ()
    summary_rows: Tuple[tuple, ...] = ()
    gap_seconds: Optional[float] = None

    def paths(self) -> List[str]:
        return [p for p in (self.image_path, self.csv_path, self.summary_path) if p]


@dataclass(frozen=True)
class ReportResult:
    """报告导出结果：写出的文件、明细 CSV 的数据行数与处理的数据点数。"""
    paths: Tuple[str, ...]
    row_count: int
    point_count: int


class ReportWorker(BackgroundTask):
    """
    分析报告导出线程：写报告图片、带元数据与空洞空行的明细 CSV、指标 × 区间摘要 CSV。

    每个文件都先写 <目标>.tmp、全部成功后逐个 os.replace 到目标；取消或失败只删除
    临时文件，不触碰已存在的目标文件。结果经基类 result 发出，失败经 failed 发出，
    进度经 progress 发出已处理的数据点数；完成/失败信号是 run() 的最后一个动作，取消后
    两者都不发。线程对象的释放由线程登记表挂在 QThread.finished 上，页面不要在这些
    信号的槽里释放线程对象。
    """

    def __init__(self, db: Database, job: ReportJob, parent=None):
        """
        Args:
            db: 调用方已有的 Database 实例。线程内不再新建实例：Database 构造时会开
                BEGIN IMMEDIATE 写事务，会和"清理并压缩数据库"抢写锁；而这里用到的
                points.iter_batches / markers.list 每次调用都自建连接，跨线程共用是安全的
            job: 本次导出的全部输入
            parent: 父对象
        """
        super().__init__(parent)
        self.db = db
        self.job = job
        self._tmp_paths: List[str] = []

    def _tmp(self, path: str) -> str:
        tmp = path + '.tmp'
        self._tmp_paths.append(tmp)
        return tmp

    def execute(self, token: CancelToken) -> Optional[ReportResult]:
        job = self.job
        rows = points = 0
        written: List[Tuple[str, str]] = []
        try:
            if job.image_path:
                tmp = self._tmp(job.image_path)
                if job.image is None or not job.image.save(tmp, 'PNG'):
                    raise OSError(f"无法写入图片：{job.image_path}")
                written.append((tmp, job.image_path))
            if job.summary_path and not token.is_cancelled:
                tmp = self._tmp(job.summary_path)
                with open(tmp, 'w', newline='', encoding='utf-8-sig') as f:
                    for line in job.metadata:
                        f.write(line + '\n')
                    writer = csv.writer(f)
                    writer.writerow(SUMMARY_HEADER)
                    writer.writerows(job.summary_rows)
                written.append((tmp, job.summary_path))
            if job.csv_path and not token.is_cancelled:
                tmp = self._tmp(job.csv_path)
                rows, points = self._write_csv(tmp, token)
                written.append((tmp, job.csv_path))
            if token.is_cancelled:
                logger.info("报告导出已取消: task_id=%s", job.task.task_id)
                self._cleanup()
                return None
            for tmp, target in written:
                os.replace(tmp, target)
        except Exception:
            logger.error("报告导出失败: task_id=%s", job.task.task_id, exc_info=True)
            self._cleanup()
            raise
        self.progress.emit(points)
        return ReportResult(tuple(job.paths()), rows, points)

    def _write_csv(self, tmp: str, token: CancelToken) -> Tuple[int, int]:
        task = self.job.task
        processed = rows = 0
        batches = self.db.points.iter_batches(task.task_id, FETCH_BATCH_SIZE)
        try:
            markers = self.db.markers.list(task.task_id)

            def _points():
                nonlocal processed
                for batch in batches:
                    for dp in batch:
                        if token.is_cancelled:
                            return
                        processed += 1
                        if processed % FETCH_BATCH_SIZE == 0:
                            self.progress.emit(processed)
                        yield dp

            with open(tmp, 'w', newline='', encoding='utf-8-sig') as f:
                for line in self.job.metadata:
                    f.write(line + '\n')
                writer = csv.writer(f)
                writer.writerow(build_report_csv_header(task))
                for row in report_rows(task, _points(), markers, self.job.gap_seconds):
                    if token.is_cancelled:
                        break
                    writer.writerow(row)
                    if row:
                        rows += 1
        finally:
            batches.close()
        return rows, processed

    def failure_message(self, exc: Exception) -> str:
        return f"导出失败：{exc}"

    def _cleanup(self) -> None:
        for tmp in self._tmp_paths:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                logger.error("清理临时导出文件失败: %s", tmp, exc_info=True)
