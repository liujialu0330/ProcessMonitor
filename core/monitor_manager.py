"""
监控管理器
管理所有监控任务的创建、启动、停止等操作；由组合根构造并注入（不是单例）
"""
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from PyQt5 import sip
from PyQt5.QtCore import QObject, pyqtSignal

from core.contracts import StopReason, TaskInfo, TaskStatus
from core.monitor_task import ACTIVE_STATES, LIVE_STATES, MonitorWorker, WorkerState
from core.snapshot_hub import SnapshotHub, default_hub
from data.database import Database
from data.errors import StorageError
from data.models import MARKER_KINDS, Marker, TaskRecord
from data.repos.point_repo import PointQuery
from utils.thread_utils import shutdown_thread
import config

logger = logging.getLogger(__name__)

# 打点默认标签序列：任务还没有基线时为"基线"，之后为"第 N 轮"（N = 用户打点数 + 1）
BASELINE_LABEL = "基线"
# 基线值 = 基线打点之后前 N 个样本的中位数（避免单个尖峰样本带偏参考值）
BASELINE_SAMPLE_COUNT = 5
# 查基线样本的时间窗口 = 周期 × 样本数 × 该倍数 + 余量：容忍调度抖动，又不至于
# 为一个很早的基线把整段历史读出来
_BASELINE_WINDOW_FACTOR = 4
_BASELINE_WINDOW_SLACK = timedelta(seconds=5)


def round_label(n: int) -> str:
    """第 n 轮的默认打点标签。"""
    return f"第 {n} 轮"


def default_marker_kind(default_label: str, label: str) -> str:
    """打点类型由点击时的默认标签决定：默认是"基线"（任务尚无基线）时，用户改了
    名称也仍是基线；名称恰为"基线"时同样记为基线。其余为普通用户打点。"""
    if default_label == BASELINE_LABEL or (label or '').strip() == BASELINE_LABEL:
        return 'baseline'
    return 'user'


def median_of(values: Sequence[float]) -> float:
    """基线值口径：前 BASELINE_SAMPLE_COUNT 个样本的中位数（偶数个取中间两值平均）。"""
    return float(statistics.median(list(values)[:BASELINE_SAMPLE_COUNT]))


@dataclass(frozen=True)
class BaselineSamples:
    """基线打点之后的样本（每指标最多 BASELINE_SAMPLE_COUNT 个，按时间先后）。

    settled 为 True 表示样本不会再增加（窗口之后已有数据，或任务已不再采集），
    不足 N 个也按已有的算；为 False 表示基线是刚打的，调用方应继续用实时数据补足。
    """
    marker: Marker
    samples: Dict[str, List[float]] = field(default_factory=dict)
    settled: bool = False

# WorkerState -> 快照里的 TaskStatus（RUNNING 覆盖暂停中与停止收尾中）
_STATUS_BY_STATE = {
    WorkerState.WAITING: TaskStatus.PENDING,
    WorkerState.RUNNING: TaskStatus.RUNNING,
    WorkerState.PAUSED: TaskStatus.RUNNING,
    WorkerState.STOPPING: TaskStatus.RUNNING,
    WorkerState.STOPPED: TaskStatus.STOPPED,
}


