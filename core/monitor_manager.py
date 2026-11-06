"""
监控管理器
管理所有监控任务的创建、启动、停止等操作；由组合根构造并注入（不是单例）
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from PyQt5 import sip
from PyQt5.QtCore import QObject, pyqtSignal

from core.contracts import StopReason, TaskInfo, TaskStatus
from core.monitor_task import ACTIVE_STATES, LIVE_STATES, MonitorWorker, WorkerState
from data.database import Database
from data.errors import StorageError
from data.models import TaskRecord
from utils.thread_utils import shutdown_thread
import config

logger = logging.getLogger(__name__)

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

    def __init__(self, db: Database, parent: Optional[QObject] = None):
        """
        Args:
            db: 数据库实例（与各页面共用同一实例，由组合根注入）
            parent: Qt 父对象（可选）
        """
        super().__init__(parent)

        # 任务字典 {task_id: MonitorWorker}
        self._tasks: Dict[str, MonitorWorker] = {}

        # 维护锁：数据库清理/压缩期间禁止创建、启动任务
        self._maintenance = False

        # 进行中的导出数：导出线程长时间读库，与压缩数据库互相抢锁，二者互斥
        self._export_count = 0

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

    def pause_task(self, task_id: str) -> bool:
        """
        暂停监控任务

        Args:
            task_id: 任务ID

        Returns:
            bool: 暂停是否成功
        """
        task = self._tasks.get(task_id)
        return task is not None and task.pause()

    def resume_task(self, task_id: str) -> bool:
        """
        恢复监控任务

        Args:
            task_id: 任务ID

        Returns:
            bool: 恢复是否成功
        """
        task = self._tasks.get(task_id)
        return task is not None and task.resume()

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

    def join_all(self, timeout_ms: int) -> bool:
        """
        退出路径的 join：对仍在运行的任务线程逐个带超时等待（超时只记日志）。
        入口用 os._exit() 结束进程，若不等待，仍在收尾写库的线程会被中途掐死、
        丢失最后一批数据；超时则以"总期限 + 日志"代替无限等待。

        Args:
            timeout_ms: 每个线程的等待上限（毫秒）

        Returns:
            bool: 全部线程都在超时前结束返回 True
        """
        all_finished = True
        for task in list(self._tasks.values()):
            if task.isRunning() and not shutdown_thread(task, timeout_ms=timeout_ms):
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

    # ========== 任务数据与标记 ==========

    def add_marker(self, task_id: str, label: str,
                   timestamp: Optional[datetime] = None) -> Optional[datetime]:
        """
        为任务添加一个时间标记。

        Args:
            task_id: 任务ID
            label: 标记文字
            timestamp: 标记时间。界面应传入用户点"打点"的时刻：输入标签要花好几秒，
                取确认时刻会让竖线落在事件之后；为 None 时取当前时间

        Returns:
            Optional[datetime]: 标记时间戳；任务不存在、标签为空或写库失败返回 None
        """
        label = (label or '').strip()
        if not label:
            return None
        if task_id not in self._tasks and self.db.get_task(task_id) is None:
            return None
        if timestamp is None:
            timestamp = datetime.now()
        try:
            marker_id = self.db.markers.add(task_id, timestamp, label)
        except StorageError:
            logger.error("添加标记失败: task_id=%s", task_id, exc_info=True)
            return None
        logger.info("添加标记: task_id=%s marker_id=%s", task_id, marker_id)
        return timestamp

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
