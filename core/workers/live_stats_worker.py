"""卡片趋势重建：后台读取聚合量，界面只合并有界的最新采样。"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

from core.workers.base import BackgroundTask
from data.repos.analytics_repo import MetricAggregates
from utils.metrics import VALUE_CLASS_LEVEL, value_class


@dataclass(frozen=True)
class LiveStatsRequest:
    task_id: str
    metrics: Tuple[str, ...]
    since: Optional[datetime]
    sequence: int


@dataclass(frozen=True)
class LiveStatsResult:
    request: LiveStatsRequest
    aggregates: Dict[str, MetricAggregates]


class LiveStatsWorker(BackgroundTask):
    def __init__(self, db, request: LiveStatsRequest):
        super().__init__()
        self.db, self.request = db, request

    def execute(self, token):
        aggregates = {}
        for metric in self.request.metrics:
            if token.is_cancelled:
                return None
            if value_class(metric) == VALUE_CLASS_LEVEL:
                aggregates[metric] = self.db.analytics.aggregates(
                    self.request.task_id, metric, since=self.request.since)
        return LiveStatsResult(self.request, aggregates)
