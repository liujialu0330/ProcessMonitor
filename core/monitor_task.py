"""
监控任务模块
单个监控任务的实现（MonitorWorker），继承QThread在后台运行，维护显式状态 WorkerState
"""
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Callable, Dict, Optional, List, Tuple
from PyQt5.QtCore import QThread, pyqtSignal

from core.contracts import StopReason, TaskStatus, UnavailableReason
from core.cycle_stats import CycleStats, Histogram, merge_histograms
from core.exit_forensics import (ExitCodeProbe, NullExitCodeProbe, crash_marker_label,
                                 format_exit_code, is_abnormal_exit, normalize_exit_code,
                                 open_exit_probe)
from core.group_aggregator import MemberSnapshot
from core.group_collector import GroupCollector
from core.process_collector import ProcessCollector
from core.process_tree import GroupSpec
from core.restart_finder import RestartTarget, default_restart_finder, exe_path_of
from core.snapshot_hub import SnapshotHub
from data.mappers import to_ms
from data.models import (TARGET_NAME, TARGET_PID, TARGET_TREE, DataPoint, MemberRecord,
                         QualityStats, TaskRecord)
from data.errors import StorageError, StorageFullError
from utils.metrics import MetricType, is_member_series
import config

if TYPE_CHECKING:
    from data.database import Database

logger = logging.getLogger(__name__)

# flush 失败缓冲上限：超过后丢弃最旧数据，防止长时间写库失败导致内存无界增长
MAX_BUFFER_SIZE = 1000
# 连续 flush 失败达到该次数后，通过 error_occurred 通知 UI 一次（_notified 锁存，避免刷屏）
CONSECUTIVE_FAILURE_NOTIFY_THRESHOLD = 3
# flush 持续失败时的日志节流：首次必记，之后每隔该次数记一条（带累计次数）
FLUSH_FAILURE_LOG_INTERVAL = 100

# 进程组实例留痕（task_members）写库失败时缓冲上限：超出丢弃最旧的，防止库长时间不可写时
# 内存无界增长（留痕只是附带信息，丢几条不影响采集数据）
MAX_PENDING_MEMBER_CHANGES = 5000

# 暂停/恢复时自动写入的系统打点 {kind: 标签}：分析时据此把暂停期间识别为空洞，
# 而不是误判为"进程在这段时间没有变化"
SYSTEM_MARKER_LABELS = {'pause': '暂停', 'resume': '恢复'}
# 被测进程重启续接 / 工具重启后续采时写入的系统打点（标签含动态内容，见 _adopt_restarted_process）
RECOVER_MARKER_LABEL = "工具重启后继续监控"

# 磁盘写满时保留在内存里的数据上限：约一小时的数据点，另设绝对上限防止成员序列很多的进程组
# 把内存撑爆；超出丢弃最旧的并记日志
FULL_HOLD_SECONDS = 3600
FULL_HOLD_MAX_POINTS = 500_000

# 等待被测进程重启时查找新实例的间隔（秒）
RESTART_POLL_INTERVAL_S = 1.0
# 采集发现进程已没了、但它的退出码句柄还没显示"已结束"时，重读退出码的次数与间隔（秒）
EXIT_CODE_READ_ATTEMPTS = 5
EXIT_CODE_READ_INTERVAL_S = 0.1
# 续接策略（是否续接、次数上限）从库里重读的最小间隔（秒）：方案在任务启动后才落库
RESTART_POLICY_REFRESH_S = 30.0
# 进程创建时间的比对容差（秒）：与采集器识别 PID 复用的口径相同
CREATE_TIME_TOLERANCE_S = 0.01


class SystemClock:
    """
    采集线程的默认时钟：单调时钟、墙钟与可唤醒等待都直接用标准库。

    MonitorWorker 的调度（截止时间）、落库时间戳与等待全部经时钟对象，测试与加速
    浸泡注入假时钟即可在不真正睡眠的情况下驱动成千上万个周期。替身只需提供同名的
    三个方法：
    - monotonic() -> float：单调秒数，用于算截止时间、剩余等待与调度延迟；
    - now() -> datetime：落库时间戳（任务开始/结束、数据点、默认打点时刻）；
    - wait(event, timeout) -> bool：等待调度事件，timeout 为 None 表示无超时；
      返回值与 threading.Event.wait 相同（事件已置位为 True）。

    可选的 perf_counter() -> float：高精度计时，只用于量采集与写库耗时（周期统计）。
    Windows 上 time.monotonic 的分辨率约 15.6 ms，量不出 1 ms 级的耗时；替身没有这个
    方法时退回 monotonic()，测试照样能用拨动时钟的方式制造耗时。
    """

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now()

    def wait(self, event: threading.Event, timeout: Optional[float] = None) -> bool:
        return event.wait(timeout)

    def perf_counter(self) -> float:
        return time.perf_counter()


# 无状态，所有任务共用一个实例
SYSTEM_CLOCK = SystemClock()


class WorkerState(Enum):
    """监控线程的显式状态。

    迁移（其余组合一律拒绝，保持原状态）：
        WAITING  --start()-->  RUNNING
        WAITING  --stop()-->   STOPPED   （从未启动，没有线程需要收尾）
        RUNNING  --pause()-->  PAUSED
        PAUSED   --resume()--> RUNNING
        RUNNING/PAUSED --stop() 或主循环自行结束--> STOPPING
        STOPPING --收尾落库完成--> STOPPED
        RUNNING/PAUSED --被测进程退出且允许续接--> WAITING_RESTART
        WAITING_RESTART --找到新进程--> RUNNING        （同一任务续接，不写数据点；进入等待前处于
                                                        暂停的回到 PAUSED，不悄悄恢复采集）
        WAITING_RESTART --stop()--> STOPPING           （按退出原因结束，不算用户中断）
        WAITING_RESTART 中 pause() 被拒绝：没有数据可暂停
    STOPPED 是终态：线程对象不可复用，重新监控必须新建任务。
    """
    WAITING = "waiting"      # 已创建，未启动
    RUNNING = "running"      # 采集中
    PAUSED = "paused"        # 已暂停（线程仍在，等待恢复或停止）
    # 被测进程已退出，线程仍在，等它重新启动后在同一任务里继续记录（期间不采集、不写数据点）
    WAITING_RESTART = "waiting_restart"
    STOPPING = "stopping"    # 已请求停止或已退出主循环，正在收尾（flush/写状态）
    STOPPED = "stopped"      # 收尾已完成，不会再写库


# 已请求运行且未请求停止：is_running() 的口径（暂停中也算）
ACTIVE_STATES = frozenset({WorkerState.RUNNING, WorkerState.PAUSED, WorkerState.WAITING_RESTART})
# 线程仍可能写库：运行计数与维护互斥的口径（含停止收尾中）
LIVE_STATES = frozenset({WorkerState.RUNNING, WorkerState.PAUSED, WorkerState.WAITING_RESTART,
                         WorkerState.STOPPING})


@dataclass(frozen=True)
class TargetExit:
    """被测进程退出的判定结果：退出码（None 为未知）与据此得出的停止原因。"""
    exit_code: Optional[int]
    reason: StopReason


@dataclass
class _PreparedRestart:
    """找到的新进程及为它备好的采集器、退出码句柄（核对身份通过后才接上）。"""
    pid: int
    create_time: Optional[float]
    collector: object
    probe: ExitCodeProbe
    exe_path: Optional[str]
    group_spec: Optional[GroupSpec]


