"""
证据包导出工作线程

EvidenceWorker 复用 ReportWorker 的三件套写出逻辑（报告图片、明细 CSV、摘要 CSV），
但写到一个临时工作目录里，随后调 core.evidence.build_evidence_package 打成 zip 写到
用户选的位置；工作目录无论成功、失败还是取消都会删除。zip 本身先写同目录临时文件再
替换目标（见 core.evidence），所以失败不会留下半个 zip、也不破坏已存在的同名文件。

与 ReportWorker 一样登记到线程登记表、与清理数据库互斥（由分析页视图模型的
start_export 负责）；结果仍是 ReportResult，paths 只有 zip 一个文件。
"""
import logging
import os
import shutil
import tempfile
from dataclasses import replace
from typing import Optional

from core.evidence import build_evidence_package
from core.history_query import TaskAnalysis
from core.report_worker import ReportJob, ReportResult, ReportWorker
from core.workers.base import CancelToken

logger = logging.getLogger(__name__)

# 临时工作目录名前缀（系统临时目录下）
EVIDENCE_WORK_DIR_PREFIX = 'pm_evidence_'
MSG_FAILED = "生成证据包失败：{detail}"


def _inside(directory: str, path: Optional[str]) -> Optional[str]:
    """把三件套的目标路径换到工作目录下（只保留文件名，zip 里即是这个名字）。"""
    return os.path.join(directory, os.path.basename(path)) if path else None


class EvidenceWorker(ReportWorker):
    """证据包导出线程：三件套写到临时工作目录 → 打包 zip 到 out_path。"""

    def __init__(self, db, job: ReportJob, *, analysis: TaskAnalysis, out_path: str,
                 log_path: Optional[str], clock, stop_reason=None, parent=None):
        """
        Args:
            db: 调用方已有的 Database 实例（同 ReportWorker；证据包另用它补任务元数据）
            job: 三件套的导出任务；路径只取文件名，实际写到临时工作目录
            analysis: 分析页当前的 TaskAnalysis（判定数值取自它，与界面一致）
            out_path: 目标 zip 路径
            log_path: app.log 路径；None 不收录日志摘录
            clock: 时钟（只用 now()）
            stop_reason: 任务的停止原因（命令行导出传内存里记下的）；None 时由证据包取任务
                记录里保存的
        """
        super().__init__(db, job, parent)
        self.analysis = analysis
        self.out_path = out_path
        self.log_path = log_path
        self.clock = clock
        self.stop_reason = stop_reason

    def execute(self, token: CancelToken) -> Optional[ReportResult]:
        work_dir = tempfile.mkdtemp(prefix=EVIDENCE_WORK_DIR_PREFIX)
        try:
            job = self.job
            self.job = replace(job, image_path=_inside(work_dir, job.image_path),
                               csv_path=_inside(work_dir, job.csv_path),
                               summary_path=_inside(work_dir, job.summary_path))
            report = super().execute(token)
            if report is None or token.is_cancelled:
                logger.info("证据包导出已取消: task_id=%s", job.task.task_id)
                return None
            build_evidence_package(
                self.db, self.job.task, self.analysis,
                report_png=self.job.image_path, detail_csv=self.job.csv_path,
                summary_csv=self.job.summary_path, out_path=self.out_path,
                log_path=self.log_path, clock=self.clock, stop_reason=self.stop_reason)
            return ReportResult((self.out_path,), report.row_count, report.point_count)
        except Exception:
            logger.error("证据包导出失败: task_id=%s", self.job.task.task_id, exc_info=True)
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def failure_message(self, exc: Exception) -> str:
        return MSG_FAILED.format(detail=exc)