class MonitorManager(QObject):
    """
    监控管理器
    负责管理所有监控任务。任务线程结束（QThread.finished）后由管理器自己移除并
    释放线程对象，界面不需要、也不应该替它决定何时释放。
    """

    # 信号定义
    task_added = pyqtSignal(str)           # 任务添加信号 (task_id)
    task_started = pyqtSignal(str)         # 任务启动信号 (task_id)
    task_stopped = pyqtSignal(str, object)  # 任务停止信号 (task_id, StopReason)
    task_removed = pyqtSignal(str)         # 任务移除信号 (task_id)
    data_updated = pyqtSignal(str, dict)   # 数据更新信号 (task_id, {指标类型: 指标值})
    error_occurred = pyqtSignal(str, str)  # 错误信号 (task_id, error_message)
    # 指标不可用信号 (task_id, {指标类型: UnavailableReason})
    metrics_unavailable = pyqtSignal(str, dict)
    task_limit_reached = pyqtSignal()      # 任务数量达到上限信号
    # 某任务的打点有变化（新增、改名、删除、换基线、暂停/恢复系统打点）(task_id)：
    # 卡片与分析页据此重新读取打点，不必各自轮询
    markers_changed = pyqtSignal(str)

    def __init__(self, db: Database, parent: Optional[QObject] = None, *,
                 snapshot_hub: Optional[SnapshotHub] = None):
        """
        Args:
            db: 数据库实例（与各页面共用同一实例，由组合根注入）
            parent: Qt 父对象（可选）
            snapshot_hub: 全系统快照中心（可选）。None 用进程内共享实例
                core.snapshot_hub.default_hub()（与进程列表共用同一份快照）；
                全部任务线程共用它，按相位对齐采样
        """
        super().__init__(parent)

        self._snapshot_hub = snapshot_hub if snapshot_hub is not None else default_hub()

        # 任务字典 {task_id: MonitorWorker}
        self._tasks: Dict[str, MonitorWorker] = {}

        # 维护锁：数据库清理/压缩期间禁止创建、启动任务
        self._maintenance = False

        # 进行中的导出数：导出线程长时间读库，与压缩数据库互相抢锁，二者互斥
        self._export_count = 0

        # 打点 ID -> 所属任务：改名/删除只给 marker_id，发 markers_changed 需要 task_id。
        # 经 add_marker / list_markers 见过的打点都会登记
        self._marker_owner: Dict[int, str] = {}

        self.db = db

    def create_task(self, pid: int, process_name: str, metric_types: List[str],
                    interval: float = None, note: str = '',
                    create_time: Optional[float] = None) -> Optional[str]:
        """
        创建新的监控任务

        Args:
            pid: 进程ID
            process_name: 进程名称
            metric_types: 监控指标类型列表
            interval: 采集间隔（可选）
            note: 任务备注（可选）
            create_time: 所选进程的创建时间（可选）。任务线程启动时据此确认 PID
                未被别的进程复用；为 None 时跳过比对

        Returns:
            Optional[str]: 任务ID，创建失败（数据库维护中/任务数达上限）返回None
        """
        # 数据库清理/压缩期间不允许新建任务
        if self._maintenance:
            self.error_occurred.emit('', '正在清理数据库，请稍后再试')
            return None

        # 检查任务数量限制
        if len(self._tasks) >= config.MAX_MONITOR_TASKS:
            self.task_limit_reached.emit()
            return None

        # 创建任务（注入与 MonitorManager 相同的数据库实例，避免各自裸建连接）
        task = MonitorWorker(
            pid=pid,
            process_name=process_name,
            metric_types=metric_types,
            interval=interval,
            db=self.db,
            note=note,
            create_time=create_time,
            hub=self._snapshot_hub,
        )

        # 连接任务信号（工作线程发出，经队列在本对象所在的 GUI 线程执行）
        task.data_updated.connect(self._on_task_data_updated)
        task.task_stopped.connect(self._on_task_stopped)
        task.error_occurred.connect(self._on_task_error)
        task.metrics_unavailable.connect(self._on_task_metrics_unavailable)
        task.finished.connect(self._on_worker_finished)

        # 添加到字典
        self._tasks[task.task_id] = task

        # 发送信号
        self.task_added.emit(task.task_id)

        return task.task_id

    def start_task(self, task_id: str) -> bool:
        """
        启动监控任务

        只有尚未启动（WAITING）的任务可以启动：运行中、停止收尾中与已停止的
        线程对象一律拒绝，重新监控应通过 create_task 创建新任务。

        Args:
            task_id: 任务ID

        Returns:
            bool: 启动是否成功
        """
        task = self._tasks.get(task_id)
        if self._maintenance or task is None:
            return False
        if task.state is not WorkerState.WAITING:
            return False
        task.start()
        logger.info("任务启动请求: task_id=%s pid=%s", task_id, task.pid)
        self.task_started.emit(task_id)
        return True

    def stop_task(self, task_id: str) -> bool:
        """
        请求停止监控任务（非阻塞）：只改状态并唤醒线程后立即返回，不等待线程结束。
        任务收尾（flush/写状态）在其工作线程内完成，随后经 task_stopped 信号通知；
        线程结束（finished）后管理器自行移除该任务。

        Args:
            task_id: 任务ID

        Returns:
            bool: 是否已发出停止请求（未启动、已在停止或已停止返回 False）
        """
        task = self._tasks.get(task_id)
        if task and task.is_running():
            task.stop()
            logger.info("任务停止请求已发出: task_id=%s", task_id)
            return True
        return False

    def remove_task(self, task_id: str) -> bool:
        """
        移除监控任务（不阻塞）。

        线程仍在运行（含停止收尾中）时只请求停止，移除交给 finished 回调完成——
        对象在线程真正结束后才被释放；线程没在运行（未启动或已结束）时立即移除。

        Args:
            task_id: 任务ID

        Returns:
            bool: 任务存在返回 True（已移除或将在线程结束后移除）
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.isRunning():
            task.stop()
            return True
        task.stop()   # 从未启动的任务置为 STOPPED，防止之后被误启动
        self._discard(task_id)
        return True

    def pause_task(self, task_id: str, timestamp: Optional[datetime] = None) -> bool:
        """
        暂停监控任务；成功时任务线程对象写入"暂停"系统打点，随后发 markers_changed

        Args:
            task_id: 任务ID
            timestamp: 系统打点时间（缺省取调用时刻，即用户点击的时刻）

        Returns:
            bool: 暂停是否成功
        """
        task = self._tasks.get(task_id)
        if task is None or not task.pause(timestamp):
            return False
        self.markers_changed.emit(task_id)
        return True

    def resume_task(self, task_id: str, timestamp: Optional[datetime] = None) -> bool:
        """
        恢复监控任务；成功时任务线程对象写入"恢复"系统打点，随后发 markers_changed

        Args:
            task_id: 任务ID
            timestamp: 系统打点时间（缺省取调用时刻）

        Returns:
            bool: 恢复是否成功
        """
        task = self._tasks.get(task_id)
        if task is None or not task.resume(timestamp):
            return False
        self.markers_changed.emit(task_id)
        return True

    @property
    def snapshot_hub(self) -> SnapshotHub:
        """全部任务线程共用的全系统快照中心"""
        return self._snapshot_hub

    # ========== 只读快照（界面只用这一组，不接触任务线程对象） ==========

    @staticmethod
    def _snapshot(task: MonitorWorker) -> TaskInfo:
        """由任务线程对象生成只读快照。status/is_stopping 由 WorkerState 派生而非
        任务模型字段：模型里的 stopped 要等收尾写库时才置。"""
        state = task.state
        model = task.task_model
        return TaskInfo(
            task_id=task.task_id,
            pid=task.pid,
            process_name=task.process_name,
            metric_types=tuple(task.metric_types),
            interval=task.interval,
            note=task.note,
            status=_STATUS_BY_STATE[state],
            start_time=model.start_time,
            end_time=model.end_time,
            create_time=task.expected_create_time,
            is_paused=state is WorkerState.PAUSED,
            is_stopping=state is WorkerState.STOPPING,
        )

    def get_task_info(self, task_id: str) -> Optional[TaskInfo]:
        """manager 中某任务的只读快照；任务不在 manager 中（已移除或从未创建）返回 None。
        历史任务请直接查数据库。"""
        task = self._tasks.get(task_id)
        return self._snapshot(task) if task is not None else None

    def list_task_infos(self) -> List[TaskInfo]:
        """manager 中全部任务的只读快照（按创建顺序）"""
        return [self._snapshot(task) for task in self._tasks.values()]

    def is_task_active(self, task_id: str) -> bool:
        """任务是否在采集：已启动且未请求停止（暂停中也算）"""
        task = self._tasks.get(task_id)
        return task is not None and task.state in ACTIVE_STATES

    def is_task_paused(self, task_id: str) -> bool:
        """任务是否处于暂停状态（不在 manager 中返回 False）"""
        task = self._tasks.get(task_id)
        return task is not None and task.state is WorkerState.PAUSED

    def has_running_task_for_pid(self, pid: int) -> bool:
        """该 PID 是否已有在采集（未在停止）的任务"""
        return any(task.pid == pid and task.state in ACTIVE_STATES
                   for task in self._tasks.values())

    # ========== 任务线程对象（core 内部与测试使用，UI 禁用） ==========

    def get_task(self, task_id: str) -> Optional[MonitorWorker]:
        """
        获取监控任务线程对象。UI 禁用：界面请用 get_task_info 等只读快照接口，
        不得持有或调用任务线程。

        Args:
            task_id: 任务ID

        Returns:
            Optional[MonitorWorker]: 任务对象，不存在返回None
        """
        return self._tasks.get(task_id)

    def get_all_tasks(self) -> List[MonitorWorker]:
        """
        获取所有监控任务线程对象。UI 禁用：界面请用 list_task_infos。

        Returns:
            List[MonitorWorker]: 任务列表
        """
        return list(self._tasks.values())

    def get_running_tasks(self) -> List[MonitorWorker]:
        """
        获取所有在采集（未在停止）的任务线程对象。UI 禁用。

        Returns:
            List[MonitorWorker]: 正在运行的任务列表
        """
        return [task for task in self._tasks.values() if task.state in ACTIVE_STATES]

    def get_task_count(self) -> int:
        """
        获取当前任务数量

        Returns:
            int: 任务数量
        """
        return len(self._tasks)

    def can_add_task(self) -> bool:
        """
        检查是否可以添加新任务

        Returns:
            bool: 是否可以添加
        """
        return len(self._tasks) < config.MAX_MONITOR_TASKS

    def stop_all_tasks(self) -> None:
        """
        请求停止所有任务（非阻塞）：给全部任务改状态并唤醒，不等待。
        退出路径随后用 join_all 带超时等待收尾完成。
        """
        for task in list(self._tasks.values()):
            task.stop()

    def join_all(self, timeout_ms: int, *, deadline: Optional[float] = None) -> bool:
        """
        退出路径的 join：对仍在运行的任务线程逐个带超时等待（超时只记日志）。
        入口用 os._exit() 结束进程，若不等待，仍在收尾写库的线程会被中途掐死、
        丢失最后一批数据；超时则以"总期限 + 日志"代替无限等待。

        Args:
            timeout_ms: 每个线程的等待上限（毫秒）
            deadline: 总截止时间（time.monotonic() 口径）。每次等待取 timeout_ms
                与剩余时间的较小者，剩余 ≤0 时不再等待——否则多个卡住的线程会把
                单项超时累加成远超总期限的等待。None 表示只按单项超时

        Returns:
            bool: 全部线程都在超时（及截止时间）前结束返回 True
        """
        all_finished = True
        for task in list(self._tasks.values()):
            if not task.isRunning():
                continue
            wait_ms = timeout_ms
            if deadline is not None:
                wait_ms = min(timeout_ms, int((deadline - time.monotonic()) * 1000))
            if wait_ms <= 0:
                all_finished = False
                logger.error("退出收尾已到总期限，不再等待监控任务线程: %r", task)
                continue
            if not shutdown_thread(task, timeout_ms=wait_ms):
                all_finished = False
        return all_finished

    # ========== 运行状态与维护 ==========

    def running_task_count(self) -> int:
        """仍可能写库的任务数（含暂停中与停止收尾中的任务）"""
        return sum(1 for task in self._tasks.values() if task.state in LIVE_STATES)

    def has_running_tasks(self) -> bool:
        """是否存在仍在运行的任务"""
        return self.running_task_count() > 0

    def begin_maintenance(self) -> bool:
        """
        进入数据库维护状态（清理/压缩前调用）。有运行中任务或导出进行中时拒绝返回
        False（调用方可用 has_running_tasks / has_running_exports 区分原因）；否则置位
        并返回 True，此后 create_task / start_task / begin_export 被拒绝，直到
        end_maintenance。
        """
        if self.has_running_tasks() or self.has_running_exports():
            return False
        self._maintenance = True
        return True

    def end_maintenance(self):
        """退出数据库维护状态"""
        self._maintenance = False

    def is_maintenance_running(self) -> bool:
        """是否处于数据库维护状态"""
        return self._maintenance

    def begin_export(self) -> bool:
        """
        登记一次导出（启动导出线程前调用）。维护进行中时拒绝返回 False；否则计数加一
        并返回 True。每次成功的 begin_export 都必须对应一次 end_export。
        """
        if self._maintenance:
            return False
        self._export_count += 1
        return True

    def end_export(self):
        """注销一次导出（导出线程结束后调用，无论成功、失败还是取消）"""
        self._export_count = max(0, self._export_count - 1)

    def has_running_exports(self) -> bool:
        """是否有导出正在进行"""
        return self._export_count > 0

    # ========== 任务数据与打点 ==========

    def add_marker(self, task_id: str, label: str,
                   timestamp: Optional[datetime] = None,
                   kind: str = 'user') -> Optional[Marker]:
        """
        为任务添加一个打点，成功后发 markers_changed。

        Args:
            task_id: 任务ID
            label: 打点名称（去除首尾空白后不能为空）
            timestamp: 打点时间。界面应传入用户点"打点"的时刻：输入名称要花好几秒，
                取确认时刻会让竖线落在事件之后；为 None 时取当前时间
            kind: 打点类型（MARKER_KINDS）。名称恰为"基线"时一律记为基线；新增基线
                时同一任务原有的基线降为普通打点（每个任务至多一个基线）

        Returns:
            Optional[Marker]: 带数据库 ID 的打点；任务不存在、名称为空或写库失败返回 None
        """
        label = (label or '').strip()
        if not label:
            return None
        if kind not in MARKER_KINDS:
            raise ValueError(f"未知的打点类型 {kind!r}，可选：{', '.join(MARKER_KINDS)}")
        if label == BASELINE_LABEL:
            kind = 'baseline'
        if task_id not in self._tasks and self.db.get_task(task_id) is None:
            return None
        if timestamp is None:
            timestamp = datetime.now()
        try:
            marker_id = self.db.markers.add(task_id, timestamp, label, kind)
        except StorageError:
            logger.error("添加打点失败: task_id=%s", task_id, exc_info=True)
            return None
        self._marker_owner[marker_id] = task_id
        if kind == 'baseline':
            self._demote_other_baselines(task_id, keep_id=marker_id)
        logger.info("添加打点: task_id=%s marker_id=%s kind=%s", task_id, marker_id, kind)
        self.markers_changed.emit(task_id)
        return Marker(task_id=task_id, timestamp=timestamp, label=label,
                      id=marker_id, kind=kind)

    def rename_marker(self, marker_id: int, label: str,
                      task_id: Optional[str] = None) -> bool:
        """
        修改打点名称（不改变类型：基线改名后仍是基线），成功后发 markers_changed。

        Args:
            marker_id: 打点ID
            label: 新名称（去除首尾空白后不能为空）
            task_id: 所属任务（可选）。打点未经本管理器 add_marker/list_markers 见过时
                用它发信号；两边都不知道时照常改库，只是不发信号

        Returns:
            bool: 是否改成功（名称为空、打点不存在或写库失败返回 False）
        """
        label = (label or '').strip()
        if not label:
            return False
        try:
            changed = self.db.markers.update_label(marker_id, label)
        except StorageError:
            logger.error("修改打点失败: marker_id=%s", marker_id, exc_info=True)
            return False
        if changed:
            self._emit_markers_changed_for(marker_id, task_id)
        return changed

    def delete_marker(self, marker_id: int, task_id: Optional[str] = None) -> bool:
        """
        删除打点，成功后发 markers_changed。删掉基线后任务回到"无基线"，
        下一个默认标签重新是"基线"。

        Args:
            marker_id: 打点ID
            task_id: 所属任务（可选，含义同 rename_marker）

        Returns:
            bool: 是否删除成功
        """
        try:
            deleted = self.db.markers.delete(marker_id)
        except StorageError:
            logger.error("删除打点失败: marker_id=%s", marker_id, exc_info=True)
            return False
        if deleted:
            self._emit_markers_changed_for(marker_id, task_id)
            self._marker_owner.pop(marker_id, None)
        return deleted

    def set_baseline(self, task_id: str, marker_id: int) -> bool:
        """
        把任务的某个打点设为基线：原基线降为普通打点，该打点改为基线，随后发
        markers_changed。暂停/恢复系统打点不能设为基线（它们标记的是空洞边界）。

        Returns:
            bool: 是否设置成功（打点不属于该任务、是系统打点或写库失败返回 False）
        """
        markers = self.list_markers(task_id)
        target = next((m for m in markers if m.id == marker_id), None)
        if target is None or target.kind not in ('user', 'baseline'):
            return False
        if target.kind != 'baseline':
            try:
                self.db.markers.set_kind(marker_id, 'baseline')
            except StorageError:
                logger.error("设置基线失败: marker_id=%s", marker_id, exc_info=True)
                return False
        self._demote_other_baselines(task_id, keep_id=marker_id, markers=markers)
        self.markers_changed.emit(task_id)
        return True

    def list_markers(self, task_id: str) -> List[Marker]:
        """任务的全部打点（含系统打点，按时间升序）；读库失败返回空列表。"""
        try:
            markers = self.db.markers.list(task_id)
        except StorageError:
            logger.error("读取打点失败: task_id=%s", task_id, exc_info=True)
            return []
        for marker in markers:
            if marker.id is not None:
                self._marker_owner[marker.id] = task_id
        return markers

    def get_baseline(self, task_id: str,
                     markers: Optional[List[Marker]] = None) -> Optional[Marker]:
        """任务当前的基线打点；没有返回 None。库里万一残留多个，取最后添加的那个。

        Args:
            task_id: 任务ID
            markers: 已读出的该任务打点（可选，省一次查库）
        """
        if markers is None:
            markers = self.list_markers(task_id)
        baselines = [m for m in markers if m.kind == 'baseline']
        return max(baselines, key=lambda m: m.id or 0) if baselines else None

    def next_marker_label(self, task_id: str) -> str:
        """下一个打点的默认标签：尚无基线为"基线"，否则"第 N 轮"（N = 用户打点数 + 1）。
        配套的类型用 default_marker_kind(默认标签, 用户最终输入) 求得。"""
        markers = self.list_markers(task_id)
        if self.get_baseline(task_id, markers) is None:
            return BASELINE_LABEL
        return round_label(sum(1 for m in markers if m.kind == 'user') + 1)

    def baseline_samples(self, task_id: str,
                         baseline: Optional[Marker] = None) -> Optional[BaselineSamples]:
        """
        基线打点之后、各指标的前 BASELINE_SAMPLE_COUNT 个样本（按时间先后）。

        只在有限窗口内取数（周期 × 样本数 × 倍数 + 余量）。窗口之后已有数据，或任务
        已不再采集时 settled=True，不足 N 个也按已有样本算；否则基线是刚打的，调用方
        应继续用实时数据补足。只在打点、换基线时调用，不要在每个采集周期调用。

        Args:
            task_id: 任务ID
            baseline: 基线打点；None 时取任务当前基线

        Returns:
            Optional[BaselineSamples]: 任务没有基线时返回 None
        """
        if baseline is None:
            baseline = self.get_baseline(task_id)
            if baseline is None:
                return None
        task = self._tasks.get(task_id)
        record = task.task_model if task is not None else self.db.get_task(task_id)
        if record is None:
            return BaselineSamples(marker=baseline, settled=True)
        # 尚未启动的任务也算：启动后的样本都在基线之后
        live = task is not None and task.state is not WorkerState.STOPPED
        interval = float(record.interval or config.DEFAULT_INTERVAL)
        window_end = (baseline.timestamp + _BASELINE_WINDOW_SLACK
                      + timedelta(seconds=interval * BASELINE_SAMPLE_COUNT
                                  * _BASELINE_WINDOW_FACTOR))
        samples: Dict[str, List[float]] = {}
        try:
            for metric in record.metric_types:
                points = self.db.points.fetch(PointQuery(
                    task_id, metric, since=baseline.timestamp, until=window_end))
                values = [p.value for p in points[:BASELINE_SAMPLE_COUNT]]
                if values:
                    samples[metric] = values
        except StorageError:
            logger.error("读取基线样本失败: task_id=%s", task_id, exc_info=True)
            return BaselineSamples(marker=baseline, settled=not live)
        last = self.db.get_last_point_timestamp(task_id)
        settled = (not live) or (last is not None and last > window_end)
        return BaselineSamples(marker=baseline, samples=samples, settled=settled)

    def _demote_other_baselines(self, task_id: str, keep_id: int,
                                markers: Optional[List[Marker]] = None) -> None:
        """把除 keep_id 外的基线降为普通打点（每个任务至多一个基线）。失败只记日志：
        get_baseline 在残留多个基线时取最后添加的，结论不会乱。"""
        if markers is None:
            markers = self.list_markers(task_id)
        for marker in markers:
            if marker.kind == 'baseline' and marker.id != keep_id:
                try:
                    self.db.markers.set_kind(marker.id, 'user')
                except StorageError:
                    logger.error("旧基线降级失败: marker_id=%s", marker.id, exc_info=True)

    def _emit_markers_changed_for(self, marker_id: int, task_id: Optional[str]) -> None:
        owner = self._marker_owner.get(marker_id, task_id)
        if owner is None:
            logger.warning("打点 %s 的所属任务未知，未发出 markers_changed", marker_id)
            return
        self.markers_changed.emit(owner)

    def delete_task_data(self, task_id: str) -> bool:
        """
        删除任务的全部数据（任务行、数据点、标记）。
        任务仍在 _tasks 中且可能写库（运行、暂停、停止收尾中）时拒绝，返回 False。
        """
        task = self._tasks.get(task_id)
        if task is not None and task.state in LIVE_STATES:
            return False
        return self.db.delete_task(task_id)

    def load_historical_tasks(self) -> List[TaskRecord]:
        """
        从数据库加载历史任务

        Returns:
            List[TaskRecord]: 历史任务列表
        """
        return self.db.get_all_tasks()

    # ========== 私有方法：信号处理与对象释放 ==========

    def _discard(self, task_id: str) -> None:
        """从字典移除任务、释放线程对象并发出 task_removed。

        调用方保证线程已不在运行。所有权先交给 C++ 再 deleteLater：否则字典里
        最后一个 Python 引用一丢，对象会在当前调用栈内被同步析构。"""
        task = self._tasks.pop(task_id, None)
        if task is None:
            return
        sip.transferto(task, None)
        task.deleteLater()
        self.task_removed.emit(task_id)

    def _on_worker_finished(self) -> None:
        """任务线程结束（QThread.finished，经队列在 GUI 线程执行）：移除并释放所有
        已结束的线程对象。不用 sender() 定位：被提前移除的对象可能已失效，扫描
        isFinished() 同样准确且对重复调用安全。"""
        for task_id, task in list(self._tasks.items()):
            if task.isFinished():
                self._discard(task_id)

    def _on_task_data_updated(self, task_id: str, values: dict):
        """任务数据更新处理"""
        self.data_updated.emit(task_id, values)

    def _on_task_stopped(self, task_id: str, reason: StopReason):
        """任务停止处理"""
        self.task_stopped.emit(task_id, reason)

    def _on_task_error(self, task_id: str, error_msg: str):
        """任务错误处理"""
        self.error_occurred.emit(task_id, error_msg)

    def _on_task_metrics_unavailable(self, task_id: str, reasons: dict):
        """任务指标不可用处理"""
        self.metrics_unavailable.emit(task_id, reasons)


# 单元测试
if __name__ == "__main__":
    import sys
    import os
    import tempfile
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer

    # 冒烟测试使用临时目录数据库，不写项目 data\monitor.db（显式注入 db 参数）
    _tmp_dir = tempfile.mkdtemp(prefix="monitor_manager_smoke_")
    _tmp_db_path = os.path.join(_tmp_dir, "smoke_monitor.db")
    print(f"冒烟测试使用临时数据库: {_tmp_db_path}")

    # 创建应用程序
    app = QApplication(sys.argv)

    # 构造管理器（显式注入临时数据库）
    manager = MonitorManager(db=Database(_tmp_db_path))

    # 连接信号
    def on_task_added(task_id):
        print(f"[任务添加] ID: {task_id[:8]}...")

    def on_task_started(task_id):
        print(f"[任务启动] ID: {task_id[:8]}...")

    def on_task_stopped(task_id, reason):
        print(f"[任务停止] ID: {task_id[:8]}..., 原因: {reason.value}")

    def on_data_updated(task_id, values):
        formatted = ", ".join(f"{k}={v:.2f}" for k, v in values.items())
        print(f"[数据更新] ID: {task_id[:8]}..., 值: {formatted}")

    def on_error(task_id, error_msg):
        print(f"[错误] ID: {task_id[:8]}..., 错误: {error_msg}")

    def on_limit_reached():
        print("[警告] 任务数量已达上限")

    manager.task_added.connect(on_task_added)
    manager.task_started.connect(on_task_started)
    manager.task_stopped.connect(on_task_stopped)
    manager.data_updated.connect(on_data_updated)
    manager.error_occurred.connect(on_error)
    manager.task_limit_reached.connect(on_limit_reached)

    # 测试1: 创建任务
    print("测试1: 创建监控任务")
    current_pid = os.getpid()
    task_id = manager.create_task(
        pid=current_pid,
        process_name="python.exe",
        metric_types=["memory_rss", "cpu_percent"],
        interval=1.0
    )
    print(f"  任务已创建: {task_id[:8] if task_id else 'None'}...")

    # 测试2: 启动任务
    print("\n测试2: 启动任务")
    if task_id:
        manager.start_task(task_id)

    # 测试3: 检查任务状态
    def check_status():
        print("\n测试3: 检查任务状态")
        print(f"  当前任务数: {manager.get_task_count()}")
        print(f"  运行中任务数: {len(manager.get_running_tasks())}")
        print(f"  可添加新任务: {manager.can_add_task()}")

    QTimer.singleShot(2000, check_status)

    # 测试4: 停止任务
    def stop_test():
        print("\n测试4: 停止任务")
        if task_id:
            manager.stop_task(task_id)
        QTimer.singleShot(500, app.quit)

    QTimer.singleShot(4000, stop_test)

    # 运行应用
    sys.exit(app.exec_())