class MonitorWorker(QThread):
    """
    监控线程
    在独立线程中定期采集进程性能指标
    """

    # 信号定义
    data_updated = pyqtSignal(str, dict)    # 数据更新信号 (task_id, {指标类型: 指标值})
    # 带采集时刻的实时样本，发送时可能尚未落库；不能当作提交完成通知。
    sample_updated = pyqtSignal(str, dict, object)
    task_stopped = pyqtSignal(str, object)  # 任务停止信号 (task_id, StopReason)
    error_occurred = pyqtSignal(str, str)   # 错误信号 (task_id, error_message)
    # 指标不可用信号 (task_id, {指标类型: UnavailableReason})：每个指标每个任务只通知一次
    metrics_unavailable = pyqtSignal(str, dict)
    # 进程组任务的成员集合变化 (task_id)：已建成员列表或"有实例的成员"集合与上个周期不同
    # （首个周期也发一次）；pid 任务不发
    members_changed = pyqtSignal(str)
    # 工作线程写了系统打点（异常退出 / 重启 / 续采）(task_id)：界面据此重读打点
    markers_changed = pyqtSignal(str)
    # 进入 / 离开"等待被测进程重启" (task_id, 是否在等待)
    waiting_restart_changed = pyqtSignal(str, bool)
    # 被测进程已重启并续接 (task_id, 新 PID, 已续接次数)
    target_restarted = pyqtSignal(str, int, int)
    # 写库遇到磁盘已满 (task_id)：每次进入"磁盘满"只发一次（成功写入后复位），由管理器转给存储守卫暂停任务
    storage_full = pyqtSignal(str)

    def __init__(self, pid: int, process_name: str, metric_types: List[str],
                 interval: float = None, task_id: str = None, db: "Database" = None,
                 note: str = '', create_time: Optional[float] = None,
                 *, clock: Optional[SystemClock] = None,
                 hub: Optional[SnapshotHub] = None,
                 group_spec: Optional[GroupSpec] = None,
                 tree_source=None,
                 exit_probe_factory: Optional[Callable[[int], ExitCodeProbe]] = None,
                 restart_finder=None,
                 collector_factory: Optional[Callable] = None,
                 resume_on_restart: Optional[bool] = None,
                 restart_limit: Optional[int] = None,
                 restart_count: int = 0,
                 recovering: bool = False,
                 start_time: Optional[datetime] = None):
        """
        初始化监控任务

        Args:
            pid: 进程ID
            process_name: 进程名称
            metric_types: 监控指标类型列表
            interval: 采集间隔（秒），默认使用配置文件中的值
            task_id: 任务ID，默认自动生成
            db: 数据库实例（必填，由 MonitorManager 注入同一实例；保留在原位置以兼容
                位置参数调用，缺省或为 None 时抛 TypeError，不再自建连接）
            note: 任务备注（随任务行一并落库）
            create_time: 用户所选进程的创建时间（来自进程列表）。线程启动时与该 PID
                当前进程比对，不一致说明 PID 已被别的进程复用；为 None（如手动输入
                PID）时跳过比对
            clock: 时钟对象（接口见 SystemClock），None 用系统时钟；测试注入假时钟
                以免真实睡眠
            hub: 快照中心（可选，由 MonitorManager 注入共享实例）。注入后采集器从全系统
                快照取值，且截止时间对齐到 hub 的相位网格（见 _first_deadline），同周期
                任务在同一时刻采样、共用一份快照；None 时行为与旧版相同。hub 的相位原点
                与本线程的截止时间应来自同一单调时钟（生产环境均为 time.monotonic）
            group_spec: 进程组目标（tree / name，见 core.process_tree.GroupSpec）；None 为
                单个进程（pid，行为与旧版完全相同）。给定时 pid / process_name 仍是任务行
                的显示字段（tree 为根进程，name 的 pid 为 0）
            tree_source: 进程组成员来源（ProcessTreeSource），None 用从快照解析的生产实现；
                测试注入假实现
            exit_probe_factory: 按 pid 取得被测进程退出码句柄的函数（接口见
                core.exit_forensics.ExitCodeProbe），None 用 Win32 实现；按名监控不取证
            restart_finder: 等待重启时查找新进程的函数（见 core.restart_finder），None 用
                按名查找的生产实现
            collector_factory: (pid, group_spec) -> 采集器，进程重启后为新进程重建采集器；
                None 用与构造时相同的生产逻辑
            resume_on_restart / restart_limit: 被测进程退出后续接同一任务的策略；None 时每次
                退出前从库里的 tasks.resume_on_restart / restart_limit 读（随方案落库）
            restart_count: 已续接次数（工具重启后续采时由库里的 restart 打点推算）
            recovering: 工具重启后续采：沿用库里的任务行（保留原开始时间，不重置），启动时写
                "续采"打点
            start_time: 续采时任务原来的开始时间
        """
        if db is None:
            raise TypeError("MonitorWorker 需要注入 db（不再自建数据库连接）")
        super().__init__()

        # 任务信息
        self.task_id = task_id or str(uuid.uuid4())
        self.pid = pid
        self.process_name = process_name
        self.metric_types = list(metric_types)
        self.interval = interval or config.DEFAULT_INTERVAL
        self.note = note or ''
        self.expected_create_time = create_time
        self._clock = clock if clock is not None else SYSTEM_CLOCK
        # 量耗时用的计时函数（见 SystemClock 说明）
        self._timer = getattr(self._clock, 'perf_counter', None) or self._clock.monotonic

        # 周期统计：只由工作线程写；收尾时写入 tasks 的数据质量列
        self.cycle_stats = CycleStats(self.interval)

        # 显式状态：GUI 线程（start/stop/pause/resume）与工作线程（主循环、收尾）
        # 都会读写，一律经 _state_lock
        self._state_lock = threading.Lock()
        self._state = WorkerState.WAITING

        # 调度唤醒事件：stop()/pause()/resume() 置位，让等待中的工作线程立即醒来
        self._wake = threading.Event()

        # 数据采集器
        self._hub = hub
        self._tree_source = tree_source
        self._collector_factory = collector_factory
        self.group_spec = group_spec
        self.target_kind = group_spec.kind if group_spec is not None else TARGET_PID
        self.collector = self._build_collector(pid, group_spec)

        # 退出取证与续接：退出码句柄只在 pid / tree 任务取（按名监控没有单一的被测进程）
        self._exit_probe_factory = exit_probe_factory or open_exit_probe
        self._exit_probe: Optional[ExitCodeProbe] = None
        self._probe_unavailable_logged = False
        self._exe_path: Optional[str] = None
        self._restart_finder = restart_finder
        self._policy_lock = threading.Lock()
        self._policy_override = (resume_on_restart, restart_limit)
        self._policy_seed: Optional[Tuple[bool, int]] = None
        self._policy_generation = 0
        self._policy: Tuple[bool, int] = (
            (bool(resume_on_restart), int(restart_limit or 0))
            if resume_on_restart is not None else (False, 0))
        self._policy_read_at: Optional[float] = None
        self.restart_count = max(0, int(restart_count))
        self.last_exit_code: Optional[int] = None
        # 最近一次被测进程退出的判定；等待重启失败时按它结束任务
        self._last_exit: Optional[TargetExit] = None
        # 最后一次成功采样的墙钟时刻（epoch 秒）：退出码句柄读不到退出时刻时，续接查找
        # 以它为"旧进程还活着"的参照
        self._last_sample_wall: Optional[float] = None
        # 进入"等待重启"之前任务是否处于暂停：续接成功后回到暂停，不能悄悄恢复采集
        self._paused_before_wait = False
        # 工具重启后续采：库里已有的数据质量，收尾与快照时在它的基础上累加
        self._prior_quality: Optional[QualityStats] = None
        self._prior_histograms = None
        # 读退出码重试时的等待事件：永不置位，只借时钟的 wait 做可被假时钟推进的睡眠
        self._settle = threading.Event()
        self._recovering = recovering

        # 磁盘写满：保留缓冲时的状态（见 _hold_for_storage_full）
        self._storage_full_latched = False
        self._points_per_cycle = 0

        # 数据缓存（批量保存；SAVE_BATCH_SIZE 固化为1时语义为每周期一批）
        self._data_buffer: List[DataPoint] = []

        # flush 失败重试状态
        self._flush_fail_count = 0   # 连续 flush 失败次数，成功后归零
        self._notified = False       # 是否已因连续失败弹过一次 InfoBar（锁存，成功后复位）

        # 任务行是否已成功写入数据库（失败时在后续 flush 前补写）
        self._task_saved = False

        # 已通知过 UI 的不可用指标（每个指标每个任务只通知一次，避免每周期刷屏）
        self._unavailable_notified = set()

        # 进程组任务：成员快照与实例留痕（task_members）的待写缓冲。留痕只在实例出现或消失时
        # 才有变化，和样本一起在 _flush_buffer 里批量写，不为每个周期单独开事务
        self._group_members: Tuple[MemberSnapshot, ...] = ()
        self._members_truncated = False
        self._group_last_ms: Optional[int] = None
        self._pending_appeared: List[MemberRecord] = []
        self._pending_last_seen: Dict[tuple, tuple] = {}
        self._member_flush_failures = 0

        # 数据库（与 MonitorManager 持有同一实例）
        self.db = db

        # 任务模型
        self.task_model = TaskRecord(
            task_id=self.task_id,
            pid=self.pid,
            process_name=self.process_name,
            metric_types=self.metric_types,
            interval=self.interval,
            start_time=start_time,  # 启动时设置（续采沿用原开始时间）
            end_time=None,
            status=TaskStatus.PENDING,
            note=self.note,
            create_time=create_time,   # 随任务行落库，供事后识别 PID 复用
            target_kind=self.target_kind,
            target_spec=group_spec.to_json() if group_spec is not None else None,
        )

    # ========== 状态 ==========

    @property
    def state(self) -> WorkerState:
        """当前状态（线程安全）"""
        with self._state_lock:
            return self._state

    def _transition(self, allowed_from, target: WorkerState) -> bool:
        """当前状态在 allowed_from 中时迁移到 target 并返回 True，否则不动返回 False"""
        with self._state_lock:
            if self._state not in allowed_from:
                return False
            self._state = target
            return True

    def start(self, priority=QThread.InheritPriority):
        """
        启动线程（重写 QThread.start）：先迁移到 RUNNING 再调用 super().start()，
        避免线程尚未执行到 run() 前 stop() 被提前调用导致状态错乱。
        只有 WAITING 能启动：已停止的线程对象不可复用。
        """
        if not self._transition((WorkerState.WAITING,), WorkerState.RUNNING):
            logger.warning("task_id=%s 状态为 %s，忽略重复启动",
                           self.task_id, self.state.value)
            return
        self._wake.clear()
        super().start(priority)

    def run(self):
        """
        线程运行函数（重写QThread.run）。

        主体放进 try/finally：无论正常结束、主动停止还是意外异常，收尾 _teardown
        都恰好执行一次，且始终在工作线程内（保证"task_stopped 晚于落库完成"）。
        测试可在当前线程直接调用 run()：WAITING 视同已 start()。
        """
        self._transition((WorkerState.WAITING,), WorkerState.RUNNING)
        if self.state is WorkerState.STOPPED:
            # 启动前已被停止（WAITING -> STOPPED）：没有任何东西需要收尾
            return
        stop_reason = StopReason.USER
        try:
            stop_reason = self._run_loop()
        except Exception as e:
            stop_reason = StopReason.INTERNAL_ERROR
            error_msg = f"监控线程发生未预期错误: {e}"
            logger.error(error_msg, exc_info=True)
            self.error_occurred.emit(self.task_id, error_msg)
        finally:
            # 主循环自行结束（进程退出、异常）时也先进入 STOPPING：收尾还要写库，
            # 运行计数与维护互斥必须继续把它算在内
            self._transition(ACTIVE_STATES, WorkerState.STOPPING)
            try:
                self._teardown(stop_reason)
            finally:
                # _teardown 正常会在 emit 前置 STOPPED；这里兜底它被替换或中途异常的情况
                self._transition(LIVE_STATES, WorkerState.STOPPED)
                # 退出码句柄在线程结束时一定关闭（_teardown 被替换或中途异常时的兜底）
                self._close_probe()

    def _run_loop(self) -> StopReason:
        """
        任务主体：登记任务行 -> 预热 -> 定时采集循环。

        Returns:
            StopReason: 停止原因
        """
        clock = self._clock
        if self.task_model.start_time is None or not self._recovering:
            # 续采沿用任务原来的开始时间：它决定任务的整体时间轴
            self.task_model.start_time = clock.now()
        self.task_model.status = TaskStatus.RUNNING
        self._task_saved = self.db.save_task(self.task_model)
        logger.info("任务启动: task_id=%s pid=%s process=%s metrics=%s",
                    self.task_id, self.pid, self.process_name, self.metric_types)

        # 检查进程是否存在
        if not self.collector.is_process_running():
            error_msg = f"进程 {self.process_name} (PID: {self.pid}) 不存在或无法访问"
            self.error_occurred.emit(self.task_id, error_msg)
            return StopReason.PROCESS_NOT_FOUND

        # 进程列表可能已过期：选中到开始之间该 PID 若被别的进程复用，继续采集会把
        # 另一个进程的数据记到所选进程名下。读不到创建时间（如无权读取）时无法确认，
        # 按原进程处理，不误停任务
        actual_create_time = self._collector_create_time(self.collector)
        if self.expected_create_time is not None:
            if (actual_create_time is not None
                    and abs(actual_create_time - self.expected_create_time)
                    > CREATE_TIME_TOLERANCE_S):
                return self._fail_pid_reused()
        elif actual_create_time is not None and self.target_kind != TARGET_NAME:
            # 手动输入 PID 的任务事先没有创建时间：把采集器实际读到的记下来（任务行与内存），
            # 之后的 PID 复用检查、工具重启后的续采与被测进程重启的续接都以它为准
            self._backfill_create_time(actual_create_time)

        # 进程确认存在后立刻取退出码句柄：晚了进程可能已退出，就读不到退出码了
        if not self._open_forensics():
            return self._fail_pid_reused()
        self._refresh_restart_policy(force=True)
        if self._recovering:
            self._load_prior_quality()
            self._add_marker('recover', RECOVER_MARKER_LABEL, clock.now())
            self.markers_changed.emit(self.task_id)

        # 含CPU使用率指标时先预热，丢弃cpu_percent首次返回的无效0值。预热后要隔满
        # 一个周期再采：紧接着采的 CPU 样本统计窗口近乎为零，仍是 0 或量化尖峰
        cpu_primed = MetricType.CPU_PERCENT in self.metric_types
        if cpu_primed:
            self.collector.prime_cpu()

        stop_reason = StopReason.USER

        # 调度：用单调时钟的截止时间驱动，每周期 deadline += interval（有 hub 时为相位
        # 网格上的下一点，见 _next_deadline），避免"采集耗时 + 睡眠"造成的累积漂移；
        # 等待统一走时钟的 wait（默认即 Event.wait），stop/pause/resume 置位事件即可立即
        # 唤醒，无需轮询。不含 CPU 指标时启动即采一次（有 hub 时为下一个整秒网格点）。
        stats = self.cycle_stats
        timer = self._timer
        next_deadline = self._first_deadline(clock.monotonic(), cpu_primed)
        stats.start_segment(next_deadline)
        while True:
            state = self.state
            if state not in ACTIVE_STATES:
                break
            if state is WorkerState.WAITING_RESTART:
                # 等待重启只存在于 _wait_for_restart 内部，回到这里说明它异常结束而状态没复位：
                # 不能空转，按最后一次退出的原因结束任务
                logger.error("task_id=%s 回到主循环时仍处于等待重启状态，按被测进程退出结束",
                             self.task_id)
                stop_reason = (self._last_exit.reason if self._last_exit is not None
                               else StopReason.INTERNAL_ERROR)
                break
            # 暂停：无超时等待，直到 resume()/stop() 唤醒；恢复后立即采一次。
            # 暂停段不计入计划周期
            if state is WorkerState.PAUSED:
                stats.end_segment(clock.monotonic())
                clock.wait(self._wake, None)
                self._wake.clear()
                next_deadline = clock.monotonic()
                if self.state is WorkerState.RUNNING:
                    stats.start_segment(next_deadline)
                    self._flush_retained(stats)
                continue

            # 等到本周期截止时间；被唤醒后重新检查状态（调用方先改状态再 set 事件，
            # 故 clear 之后读到的状态一定是最新的）
            while self.state is WorkerState.RUNNING:
                remaining = next_deadline - clock.monotonic()
                if remaining <= 0:
                    break
                clock.wait(self._wake, remaining)
                self._wake.clear()
            if self.state is not WorkerState.RUNNING:
                continue   # 暂停或停止：回到循环顶部按新状态处理
            self._refresh_restart_policy()

            # 落后超过一个周期（如系统休眠唤醒、采集被严重拖慢）：重置为当前时间，不补采，
            # 跳过的周期计入统计
            now = clock.monotonic()
            behind = now - next_deadline
            if behind > self.interval:
                stats.skip(behind)
                next_deadline = now
            else:
                stats.lag(behind)

            if self._hub is not None:
                # 告诉采集器本周期的截止时间：缓存的快照要不早于它（减容差）才能共用，
                # 否则周期不同的任务会拿到上一个时刻的快照
                self.collector.snapshot_not_before = SnapshotHub.not_before_for(
                    next_deadline, self.interval)

            try:
                begin = timer()
                # 先问退出码句柄：进程退出后句柄还握着它，它在进程表 / 快照里会以"僵尸"
                # 形式多留一阵，只看采集结果会看漏
                values = (None if self._target_signaled()
                          else self.collector.collect_metrics(self.metric_types))
                stats.collected(timer() - begin)

                if values is not None:
                    # 同一采集周期的多个指标共用同一时间戳
                    timestamp = clock.now()
                    self._last_sample_wall = timestamp.timestamp()
                    self._points_per_cycle = max(self._points_per_cycle, len(values))
                    for metric_type, value in values.items():
                        self._data_buffer.append(DataPoint(
                            task_id=self.task_id,
                            timestamp=timestamp,
                            value=value,
                            metric_type=metric_type,
                        ))
                    if self.group_spec is not None:
                        self._after_group_cycle(timestamp)

                    # 发送更新信号（新建dict，避免emit后被修改）；全部指标本周期都不可用
                    # 时 values 为空，没有数据落库，不 emit（否则卡片采集次数虚增）。进程组
                    # 的成员序列不进信号（卡片只看合计，成员经 group_members() 取）
                    if values:
                        self.data_updated.emit(self.task_id, self._signal_values(values))
                        self.sample_updated.emit(self.task_id, self._signal_values(values), timestamp)

                    # 本周期新出现的不可用指标，一次性通知 UI（已通知过的不重复）
                    self._notify_unavailable()

                    # 批量保存（SAVE_BATCH_SIZE=1时语义为每周期一批）
                    if len(self._data_buffer) >= config.SAVE_BATCH_SIZE:
                        begin = timer()
                        self._flush_buffer()
                        stats.wrote(timer() - begin)

                else:
                    # 进程已退出（运行中 PID 被另一个进程复用也走这里，采集器无从区分）
                    outcome = self._handle_target_gone()
                    if self._wants_restart():
                        # 等待期间不采集：这一段不计入计划周期，也不写任何数据点
                        stats.end_segment(clock.monotonic())
                        if self._wait_for_restart(outcome):
                            next_deadline = self._begin_restarted_segment()
                            continue
                    stop_reason = outcome.reason
                    break

            except Exception as e:
                error_msg = f"采集数据时发生错误: {str(e)}"
                logger.error(error_msg, exc_info=True)
                self.error_occurred.emit(self.task_id, error_msg)

            next_deadline = self._next_deadline(next_deadline)

        stats.end_segment(clock.monotonic())
        return stop_reason

    # ========== 采集器与退出取证 ==========

    def _build_collector(self, pid: int, group_spec: Optional[GroupSpec]):
        """按目标建采集器（构造时与被测进程重启后各调一次）。"""
        if self._collector_factory is not None:
            return self._collector_factory(pid, group_spec)
        hub = self._hub
        if group_spec is not None:
            return GroupCollector(
                group_spec, source=self._tree_source, hub=hub,
                max_age_s=SnapshotHub.max_age_for(self.interval),
                label=self.task_id[:8])
        if hub is not None:
            return ProcessCollector(
                pid, hub=hub, max_age_s=SnapshotHub.max_age_for(self.interval))
        return ProcessCollector(pid)

    def _acquire_probe(self, pid: int) -> Tuple[ExitCodeProbe, Optional[str]]:
        """对 pid 取退出码句柄并读它的可执行文件路径（等待重启时用来认"同一个程序"）。
        拿不到句柄只降级为"未知退出码"，整个任务周期只记一次日志。不改动 self 的状态。"""
        try:
            probe = self._exit_probe_factory(pid)
        except Exception:
            logger.warning("task_id=%s 取得退出码句柄时出错，按未知退出码处理", self.task_id,
                           exc_info=True)
            probe = NullExitCodeProbe()
        if not probe.available and not self._probe_unavailable_logged:
            self._probe_unavailable_logged = True
            logger.info("task_id=%s 无法取得被测进程 %s 的退出码句柄（权限不足？），"
                        "它退出后退出码按未知处理", self.task_id, pid)
        try:
            exe_path = exe_path_of(pid)
        except Exception:
            exe_path = None
        return probe, exe_path

    def _open_forensics(self) -> bool:
        """对当前被测进程取退出码句柄并记下可执行文件路径。

        Returns:
            bool: 句柄指向的确是预期的进程。句柄的创建时间（GetProcessTimes）与预期不一致说明
            PID 在确认存在到取句柄之间被复用了，此时关掉句柄返回 False，由调用方按"PID 已被
            复用"处理；没有句柄或读不到创建时间无从核对，按一致处理。
        """
        if self.target_kind == TARGET_NAME or not self.pid:
            return True
        self._close_probe()
        probe, exe_path = self._acquire_probe(self.pid)
        if not self._probe_matches(probe, self.expected_create_time):
            probe.close()
            return False
        self._exit_probe = probe
        self._exe_path = exe_path
        return True

    def _probe_matches(self, probe: ExitCodeProbe, expected: Optional[float]) -> bool:
        """退出码句柄的创建时间与预期一致（任一方未知时视为一致）。"""
        if expected is None:
            return True
        reader = getattr(probe, 'create_time', None)
        if reader is None:
            return True
        try:
            actual = reader()
        except Exception:
            logger.warning("task_id=%s 读取退出码句柄的创建时间失败", self.task_id, exc_info=True)
            return True
        if actual is None or abs(actual - expected) <= CREATE_TIME_TOLERANCE_S:
            return True
        logger.warning("task_id=%s PID %s 的句柄创建时间与预期不符（%.3f ≠ %.3f），按 PID 已被复用处理",
                       self.task_id, self.pid, actual, expected)
        return False

    @staticmethod
    def _collector_create_time(collector) -> Optional[float]:
        """采集器绑定的进程的创建时间；采集器没有这个接口（测试替身）或读不到为 None。"""
        reader = getattr(collector, 'get_create_time', None)
        if reader is None:
            return None
        try:
            return reader()
        except Exception:
            logger.warning("读取采集器绑定进程的创建时间失败", exc_info=True)
            return None

    def _fail_pid_reused(self) -> StopReason:
        error_msg = (f"进程 {self.process_name} (PID: {self.pid}) 已退出，"
                     "该 PID 已被其他进程占用，未开始监控")
        self.error_occurred.emit(self.task_id, error_msg)
        return StopReason.PID_REUSED

    def _backfill_create_time(self, create_time: float) -> None:
        """把采集器读到的创建时间记进内存与任务行（任务行 COALESCE 保留已有值，不会被覆盖掉）。"""
        self.expected_create_time = create_time
        self.task_model.create_time = create_time
        self._task_saved = self.db.save_task(self.task_model)

    def _target_signaled(self) -> bool:
        """退出码句柄是否已显示被测进程结束（零等待）。没有句柄或探针不支持时为假。"""
        probe = self._exit_probe
        reader = getattr(probe, 'has_exited', None) if probe is not None else None
        if reader is None or not probe.available:
            return False
        try:
            return bool(reader())
        except Exception:
            logger.warning("task_id=%s 查询被测进程是否已结束失败", self.task_id, exc_info=True)
            return False

    def _close_probe(self) -> None:
        probe, self._exit_probe = self._exit_probe, None
        if probe is None:
            return
        try:
            probe.close()
        except Exception:
            logger.warning("task_id=%s 关闭退出码句柄失败", self.task_id, exc_info=True)

    def _read_exit_code(self) -> Optional[int]:
        """读被测进程的退出码；没有句柄或读不到为 None。采集发现进程没了而句柄还没显示
        "已结束"时，隔一小会儿重读几次（进程对象的收尾与进程列表的更新不是同一瞬间）。"""
        probe = self._exit_probe
        if probe is None or not probe.available:
            return None
        for attempt in range(EXIT_CODE_READ_ATTEMPTS):
            try:
                code = probe.exit_code()
            except Exception:
                logger.warning("task_id=%s 读取退出码失败", self.task_id, exc_info=True)
                return None
            if code is not None:
                return normalize_exit_code(code)
            if attempt < EXIT_CODE_READ_ATTEMPTS - 1:
                self._clock.wait(self._settle, EXIT_CODE_READ_INTERVAL_S)
        return None

    def _handle_target_gone(self) -> TargetExit:
        """被测进程已退出：读退出码、判定停止原因、异常退出时写"异常退出"打点。
        返回判定结果；是否续接由调用方决定。"""
        code = self._read_exit_code()
        self.last_exit_code = code
        abnormal = is_abnormal_exit(code)
        reason = StopReason.CRASHED if abnormal else StopReason.PROCESS_EXITED
        logger.info("task_id=%s 被测进程已退出: PID=%s 退出码=%s", self.task_id, self.pid,
                    format_exit_code(code))
        if abnormal:
            self._add_marker('crash', crash_marker_label(code), self._clock.now())
            self.markers_changed.emit(self.task_id)
        store = getattr(self.db, 'tasks', None)
        if code is not None and store is not None and self._task_saved:
            try:
                # 先记下退出码：等待重启期间工具被杀，库里仍留有这次退出的证据
                store.save_stop(self.task_id, None, code)
            except Exception:
                logger.error("task_id=%s 记录退出码失败", self.task_id, exc_info=True)
        outcome = TargetExit(code, reason)
        self._last_exit = outcome
        return outcome

    # ========== 被测进程退出后的续接 ==========

    def restart_policy(self) -> Tuple[bool, int]:
        """续接策略 (是否续接, 次数上限)；读缓存，可在任意线程调用。"""
        with self._policy_lock:
            return self._policy

    def set_restart_policy(self, enabled: bool, limit: int) -> None:
        """直接设定续接策略（之后不再从库里读）。"""
        with self._policy_lock:
            self._policy_override = (bool(enabled), int(limit))
            self._policy_seed = None
            self._policy_generation += 1
            self._policy = (bool(enabled), int(limit) if enabled else 0)

    def seed_restart_policy(self, enabled: bool, limit: int) -> None:
        """任务方案尚未落库时先使用已校验的启动策略，不覆盖显式固定策略。"""
        with self._policy_lock:
            if self._policy_override[0] is not None:
                return
            self._policy_seed = (bool(enabled), int(limit) if enabled else 0)
            self._policy_generation += 1
            self._policy = self._policy_seed

    def release_restart_policy_seed(self) -> None:
        """方案成功落库后恢复数据库策略读取，下一次检查不受刷新间隔限制。"""
        with self._policy_lock:
            if self._policy_seed is not None:
                self._policy_seed = None
                self._policy_generation += 1
                self._policy_read_at = None

    def _refresh_restart_policy(self, force: bool = False) -> None:
        """从库里重读续接策略（方案在任务启动后才落库，所以周期性重读；注入了固定策略的不读）。"""
        now = self._clock.monotonic()
        with self._policy_lock:
            if self._policy_override[0] is not None or self._policy_seed is not None:
                return
            if (not force and self._policy_read_at is not None
                    and now - self._policy_read_at < RESTART_POLICY_REFRESH_S):
                return
            self._policy_read_at = now
            generation = self._policy_generation
        loader = getattr(getattr(self.db, 'tasks', None), 'load_restart_policy', None)
        if loader is None:
            return
        try:
            policy = loader(self.task_id)
        except Exception:
            logger.warning("task_id=%s 读取续接策略失败", self.task_id, exc_info=True)
            return
        with self._policy_lock:
            # 读库期间入口可能刚保存新方案或设置固定策略，旧查询结果不能盖回去。
            if generation == self._policy_generation:
                self._policy = policy

    def _wants_restart(self) -> bool:
        """被测进程退出后是否进入"等待重启"：pid / tree 任务、策略开启、未达次数上限、
        且没有被请求停止。"""
        if self.target_kind == TARGET_NAME:
            return False
        if self.state not in (WorkerState.RUNNING, WorkerState.PAUSED):
            return False
        self._refresh_restart_policy(force=True)
        enabled, limit = self.restart_policy()
        if not enabled:
            return False
        if self.restart_count >= limit:
            logger.info("task_id=%s 已续接 %d 次，达到上限，不再等待重启", self.task_id,
                        self.restart_count)
            return False
        return True

    def _wait_for_restart(self, outcome: TargetExit) -> bool:
        """进入 WAITING_RESTART，每秒查一次新进程，直到找到（续接）或被停止。

        等待过程中出任何错（查找、封口留痕、接上新进程）都只记日志并放弃续接：调用方按
        outcome 的退出原因结束任务，而不是让线程带着"等待重启"的状态空转。

        Returns:
            bool: True 已续接（采集器已换成新进程的；状态回到 RUNNING，进入等待前处于暂停的
            回到 PAUSED）；False 被停止或放弃，调用方按 outcome 的退出原因结束任务
        """
        try:
            return self._wait_for_restart_inner()
        except Exception as exc:
            logger.error("task_id=%s 等待被测进程重启时出错，按被测进程退出结束任务（退出原因 %s）",
                         self.task_id, outcome.reason.value, exc_info=True)
            self.error_occurred.emit(self.task_id, f"等待被测进程重启时出错: {exc}")
            try:
                self.waiting_restart_changed.emit(self.task_id, False)
            except Exception:
                logger.warning("task_id=%s 通知退出等待重启失败", self.task_id, exc_info=True)
            return False

    def _wait_for_restart_inner(self) -> bool:
        with self._state_lock:
            if self._state not in (WorkerState.RUNNING, WorkerState.PAUSED):
                return False
            self._paused_before_wait = self._state is WorkerState.PAUSED
            self._state = WorkerState.WAITING_RESTART
        logger.info("task_id=%s 等待被测进程 %s 重新启动（已续接 %d 次，上限 %d%s）",
                    self.task_id, self.process_name, self.restart_count, self._policy[1],
                    "，等待前处于暂停" if self._paused_before_wait else "")
        self.waiting_restart_changed.emit(self.task_id, True)
        target = self._restart_target()
        self._release_group_instances()
        finder = self._restart_finder or default_restart_finder()
        prepared: Optional[_PreparedRestart] = None
        # 找到了但核对身份不通过的候选（它在被接上前一刻已退出、PID 被复用）：不再重复处理
        rejected = set()
        while self.state is WorkerState.WAITING_RESTART:
            try:
                hit = finder(target)
            except Exception:
                logger.warning("task_id=%s 查找重启的进程出错", self.task_id, exc_info=True)
                hit = None
            if hit is not None and (hit.pid, hit.create_time) not in rejected:
                prepared = self._prepare_restarted(hit)
                if prepared is not None:
                    break
                rejected.add((hit.pid, hit.create_time))
            # 唤醒只来自 stop()：事件置位后状态已变，循环条件随即为假
            self._clock.wait(self._wake, RESTART_POLL_INTERVAL_S)
            self._wake.clear()
        with self._state_lock:
            adopted = prepared is not None and self._state is WorkerState.WAITING_RESTART
            if adopted:
                self._state = (WorkerState.PAUSED if self._paused_before_wait
                               else WorkerState.RUNNING)
        if not adopted:
            if prepared is not None:
                prepared.probe.close()
            self.waiting_restart_changed.emit(self.task_id, False)
            return False
        try:
            self._adopt_restarted_process(prepared)
        finally:
            self.waiting_restart_changed.emit(self.task_id, False)
        return True

    def _restart_target(self) -> RestartTarget:
        """续接查找的条件：旧进程的名称、路径、创建 / 退出时刻与"不算重启"的父进程 PID。
        必须在 _release_group_instances 之前调用（进程树任务要在封口前记下旧树的成员）。"""
        exit_time = None
        probe = self._exit_probe
        reader = getattr(probe, 'exit_time', None) if probe is not None else None
        if reader is not None:
            try:
                exit_time = reader()
            except Exception:
                logger.warning("task_id=%s 读取被测进程的退出时刻失败", self.task_id,
                               exc_info=True)
        ancestors = {self.pid}
        if self.group_spec is not None:
            try:
                ancestors.update(info.pid for info in self.collector.alive_instances())
            except Exception:
                logger.warning("task_id=%s 读取旧进程树的成员失败", self.task_id, exc_info=True)
        seen = self._last_sample_wall
        if exit_time is None and seen is None:
            # 一次成功采样也没有：以开始等待的这一刻为参照，更早创建的同名进程都不算
            seen = self._clock.now().timestamp()
        return RestartTarget(
            name=self.process_name, exe_path=self._exe_path, last_pid=self.pid,
            last_create_time=self.expected_create_time, exit_time=exit_time,
            last_seen_time=seen, ancestor_pids=frozenset(pid for pid in ancestors if pid))

    def _release_group_instances(self) -> None:
        """进程树任务：旧根退出后，把留痕里仍存活的实例封口（它们随旧进程树一起结束）。"""
        if self.group_spec is None:
            return
        self._close_members()
        self._flush_members()
        self._group_last_ms = None
        self._group_members = ()

    def _prepare_restarted(self, hit) -> Optional["_PreparedRestart"]:
        """为找到的新进程备好采集器与退出码句柄，并核对身份：句柄与采集器读到的创建时间都要
        与查找结果一致，否则它在被接上前一刻已退出、PID 被别的进程占用，放弃（返回 None，
        继续等）。不改动任务自身的状态。"""
        group_spec = (GroupSpec.tree(hit.pid, hit.create_time)
                      if self.group_spec is not None else None)
        collector = self._build_collector(hit.pid, group_spec)
        try:
            # 记下 tree 根的创建时间（之后据此识别 PID 复用）；新进程若已退出，下个周期会发现
            collector.is_process_running()
        except Exception:
            logger.warning("task_id=%s 确认新进程失败", self.task_id, exc_info=True)
        if self.target_kind != TARGET_NAME:
            probe, exe_path = self._acquire_probe(hit.pid)
        else:
            probe, exe_path = NullExitCodeProbe(), None
        seen = self._collector_create_time(collector)
        if (hit.create_time is not None and seen is not None
                and abs(seen - hit.create_time) > CREATE_TIME_TOLERANCE_S):
            logger.warning("task_id=%s 候选进程 PID %s 的创建时间与查找结果不符，放弃",
                           self.task_id, hit.pid)
            probe.close()
            return None
        if not self._probe_matches(probe, hit.create_time):
            probe.close()
            return None
        return _PreparedRestart(
            pid=hit.pid, create_time=hit.create_time if hit.create_time is not None else seen,
            collector=collector, probe=probe, exe_path=exe_path, group_spec=group_spec)

    def _adopt_restarted_process(self, new: "_PreparedRestart") -> None:
        """续接到新进程：换采集器与退出码句柄、更新任务行的 PID 与创建时间、写"重启"打点。
        任务 ID、开始时间与已有数据都不变；等待期间是数据空洞，不补任何数据点。"""
        old_pid = self.pid
        self._close_probe()
        self.pid = new.pid
        self.expected_create_time = new.create_time
        self.task_model.pid = new.pid
        self.task_model.create_time = new.create_time
        if new.group_spec is not None:
            # target_spec 仍记首次启动的根：它是任务创建时的事实；运行时以新根采集
            self.group_spec = new.group_spec
        self.collector = new.collector
        self._exit_probe = new.probe
        self._exe_path = new.exe_path
        self.restart_count += 1
        self.task_model.status = TaskStatus.RUNNING
        self._task_saved = self.db.save_task(self.task_model)
        self._forget_previous_exit()
        when = self._clock.now()
        self._add_marker('restart', f"第 {self.restart_count + 1} 次启动 PID {new.pid}", when)
        logger.info("task_id=%s 被测进程已重启并续接: PID %s -> %s（第 %d 次续接）",
                    self.task_id, old_pid, new.pid, self.restart_count)
        self.markers_changed.emit(self.task_id)
        self.target_restarted.emit(self.task_id, new.pid, self.restart_count)

    def _forget_previous_exit(self) -> None:
        """新进程已接上：上一个进程的退出码不再代表任务的结局（之后用户停止时不能显示成
        "异常退出"）。那次退出留下的"异常退出"打点仍在，数据不丢。"""
        self.last_exit_code = None
        self._last_exit = None
        store = getattr(self.db, 'tasks', None)
        if store is None or not self._task_saved:
            return
        try:
            store.save_stop(self.task_id, None, None, clear_exit_code=True)
        except Exception:
            logger.error("task_id=%s 清除上一个进程的退出码失败", self.task_id, exc_info=True)

    def _begin_restarted_segment(self) -> float:
        """续接后开始新的计划周期段：与任务刚启动时一样先预热 CPU，返回首个截止时间。"""
        cpu_primed = MetricType.CPU_PERCENT in self.metric_types
        if cpu_primed:
            self.collector.prime_cpu()
        deadline = self._first_deadline(self._clock.monotonic(), cpu_primed)
        self.cycle_stats.start_segment(deadline)
        return deadline

    def _signal_values(self, values: Dict[str, float]) -> Dict[str, float]:
        """data_updated 的载荷：pid 任务原样；进程组只带合计序列（去掉 指标@成员 的序列）。"""
        if self.group_spec is None:
            return dict(values)
        return {k: v for k, v in values.items() if not is_member_series(k)}

    def _after_group_cycle(self, timestamp: datetime) -> None:
        """进程组本周期的收尾：取走成员快照、把实例的出现与消失记进待写缓冲、成员集合变化
        时通知。消失实例的 last_ms 取上个周期的时刻（它最后一次被看到）。"""
        group = getattr(self.collector, 'last_group', None)
        if group is None:
            return
        now_ms = to_ms(timestamp)
        for info in group.appeared:
            self._pending_appeared.append(MemberRecord(
                self.task_id, info.pid, info.create_time, info.name, now_ms, now_ms))
        for info in group.disappeared:
            self._pending_last_seen[info.key] = (
                self.task_id, info.pid, info.create_time,
                self._group_last_ms if self._group_last_ms is not None else now_ms)
        self._group_last_ms = now_ms
        self._group_members = group.members
        self._members_truncated = group.members_truncated
        if group.members_changed:
            self.members_changed.emit(self.task_id)

    def group_members(self) -> Tuple[MemberSnapshot, ...]:
        """进程组当前的成员快照（已建序列的全部成员，含本周期没有实例的）；pid 任务为空。
        可在任意线程调用（引用整体替换，读到的总是某个周期完整的快照）。"""
        return self._group_members

    @property
    def members_truncated(self) -> bool:
        """进程组是否出现过因成员名超过上限而没建序列的进程。"""
        return self._members_truncated

    def _first_deadline(self, now: float, cpu_primed: bool) -> float:
        """
        首个截止时间。

        无 hub：不含 CPU 指标为 now（启动即采），含 CPU 指标为 now + interval（预热后
        隔满一个周期）。
        有 hub：对齐到 hub 的相位网格（原点 = hub 创建时刻），首采用"周期与 1 秒中
        较小者"为网格，比无 hub 时最多晚 1 秒（不会因长周期白等一整个周期）；含 CPU
        指标时从 now + interval 起对齐，差分窗口仍不短于一个周期。之后每次由
        _next_deadline 落到周期网格上，同周期任务从此同时采样。
        """
        hub = self._hub
        start = now + self.interval if cpu_primed else now
        if hub is None:
            return start
        return hub.align(start, min(self.interval, 1.0))

    def _next_deadline(self, deadline: float) -> float:
        """本次截止时间之后的下一个截止时间：无 hub 为 deadline + interval；有 hub 为
        周期网格上的下一点（恢复或落后重置后的那次采样不在网格上，借此回到网格）。"""
        if self._hub is None:
            return deadline + self.interval
        return self._hub.next_slot(deadline, self.interval)

    def _notify_unavailable(self):
        """把本周期新出现且尚未通知过的不可用指标一次性 emit 出去。"""
        unavailable = getattr(self.collector, "last_unavailable", None)
        if not unavailable:
            return
        fresh: Dict[str, UnavailableReason] = {
            metric: reason for metric, reason in unavailable.items()
            if metric not in self._unavailable_notified
        }
        if not fresh:
            return
        self._unavailable_notified.update(fresh)
        logger.warning("task_id=%s 部分指标不可用（不记录数据）: %s",
                       self.task_id, fresh)
        self.metrics_unavailable.emit(self.task_id, fresh)

    def stop(self) -> bool:
        """
        请求停止：RUNNING/PAUSED 迁移到 STOPPING，让 run() 主循环自然退出；
        从未启动（WAITING）直接迁移到 STOPPED。
        收尾工作（flush/写状态/emit）全部在 run() 内部完成，因此本方法近乎立即
        返回，不得劣化。改状态后 set 事件，唤醒正在等待的工作线程（含暂停态）。

        Returns:
            bool: 本次调用是否改变了状态（重复停止返回 False）
        """
        changed = (self._transition(ACTIVE_STATES, WorkerState.STOPPING)
                   or self._transition((WorkerState.WAITING,), WorkerState.STOPPED))
        self._wake.set()
        return changed

    def pause(self, timestamp: Optional[datetime] = None, *, write_marker: bool = True) -> bool:
        """暂停监控；等待重启时记住暂停意图，续接后保持暂停。
        切换成功后写一条"暂停"系统打点，时间为 timestamp（缺省取调用时刻）。"""
        with self._state_lock:
            if self._state is WorkerState.WAITING_RESTART:
                if self._paused_before_wait:
                    return False
                self._paused_before_wait = True
            elif self._state is WorkerState.RUNNING:
                self._state = WorkerState.PAUSED
            else:
                return False
        self._wake.set()
        if write_marker:
            self._write_system_marker('pause', timestamp)
        return True

    def resume(self, timestamp: Optional[datetime] = None, *, write_marker: bool = True) -> bool:
        """恢复监控（仅 PAUSED 可恢复；唤醒暂停等待，工作线程立即采集一次）。
        切换成功后写一条"恢复"系统打点，时间为 timestamp（缺省取调用时刻）。"""
        with self._state_lock:
            if self._state is WorkerState.WAITING_RESTART:
                if not self._paused_before_wait:
                    return False
                self._paused_before_wait = False
            elif self._state is WorkerState.PAUSED:
                self._state = WorkerState.RUNNING
            else:
                return False
        self._wake.set()
        if write_marker:
            self._write_system_marker('resume', timestamp)
        return True

    def _write_system_marker(self, kind: str, timestamp: Optional[datetime]) -> None:
        """写暂停/恢复系统打点。在调用 pause()/resume() 的线程（GUI 线程）同步写库：
        时间取点击时刻，不受工作线程调度延迟影响。写库失败只记日志——打点只是
        辅助信息，不能因此让暂停/恢复本身失败。"""
        when = timestamp if timestamp is not None else self._clock.now()
        self._add_marker(kind, SYSTEM_MARKER_LABELS[kind], when)

    def _add_marker(self, kind: str, label: str, when: datetime) -> None:
        """写一条系统打点；失败只记日志。"""
        try:
            self.db.markers.add(self.task_id, when, label, kind)
        except Exception:
            logger.error("task_id=%s 写入%s打点失败", self.task_id, label, exc_info=True)

    def is_running(self) -> bool:
        """已请求运行且未请求停止（暂停中也算）。只由状态派生，不与 isRunning()
        做或运算：否则停止收尾期间会被冲回 True，等它变 False 的调用方会挂起"""
        return self.state in ACTIVE_STATES

    def is_paused(self) -> bool:
        """任务是否已暂停"""
        with self._state_lock:
            return (self._state is WorkerState.PAUSED
                    or (self._state is WorkerState.WAITING_RESTART and self._paused_before_wait))

    def is_waiting_restart(self) -> bool:
        """被测进程已退出，任务正在等它重新启动"""
        return self.state is WorkerState.WAITING_RESTART

    def get_task_info(self) -> TaskRecord:
        """获取任务信息"""
        return self.task_model

    def quality_snapshot(self) -> Optional[QualityStats]:
        """当前的采集数据质量（可在任意线程调用；运行中的段算到此刻为止）；
        还没开始调度（未启动、启动即失败）时为 None"""
        stats = self.cycle_stats
        if not stats.has_data:
            return None
        return self._with_prior_quality(stats.summary(self._clock.monotonic()))

    def held_point_count(self) -> int:
        """因磁盘已满而留在内存、尚未写进数据库的数据点数（没有这种情况为 0）。
        可在任意线程调用；退出时仍写不进去的这部分会丢失，退出确认据此提示用户。"""
        return len(self._data_buffer) if self._storage_full_latched else 0

    def _with_prior_quality(self, quality: QualityStats) -> QualityStats:
        """工具重启后续采：周期数累加，直方图合并后重新取 p95；历史记录没有分布时
        保守取已有 p95 与本段的较大值。没有历史质量时原样返回。"""
        prior = self._prior_quality
        if prior is None:
            return quality

        def worst(a: Optional[int], b: Optional[int]) -> Optional[int]:
            values = [v for v in (a, b) if v is not None]
            return max(values) if values else None

        histograms = self._combined_histograms()

        def percentile(name, old, new):
            return (Histogram.restore(histograms[name]).p95_ms()
                    if histograms is not None else worst(old, new))

        return QualityStats(
            planned_cycles=prior.planned_cycles + quality.planned_cycles,
            done_cycles=prior.done_cycles + quality.done_cycles,
            missed_cycles=prior.missed_cycles + quality.missed_cycles,
            lag_p95_ms=percentile('lag', prior.lag_p95_ms, quality.lag_p95_ms),
            collect_p95_ms=percentile('collect', prior.collect_p95_ms, quality.collect_p95_ms),
            write_p95_ms=percentile('write', prior.write_p95_ms, quality.write_p95_ms))

    def _combined_histograms(self):
        if self._prior_quality is not None and self._prior_histograms is None:
            return None
        return merge_histograms(self.cycle_stats.histograms(), self._prior_histograms)

    def _load_prior_quality(self) -> None:
        """续采启动时读一次库里已有的数据质量（见 _with_prior_quality）；失败只记日志。"""
        try:
            self._prior_quality = self.db.tasks.load_quality(self.task_id)
            self._prior_histograms = self.db.tasks.load_quality_histograms(self.task_id)
        except Exception:
            logger.warning("task_id=%s 读取已有数据质量失败，本次只记续采之后的", self.task_id,
                           exc_info=True)

    def _teardown(self, reason: StopReason):
        """
        收尾（由 run() 的 finally 在工作线程内调用且仅调用一次）：
        最后一次 flush -> 写 stopped 状态 -> 写数据质量（周期统计）-> emit task_stopped。
        各步骤互不连累：任何一步异常都只记日志，task_stopped 一定会发出。

        Args:
            reason: 停止原因
        """
        # 仍存活的实例以最后一个周期为"最后一次见到"
        self._close_members()
        # 收尾 flush 没有下一轮重试机会，失败也要继续走完收尾流程
        try:
            self._flush_buffer(is_teardown=True)
        except Exception:
            logger.error("task_id=%s 收尾 flush 异常", self.task_id, exc_info=True)

        self.task_model.end_time = self._clock.now()
        self.task_model.status = TaskStatus.STOPPED
        try:
            if self._task_saved:
                self.db.update_task_status(
                    self.task_id, TaskStatus.STOPPED.value, self.task_model.end_time)
            else:
                # 启动时任务行没写成功：用最终状态补写一次，避免任务行缺失
                self._task_saved = self.db.save_task(self.task_model)
        except Exception:
            logger.error("task_id=%s 写入停止状态失败", self.task_id, exc_info=True)
        self._save_quality()
        self._save_stop(reason)
        self._close_probe()
        logger.info("任务停止: task_id=%s 原因=%s", self.task_id, reason.value)

        # 先置 STOPPED 再发信号：界面收到 task_stopped 时运行计数已不含本任务
        # （此后线程不再写库，维护互斥也随之放行）
        self._transition(LIVE_STATES, WorkerState.STOPPED)
        self.task_stopped.emit(self.task_id, reason)

    def _save_stop(self, reason: StopReason) -> None:
        """收尾时记录停止原因与被测进程的退出码（UI 导出证据包、命令行状态据此取真实停止原因）。
        失败只记日志。"""
        store = getattr(self.db, 'tasks', None)
        if store is None or not self._task_saved:
            return
        text = (format_exit_code(self.last_exit_code)
                if reason is StopReason.CRASHED else None)
        try:
            store.save_stop(self.task_id, reason.value, self.last_exit_code, text)
        except Exception:
            logger.error("task_id=%s 写入停止原因失败", self.task_id, exc_info=True)

    def _save_quality(self) -> None:
        """收尾时把周期统计写入 tasks 的数据质量列。主循环异常退出时活动段可能未结束，
        这里按此刻补结束；失败只记日志（数据质量只是辅助信息，不能连累收尾）。"""
        stats = self.cycle_stats
        try:
            stats.end_segment(self._clock.monotonic())
            if not stats.has_data or not self._task_saved:
                return
            quality = self._with_prior_quality(stats.summary())
            self.task_model.quality = quality
            self.db.tasks.save_quality(self.task_id, quality,
                                       histograms=self._combined_histograms())
            logger.info("task_id=%s 数据质量: 计划 %d 完成 %d 跳过 %d 周期，"
                        "调度延迟 p95 %s ms，采集 p95 %s ms，写库 p95 %s ms",
                        self.task_id, quality.planned_cycles, quality.done_cycles,
                        quality.missed_cycles, quality.lag_p95_ms,
                        quality.collect_p95_ms, quality.write_p95_ms)
        except Exception:
            logger.error("task_id=%s 写入数据质量失败", self.task_id, exc_info=True)

    def _ensure_task_row(self) -> bool:
        """确保任务行已入库（启动时 save_task 失败的补写，走 upsert）"""
        if not self._task_saved:
            self._task_saved = self.db.save_task(self.task_model)
        return self._task_saved

    def _flush_buffer(self, is_teardown: bool = False):
        """
        将缓冲区数据保存到数据库。

        写数据点前先确保任务行存在（sample_count 随数据点写入同事务更新，没有任务行
        会丢计数）。失败时保留缓冲，交给下一采集周期（或下一次显式调用）重试，不丢数据；
        缓冲超过 MAX_BUFFER_SIZE 时丢弃最旧的数据并记日志；连续失败达到阈值
        经 error_occurred 通知 UI 一次（_notified 锁存，成功后复位）。
        失败日志做节流：首次与此后每 FLUSH_FAILURE_LOG_INTERVAL 次才记一条，避免
        数据库长时间不可用时每个周期刷屏。

        Args:
            is_teardown: 是否为收尾阶段的最后一次 flush（无重试机会，失败需明确记日志）
        """
        points_ok = True
        if self._data_buffer:
            points_ok = self._flush_points(is_teardown)
        if points_ok or is_teardown:
            self._flush_members(is_teardown)

    def _flush_points(self, is_teardown: bool) -> bool:
        """把缓冲的数据点写库；成功返回 True（缓冲已清空），失败返回 False（缓冲保留）。"""
        success = self._ensure_task_row() and self.db.save_data_points(self._data_buffer)

        if success:
            if self._flush_fail_count:
                logger.info("task_id=%s flush 重试成功（此前连续失败 %d 次），落库 %d 条",
                            self.task_id, self._flush_fail_count, len(self._data_buffer))
            self._data_buffer.clear()
            self._flush_fail_count = 0
            self._notified = False
            self._storage_full_latched = False
            return True

        # 磁盘已满：与普通失败不同，数据留在内存里等空间回升，并通知守卫暂停任务
        if (not is_teardown and isinstance(
                getattr(self.db, 'last_save_failure', None), StorageFullError)):
            self._hold_for_storage_full()
            return False

        # 失败：缓冲保留，等待下一轮重试
        self._flush_fail_count += 1
        if (self._flush_fail_count == 1
                or self._flush_fail_count % FLUSH_FAILURE_LOG_INTERVAL == 0):
            logger.error("task_id=%s flush 失败（连续第%d次），缓冲保留待重试，当前缓冲 %d 条",
                         self.task_id, self._flush_fail_count, len(self._data_buffer))

        if len(self._data_buffer) > MAX_BUFFER_SIZE:
            dropped = len(self._data_buffer) - MAX_BUFFER_SIZE
            del self._data_buffer[:dropped]
            logger.error("task_id=%s flush 缓冲超过上限 %d 条，丢弃最旧 %d 条数据",
                         self.task_id, MAX_BUFFER_SIZE, dropped)

        if (not is_teardown
                and self._flush_fail_count >= CONSECUTIVE_FAILURE_NOTIFY_THRESHOLD
                and not self._notified):
            self._notified = True
            self.error_occurred.emit(
                self.task_id,
                f"数据连续 {self._flush_fail_count} 次保存失败，可能丢失部分历史数据"
            )

        if is_teardown:
            # 收尾阶段失败没有下一轮重试机会，明确记录未落库条数
            logger.error("task_id=%s 任务收尾 flush 失败，%d 条数据未落库",
                         self.task_id, len(self._data_buffer))
        return False

    def _hold_for_storage_full(self) -> None:
        """磁盘已满：保留缓冲（不丢数据），按"约一小时的数据"限制上限，并向管理器报告一次，
        由存储守卫暂停任务；用户恢复后下一次写库先补写这些数据（见 _flush_retained）。"""
        self._flush_fail_count += 1
        cycles = max(1, math.ceil(FULL_HOLD_SECONDS / self.interval))
        cap = min(FULL_HOLD_MAX_POINTS,
                  max(MAX_BUFFER_SIZE, self._points_per_cycle * cycles))
        if len(self._data_buffer) > cap:
            dropped = len(self._data_buffer) - cap
            del self._data_buffer[:dropped]
            logger.error("task_id=%s 磁盘已满期间缓冲超过上限 %d 条（约 %d 秒的数据），"
                         "丢弃最旧 %d 条", self.task_id, cap, FULL_HOLD_SECONDS, dropped)
        if not self._storage_full_latched:
            self._storage_full_latched = True
            logger.error("task_id=%s 写库遇到磁盘已满，保留 %d 条数据在内存并请求暂停任务",
                         self.task_id, len(self._data_buffer))
            self.storage_full.emit(self.task_id)

    def _flush_retained(self, stats: CycleStats) -> None:
        """恢复采集（暂停后）时先把保留的缓冲写库，再采新数据；仍写不进去就保持保留，
        下一次采集后的写库失败会再次报告磁盘已满。"""
        if not self._data_buffer:
            return
        # 允许再次报告：用户恢复时磁盘可能仍是满的
        self._storage_full_latched = False
        begin = self._timer()
        try:
            self._flush_buffer()
        except Exception:
            logger.error("task_id=%s 恢复后补写保留的数据失败", self.task_id, exc_info=True)
            return
        stats.wrote(self._timer() - begin)

    def _close_members(self) -> None:
        """收尾：把仍存活的进程组实例的 last_ms 记为最后一个周期的时刻。"""
        if self.group_spec is None or self._group_last_ms is None:
            return
        try:
            for info in self.collector.alive_instances():
                self._pending_last_seen[info.key] = (
                    self.task_id, info.pid, info.create_time, self._group_last_ms)
        except Exception:
            logger.error("task_id=%s 收尾记录进程组实例失败", self.task_id, exc_info=True)

    def _flush_members(self, is_teardown: bool = False) -> None:
        """把待写的实例出现 / 消失一个事务写进 task_members。失败保留缓冲（下次 flush 重试），
        日志节流同数据点；缓冲超限丢弃最旧的。留痕只是附带信息，失败不影响采集。"""
        if not (self._pending_appeared or self._pending_last_seen):
            return
        store = getattr(self.db, 'members', None)
        if store is None:
            self._pending_appeared.clear()
            self._pending_last_seen.clear()
            return
        try:
            store.apply(list(self._pending_appeared), list(self._pending_last_seen.values()))
        except StorageError:
            self._member_flush_failures += 1
            if (self._member_flush_failures == 1
                    or self._member_flush_failures % FLUSH_FAILURE_LOG_INTERVAL == 0):
                logger.error("task_id=%s 保存进程组成员留痕失败（连续第%d次），保留待重试",
                             self.task_id, self._member_flush_failures, exc_info=True)
            overflow = len(self._pending_appeared) - MAX_PENDING_MEMBER_CHANGES
            if overflow > 0:
                del self._pending_appeared[:overflow]
            if is_teardown:
                logger.error("task_id=%s 收尾时进程组成员留痕未落库", self.task_id)
            return
        self._member_flush_failures = 0
        self._pending_appeared.clear()
        self._pending_last_seen.clear()


