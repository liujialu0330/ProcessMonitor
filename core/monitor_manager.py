"""
监控管理器
管理所有监控任务的创建、启动、停止等操作
采用单例模式，确保全局唯一
"""
from typing import Dict, List, Optional
from PyQt5.QtCore import QObject, pyqtSignal

from core.monitor_task import MonitorTask
from data.database import Database
from data.models import MonitorTask as TaskModel
import config


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
    data_updated = pyqtSignal(str, float)  # 数据更新信号 (task_id, value)
    error_occurred = pyqtSignal(str, str)  # 错误信号 (task_id, error_message)
    task_limit_reached = pyqtSignal()      # 任务数量达到上限信号

    def __new__(cls):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """初始化管理器"""
        # 避免重复初始化
        if self._initialized:
            return

        super().__init__()

        # 任务字典 {task_id: MonitorTask}
        self._tasks: Dict[str, MonitorTask] = {}

        # 数据库
        self.db = Database()

        # 标记已初始化
        self._initialized = True

    def create_task(self, pid: int, process_name: str, metric_type: str,
                   interval: float = None) -> Optional[str]:
        """
        创建新的监控任务

        Args:
            pid: 进程ID
            process_name: 进程名称
            metric_type: 监控指标类型
            interval: 采集间隔（可选）

        Returns:
            Optional[str]: 任务ID，创建失败返回None
        """
        # 检查任务数量限制
        if len(self._tasks) >= config.MAX_MONITOR_TASKS:
            self.task_limit_reached.emit()
            return None

        # 创建任务
        task = MonitorTask(
            pid=pid,
            process_name=process_name,
            metric_type=metric_type,
            interval=interval
        )

        # 连接任务信号
        task.data_updated.connect(self._on_task_data_updated)
        task.task_stopped.connect(self._on_task_stopped)
        task.error_occurred.connect(self._on_task_error)

        # 添加到字典
        self._tasks[task.task_id] = task

        # 发送信号
        self.task_added.emit(task.task_id)

        return task.task_id

    def start_task(self, task_id: str) -> bool:
        """
        启动监控任务

        Args:
            task_id: 任务ID

        Returns:
            bool: 启动是否成功
        """
        task = self._tasks.get(task_id)
        if task and not task.is_running():
            task.start()
            self.task_started.emit(task_id)
            return True
        return False

    def stop_task(self, task_id: str) -> bool:
        """
        停止监控任务

        Args:
            task_id: 任务ID

        Returns:
            bool: 停止是否成功
        """
        task = self._tasks.get(task_id)
        if task and task.is_running():
            task.stop()
            task.wait()  # 等待线程结束
            return True
        return False

    def remove_task(self, task_id: str) -> bool:
        """
        移除监控任务（停止并删除）

        Args:
            task_id: 任务ID

        Returns:
            bool: 移除是否成功
        """
        task = self._tasks.get(task_id)
        if task:
            # 如果正在运行，先停止
            if task.is_running():
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
        """停止所有任务"""
        for task_id in list(self._tasks.keys()):
            self.stop_task(task_id)

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

    def _on_task_data_updated(self, task_id: str, value: float):
        """任务数据更新处理"""
        self.data_updated.emit(task_id, value)

    def _on_task_stopped(self, task_id: str, reason: str):
        """任务停止处理"""
        self.task_stopped.emit(task_id, reason)

    def _on_task_error(self, task_id: str, error_msg: str):
        """任务错误处理"""
        self.error_occurred.emit(task_id, error_msg)


# 单元测试
if __name__ == "__main__":
    import sys
    import os
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import QTimer

    # 创建应用程序
    app = QApplication(sys.argv)

    # 获取管理器实例
    manager = MonitorManager()

    # 连接信号
    def on_task_added(task_id):
        print(f"[任务添加] ID: {task_id[:8]}...")

    def on_task_started(task_id):
        print(f"[任务启动] ID: {task_id[:8]}...")

    def on_task_stopped(task_id, reason):
        print(f"[任务停止] ID: {task_id[:8]}..., 原因: {reason}")

    def on_data_updated(task_id, value):
        print(f"[数据更新] ID: {task_id[:8]}..., 值: {value:.2f}")

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
        metric_type="memory_rss",
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
