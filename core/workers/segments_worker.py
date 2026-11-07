"""
逐启动段判定的后台加载

被测进程重启续接过的任务，分析页的整体判定要逐段做（见 core.verdict）：更早的各段每段都要
再对每个序列查一遍，查询量随段数与成员数增长，不能放在界面线程。与 pmctl、证据包用同一个
core.history_query.load_segments，口径一致；这里只是它的线程外壳。

没有重启的任务只有一段、不需要查库，由视图模型直接用已有快照构造，不会走到这里。

load_segments_bundle 是与线程无关的同步函数（测试与视图模型的同步执行器直接用它）；
SegmentsWorker 把结果经 result_ready 以 SegmentsBundle 返回（失败时 error 非空、segments 为空）。
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Mapping, Optional, Tuple

from PyQt5.QtCore import pyqtSignal

from core.history_query import HistorySnapshot, SegmentAnalysis, load_segments
from core.workers.base import BackgroundTask, CancelToken

logger = logging.getLogger(__name__)

__all__ = ['SegmentsBundle', 'SegmentsRequest', 'SegmentsWorker', 'load_segments_bundle']


@dataclass(frozen=True, eq=False)
class SegmentsRequest:
    """一次分段判定：任务、指标，以及这一批合计 / 成员快照（最后一段直接用它们作依据，
    reuse_last 为假时最后一段也重查）。快照只读，可安全跨线程传递。"""
    task_id: str
    metrics: Tuple[str, ...]
    snapshots: Mapping[str, HistorySnapshot]
    member_snapshots: Mapping[str, HistorySnapshot] = field(default_factory=dict)
    reuse_last: bool = True
    request_seq: int = 0


@dataclass(frozen=True, eq=False)
class SegmentsBundle:
    """分段判定结果；error 非空表示失败（segments 为空）。"""
    request: SegmentsRequest
    segments: List[SegmentAnalysis] = field(default_factory=list)
    error: Optional[str] = None


def load_segments_bundle(db, request: SegmentsRequest,
                         is_cancelled: Optional[Callable[[], bool]] = None
                         ) -> Optional[SegmentsBundle]:
    """同步做逐段判定；is_cancelled 为真时返回 None。查询失败（StorageError）向外抛。"""
    segments = load_segments(
        db, request.task_id, request.metrics, request.snapshots, request.member_snapshots,
        is_cancelled=is_cancelled, reuse_last=request.reuse_last)
    if segments is None:
        return None
    return SegmentsBundle(request, segments)


class SegmentsWorker(BackgroundTask):
    """分段判定线程：SegmentsWorker(db, request)。result_ready 是 run() 的最后一个动作，
    成功与失败都经它返回一个 SegmentsBundle；取消只是让结果不再发出。"""

    result_ready = pyqtSignal(object)

    def __init__(self, db, request: SegmentsRequest, parent=None):
        super().__init__(parent, request_seq=request.request_seq)
        self._db = db
        self._request = request

    @property
    def request(self) -> SegmentsRequest:
        return self._request

    def execute(self, token: CancelToken) -> Optional[SegmentsBundle]:
        return load_segments_bundle(self._db, self._request, lambda: token.is_cancelled)

    def handle_result(self, value: Optional[SegmentsBundle]) -> None:
        super().handle_result(value)
        if value is not None:
            self.result_ready.emit(value)

    def handle_failure(self, exc: Exception) -> None:
        logger.error("分段判定加载失败", exc_info=exc)
        message = self.failure_message(exc)
        self.failed.emit(message)
        self.result_ready.emit(SegmentsBundle(self._request, error=message))