# 旧名兼容：保留一个版本，新代码请用 MonitorWorker
MonitorTask = MonitorWorker


# 单元测试
if __name__ == "__main__":
    import sys
    import os
    import tempfile
    from PyQt5.QtWidgets import QApplication
    from data.database import Database

    # 冒烟测试使用临时目录数据库，不写项目 data\monitor.db（显式注入 db 参数，不再
    # 改写 config.DB_PATH）
    _tmp_dir = tempfile.mkdtemp(prefix="monitor_task_smoke_")
    _tmp_db_path = os.path.join(_tmp_dir, "smoke_monitor.db")
    print(f"冒烟测试使用临时数据库: {_tmp_db_path}")

    # 创建应用程序
    app = QApplication(sys.argv)

    # 获取当前进程PID
    current_pid = os.getpid()
    print(f"测试监控当前进程: PID={current_pid}")

    # 创建监控任务（多指标，显式注入临时数据库）
    task = MonitorWorker(
        pid=current_pid,
        process_name="python.exe",
        metric_types=["memory_rss", "cpu_percent", "num_threads"],
        interval=1.0,
        db=Database(_tmp_db_path)
    )

    # 连接信号
    def on_data_updated(task_id, values):
        formatted = ", ".join(f"{k}={v:.2f}" for k, v in values.items())
        print(f"[数据更新] 任务: {task_id[:8]}..., 值: {formatted}")

    def on_task_stopped(task_id, reason):
        print(f"[任务停止] 任务: {task_id[:8]}..., 原因: {reason.value}")
        app.quit()

    def on_error(task_id, error_msg):
        print(f"[错误] 任务: {task_id[:8]}..., 错误: {error_msg}")

    task.data_updated.connect(on_data_updated)
    task.task_stopped.connect(on_task_stopped)
    task.error_occurred.connect(on_error)

    # 启动任务
    print("启动监控任务...")
    task.start()

    # 5秒后停止
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(5000, task.stop)

    # 运行应用
    sys.exit(app.exec_())
