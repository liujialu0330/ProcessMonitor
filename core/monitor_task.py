"""
监控任务模块
单个监控任务的实现，继承QThread在后台运行
"""
import logging
import uuid
from datetime import datetime
from typing import Optional, List
from PyQt5.QtCore import QThread, pyqtSignal

from core.process_collector import ProcessCollector
from data.models import MonitorTask as TaskModel, DataPoint
from data.database import Database
from utils.metrics import MetricType
import config

logger = logging.getLogger(__name__)

# flush 失败缓冲上限：超过后丢弃最旧数据，防止长时间写库失败导致内存无界增长
MAX_BUFFER_SIZE = 1000
# 连续 flush 失败达到该次数后，通过 error_occurred 通知 UI 一次（_notified 锁存，避免刷屏）
CONSECUTIVE_FAILURE_NOTIFY_THRESHOLD = 3


class MonitorTask(QThread):
    """
    监控任务类
    在独立线程中定期采集进程性能指标
    """

    # 信号定义
    data_updated = pyqtSignal(str, dict)    # 数据更新信号 (task_id, {指标类型: 指标值})
    task_stopped = pyqtSignal(str, str)     # 任务停止信号 (task_id, reason)
    error_occurred = pyqtSignal(str, str)   # 错误信号 (task_id, error_message)

    def __init__(self, pid: int, process_name: str, metric_types: List[str],
                 interval: float = None, task_id: str = None, db: Database = None):
        """
        初始化监控任务

        Args:
            pid: 进程ID
            process_name: 进程名称
            metric_types: 监控指标类型列表
            interval: 采集间隔（秒），默认使用配置文件中的值
            task_id: 任务ID，默认自动生成
            db: 数据库实例（可选，默认回退新建 Database()；生产路径由 MonitorManager 注入）
        """
        super().__init__()

        # 任务信息
        self.task_id = task_id or str(uuid.uuid4())
        self.pid = pid
        self.process_name = process_name
        self.metric_types = list(metric_types)
        self.interval = interval or config.DEFAULT_INTERVAL

        # 任务状态
        self._running = False
        self._paused = False

        # 数据采集器
        self.collector = ProcessCollector(pid)

        # 数据缓存（批量保存；SAVE_BATCH_SIZE 固化为1时语义为每周期一批）
        self._data_buffer: List[DataPoint] = []

        # flush 失败重试状态
        self._flush_fail_count = 0   # 连续 flush 失败次数，成功后归零
        self._notified = False       # 是否已因连续失败弹过一次 InfoBar（锁存，成功后复位）

        # 数据库（生产路径应由 MonitorManager 注入，回退仅为兼容兜底）
        self.db = db if db is not None else Database()

        # 任务模型
        self.task_model = TaskModel(
            task_id=self.task_id,
            pid=self.pid,
            process_name=self.process_name,
            metric_types=self.metric_types,
            interval=self.interval,
            start_time=None,  # 启动时设置
            end_time=None,
            status='pending',
        )

    def start(self, priority=QThread.InheritPriority):
        """
        启动线程（重写 QThread.start）：先置运行标志再调用 super().start()，
        避免线程尚未真正执行到 run() 内部置位语句前 stop() 被提前调用导致状态错乱。
        """
        self._running = True
        super().start(priority)

    def run(self):
        """线程运行函数（重写QThread.run）"""
        # 更新任务状态
        self.task_model.start_time = datetime.now()
        self.task_model.status = 'running'
        self.db.save_task(self.task_model)
        logger.info("任务启动: task_id=%s pid=%s process=%s metrics=%s",
                    self.task_id, self.pid, self.process_name, self.metric_types)

        # 检查进程是否存在
        if not self.collector.is_process_running():
            error_msg = f"进程 {self.process_name} (PID: {self.pid}) 不存在或无法访问"
            self.error_occurred.emit(self.task_id, error_msg)
            self._running = False
            self._teardown("进程不存在")
            return

        # 含CPU使用率指标时先预热，丢弃cpu_percent首次返回的无效0值
        if MetricType.CPU_PERCENT in self.metric_types:
            self.collector.prime_cpu()

        stop_reason = "用户停止"

        # 主循环：定时采集数据
        while self._running:
            # 如果暂停，等待
            if self._paused:
                self.msleep(100)
                continue

            # 采集数据
            try:
                values = self.collector.collect_metrics(self.metric_types)

                if values is not None:
                    # 同一采集周期的多个指标共用同一时间戳
                    timestamp = datetime.now()
                    for metric_type, value in values.items():
                        self._data_buffer.append(DataPoint(
                            task_id=self.task_id,
                            timestamp=timestamp,
                            value=value,
                            metric_type=metric_type,
                        ))

                    # 发送更新信号（新建dict，避免emit后被修改）
                    self.data_updated.emit(self.task_id, dict(values))

                    # 批量保存（SAVE_BATCH_SIZE=1时语义为每周期一批）
                    if len(self._data_buffer) >= config.SAVE_BATCH_SIZE:
                        self._flush_buffer()

                else:
                    # 进程已终止
                    stop_reason = "进程已终止"
                    self._running = False
                    break

            except Exception as e:
                error_msg = f"采集数据时发生错误: {str(e)}"
                logger.error(error_msg, exc_info=True)
                self.error_occurred.emit(self.task_id, error_msg)

            # 等待下一个采集周期，拆分为短间隔以便快速响应停止
            remaining = int(self.interval * 1000)
            while remaining > 0 and self._running:
                sleep_time = min(remaining, 100)
                self.msleep(sleep_time)
                remaining -= sleep_time

        # 主循环退出后统一收尾（无论因用户停止还是进程消亡，只走这一条路径，且始终在
        # 工作线程内执行，修复"task_stopped 先于落库完成"与"GUI线程跨线程写库"两个隐患）
        self._teardown(stop_reason)

    def stop(self):
        """
        停止监控任务：只置运行标志，让 run() 主循环在下一次检查时自然退出。
        收尾工作（flush/写状态/emit）全部移到 run() 内部完成，因此本方法能保持
        近乎立即返回的响应速度——这是 v1.0.6 的专项优化，不得劣化。
        """
        self._running = False

    def pause(self):
        """暂停监控"""
        self._paused = True

    def resume(self):
        """恢复监控"""
        self._paused = False

    def is_running(self) -> bool:
        """任务是否正在运行（只读 _running，不与 isRunning() 做或运算，
        否则 stop() 置的 False 会被冲回 True，导致 wait() 挂起）"""
        return self._running

    def is_paused(self) -> bool:
        """任务是否已暂停"""
        return self._paused

    def get_task_info(self) -> TaskModel:
        """获取任务信息"""
        return self.task_model

    def _teardown(self, reason: str):
        """
        收尾（在 run() 主循环退出后于工作线程内调用且仅调用一次）：
        最后一次 flush -> 写 stopped 状态 -> emit task_stopped

        Args:
            reason: 停止原因
        """
        # 收尾 flush 没有下一轮重试机会，失败也要继续走完收尾流程
        self._flush_buffer(is_teardown=True)

        self.task_model.end_time = datetime.now()
        self.task_model.status = 'stopped'
        self.db.update_task_status(self.task_id, 'stopped', self.task_model.end_time)
        logger.info("任务停止: task_id=%s 原因=%s", self.task_id, reason)

        # 发送停止信号
        self.task_stopped.emit(self.task_id, reason)

    def _flush_buffer(self, is_teardown: bool = False):
        """
        将缓冲区数据保存到数据库。

        失败时保留缓冲，交给下一采集周期（或下一次显式调用）重试，不丢数据；
        缓冲超过 MAX_BUFFER_SIZE 时丢弃最旧的数据并记日志；连续失败达到阈值
        经 error_occurred 通知 UI 一次（_notified 锁存，成功后复位）。

        Args:
            is_teardown: 是否为收尾阶段的最后一次 flush（无重试机会，失败需明确记日志）
        """
        if not self._data_buffer:
            return

        success = self.db.save_data_points(self._data_buffer)

        if success:
            if self._flush_fail_count:
                logger.info("task_id=%s flush 重试成功，落库 %d 条",
                            self.task_id, len(self._data_buffer))
            self._data_buffer.clear()
            self._flush_fail_count = 0
            self._notified = False
            return

        # 失败：缓冲保留，等待下一轮重试
        self._flush_fail_count += 1
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


# 单元测试
if __name__ == "__main__":
    import sys
    import os
    import tempfile
    from PyQt5.QtWidgets import QApplication

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
    task = MonitorTask(
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
