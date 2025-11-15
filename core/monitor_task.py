"""
监控任务模块
单个监控任务的实现，继承QThread在后台运行
"""
import uuid
from datetime import datetime
from typing import Optional, List
from PyQt5.QtCore import QThread, pyqtSignal

from core.process_collector import ProcessCollector
from data.models import MonitorTask as TaskModel, DataPoint
from data.database import Database
import config


class MonitorTask(QThread):
    """
    监控任务类
    在独立线程中定期采集进程性能指标
    """

    # 信号定义
    data_updated = pyqtSignal(str, float)  # 数据更新信号 (task_id, value)
    task_stopped = pyqtSignal(str, str)     # 任务停止信号 (task_id, reason)
    error_occurred = pyqtSignal(str, str)   # 错误信号 (task_id, error_message)

    def __init__(self, pid: int, process_name: str, metric_type: str,
                 interval: float = None, task_id: str = None):
        """
        初始化监控任务

        Args:
            pid: 进程ID
            process_name: 进程名称
            metric_type: 监控指标类型
            interval: 采集间隔（秒），默认使用配置文件中的值
            task_id: 任务ID，默认自动生成
        """
        super().__init__()

        # 任务信息
        self.task_id = task_id or str(uuid.uuid4())
        self.pid = pid
        self.process_name = process_name
        self.metric_type = metric_type
        self.interval = interval or config.DEFAULT_INTERVAL

        # 任务状态
        self._running = False
        self._paused = False

        # 数据采集器
        self.collector = ProcessCollector(pid)

        # 数据缓存（批量保存）
        self._data_buffer: List[DataPoint] = []

        # 数据库
        self.db = Database()

        # 任务模型
        self.task_model = TaskModel(
            task_id=self.task_id,
            pid=self.pid,
            process_name=self.process_name,
            metric_type=self.metric_type,
            interval=self.interval,
            start_time=None,  # 启动时设置
            end_time=None,
            status='pending',
        )

    def run(self):
        """线程运行函数（重写QThread.run）"""
        self._running = True

        # 更新任务状态
        self.task_model.start_time = datetime.now()
        self.task_model.status = 'running'
        self.db.save_task(self.task_model)

        # 检查进程是否存在
        if not self.collector.is_process_running():
            error_msg = f"进程 {self.process_name} (PID: {self.pid}) 不存在或无法访问"
            self.error_occurred.emit(self.task_id, error_msg)
            self._stop_task("进程不存在")
            return

        # 主循环：定时采集数据
        while self._running:
            # 如果暂停，等待
            if self._paused:
                self.msleep(100)
                continue

            # 采集数据
            try:
                value = self.collector.collect_metric(self.metric_type)

                if value is not None:
                    # 创建数据点
                    data_point = DataPoint(
                        task_id=self.task_id,
                        timestamp=datetime.now(),
                        value=value,
                    )

                    # 添加到缓冲区
                    self._data_buffer.append(data_point)

                    # 发送更新信号
                    self.data_updated.emit(self.task_id, value)

                    # 批量保存
                    if len(self._data_buffer) >= config.SAVE_BATCH_SIZE:
                        self._flush_buffer()

                else:
                    # 进程已终止
                    self._stop_task("进程已终止")
                    break

            except Exception as e:
                error_msg = f"采集数据时发生错误: {str(e)}"
                self.error_occurred.emit(self.task_id, error_msg)

            # 等待下一个采集周期
            self.msleep(int(self.interval * 1000))

        # 停止时保存剩余数据
        self._flush_buffer()

    def stop(self):
        """停止监控任务"""
        self._stop_task("用户停止")

    def pause(self):
        """暂停监控"""
        self._paused = True

    def resume(self):
        """恢复监控"""
        self._paused = False

    def is_running(self) -> bool:
        """任务是否正在运行"""
        return self._running

    def is_paused(self) -> bool:
        """任务是否已暂停"""
        return self._paused

    def get_task_info(self) -> TaskModel:
        """获取任务信息"""
        return self.task_model

    def _stop_task(self, reason: str):
        """
        内部方法：停止任务

        Args:
            reason: 停止原因
        """
        self._running = False

        # 更新任务状态
        self.task_model.end_time = datetime.now()
        self.task_model.status = 'stopped'
        self.db.update_task_status(self.task_id, 'stopped', self.task_model.end_time)

        # 发送停止信号
        self.task_stopped.emit(self.task_id, reason)

    def _flush_buffer(self):
        """将缓冲区数据保存到数据库"""
        if self._data_buffer:
            self.db.save_data_points(self._data_buffer)
            self._data_buffer.clear()


# 单元测试
if __name__ == "__main__":
    import sys
    import os
    from PyQt5.QtWidgets import QApplication

    # 创建应用程序
    app = QApplication(sys.argv)

    # 获取当前进程PID
    current_pid = os.getpid()
    print(f"测试监控当前进程: PID={current_pid}")

    # 创建监控任务
    task = MonitorTask(
        pid=current_pid,
        process_name="python.exe",
        metric_type="memory_rss",
        interval=1.0
    )

    # 连接信号
    def on_data_updated(task_id, value):
        print(f"[数据更新] 任务: {task_id[:8]}..., 值: {value:.2f} MB")

    def on_task_stopped(task_id, reason):
        print(f"[任务停止] 任务: {task_id[:8]}..., 原因: {reason}")
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
