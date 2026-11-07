"""
监控任务模块
单个监控任务的实现（MonitorWorker），继承QThread在后台运行，维护显式状态 WorkerState
"""
import logging
import threading
import time
import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Dict, Optional, List
from PyQt5.QtCore import QThread, pyqtSignal

from core.contracts import StopReason, TaskStatus, UnavailableReason
from core.process_collector import ProcessCollector
from core.snapshot_hub import SnapshotHub
from data.models import TaskRecord, DataPoint
from utils.metrics import MetricType
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

# 暂停/恢复时自动写入的系统打点 {kind: 标签}：分析时据此把暂停期间识别为空洞，
# 而不是误判为"进程在这段时间没有变化"
SYSTEM_MARKER_LABELS = {'pause': '暂停', 'resume': '恢复'}


class SystemClock:
    """
    采集线程的默认时钟：单调时钟、墙钟与可唤醒等待都直接用标准库。

    MonitorWorker 的调度（截止时间）、落库时间戳与等待全部经时钟对象，测试与加速
    浸泡注入假时钟即可在不真正睡眠的情况下驱动成千上万个周期。替身只需提供同名的
    三个方法：
    - monotonic() -> float：单调秒数，只用于算截止时间与剩余等待；
    - now() -> datetime：落库时间戳（任务开始/结束、数据点、默认打点时刻）；
    - wait(event, timeout) -> bool：等待调度事件，timeout 为 None 表示无超时；
      返回值与 threading.Event.wait 相同（事件已置位为 True）。
    """

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now()

    def wait(self, event: threading.Event, timeout: Optional[float] = None) -> bool:
        return event.wait(timeout)


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
    STOPPED 是终态：线程对象不可复用，重新监控必须新建任务。
    """
    WAITING = "waiting"      # 已创建，未启动
    RUNNING = "running"      # 采集中
    PAUSED = "paused"        # 已暂停（线程仍在，等待恢复或停止）
    STOPPING = "stopping"    # 已请求停止或已退出主循环，正在收尾（flush/写状态）
    STOPPED = "stopped"      # 收尾已完成，不会再写库


# 已请求运行且未请求停止：is_running() 的口径（暂停中也算）
ACTIVE_STATES = frozenset({WorkerState.RUNNING, WorkerState.PAUSED})
# 线程仍可能写库：运行计数与维护互斥的口径（含停止收尾中）
LIVE_STATES = frozenset({WorkerState.RUNNING, WorkerState.PAUSED, WorkerState.STOPPING})


class MonitorWorker(QThread):
    """
    监控线程
    在独立线程中定期采集进程性能指标
    """

    # 信号定义
    data_updated = pyqtSignal(str, dict)    # 数据更新信号 (task_id, {指标类型: 指标值})
    task_stopped = pyqtSignal(str, object)  # 任务停止信号 (task_id, StopReason)
    error_occurred = pyqtSignal(str, str)   # 错误信号 (task_id, error_message)
    # 指标不可用信号 (task_id, {指标类型: UnavailableReason})：每个指标每个任务只通知一次
    metrics_unavailable = pyqtSignal(str, dict)

    def __init__(self, pid: int, process_name: str, metric_types: List[str],
                 interval: float = None, task_id: str = None, db: "Database" = None,
                 note: str = '', create_time: Optional[float] = None,
                 *, clock: Optional[SystemClock] = None,
                 hub: Optional[SnapshotHub] = None):
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

        # 显式状态：GUI 线程（start/stop/pause/resume）与工作线程（主循环、收尾）
        # 都会读写，一律经 _state_lock
        self._state_lock = threading.Lock()
        self._state = WorkerState.WAITING

        # 调度唤醒事件：stop()/pause()/resume() 置位，让等待中的工作线程立即醒来
        self._wake = threading.Event()

        # 数据采集器
        self._hub = hub
        if hub is not None:
            self.collector = ProcessCollector(
                pid, hub=hub, max_age_s=SnapshotHub.max_age_for(self.interval))
        else:
            self.collector = ProcessCollector(pid)

        # 数据缓存（批量保存；SAVE_BATCH_SIZE 固化为1时语义为每周期一批）
        self._data_buffer: List[DataPoint] = []

        # flush 失败重试状态
        self._flush_fail_count = 0   # 连续 flush 失败次数，成功后归零
        self._notified = False       # 是否已因连续失败弹过一次 InfoBar（锁存，成功后复位）

        # 任务行是否已成功写入数据库（失败时在后续 flush 前补写）
        self._task_saved = False

        # 已通知过 UI 的不可用指标（每个指标每个任务只通知一次，避免每周期刷屏）
        self._unavailable_notified = set()

        # 数据库（与 MonitorManager 持有同一实例）
        self.db = db

        # 任务模型
        self.task_model = TaskRecord(
            task_id=self.task_id,
            pid=self.pid,
            process_name=self.process_name,
            metric_types=self.metric_types,
            interval=self.interval,
            start_time=None,  # 启动时设置
            end_time=None,
            status=TaskStatus.PENDING,
            note=self.note,
            create_time=create_time,   # 随任务行落库，供事后识别 PID 复用
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

    def _run_loop(self) -> StopReason:
        """
        任务主体：登记任务行 -> 预热 -> 定时采集循环。

        Returns:
            StopReason: 停止原因
        """
        clock = self._clock
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
        if self.expected_create_time is not None:
            actual_create_time = self.collector.get_create_time()
            if (actual_create_time is not None
                    and abs(actual_create_time - self.expected_create_time) > 0.01):
                error_msg = (f"进程 {self.process_name} (PID: {self.pid}) 已退出，"
                             "该 PID 已被其他进程占用，未开始监控")
                self.error_occurred.emit(self.task_id, error_msg)
                return StopReason.PID_REUSED

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
        next_deadline = self._first_deadline(clock.monotonic(), cpu_primed)
        while True:
            state = self.state
            if state not in ACTIVE_STATES:
                break
            # 暂停：无超时等待，直到 resume()/stop() 唤醒；恢复后立即采一次
            if state is WorkerState.PAUSED:
                clock.wait(self._wake, None)
                self._wake.clear()
                next_deadline = clock.monotonic()
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

            # 落后超过一个周期（如系统休眠唤醒、采集被严重拖慢）：重置为当前时间，不补采
            now = clock.monotonic()
            if now - next_deadline > self.interval:
                next_deadline = now

            try:
                values = self.collector.collect_metrics(self.metric_types)

                if values is not None:
                    # 同一采集周期的多个指标共用同一时间戳
                    timestamp = clock.now()
                    for metric_type, value in values.items():
                        self._data_buffer.append(DataPoint(
                            task_id=self.task_id,
                            timestamp=timestamp,
                            value=value,
                            metric_type=metric_type,
                        ))

                    # 发送更新信号（新建dict，避免emit后被修改）；全部指标本周期都不可用
                    # 时 values 为空，没有数据落库，不 emit（否则卡片采集次数虚增）
                    if values:
                        self.data_updated.emit(self.task_id, dict(values))

                    # 本周期新出现的不可用指标，一次性通知 UI（已通知过的不重复）
                    self._notify_unavailable()

                    # 批量保存（SAVE_BATCH_SIZE=1时语义为每周期一批）
                    if len(self._data_buffer) >= config.SAVE_BATCH_SIZE:
                        self._flush_buffer()

                else:
                    # 进程已退出（运行中 PID 被另一个进程复用也走这里，采集器无从区分）
                    stop_reason = StopReason.PROCESS_EXITED
                    break

            except Exception as e:
                error_msg = f"采集数据时发生错误: {str(e)}"
                logger.error(error_msg, exc_info=True)
                self.error_occurred.emit(self.task_id, error_msg)

            next_deadline = self._next_deadline(next_deadline)

        return stop_reason

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

    def pause(self, timestamp: Optional[datetime] = None) -> bool:
        """暂停监控（仅 RUNNING 可暂停；唤醒当前等待，工作线程转入暂停等待）。
        切换成功后写一条"暂停"系统打点，时间为 timestamp（缺省取调用时刻）。"""
        if not self._transition((WorkerState.RUNNING,), WorkerState.PAUSED):
            return False
        self._wake.set()
        self._write_system_marker('pause', timestamp)
        return True

    def resume(self, timestamp: Optional[datetime] = None) -> bool:
        """恢复监控（仅 PAUSED 可恢复；唤醒暂停等待，工作线程立即采集一次）。
        切换成功后写一条"恢复"系统打点，时间为 timestamp（缺省取调用时刻）。"""
        if not self._transition((WorkerState.PAUSED,), WorkerState.RUNNING):
            return False
        self._wake.set()
        self._write_system_marker('resume', timestamp)
        return True

    def _write_system_marker(self, kind: str, timestamp: Optional[datetime]) -> None:
        """写暂停/恢复系统打点。在调用 pause()/resume() 的线程（GUI 线程）同步写库：
        时间取点击时刻，不受工作线程调度延迟影响。写库失败只记日志——打点只是
        辅助信息，不能因此让暂停/恢复本身失败。"""
        when = timestamp if timestamp is not None else self._clock.now()
        try:
            self.db.markers.add(self.task_id, when, SYSTEM_MARKER_LABELS[kind], kind)
        except Exception:
            logger.error("task_id=%s 写入%s打点失败", self.task_id,
                         SYSTEM_MARKER_LABELS[kind], exc_info=True)

    def is_running(self) -> bool:
        """已请求运行且未请求停止（暂停中也算）。只由状态派生，不与 isRunning()
        做或运算：否则停止收尾期间会被冲回 True，等它变 False 的调用方会挂起"""
        return self.state in ACTIVE_STATES

    def is_paused(self) -> bool:
        """任务是否已暂停"""
        return self.state is WorkerState.PAUSED

    def get_task_info(self) -> TaskRecord:
        """获取任务信息"""
        return self.task_model

    def _teardown(self, reason: StopReason):
        """
        收尾（由 run() 的 finally 在工作线程内调用且仅调用一次）：
        最后一次 flush -> 写 stopped 状态 -> emit task_stopped。
        各步骤互不连累：任何一步异常都只记日志，task_stopped 一定会发出。

        Args:
            reason: 停止原因
        """
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
        logger.info("任务停止: task_id=%s 原因=%s", self.task_id, reason.value)

        # 先置 STOPPED 再发信号：界面收到 task_stopped 时运行计数已不含本任务
        # （此后线程不再写库，维护互斥也随之放行）
        self._transition(LIVE_STATES, WorkerState.STOPPED)
        self.task_stopped.emit(self.task_id, reason)

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
        if not self._data_buffer:
            return

        success = self._ensure_task_row() and self.db.save_data_points(self._data_buffer)

        if success:
            if self._flush_fail_count:
                logger.info("task_id=%s flush 重试成功（此前连续失败 %d 次），落库 %d 条",
                            self.task_id, self._flush_fail_count, len(self._data_buffer))
            self._data_buffer.clear()
            self._flush_fail_count = 0
            self._notified = False
            return

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
