"""
自动打点服务（AutoMarkerService）

按任务的自动化方案（AutomationPlan）定时打点、监听日志文件匹配打点、判断停止条件、
到期请求写阶段快照，并在任务结束后请求自动导出。只经 MonitorManager 的公开命令与信号工作：
add_marker / next_marker_label / list_markers / is_task_paused / stop_task，
task_started / task_stopped / data_updated / markers_changed——不接触任务线程对象。

驱动方式（不另开定时器）：
- 每个采集周期 manager.data_updated 到来时处理该任务；
- 外部再以约 1 秒调用一次 tick()（监控页 Presenter 已有的每秒定时器），保证采集周期
  很长或任务暂停（暂停时不发 data_updated）时也能准时打点、按时长停止。
同一时刻被两条路径各调用一次也不会重复打点：定时打点按计划时刻推进，日志行读过即消费。

暂停语义：暂停期间不打点、不计时；恢复后从恢复时刻重新计时（下一次定时打点在恢复后
满 N 秒）。暂停期间新写入的日志行照常读掉但不打点，不会在恢复后补打。阶段快照与定时
打点同口径：暂停期间不请求、不累计，恢复后满 N 小时才请求下一次。
停止时长按墙钟从任务开始算，暂停的时间也算在内。

阶段快照只发 stage_snapshot_due 信号，不在这里读库写文件（由
core.automation.stage_snapshot.StageSnapshotWriter 在后台执行）。

所有时间经注入的时钟（只用 now()），测试用假时钟驱动，不真实等待。
在界面线程使用（与 manager 同线程）。

故障隔离：tick() 与 data_updated 的处理按任务 try/except——一个任务的方案出问题（日志文件
读不了、计时溢出等）只影响它自己，同一任务的同类异常只记一次 ERROR（之后记 DEBUG），
其余任务照常打点与判断停止条件。
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional, Set

from PyQt5.QtCore import QObject, pyqtSignal

from core.automation.log_watcher import LogWatcher
from core.automation.plan import AutomationPlan
from core.automation.stop_conditions import StopState, should_stop
from core.monitor_manager import default_marker_kind

logger = logging.getLogger(__name__)

# 一次处理最多为日志匹配打多少个点：被测程序刷屏时不至于一次写入成百上千个打点
MAX_LOG_MARKERS_PER_TICK = 10


@dataclass
class _Tracked:
    """一个被跟踪任务的自动化进度。"""
    plan: AutomationPlan
    # 任务开始时刻（停止时长从这里算）
    started_at: datetime
    # 下一次定时打点的计划时刻；不定时打点或暂停中为 None
    next_due: Optional[datetime] = None
    paused: bool = False
    watcher: Optional[LogWatcher] = None
    # kind='user' 的打点数（手动与自动合计）
    rounds: int = 0
    # 开始时刻由 attach 指定（工具重启后续采：从任务原来的开始时间算），之后 task_started
    # 不再把它改成当前时刻
    fixed_start: bool = False
    # 已因停止条件请求停止：不再打点、不再判断
    stop_reason: Optional[str] = None
    # 下一次阶段快照的计划时刻；不写快照或暂停中为 None
    snapshot_due: Optional[datetime] = None
    # 已请求的阶段快照数（下一次的序号 = 它 + 1）
    snapshot_count: int = 0


class AutoMarkerService(QObject):
    """自动打点与停止条件。

    信号：
        auto_stopped(str, str): 已按停止条件请求停止任务 (task_id, 原因代码 AUTO_STOP_*)；
            界面经 ui.formatting.auto_stop_text 显示
        export_requested(str, str): 带"停止后自动导出"的任务已结束（数据已全部落库）
            (task_id, 导出目录)
        stage_snapshot_due(str, str, int): 到了写阶段快照的时刻 (task_id, 快照目录,
            序号从 1 起)；接 StageSnapshotWriter.write
    """

    auto_stopped = pyqtSignal(str, str)
    export_requested = pyqtSignal(str, str)
    stage_snapshot_due = pyqtSignal(str, str, int)

    def __init__(self, manager, clock, parent: Optional[QObject] = None, *,
                 watcher_factory: Optional[Callable[..., LogWatcher]] = None):
        """
        Args:
            manager: MonitorManager（或同接口的替身）
            clock: 提供 now() 的时钟（ui.services.clock.Clock 或 core 的 SystemClock）
            parent: Qt 父对象
            watcher_factory: 创建日志读尾器的工厂，签名同 LogWatcher(path, pattern,
                encoding, clock)；测试可替换
        """
        super().__init__(parent)
        self._manager = manager
        self._clock = clock
        self._watcher_factory = watcher_factory or LogWatcher
        self._tracked: Dict[str, _Tracked] = {}
        # 处理出错已记过 ERROR 的任务（同一任务只记一次，解除跟踪时清除）
        self._failed: Set[str] = set()

        manager.task_started.connect(self._on_task_started)
        manager.task_stopped.connect(self._on_task_stopped)
        manager.data_updated.connect(self._on_data_updated)
        manager.markers_changed.connect(self._on_markers_changed)

    # ========== 跟踪 ==========

    def attach(self, task_id: str, plan: AutomationPlan,
               started_at: Optional[datetime] = None) -> bool:
        """
        开始按方案跟踪任务。计时从此刻起算；任务随后发出 task_started 时以启动时刻
        重新起算（通常在 create_task 之后、start_task 之前 attach）。同一任务再次
        attach 会替换原方案并重新计时。

        started_at：任务的开始时刻，只影响"按时长停止"的计算（墙钟，暂停也算，与
        core.verdict.stop_condition_reached 同口径）。工具重启后续采的任务传入它的原开始
        时间，已经过去的时长才不会被清零；此后 task_started 也不会再改它。定时打点与阶段
        快照的节奏仍从此刻起算。

        Returns:
            bool: 是否开始跟踪（方案没有任何自动化内容时不跟踪，返回 False）
        """
        if plan is None or not plan.needs_service():
            self._tracked.pop(task_id, None)
            return False
        now = self._clock.now()
        tracked = _Tracked(plan=plan, started_at=started_at or now,
                           fixed_start=started_at is not None)
        if plan.has_log_trigger:
            try:
                tracked.watcher = self._watcher_factory(
                    plan.log_path, plan.log_pattern, plan.log_encoding, self._clock)
            except Exception:
                # 方案在入口已校验过；这里兜底（如规则在别处被改坏）只放弃日志触发
                logger.error("日志触发初始化失败，已忽略: task_id=%s path=%s",
                             task_id, plan.log_path, exc_info=True)
        self._restart_timer(tracked, now)
        tracked.rounds = self._count_rounds(task_id)
        self._tracked[task_id] = tracked
        logger.info("自动化方案已挂接: task_id=%s plan=%s", task_id, plan.to_json())
        return True

    def detach(self, task_id: str) -> None:
        """停止跟踪任务（不存在时忽略）。"""
        self._failed.discard(task_id)
        if self._tracked.pop(task_id, None) is not None:
            logger.info("自动化方案已解除: task_id=%s", task_id)

    def is_attached(self, task_id: str) -> bool:
        return task_id in self._tracked

    def plan_of(self, task_id: str) -> Optional[AutomationPlan]:
        tracked = self._tracked.get(task_id)
        return tracked.plan if tracked is not None else None

    def attached_ids(self):
        return list(self._tracked)

    def next_due(self, task_id: str) -> Optional[datetime]:
        """下一次定时打点的计划时刻（测试与诊断用）；暂停中或不定时为 None。"""
        tracked = self._tracked.get(task_id)
        return tracked.next_due if tracked is not None else None

    def next_snapshot_due(self, task_id: str) -> Optional[datetime]:
        """下一次阶段快照的计划时刻（测试与诊断用）；暂停中或不写快照为 None。"""
        tracked = self._tracked.get(task_id)
        return tracked.snapshot_due if tracked is not None else None

    # ========== 驱动 ==========

    def tick(self) -> None:
        """处理全部被跟踪的任务（外部约每秒调用一次）。任务之间互相隔离：一个出错不影响其他。"""
        for task_id in list(self._tracked):
            self._process_safely(task_id)

    def _process_safely(self, task_id: str) -> None:
        try:
            self.process(task_id)
        except Exception:
            if task_id in self._failed:
                logger.debug("自动打点处理再次出错: task_id=%s", task_id, exc_info=True)
            else:
                self._failed.add(task_id)
                logger.error("自动打点处理出错（该任务之后的同类错误不再重复记录）: task_id=%s",
                             task_id, exc_info=True)

    def process(self, task_id: str) -> None:
        """处理一个任务：同步暂停状态 → 定时打点 → 日志打点 → 阶段快照 → 判断停止条件。
        暂停/恢复命令成功后立即调用一次，可让恢复时刻精确到命令发出的那一刻。"""
        tracked = self._tracked.get(task_id)
        if tracked is None or tracked.stop_reason is not None:
            return
        now = self._clock.now()
        self._sync_pause(task_id, tracked, now)
        if tracked.paused:
            if tracked.watcher is not None:
                tracked.watcher.poll()      # 暂停期间的日志行读掉不打点
        else:
            self._timed_marker(task_id, tracked, now)
            self._log_markers(task_id, tracked)
            self._stage_snapshot(task_id, tracked, now)
        if self._tracked.get(task_id) is tracked:
            self._check_stop(task_id, tracked, now)

    # ========== 内部：计时与暂停 ==========

    @staticmethod
    def _restart_timer(tracked: _Tracked, now: datetime) -> None:
        seconds = tracked.plan.timed_marker_seconds
        try:
            tracked.next_due = now + timedelta(seconds=seconds) if seconds else None
        except (OverflowError, ValueError):
            # 方案在入口已限制上限（30 天）；这里兜底，溢出时放弃定时打点
            logger.error("定时打点间隔超出范围，已忽略: %r", seconds)
            tracked.next_due = None
        step = _snapshot_step(tracked.plan)
        try:
            tracked.snapshot_due = now + step if step is not None else None
        except (OverflowError, ValueError):
            logger.error("阶段快照间隔超出范围，已忽略: %r", step)
            tracked.snapshot_due = None

    def _sync_pause(self, task_id: str, tracked: _Tracked, now: datetime) -> None:
        paused = bool(self._manager.is_task_paused(task_id))
        if paused == tracked.paused:
            return
        tracked.paused = paused
        if paused:
            # 暂停：剩余的等待时间作废（暂停期间不累计）
            tracked.next_due = None
            tracked.snapshot_due = None
        else:
            self._restart_timer(tracked, now)

    def _timed_marker(self, task_id: str, tracked: _Tracked, now: datetime) -> None:
        due = tracked.next_due
        if due is None or now < due:
            return
        step = timedelta(seconds=tracked.plan.timed_marker_seconds)
        if now - due < step:
            # 正常情况：打点时刻取计划时刻，不受 tick 抖动影响；节奏锚定在起点
            when = due
            tracked.next_due = due + step
        else:
            # 落后超过一个间隔（休眠唤醒、界面长时间卡住）：在当前时刻打一次，从现在
            # 重新计时，不补打错过的轮次
            when = now
            tracked.next_due = now + step
        self._add_marker(task_id, None, when, source="定时")

    def _stage_snapshot(self, task_id: str, tracked: _Tracked, now: datetime) -> None:
        due = tracked.snapshot_due
        if due is None or now < due:
            return
        step = _snapshot_step(tracked.plan)
        # 与定时打点同理：正常时节奏锚定在起点；落后超过一个周期（休眠唤醒）只请求这一次，
        # 从现在重新计时，不补写错过的快照
        tracked.snapshot_due = due + step if now - due < step else now + step
        tracked.snapshot_count += 1
        logger.info("阶段快照到期: task_id=%s index=%d", task_id, tracked.snapshot_count)
        self.stage_snapshot_due.emit(task_id, tracked.plan.stage_snapshot_dir,
                                     tracked.snapshot_count)

    def _log_markers(self, task_id: str, tracked: _Tracked) -> None:
        if tracked.watcher is None:
            return
        matches = tracked.watcher.poll()
        if len(matches) > MAX_LOG_MARKERS_PER_TICK:
            logger.warning("日志一次匹配 %d 行，只为前 %d 行打点: task_id=%s",
                           len(matches), MAX_LOG_MARKERS_PER_TICK, task_id)
            matches = matches[:MAX_LOG_MARKERS_PER_TICK]
        for match in matches:
            if tracked.stop_reason is not None or self._tracked.get(task_id) is not tracked:
                return
            self._add_marker(task_id, match.label, match.timestamp, source="日志")

    def _add_marker(self, task_id: str, label: Optional[str], when: datetime,
                    source: str) -> None:
        default_label = self._manager.next_marker_label(task_id)
        label = label or default_label
        marker = self._manager.add_marker(
            task_id, label, when, kind=default_marker_kind(default_label, label))
        if marker is None:
            logger.error("自动打点失败（%s）: task_id=%s label=%s", source, task_id, label)
        else:
            logger.info("自动打点（%s）: task_id=%s label=%s", source, task_id, label)

    # ========== 内部：轮数与停止 ==========

    def _count_rounds(self, task_id: str) -> int:
        return sum(1 for m in self._manager.list_markers(task_id) if m.kind == 'user')

    def _check_stop(self, task_id: str, tracked: _Tracked, now: datetime) -> None:
        reason = should_stop(StopState(tracked.started_at, tracked.rounds), tracked.plan, now)
        if reason is None:
            return
        tracked.stop_reason = reason
        tracked.next_due = None
        tracked.snapshot_due = None
        logger.info("满足自动停止条件（%s），请求停止任务: task_id=%s", reason, task_id)
        if self._manager.stop_task(task_id):
            self.auto_stopped.emit(task_id, reason)
        else:
            logger.info("任务已在停止或已结束，未再请求停止: task_id=%s", task_id)

    # ========== manager 信号 ==========

    def _on_task_started(self, task_id: str) -> None:
        tracked = self._tracked.get(task_id)
        if tracked is None:
            return
        now = self._clock.now()
        if not tracked.fixed_start:
            tracked.started_at = now
        tracked.paused = False
        self._restart_timer(tracked, now)

    def _on_task_stopped(self, task_id: str, _reason=None) -> None:
        """任务已结束（收尾已落库）：按方案请求自动导出，然后解除跟踪。"""
        tracked = self._tracked.get(task_id)
        if tracked is None:
            return
        self.detach(task_id)
        plan = tracked.plan
        if plan.export_on_stop and plan.export_dir:
            self.export_requested.emit(task_id, plan.export_dir)

    def _on_data_updated(self, task_id: str, _values=None) -> None:
        if task_id in self._tracked:
            self._process_safely(task_id)

    def _on_markers_changed(self, task_id: str) -> None:
        tracked = self._tracked.get(task_id)
        if tracked is not None:
            tracked.rounds = self._count_rounds(task_id)


def _snapshot_step(plan: AutomationPlan) -> Optional[timedelta]:
    """阶段快照的周期；方案不写快照时为 None。"""
    # 方案在入口已校验过范围；这里兜底非正数与 NaN，免得周期为 0 时每次 tick 都请求
    if not plan.has_stage_snapshot or not plan.stage_snapshot_hours > 0:
        return None
    try:
        return timedelta(hours=plan.stage_snapshot_hours)
    except (OverflowError, ValueError):
        logger.error("阶段快照间隔超出范围，已忽略: %r", plan.stage_snapshot_hours)
        return None
