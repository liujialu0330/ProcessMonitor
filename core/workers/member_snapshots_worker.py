"""
进程组成员曲线的后台加载

分析页对进程组任务（进程树 / 同名进程）除合计外还要读每个成员的每个序列（最多 20 个成员 ×
全部指标），一次刷新可能是上百个查询：不像合计那样"一个指标一个线程"，而是整批放进一个后台
线程顺序跑，既不卡界面线程，也不在每次刷新时同时拉起上百个线程。

同时取回成员概况（task_members 留痕合并成的"显示名 / 出现过的实例数"）供成员下拉与成员结论
使用：留痕读失败只影响显示名（退回成员键），不影响曲线。

load_member_bundle 是与线程无关的同步函数（测试与视图模型的同步执行器直接用它）；
MemberSnapshotsWorker 只是它的线程外壳，结果经 result_ready 以 MemberBundle 返回（失败时
error 非空、快照为空）。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from PyQt5.QtCore import pyqtSignal

from core.history_query import HistorySnapshot, load_member_snapshots
from core.workers.base import BackgroundTask, CancelToken
from data.errors import StorageError

logger = logging.getLogger(__name__)

__all__ = ['MemberBundle', 'MemberRequest', 'MemberSnapshotsWorker', 'load_member_bundle']


@dataclass(frozen=True)
class MemberRequest:
    """一次成员曲线加载：任务、要读的指标（合计里有哪些指标）与同一查询范围。
    range_key / since / until / anchor / table_limit / max_buckets 与合计查询取同样的值，
    口径一致。"""
    task_id: str
    metrics: Tuple[str, ...]
    range_key: str
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    anchor: Optional[datetime] = None
    table_limit: Optional[int] = None
    max_buckets: Optional[int] = None
    request_seq: int = 0

    def options(self) -> dict:
        """交给 build_request 的可选参数（未给的不传，沿用其默认值）。"""
        options = {}
        if self.since is not None:
            options['since'] = self.since
        if self.until is not None:
            options['until'] = self.until
        if self.anchor is not None:
            options['anchor'] = self.anchor
        if self.table_limit is not None:
            options['table_limit'] = self.table_limit
        if self.max_buckets is not None:
            options['max_buckets'] = self.max_buckets
        return options


@dataclass(frozen=True)
class MemberBundle:
    """成员曲线加载结果：snapshots 是 {"指标@成员键": 快照}，summaries 是成员概况
    （MemberSummary，按首次出现排列）；error 非空表示整体失败（snapshots 为空）。"""
    request: MemberRequest
    snapshots: Dict[str, HistorySnapshot] = field(default_factory=dict)
    summaries: tuple = ()
    error: Optional[str] = None


def load_member_bundle(db, request: MemberRequest,
                       is_cancelled: Optional[Callable[[], bool]] = None
                       ) -> Optional[MemberBundle]:
    """同步加载成员曲线与成员概况；is_cancelled 为真时返回 None。"""
    snapshots = load_member_snapshots(
        db, request.task_id, request.metrics, request.range_key, analysis=True,
        is_cancelled=is_cancelled, **request.options())
    if snapshots is None:
        return None
    summaries: tuple = ()
    members = getattr(db, 'members', None)
    if members is not None and snapshots:
        try:
            summaries = tuple(members.list_members(request.task_id))
        except StorageError:
            logger.warning("读取进程组成员概况失败，成员显示名退回成员键: %s",
                           request.task_id, exc_info=True)
    return MemberBundle(request, snapshots, summaries)


class MemberSnapshotsWorker(BackgroundTask):
    """成员曲线加载线程：MemberSnapshotsWorker(db, request)。result_ready 是 run() 的最后
    一个动作，成功与失败都经它返回一个 MemberBundle；取消只是让结果不再发出。"""

    result_ready = pyqtSignal(object)

    def __init__(self, db, request: MemberRequest, parent=None):
        super().__init__(parent, request_seq=request.request_seq)
        self._db = db
        self._request = request

    @property
    def request(self) -> MemberRequest:
        return self._request

    def execute(self, token: CancelToken) -> Optional[MemberBundle]:
        return load_member_bundle(self._db, self._request, lambda: token.is_cancelled)

    def handle_result(self, value: Optional[MemberBundle]) -> None:
        super().handle_result(value)
        if value is not None:
            self.result_ready.emit(value)

    def handle_failure(self, exc: Exception) -> None:
        logger.error("进程组成员曲线加载失败", exc_info=exc)
        message = self.failure_message(exc)
        self.failed.emit(message)
        self.result_ready.emit(MemberBundle(self._request, error=message))
