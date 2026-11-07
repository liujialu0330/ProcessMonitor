"""
阶段快照写入（StageSnapshotWriter）

浸泡测试一跑十几个小时，中途想看进展不必停任务：方案设了"每 N 小时写阶段快照"时，
AutoMarkerService 到期发 stage_snapshot_due(task_id, 目录, 序号)，本模块据此写出
**报告图片 + 摘要 CSV**（与分析页"导出报告"同一套内容，范围为"全部"；不写明细 CSV——
它随时长线性增长，十几个小时就是几十上百 MB，阶段快照只需要看趋势与区间结论）。

一次写入分三段：
1. 工作线程（StageAnalysisWorker）：建目录、读任务、按"全部"范围逐指标查分析快照
   （与分析页同一 run_history_query，分桶有上限，数据再多也是有界的）；
2. 界面线程：用注入的 renderer 离屏渲染报告图片（图表是控件，只能在界面线程画；
   渲染函数在界面层，core 不反向依赖 ui，故由组装方注入）；
3. 工作线程（ReportWorker）：写图片与摘要 CSV（先写 .tmp，全部成功再原子替换）。

整段期间经 manager.begin_export / end_export 登记，与清理数据库互斥。

失败语义：写快照是附带功能，失败绝不影响采集；同一任务只经 failed 提示一次（之后的
失败只记日志），避免每 N 小时弹一次同样的错误。同一任务上一次还没写完时跳过本次并记日志。

文件命名：<进程名>_<task_id 前 8 位>_阶段<序号两位>_<yyyyMMdd-HHmm>.png / .csv，
时间取请求时刻（注入的时钟）。
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from core.history_query import (DEFAULT_RANGE_KEY, TaskAnalysis, build_request,
                                run_history_query)
from core.report import metadata_lines, summary_rows
from core.report_worker import ReportJob, ReportWorker
from core.workers.base import BackgroundTask, CancelToken, TaskError
from data.models import MonitorTask

logger = logging.getLogger(__name__)

IMAGE_SUFFIX = '.png'
SUMMARY_SUFFIX = '.csv'
BASENAME_TEMPLATE = "{name}_{short_id}_阶段{index:02d}_{stamp}"
STAMP_FORMAT = '%Y%m%d-%H%M'
SHORT_ID_LENGTH = 8

# 报告图片信息行与摘要 CSV 元数据里的"范围"
RANGE_TEXT_TEMPLATE = "全部（运行中第 {index} 次阶段快照）"

THREAD_NAME = "阶段快照"
# 退出时等待的上限：读库段在两个指标之间检查取消，写文件段只有图片与摘要两个小文件
THREAD_TIMEOUT_MS = 3000

# ---------- 失败文案 ----------
ERR_NO_DATABASE = "未连接数据库，未能写阶段快照"
ERR_MAINTENANCE = "正在清理数据库，未能写阶段快照"
ERR_NO_TASK = "找不到该任务的数据，未能写阶段快照"
ERR_MAKE_DIR = "无法创建快照目录：{detail}"
ERR_READ = "读取数据失败：{detail}"
ERR_RENDER = "报告图片生成失败：{detail}"

_INVALID_CHARS = '\\/:*?"<>|\r\n\t'

# renderer(task, analysis, generated_at, range_text) -> QImage；在界面线程调用
Renderer = Callable[[MonitorTask, TaskAnalysis, datetime, str], Any]


def snapshot_basename(task: MonitorTask, index: int, at: datetime) -> str:
    """阶段快照文件的基名（不含扩展名）。进程名去 .exe 与文件名非法字符。"""
    name = task.process_name or 'task'
    if name.lower().endswith('.exe'):
        name = name[:-4]
    name = ''.join('_' if ch in _INVALID_CHARS else ch for ch in name).strip(' .') or 'task'
    return BASENAME_TEMPLATE.format(name=name, short_id=task.task_id[:SHORT_ID_LENGTH],
                                    index=index, stamp=at.strftime(STAMP_FORMAT))


def snapshot_paths(directory: str, task: MonitorTask, index: int,
                   at: datetime) -> Tuple[str, str]:
    """(报告图片路径, 摘要 CSV 路径)。"""
    base = os.path.join(directory, snapshot_basename(task, index, at))
    return base + IMAGE_SUFFIX, base + SUMMARY_SUFFIX


def range_text(index: int) -> str:
    """阶段快照的"范围"文案（renderer 画信息行时也用它）。"""
    return RANGE_TEXT_TEMPLATE.format(index=index)


class StageAnalysisWorker(BackgroundTask):
    """阶段快照的读库段：建目录、读任务、按"全部"范围逐指标查分析快照。

    结果为 (MonitorTask, TaskAnalysis)；取消时返回 None（基类随即不发结果）。
    """

    def __init__(self, db, task_id: str, directory: str, parent=None):
        super().__init__(parent)
        self.db = db
        self.task_id = task_id
        self.directory = directory

    def execute(self, token: CancelToken) -> Optional[Tuple[MonitorTask, TaskAnalysis]]:
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError as exc:
            logger.warning("阶段快照目录无法创建: %s", self.directory, exc_info=True)
            raise TaskError(ERR_MAKE_DIR.format(detail=exc)) from exc
        task = self.db.tasks.get(self.task_id)
        if task is None or task.start_time is None:
            raise TaskError(ERR_NO_TASK)
        snapshots = {}
        for metric in task.metric_types:
            if token.is_cancelled:
                return None
            snapshot = run_history_query(
                self.db, build_request(self.task_id, metric, DEFAULT_RANGE_KEY, analysis=True))
            if snapshot.error is not None:
                # run_history_query 已带堆栈记过日志
                raise TaskError(ERR_READ.format(detail=snapshot.error))
            snapshots[metric] = snapshot
        return task, TaskAnalysis(self.task_id, DEFAULT_RANGE_KEY, snapshots)


@dataclass
class _Job:
    """一次进行中的阶段快照。"""
    task_id: str
    directory: str
    index: int
    at: datetime
    # 当前这一段的线程；它结束时若仍是它，整次写入就结束了
    worker: Any = None


class StageSnapshotWriter(QObject):
    """按请求在后台写阶段快照（报告图片 + 摘要 CSV）。在界面线程使用。

    信号：
        written(str, object): 写好了 (task_id, (图片路径, 摘要路径))
        failed(str, str): 写失败 (task_id, 说明)。同一任务只发一次，之后的失败只记日志
    """

    written = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self, db, manager, threads, clock, parent: Optional[QObject] = None, *,
                 renderer: Optional[Renderer] = None,
                 analysis_worker_factory: Optional[Callable[..., BackgroundTask]] = None,
                 report_worker_factory: Optional[Callable[..., BackgroundTask]] = None):
        """
        Args:
            db: 组合根的 Database 实例（None 表示未连接数据库，每次写入都失败）
            manager: MonitorManager（用 is_maintenance_running / begin_export / end_export）
            threads: 线程登记表（core.shutdown.ThreadRegistry）；None 时自行持有并释放线程
            clock: 提供 now() 的时钟（文件名与"生成于"的时间）
            parent: Qt 父对象
            renderer: 报告图片渲染函数 renderer(task, analysis, generated_at, range_text)
                -> QImage，在界面线程调用（range_text 即"全部（运行中第 N 次阶段快照）"，
                画在信息行）；None 时只写摘要 CSV 并记错误日志
            analysis_worker_factory: 读库段线程的工厂，签名同 StageAnalysisWorker(db,
                task_id, directory)；测试可替换
            report_worker_factory: 写文件段线程的工厂，签名同 ReportWorker(db, job)
        """
        super().__init__(parent)
        self._db = db
        self._manager = manager
        self._threads = threads
        self._clock = clock
        self._renderer = renderer
        self._analysis_factory = analysis_worker_factory or StageAnalysisWorker
        self._report_factory = report_worker_factory or ReportWorker
        self._jobs: Dict[str, _Job] = {}
        self._reported: Set[str] = set()
        # 没有登记表时自己持有线程引用，结束后释放
        self._owned: List[BackgroundTask] = []

    # ========== 命令 ==========

    def write(self, task_id: str, directory: str, index: int) -> bool:
        """请求写第 index 次阶段快照到 directory。返回是否已开始（跳过或立即失败为 False）。"""
        if task_id in self._jobs:
            logger.info("上一次阶段快照尚未写完，跳过本次: task_id=%s index=%d",
                        task_id, index)
            return False
        if self._db is None:
            self._report_failure(task_id, ERR_NO_DATABASE)
            return False
        if self._manager.is_maintenance_running() or not self._manager.begin_export():
            self._report_failure(task_id, ERR_MAINTENANCE)
            return False
        job = _Job(task_id, directory, index, self._clock.now())
        self._jobs[task_id] = job
        logger.info("开始写阶段快照: task_id=%s index=%d dir=%s", task_id, index, directory)
        worker = self._analysis_factory(self._db, task_id, directory)
        worker.result.connect(lambda value, j=job, w=worker: self._on_analysis(j, w, value))
        worker.failed.connect(lambda message, j=job: self._report_failure(j.task_id, message))
        self._run(job, worker)
        return True

    def is_busy(self, task_id: str) -> bool:
        """该任务是否有阶段快照正在写。"""
        return task_id in self._jobs

    def has_reported_failure(self, task_id: str) -> bool:
        """该任务的失败是否已经提示过（之后的失败不再发 failed）。"""
        return task_id in self._reported

    def forget(self, task_id: str) -> None:
        """清掉该任务的"已提示过失败"标记（任务移除时调用；不影响正在写的快照）。"""
        self._reported.discard(task_id)

    # ========== 内部：分段 ==========

    def _on_analysis(self, job: _Job, worker: BackgroundTask, value) -> None:
        if value is None or worker.is_cancelled:
            return
        task, analysis = value
        image = None
        if self._renderer is None:
            logger.error("未配置阶段快照的图片渲染，只写摘要 CSV: task_id=%s", job.task_id)
        else:
            try:
                image = self._renderer(task, analysis, job.at, range_text(job.index))
            except Exception as exc:
                logger.error("阶段快照图片渲染失败: task_id=%s", job.task_id, exc_info=True)
                self._report_failure(job.task_id, ERR_RENDER.format(detail=exc))
                return
        image_path, summary_path = snapshot_paths(job.directory, task, job.index, job.at)
        points = max((s.total_count for s in analysis.snapshots.values()), default=0)
        report_job = ReportJob(
            task=task,
            image_path=image_path if image is not None else None, image=image,
            summary_path=summary_path,
            metadata=tuple(metadata_lines(
                task, exported_at=job.at, range_text=range_text(job.index),
                sample_count=points or None, end_time=task.end_time)),
            summary_rows=tuple(tuple(r) for r in summary_rows(analysis.snapshots)))
        writer = self._report_factory(self._db, report_job)
        writer.result.connect(lambda result, j=job: self._on_written(j, result))
        writer.failed.connect(lambda message, j=job: self._report_failure(j.task_id, message))
        self._run(job, writer)

    def _on_written(self, job: _Job, result) -> None:
        paths = tuple(getattr(result, 'paths', ()) or ())
        logger.info("阶段快照已写出: task_id=%s index=%d paths=%s",
                    job.task_id, job.index, paths)
        self.written.emit(job.task_id, paths)

    def _run(self, job: _Job, worker: BackgroundTask) -> None:
        job.worker = worker
        worker.finished.connect(lambda j=job, w=worker: self._on_worker_finished(j, w))
        if self._threads is not None:
            self._threads.register(worker, name=THREAD_NAME, cancel=worker.cancel,
                                   timeout_ms=THREAD_TIMEOUT_MS)
        else:
            self._owned.append(worker)
        worker.start()

    def _on_worker_finished(self, job: _Job, worker: BackgroundTask) -> None:
        """线程结束（成功、失败或取消）：若下一段没有接上，整次写入到此结束。"""
        if worker in self._owned:
            self._owned.remove(worker)
            worker.deleteLater()
        if job.worker is not worker:
            return
        if self._jobs.get(job.task_id) is job:
            del self._jobs[job.task_id]
        self._manager.end_export()

    def _report_failure(self, task_id: str, message: str) -> None:
        if task_id in self._reported:
            logger.warning("阶段快照写入失败（已提示过，不再提示）: task_id=%s %s",
                           task_id, message)
            return
        logger.warning("阶段快照写入失败: task_id=%s %s", task_id, message)
        self._reported.add(task_id)
        self.failed.emit(task_id, message)
