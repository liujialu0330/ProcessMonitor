"""
监控管理器
管理所有监控任务的创建、启动、停止等操作；由组合根构造并注入（不是单例）
"""
import logging
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PyQt5 import sip
from PyQt5.QtCore import QObject, pyqtSignal

from core.contracts import StopReason, TaskInfo, TaskStatus
from core.group_aggregator import MemberSnapshot
from core.monitor_task import (ACTIVE_STATES, LIVE_STATES, SYSTEM_CLOCK, MonitorWorker,
                               WorkerState)
from core.process_tree import GroupSpec
from core.snapshot_hub import SnapshotHub, default_hub
from core.target_info import TargetInfo
from core.target_info import probe as default_target_probe
from data.database import Database
from data.errors import StorageError
from data.models import (MARKER_KINDS, TARGET_KINDS, TARGET_NAME, TARGET_PID, TARGET_TREE,
                         Marker, QualityStats, TaskRecord)
from data.repos.point_repo import PointQuery
from utils.metrics import PROCESS_COUNT
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

# 被测目标元数据探针线程名前缀（日志与排查用）
TARGET_PROBE_THREAD_PREFIX = "目标元数据探针"


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

# WorkerState -> 快照里的 TaskStatus（RUNNING 覆盖暂停中、等待重启与停止收尾中）
_STATUS_BY_STATE = {
    WorkerState.WAITING: TaskStatus.PENDING,
    WorkerState.RUNNING: TaskStatus.RUNNING,
    WorkerState.PAUSED: TaskStatus.RUNNING,
    WorkerState.WAITING_RESTART: TaskStatus.RUNNING,
    WorkerState.STOPPING: TaskStatus.RUNNING,
    WorkerState.STOPPED: TaskStatus.STOPPED,
}


def _quality_of(task) -> Optional[QualityStats]:
    """任务线程当前的数据质量快照；取不到（尚未调度、替身对象）时为 None。
    快照是给界面与命令行看的附带信息，任何异常都不能让 get_task_info 失败。"""
    snapshot = getattr(task, 'quality_snapshot', None)
    if snapshot is None:
        return None
    try:
        return snapshot()
    except Exception:
        logger.warning("读取任务数据质量失败: task_id=%s", getattr(task, 'task_id', '?'),
                       exc_info=True)
        return None


