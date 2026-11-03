"""
监控管理器
管理所有监控任务的创建、启动、停止等操作
采用单例模式，确保全局唯一
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional
from PyQt5.QtCore import QObject, pyqtSignal

from core.monitor_task import MonitorTask
from data.database import Database
from data.models import MonitorTask as TaskModel
import config

logger = logging.getLogger(__name__)


class MonitorManager(QObject):
    """
    监控管理器（单例模式）
    负责管理所有监控任务
    """

    # 单例实例
    _instance = None

    # 信号定义
    task_added = pyqtSignal(str)           # 任务添加信号 (task_id)
    task_started = pyqtSignal(str)         # 任务启动信号 (task_id)
    task_stopped = pyqtSignal(str, str)    # 任务停止信号 (task_id, reason)
    task_removed = pyqtSignal(str)         # 任务移除信号 (task_id)
    data_updated = pyqtSignal(str, dict)   # 数据更新信号 (task_id, {指标类型: 指标值})
    error_occurred = pyqtSignal(str, str)  # 错误信号 (task_id, error_message)
    metrics_unavailable = pyqtSignal(str, dict)  # 指标不可用信号 (task_id, {指标类型: 原因})
    task_limit_reached = pyqtSignal()      # 任务数量达到上限信号

    def __new__(cls, db=None):
        """单例模式实现（Python 以相同实参先调 __new__ 再调 __init__，签名必须与
        __init__ 保持一致，否则传 db 参数会 TypeError）"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db=None):
        """初始化管理器（单例：仅首次构造时传入的 db 生效，后续调用 MonitorManager(db=...)
        不会替换已存在实例的数据库连接）"""
        # 避免重复初始化
        if self._initialized:
            return

        super().__init__()

        # 任务字典 {task_id: MonitorTask}
        self._tasks: Dict[str, MonitorTask] = {}

        # 维护锁：数据库清理/压缩期间禁止创建、启动任务
        self._maintenance = False

        # 数据库（生产路径应由 MainWindow 注入，回退仅为兼容兜底）
        self.db = db if db is not None else Database()

        # 标记已初始化
        self._initialized = True

    def create_task(self, pid: int, process_name: str, metric_types: List[str],
                    interval: float = None, note: str = '') -> Optional[str]:
        """
        创建新的监控任务

        Args:
            pid: 进程ID
            process_name: 进程名称
            metric_types: 监控指标类型列表
            interval: 采集间隔（可选）
            note: 任务备注（可选）

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
        task = MonitorTask(
            pid=pid,
            process_name=process_name,
            metric_types=metric_types,
            interval=interval,
            db=self.db,
            note=note,
        )

        # 连接任务信号
        task.data_updated.connect(self._on_task_data_updated)
        task.task_stopped.connect(self._on_task_stopped)
        task.error_occurred.connect(self._on_task_error)
        task.metrics_unavailable.connect(self._on_task_metrics_unavailable)

        # 添加到字典
        self._tasks[task.task_id] = task

        # 发送信号
        self.task_added.emit(task.task_id)

        return task.task_id

    def start_task(self, task_id: str) -> bool:
        """
        启动监控任务

        已结束（isFinished）的任务一律拒绝：任务停止后线程对象不可复用，
        重新监控应通过 create_task 创建新任务。线程仍在收尾（isRunning）
        时同样拒绝，避免 start() 把刚置位的停止标志冲掉。

        Args:
            task_id: 任务ID

        Returns:
            bool: 启动是否成功
        """
        task = self._tasks.get(task_id)
        if self._maintenance or task is None:
            return False
        if task.is_running() or task.isRunning() or task.isFinished():
            return False
        task.start()
        logger.info("任务启动请求: task_id=%s pid=%s", task_id, task.pid)
        self.task_started.emit(task_id)
        return True

    def stop_task(self, task_id: str) -> bool:
        """
        请求停止监控任务（非阻塞）：只置停止标志后立即返回，不等待线程结束。
        任务收尾（flush/写状态）在其工作线程内完成，随后经 task_stopped 信号通知；
        UI 收到信号后再调用 remove_task（此时只需极短 wait）。

        Args:
            task_id: 任务ID

        Returns:
            bool: 是否已发出停止请求
        """
        task = self._tasks.get(task_id)
        if task and task.is_running():
            task.stop()
            logger.info("任务停止请求已发出: task_id=%s", task_id)
            return True
        return False

    def remove_task(self, task_id: str) -> bool:
        """
        移除监控任务（停止并删除）

        只要线程仍在运行（含停止收尾中）就等待其结束，而不只看运行标志，
        保证对象被丢弃时线程一定已退出。

        Args:
            task_id: 任务ID

        Returns:
            bool: 移除是否成功
        """
        task = self._tasks.get(task_id)
        if task:
            if task.isRunning():
                task.stop()
                task.wait()

            # 从字典中移除
            del self._tasks[task_id]

            # 发送信号
            self.task_removed.emit(task_id)

            return True
        return False

    def pause_task(self, task_id: str) -> bool:
        """
        暂停监控任务

        Args:
            task_id: 任务ID

        Returns:
            bool: 暂停是否成功
        """
        task = self._tasks.get(task_id)
        if task and task.is_running() and not task.is_paused():
            task.pause()
            return True
        return False

    def resume_task(self, task_id: str) -> bool:
        """
        恢复监控任务

        Args:
            task_id: 任务ID

        Returns:
            bool: 恢复是否成功
        """
        task = self._tasks.get(task_id)
        if task and task.is_running() and task.is_paused():
            task.resume()
            return True
        return False

    def get_task(self, task_id: str) -> Optional[MonitorTask]:
        """
        获取监控任务对象

        Args:
            task_id: 任务ID

        Returns:
            Optional[MonitorTask]: 任务对象，不存在返回None
        """
        return self._tasks.get(task_id)

    def get_all_tasks(self) -> List[MonitorTask]:
        """
        获取所有监控任务

        Returns:
            List[MonitorTask]: 任务列表
        """
        return list(self._tasks.values())

    def get_running_tasks(self) -> List[MonitorTask]:
        """
        获取所有正在运行的任务

        Returns:
            List[MonitorTask]: 正在运行的任务列表
        """
        return [task for task in self._tasks.values() if task.is_running()]

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

    def stop_all_tasks(self):
        """
        停止所有任务（退出路径，阻塞）：先给全部任务置停止标志，再逐个无超时 wait。
        无超时 join 是退出时 os._exit 的数据安全前提——每个任务的收尾 flush 与
        状态落库都已完成才会返回，勿给 wait 加超时。
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            if task.isRunning():
                task.stop()
        for task in tasks:
            if task.isRunning():
                task.wait()

    # ========== 运行状态与维护 ==========

    def running_task_count(self) -> int:
        """仍有线程在运行的任务数（含暂停中与停止收尾中的任务）"""
        return sum(1 for task in self._tasks.values() if task.isRunning())

    def has_running_tasks(self) -> bool:
        """是否存在仍在运行的任务"""
        return self.running_task_count() > 0

    def begin_maintenance(self) -> bool:
        """
        进入数据库维护状态（清理/压缩前调用）。有运行中任务时拒绝返回 False；
        否则置位并返回 True，此后 create_task / start_task 被拒绝，直到 end_maintenance。
        """
        if self.has_running_tasks():
            return False
        self._maintenance = True
        return True

    def end_maintenance(self):
        """退出数据库维护状态"""
        self._maintenance = False

    def is_maintenance_running(self) -> bool:
        """是否处于数据库维护状态"""
        return self._maintenance

    # ========== 任务数据与标记 ==========

    def add_marker(self, task_id: str, label: str) -> Optional[datetime]:
        """
        以当前时间为任务添加一个时间标记。

        Returns:
            Optional[datetime]: 标记时间戳；任务不存在、标签为空或写库失败返回 None
        """
        label = (label or '').strip()
        if not label:
            return None
        if task_id not in self._tasks and self.db.get_task(task_id) is None:
            return None
        timestamp = datetime.now()
        if not self.db.add_marker(task_id, timestamp, label):
            return None
        return timestamp

    def delete_task_data(self, task_id: str) -> bool:
        """
        删除任务的全部数据（任务行、数据点、标记）。
        任务仍在 _tasks 中且线程在运行时拒绝，返回 False。
        """
        task = self._tasks.get(task_id)
        if task is not None and task.isRunning():
            return False
        return self.db.delete_task(task_id)

    def get_task_info(self, task_id: str) -> Optional[TaskModel]:
        """
        获取任务信息

        Args:
            task_id: 任务ID

        Returns:
            Optional[TaskModel]: 任务信息模型
        """
        task = self._tasks.get(task_id)
        if task:
            return task.get_task_info()
        # 如果当前不在运行，尝试从数据库获取
        return self.db.get_task(task_id)

    def load_historical_tasks(self) -> List[TaskModel]:
        """
        从数据库加载历史任务

        Returns:
            List[TaskModel]: 历史任务列表
        """
        return self.db.get_all_tasks()

    # ========== 私有方法：信号处理 ==========

    def _on_task_data_updated(self, task_id: str, values: dict):
        """任务数据更新处理"""
        self.data_updated.emit(task_id, values)

    def _on_task_stopped(self, task_id: str, reason: str):
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

    # 冒烟测试使用临时目录数据库，不写项目 data\monitor.db（显式注入 db 参数，不再
    # 改写 config.DB_PATH）
    _tmp_dir = tempfile.mkdtemp(prefix="monitor_manager_smoke_")
    _tmp_db_path = os.path.join(_tmp_dir, "smoke_monitor.db")
    print(f"冒烟测试使用临时数据库: {_tmp_db_path}")

    # 创建应用程序
    app = QApplication(sys.argv)

    # 获取管理器实例（显式注入临时数据库）
    manager = MonitorManager(db=Database(_tmp_db_path))

    # 连接信号
    def on_task_added(task_id):
        print(f"[任务添加] ID: {task_id[:8]}...")

    def on_task_started(task_id):
        print(f"[任务启动] ID: {task_id[:8]}...")

    def on_task_stopped(task_id, reason):
        print(f"[任务停止] ID: {task_id[:8]}..., 原因: {reason}")

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
