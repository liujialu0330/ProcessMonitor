"""多任务对比批量查询与导出：每批只占一个后台线程。"""
import csv
import math
import os
import tempfile
from dataclasses import dataclass

from core.comparison import curve_for, matrix_cell, task_label
from core.history_query import run_history_query
from core.report_worker import ReportResult
from core.workers.base import BackgroundTask, TaskError
from utils.metrics import get_metric_display_name, get_metric_unit


class ComparisonQueryWorker(BackgroundTask):
    def __init__(self, db, requests):
        super().__init__()
        self.db, self.requests = db, tuple(requests)

    def execute(self, token):
        results = []
        for request in self.requests:
            if token.is_cancelled:
                return []
            results.append(run_history_query(self.db, request))
        return results


@dataclass(frozen=True)
class ComparisonExportJob:
    path: str
    entries: tuple
    metrics: tuple
    focus_metric: str
    alignment: str
    image: object = None


class ComparisonExportWorker(BackgroundTask):
    def __init__(self, db, job):
        super().__init__()
        self.job = job

    def execute(self, token):
        job = self.job
        directory = os.path.dirname(os.path.abspath(job.path))
        fd, temporary = tempfile.mkstemp(prefix='.comparison-', suffix='.tmp', dir=directory)
        os.close(fd)
        try:
            count = 0
            if job.image is not None:
                if not job.image.save(temporary, 'PNG'):
                    raise TaskError('对比图片保存失败')
            else:
                with open(temporary, 'w', encoding='utf-8-sig', newline='') as output:
                    writer = csv.writer(output)
                    writer.writerow(['任务 ID', '任务', '指标', '原始单位', '每轮均增', '有效轮数', '判定'])
                    for entry in job.entries:
                        for metric in job.metrics:
                            if token.is_cancelled:
                                return None
                            cell = matrix_cell(entry, metric)
                            writer.writerow([entry.task.task_id, task_label(entry.task),
                                             get_metric_display_name(metric), get_metric_unit(metric),
                                             cell.mean_delta, cell.rounds, cell.message or cell.verdict])
                            count += 1
                    writer.writerow([])
                    writer.writerow(['任务 ID', '任务', '焦点指标', '对齐方式', '横坐标', '较起点变化', '说明'])
                    for entry in job.entries:
                        curve = curve_for(entry, job.focus_metric, job.alignment)
                        if not curve.xs:
                            writer.writerow([entry.task.task_id, curve.name, job.focus_metric,
                                             job.alignment, '', '', curve.message])
                        for x, y in zip(curve.xs, curve.ys):
                            if token.is_cancelled:
                                return None
                            writer.writerow([entry.task.task_id, curve.name, job.focus_metric,
                                             job.alignment, x, y if math.isfinite(y) else '', curve.message])
            if token.is_cancelled:
                return None
            os.replace(temporary, job.path)
            return ReportResult((job.path,), count, count)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