def _restart_limit_of(task) -> int:
    """任务的续接次数上限（不续接为 0）；替身对象没有该接口时为 0。"""
    reader = getattr(task, 'restart_policy', None)
    if reader is None:
        return 0
    try:
        enabled, limit = reader()
    except Exception:
        return 0
    return int(limit) if enabled else 0


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
    # 暂停 / 恢复成功 (task_id)：无论命令来自监控页、托盘还是命令行（IPC），界面都据此
    # 同步卡片状态
    task_paused = pyqtSignal(str)
    task_resumed = pyqtSignal(str)
    # 进程组任务（tree / name）的成员集合变化 (task_id)：已建成员列表或"有实例的成员"集合与
    # 上个周期不同才发（首个周期发一次）；实例数的涨落不发，那体现在进程总数的采集值里。
    # 当前成员用 group_members(task_id) 取
    members_changed = pyqtSignal(str)
    # 被测目标元数据已探测 (task_id, TargetInfo)：任务卡片据此补上悬停提示里的被测程序行
    target_info_ready = pyqtSignal(str, object)
    # 被测进程退出后进入 / 离开"等待重启" (task_id, 是否在等待)：卡片据此显示"等待重启（第 n/m 次）"
    task_waiting_restart = pyqtSignal(str, bool)
    # 被测进程已重启并续接到同一任务 (task_id, 已续接次数)：PID 已变，卡片可刷新
    task_restarted = pyqtSignal(str, int)
    # 某任务写库遇到磁盘已满 (task_id)：存储守卫据此暂停任务（见 StorageGuard）。先于管理器
    # 自己的兜底暂停发出，守卫才能把该任务记入"因空间不足暂停"
    storage_full_detected = pyqtSignal(str)
    # 内部：探针线程通知本对象所在线程来取结果 (task_id)；结果本身放在 _probe_results 里，
    # 退出路径的 join_all 不经事件循环也能直接取走
    _target_probed = pyqtSignal(str)

    def __init__(self, db: Database, parent: Optional[QObject] = None, *,
                 snapshot_hub: Optional[SnapshotHub] = None,
                 target_probe: Optional[Callable[..., TargetInfo]] = None,
                 clock=None,
                 dpi_scale: Optional[Callable[[], Optional[float]]] = None,
                 tree_source_factory: Optional[Callable[[GroupSpec], object]] = None):
        """
        Args:
            db: 数据库实例（与各页面共用同一实例，由组合根注入）
            parent: Qt 父对象（可选）
            snapshot_hub: 全系统快照中心（可选）。None 用进程内共享实例
                core.snapshot_hub.default_hub()（与进程列表共用同一份快照）；
                全部任务线程共用它，按相位对齐采样
            target_probe: 被测目标元数据探针，签名同 core.target_info.probe
                (pid, *, clock, dpi_scale) -> TargetInfo；测试注入替身
            clock: 探针 captured_at 取时的时钟（只用 now()），缺省为系统时钟
            dpi_scale: 读显示缩放倍数的函数（界面层提供，见 set_dpi_scale_provider）；
                无窗口模式为 None，元数据里缩放留空
            tree_source_factory: 进程组成员来源的工厂 (GroupSpec) -> ProcessTreeSource；
                None 用从快照解析的生产实现；测试注入假实现
        """
        super().__init__(parent)

        self._snapshot_hub = snapshot_hub if snapshot_hub is not None else default_hub()

        # 被测目标元数据：任务启动后在后台线程探测（platform.win32_ver 一次就要约 20 ms，
        # 整个探针 30–80 ms，不放在界面线程），结果经 _target_probed 回到本线程写库。
        # 任务行由采集线程启动时写入，探测完还没落库时 save_meta 返回 False，挂在
        # _pending_meta 里，在 data_updated / task_stopped 时补存
        self._target_probe = target_probe if target_probe is not None else default_target_probe
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        self._dpi_scale = dpi_scale
        self._tree_source_factory = tree_source_factory
        # 传给每个任务线程的额外构造参数（退出码探针 / 重启查找 / 采集器工厂 / 时钟），测试经
        # set_worker_options 注入替身，生产保持缺省
        self._worker_options: Dict[str, object] = {}
        self._probe_threads: Dict[str, threading.Thread] = {}
        self._probe_results: Dict[str, Optional[TargetInfo]] = {}
        self._probe_lock = threading.Lock()
        self._target_infos: Dict[str, TargetInfo] = {}
        self._pending_meta: Dict[str, str] = {}
        self._target_probed.connect(self._on_target_probed)

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
                    create_time: Optional[float] = None, *,
                    target_kind: str = TARGET_PID,
                    resume_on_restart: Optional[bool] = None,
                    restart_limit: Optional[int] = None) -> Optional[str]:
        """
        创建新的监控任务

        Args:
            pid: 进程ID（tree 为根进程；name 忽略，任务行记为 0）
            process_name: 进程名称（name 为要监控的进程名，不区分大小写）
            metric_types: 监控指标类型列表。进程组（tree / name）任务缺少"进程总数"
                （process_count）时自动补在末尾：它是组任务的基本指标，也是 name 任务
                成员归零周期里唯一写入的数据；对组不适用的指标（峰值类、基本优先级）
                原样保留，采集时按"不适用"记为不可用，选择时的剔除由调用方做
                （utils.metrics.filter_group_metrics）
            interval: 采集间隔（可选）
            note: 任务备注（可选）
            create_time: 所选进程的创建时间（可选；tree 为根进程的）。任务线程启动时据此
                确认 PID 未被别的进程复用；为 None 时跳过比对
            target_kind: 监控目标类型（'pid' 单个进程，默认；'tree' 根进程及其全部后代；
                'name' 全部同名进程）。组任务只占 1 个任务配额
            resume_on_restart / restart_limit: 被测进程退出后续接同一任务的策略（pid / tree
                任务有效）；None 表示由库里 tasks.resume_on_restart / restart_limit 决定
                （随自动化方案落库）

        Returns:
            Optional[str]: 任务ID，创建失败（数据库维护中/任务数达上限）返回None
        """
        if target_kind not in TARGET_KINDS:
            raise ValueError(f"未知的监控目标类型 {target_kind!r}，可选：{', '.join(TARGET_KINDS)}")
        group_spec: Optional[GroupSpec] = None
        metric_types = list(metric_types)
        if target_kind == TARGET_TREE:
            group_spec = GroupSpec.tree(pid, create_time)
        elif target_kind == TARGET_NAME:
            group_spec = GroupSpec.by_name(process_name)
            pid, create_time = 0, None
        if group_spec is not None and PROCESS_COUNT not in metric_types:
            metric_types.append(PROCESS_COUNT)

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
            group_spec=group_spec,
            tree_source=(self._tree_source_factory(group_spec)
                         if group_spec is not None and self._tree_source_factory is not None
                         else None),
            resume_on_restart=resume_on_restart,
            restart_limit=restart_limit,
            **self._worker_options,
        )
        self._connect_worker(task)
        return self._register_worker(task)

    def _connect_worker(self, task: MonitorWorker) -> None:
        """连接任务信号（工作线程发出，经队列在本对象所在的 GUI 线程执行）。"""
        task.members_changed.connect(self._on_task_members_changed)
        task.data_updated.connect(self._on_task_data_updated)
        task.task_stopped.connect(self._on_task_stopped)
        task.error_occurred.connect(self._on_task_error)
        task.metrics_unavailable.connect(self._on_task_metrics_unavailable)
        task.markers_changed.connect(self.markers_changed)
        task.waiting_restart_changed.connect(self.task_waiting_restart)
        task.target_restarted.connect(self._on_task_restarted)
        task.storage_full.connect(self._on_task_storage_full)
        task.finished.connect(self._on_worker_finished)

    def _register_worker(self, task: MonitorWorker) -> str:
        """加入任务字典并发 task_added。"""
        self._tasks[task.task_id] = task
        self.task_added.emit(task.task_id)
        return task.task_id

    def set_restart_policy(self, task_id: str, enabled: bool, limit: int) -> bool:
        """直接设定运行中任务的续接策略（覆盖库里的方案值）。任务不存在返回 False。"""
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.set_restart_policy(enabled, limit)
        return True

    def recover_task(self, task_id: str) -> bool:
        """
        工具重启后续采：用库里的任务行重建采集线程（任务 ID、开始时间与已有数据都不变），
        目标进程仍在运行且创建时间一致时才有意义（由调用方事先判定，见 core.session_recovery）。
        线程启动后写"续采"打点；中断期间是数据空洞。已在管理器中、维护中、数量已满返回 False。
        """
        if self._maintenance or task_id in self._tasks:
            return False
        if len(self._tasks) >= config.MAX_MONITOR_TASKS:
            self.task_limit_reached.emit()
            return False
        record = self.db.get_task(task_id)
        if record is None or record.status != TaskStatus.RUNNING.value:
            return False
        group_spec: Optional[GroupSpec] = None
        try:
            if record.target_kind == TARGET_TREE:
                # 进程树以任务行里的当前根为准（发生过重启续接时它已不是 target_spec 里的首个根）
                group_spec = GroupSpec.tree(record.pid, record.create_time)
            elif record.target_kind == TARGET_NAME:
                group_spec = GroupSpec.from_json(record.target_kind, record.target_spec)
        except ValueError:
            logger.error("续采失败：任务目标参数无法还原 task_id=%s", task_id, exc_info=True)
            return False
        restarts = sum(1 for m in self.list_markers(task_id) if m.kind == 'restart')
        task = MonitorWorker(
            pid=record.pid, process_name=record.process_name,
            metric_types=list(record.metric_types), interval=record.interval,
            task_id=record.task_id, db=self.db, note=record.note,
            create_time=record.create_time, hub=self._snapshot_hub,
            group_spec=group_spec,
            tree_source=(self._tree_source_factory(group_spec)
                         if group_spec is not None and self._tree_source_factory is not None
                         else None),
            restart_count=restarts, recovering=True, start_time=record.start_time,
            **self._worker_options)
        self._connect_worker(task)
        self._register_worker(task)
        logger.info("续采任务: task_id=%s pid=%s", task_id, record.pid)
        return self.start_task(task_id)

    def set_worker_options(self, **options) -> None:
        """设定之后新建的任务线程的额外构造参数（exit_probe_factory / restart_finder /
        collector_factory / clock）。只给测试与组合根注入替身用。"""
        self._worker_options = dict(options)

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
        logger.info("任务启动请求: task_id=%s pid=%s target=%s", task_id, task.pid,
                    getattr(task, 'target_kind', TARGET_PID))
        self.task_started.emit(task_id)
        if getattr(task, 'target_kind', TARGET_PID) != TARGET_NAME:
            # 按名监控没有单一的被测进程可探测
            self._start_target_probe(task_id, task.pid)
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
        暂停监控任务；成功时任务线程对象写入"暂停"系统打点，随后发 task_paused 与
        markers_changed

        Args:
            task_id: 任务ID
            timestamp: 系统打点时间（缺省取调用时刻，即用户点击的时刻）

        Returns:
            bool: 暂停是否成功
        """
        task = self._tasks.get(task_id)
        if task is None or not task.pause(timestamp):
            return False
        self.task_paused.emit(task_id)
        self.markers_changed.emit(task_id)
        return True

    def resume_task(self, task_id: str, timestamp: Optional[datetime] = None) -> bool:
        """
        恢复监控任务；成功时任务线程对象写入"恢复"系统打点，随后发 task_resumed 与
        markers_changed

        Args:
            task_id: 任务ID
            timestamp: 系统打点时间（缺省取调用时刻）

        Returns:
            bool: 恢复是否成功
        """
        task = self._tasks.get(task_id)
        if task is None or not task.resume(timestamp):
            return False
        self.task_resumed.emit(task_id)
        self.markers_changed.emit(task_id)
        return True

    @property
    def snapshot_hub(self) -> SnapshotHub:
        """全部任务线程共用的全系统快照中心"""
        return self._snapshot_hub

    # ========== 被测目标元数据 ==========

    def set_dpi_scale_provider(self, provider: Optional[Callable[[], Optional[float]]]) -> None:
        """设置读显示缩放倍数的函数（主窗口构造时传入；它只能在界面线程调用，
        探针启动前在本线程读一次再交给探针线程）。"""
        self._dpi_scale = provider

    def target_info(self, task_id: str) -> Optional[TargetInfo]:
        """任务的被测目标元数据（探测完成后可用；任务移出管理器后为 None，请读库）。"""
        return self._target_infos.get(task_id)

    def _read_dpi_scale(self) -> Optional[float]:
        provider = self._dpi_scale
        if provider is None:
            return None
        try:
            value = provider()
            return float(value) if value else None
        except Exception:
            logger.debug("读取显示缩放失败，元数据里缩放留空", exc_info=True)
            return None

    def _start_target_probe(self, task_id: str, pid: int) -> None:
        """在后台线程探测被测目标元数据。守护线程：探针卡住也不拖住退出（退出路径的
        join_all 只等到总期限）。"""
        dpi_scale = self._read_dpi_scale()
        thread = threading.Thread(
            target=self._run_target_probe, args=(task_id, pid, dpi_scale),
            name=f"{TARGET_PROBE_THREAD_PREFIX}-{task_id[:8]}", daemon=True)
        self._probe_threads[task_id] = thread
        thread.start()

    def _run_target_probe(self, task_id: str, pid: int, dpi_scale: Optional[float]) -> None:
        """探针线程：探测失败只记日志（元数据是附带信息，不影响任务）。"""
        try:
            info = self._target_probe(pid, clock=self._clock, dpi_scale=dpi_scale)
        except Exception:
            logger.warning("被测目标元数据探测失败: task_id=%s pid=%s", task_id, pid,
                           exc_info=True)
            info = None
        with self._probe_lock:
            self._probe_results[task_id] = info
        try:
            self._target_probed.emit(task_id)
        except RuntimeError:
            # 管理器已被销毁（测试收尾或进程退出中），结果无处可交
            logger.debug("管理器已销毁，丢弃元数据探测结果: task_id=%s", task_id)

    def _on_target_probed(self, task_id: str) -> None:
        """探测结果回到本线程：缓存、写库（任务行还没落库时挂起待补存）、通知界面。
        结果已被 join_all 取走时什么也不做。"""
        with self._probe_lock:
            if task_id not in self._probe_results:
                return
            info = self._probe_results.pop(task_id)
        self._probe_threads.pop(task_id, None)
        if info is None:
            return
        if task_id in self._tasks:
            self._target_infos[task_id] = info
        try:
            self._pending_meta[task_id] = info.to_json()
        except Exception:
            logger.warning("被测目标元数据序列化失败: task_id=%s", task_id, exc_info=True)
        else:
            if not self._save_pending_meta(task_id) and task_id not in self._tasks:
                # 任务已结束并移出管理器：任务行若此时还不存在，就再也不会有了
                self._drop_pending_meta(task_id)
        self.target_info_ready.emit(task_id, info)

    def _save_pending_meta(self, task_id: str) -> bool:
        """把挂起的元数据写进 tasks.meta_json。命中任务行或写库失败都不再重试
        （失败重试只会每个周期刷一条同样的错误日志）；任务行尚不存在返回 False、继续挂起。"""
        meta_json = self._pending_meta.get(task_id)
        if meta_json is None:
            return True
        store = getattr(self.db, 'tasks', None)
        if store is None:
            self._pending_meta.pop(task_id, None)
            return False
        try:
            saved = bool(store.save_meta(task_id, meta_json))
        except StorageError:
            logger.error("保存被测目标元数据失败: task_id=%s", task_id, exc_info=True)
            self._pending_meta.pop(task_id, None)
            return False
        if saved:
            self._pending_meta.pop(task_id, None)
            logger.info("被测目标元数据已保存: task_id=%s", task_id)
        return saved

    def _drop_pending_meta(self, task_id: str) -> None:
        if self._pending_meta.pop(task_id, None) is not None:
            logger.warning("任务行始终未落库，放弃保存被测目标元数据: task_id=%s", task_id)

    def _join_target_probes(self, deadline: Optional[float], timeout_ms: int) -> bool:
        """等探针线程结束，并把结果当场交付（退出路径随后就关库，不能等事件循环）。"""
        all_finished = True
        for task_id, thread in list(self._probe_threads.items()):
            wait_s = timeout_ms / 1000.0
            if deadline is not None:
                wait_s = min(wait_s, deadline - time.monotonic())
            if wait_s > 0:
                thread.join(wait_s)
            if thread.is_alive():
                all_finished = False
                logger.error("被测目标元数据探针未在期限内结束: task_id=%s", task_id)
        with self._probe_lock:
            finished = list(self._probe_results)
        for task_id in finished:
            self._on_target_probed(task_id)
        return all_finished

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
            quality=_quality_of(task),
            target_kind=getattr(task, 'target_kind', TARGET_PID),
            members_truncated=getattr(task, 'members_truncated', False),
            waiting_restart=state is WorkerState.WAITING_RESTART,
            restart_count=getattr(task, 'restart_count', 0),
            restart_limit=_restart_limit_of(task),
            exit_code=getattr(task, 'last_exit_code', None),
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
        """该 PID 是否已有在采集（未在停止）的单进程任务（进程树 / 按名任务不算：它们监控的
        是一组进程，与同 PID 的单进程任务并不重复）"""
        return any(task.pid == pid and getattr(task, 'target_kind', TARGET_PID) == TARGET_PID
                   and task.state in ACTIVE_STATES for task in self._tasks.values())

    def group_members(self, task_id: str) -> Tuple[MemberSnapshot, ...]:
        """进程组任务当前的成员快照，供任务卡片用：每个已建序列的成员一项（成员键、显示名、
        当前实例数、最近一个周期的各指标合计值），含本周期没有实例的（实例数为 0），按建立
        顺序排列。pid 任务、任务不在管理器中、还没采集到第一个周期时返回空元组。"""
        task = self._tasks.get(task_id)
        reader = getattr(task, 'group_members', None)
        return reader() if reader is not None else ()

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
        # 任务线程收尾后任务行一定已落库：等探针结束并交付结果，挂起的元数据在这里补存
        if not self._join_target_probes(deadline, timeout_ms):
            all_finished = False
        for task_id in list(self._pending_meta):
            self._save_pending_meta(task_id)
        return all_finished

    # ========== 运行状态与维护 ==========

    def running_task_count(self) -> int:
        """仍可能写库的任务数（含暂停中与停止收尾中的任务）"""
        return sum(1 for task in self._tasks.values() if task.state in LIVE_STATES)

    def held_point_count(self) -> int:
        """因磁盘已满而留在内存、尚未写进数据库的数据点总数（全部任务之和；没有为 0）。
        退出时仍写不进去的这部分会丢失，退出确认据此提示用户。"""
        total = 0
        for task in self._tasks.values():
            reader = getattr(task, 'held_point_count', None)
            if reader is not None:
                total += reader()
        return total

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
        self._target_infos.pop(task_id, None)
        if task_id in self._pending_meta and not self._save_pending_meta(task_id):
            self._drop_pending_meta(task_id)
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

    def _on_task_members_changed(self, task_id: str):
        """进程组成员集合变化：原样转发"""
        self.members_changed.emit(task_id)

    def _on_task_data_updated(self, task_id: str, values: dict):
        """任务数据更新处理：首个周期到达时任务行已落库，挂起的元数据在这里补存"""
        if task_id in self._pending_meta:
            self._save_pending_meta(task_id)
        self.data_updated.emit(task_id, values)

    def _on_task_stopped(self, task_id: str, reason: StopReason):
        """任务停止处理：收尾已写过任务行（一个周期都没采到就结束时元数据在这里补存）；
        仍写不进去说明任务行从未落库，放弃"""
        if task_id in self._pending_meta and not self._save_pending_meta(task_id):
            self._drop_pending_meta(task_id)
        self.task_stopped.emit(task_id, reason)

    def _on_task_restarted(self, task_id: str, _pid: int, count: int) -> None:
        """线程发出的是 (任务, 新 PID, 次数)，对外只给次数：新 PID 经 get_task_info 取。"""
        self.task_restarted.emit(task_id, count)

    def _on_task_storage_full(self, task_id: str) -> None:
        """任务报告磁盘已满：先通知存储守卫（它会暂停全部运行中任务并记入"因空间不足暂停"），
        没有守卫接线时自己暂停这个任务，保证缓冲的数据不再无谓增长。"""
        logger.error("任务写库遇到磁盘已满: task_id=%s", task_id)
        self.storage_full_detected.emit(task_id)
        if not self.is_task_paused(task_id) and self.is_task_active(task_id):
            self.pause_task(task_id)

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
