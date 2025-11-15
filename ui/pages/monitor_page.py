"""
实时监控页面
显示进程选择、监控任务列表等
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QGridLayout, QScrollArea, QSizePolicy)
from qfluentwidgets import (
    LineEdit, ComboBox, PushButton, PrimaryPushButton,
    CardWidget, FluentIcon, InfoBar, InfoBarPosition,
    BodyLabel, CaptionLabel, StrongBodyLabel
)

from core.monitor_manager import MonitorManager
from core.process_collector import ProcessCollector
from utils.metrics import (
    AVAILABLE_METRICS, get_metric_display_name,
    format_metric_value, MetricType
)
import config


class TaskCard(CardWidget):
    """监控任务卡片组件"""

    # 停止按钮点击信号
    stop_clicked = pyqtSignal(str)  # task_id

    def __init__(self, task_id: str, process_name: str, pid: int,
                 metric_type: str, parent=None):
        """
        初始化任务卡片

        Args:
            task_id: 任务ID
            process_name: 进程名称
            pid: 进程ID
            metric_type: 监控指标类型
            parent: 父窗口
        """
        super().__init__(parent)

        self.task_id = task_id
        self.process_name = process_name
        self.pid = pid
        self.metric_type = metric_type
        self.current_value = 0.0

        self._init_ui()

    def _init_ui(self):
        """初始化UI"""
        # 设置卡片样式
        self.setFixedHeight(100)

        # 主布局
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 15, 20, 15)

        # 左侧：进程信息和数据
        left_layout = QVBoxLayout()

        # 进程名和PID
        title_label = StrongBodyLabel(f"{self.process_name} (PID: {self.pid})")
        left_layout.addWidget(title_label)

        # 监控指标
        metric_label = CaptionLabel(f"指标: {get_metric_display_name(self.metric_type)}")
        left_layout.addWidget(metric_label)

        # 当前值
        self.value_label = BodyLabel("当前值: --")
        left_layout.addWidget(self.value_label)

        left_layout.addStretch()

        # 右侧：停止按钮
        self.stop_button = PushButton("停止", self, FluentIcon.CANCEL)
        self.stop_button.clicked.connect(lambda: self.stop_clicked.emit(self.task_id))
        self.stop_button.setFixedWidth(100)

        # 添加到主布局
        layout.addLayout(left_layout, 1)
        layout.addWidget(self.stop_button, 0, Qt.AlignRight | Qt.AlignVCenter)

    def update_value(self, value: float):
        """
        更新显示值

        Args:
            value: 新的值
        """
        self.current_value = value
        formatted_value = format_metric_value(self.metric_type, value)
        self.value_label.setText(f"当前值: {formatted_value}")


class MonitorPage(QScrollArea):
    """实时监控页面"""

    def __init__(self, parent=None):
        """初始化页面"""
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("monitorPage")

        # 监控管理器
        self.manager = MonitorManager()

        # 任务卡片字典 {task_id: TaskCard}
        self.task_cards = {}

        # 初始化UI
        self._init_ui()

        # 连接信号
        self._connect_signals()

    def _init_ui(self):
        """初始化UI"""
        # 设置滚动区域属性
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 创建主容器
        container = QWidget()
        self.setWidget(container)

        # 主布局
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(20)

        # ========== 进程选择区域 ==========
        select_card = CardWidget()
        select_layout = QGridLayout(select_card)
        select_layout.setContentsMargins(20, 20, 20, 20)
        select_layout.setSpacing(15)

        # 标题
        title_label = StrongBodyLabel("创建监控任务")
        select_layout.addWidget(title_label, 0, 0, 1, 4)

        # PID输入
        pid_label = BodyLabel("进程PID:")
        self.pid_input = LineEdit()
        self.pid_input.setPlaceholderText("输入进程PID")
        self.pid_input.setFixedWidth(150)
        select_layout.addWidget(pid_label, 1, 0)
        select_layout.addWidget(self.pid_input, 1, 1)

        # 或者文本
        or_label = BodyLabel("或")
        select_layout.addWidget(or_label, 1, 2, Qt.AlignCenter)

        # 进程选择下拉框
        process_label = BodyLabel("选择进程:")
        self.process_combo = ComboBox()
        self.process_combo.setPlaceholderText("从列表中选择进程")
        self.process_combo.setMinimumWidth(250)
        select_layout.addWidget(process_label, 2, 0)
        select_layout.addWidget(self.process_combo, 2, 1, 1, 2)

        # 刷新进程列表按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._refresh_process_list)
        select_layout.addWidget(self.refresh_button, 2, 3)

        # 监控指标选择
        metric_label = BodyLabel("监控指标:")
        self.metric_combo = ComboBox()
        self.metric_combo.setPlaceholderText("选择要监控的指标")
        self.metric_combo.setMinimumWidth(250)
        select_layout.addWidget(metric_label, 3, 0)
        select_layout.addWidget(self.metric_combo, 3, 1, 1, 2)

        # 填充指标选项
        self._fill_metric_options()

        # 采集周期
        interval_label = BodyLabel("采集周期:")
        self.interval_combo = ComboBox()
        self.interval_combo.addItems(["0.5秒", "1秒", "2秒", "5秒", "10秒"])
        self.interval_combo.setCurrentIndex(1)  # 默认1秒
        self.interval_combo.setFixedWidth(150)
        select_layout.addWidget(interval_label, 4, 0)
        select_layout.addWidget(self.interval_combo, 4, 1)

        # 开始监控按钮
        self.start_button = PrimaryPushButton("开始监控", self, FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.start_button.setFixedWidth(150)
        select_layout.addWidget(self.start_button, 5, 1, Qt.AlignRight)

        main_layout.addWidget(select_card)

        # ========== 监控任务列表区域 ==========
        tasks_label = StrongBodyLabel("监控任务列表")
        main_layout.addWidget(tasks_label)

        # 任务列表容器
        self.tasks_container = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_container)
        self.tasks_layout.setSpacing(10)
        self.tasks_layout.addStretch()

        main_layout.addWidget(self.tasks_container)
        main_layout.addStretch()

        # 初始加载进程列表
        self._refresh_process_list()

    def _fill_metric_options(self):
        """填充监控指标选项"""
        for category, metrics in AVAILABLE_METRICS.items():
            for metric in metrics:
                display_name = get_metric_display_name(metric)
                self.metric_combo.addItem(f"{category} - {display_name}", metric)

    def _refresh_process_list(self):
        """刷新进程列表"""
        self.process_combo.clear()
        processes = ProcessCollector.get_all_processes()

        for pid, name in processes:
            self.process_combo.addItem(f"{name} (PID: {pid})", (pid, name))

    def _connect_signals(self):
        """连接信号"""
        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_started.connect(self._on_task_started)
        self.manager.task_stopped.connect(self._on_task_stopped)
        self.manager.data_updated.connect(self._on_data_updated)
        self.manager.error_occurred.connect(self._on_error)
        self.manager.task_limit_reached.connect(self._on_limit_reached)

    def _on_start_clicked(self):
        """开始监控按钮点击事件"""
        # 获取PID
        pid = None
        process_name = None

        # 优先使用PID输入
        pid_text = self.pid_input.text().strip()
        if pid_text:
            try:
                pid = int(pid_text)
                # 获取进程名
                collector = ProcessCollector(pid)
                process_name = collector.get_process_name()
                if not process_name:
                    InfoBar.error(
                        title="错误",
                        content=f"进程 PID {pid} 不存在或无法访问",
                        parent=self,
                        position=InfoBarPosition.TOP
                    )
                    return
            except ValueError:
                InfoBar.error(
                    title="错误",
                    content="PID必须是数字",
                    parent=self,
                    position=InfoBarPosition.TOP
                )
                return
        else:
            # 使用进程选择
            index = self.process_combo.currentIndex()
            if index >= 0:
                pid, process_name = self.process_combo.itemData(index)
            else:
                InfoBar.warning(
                    title="提示",
                    content="请输入PID或从列表中选择进程",
                    parent=self,
                    position=InfoBarPosition.TOP
                )
                return

        # 获取监控指标
        metric_index = self.metric_combo.currentIndex()
        if metric_index < 0:
            InfoBar.warning(
                title="提示",
                content="请选择要监控的指标",
                parent=self,
                position=InfoBarPosition.TOP
            )
            return

        metric_type = self.metric_combo.itemData(metric_index)

        # 获取采集周期
        interval_text = self.interval_combo.currentText()
        interval = float(interval_text.replace("秒", ""))

        # 创建并启动任务
        task_id = self.manager.create_task(pid, process_name, metric_type, interval)
        if task_id:
            self.manager.start_task(task_id)
        else:
            # 已在_on_limit_reached中处理
            pass

    def _on_task_added(self, task_id: str):
        """任务添加事件"""
        # 获取任务信息
        task = self.manager.get_task(task_id)
        if task:
            task_info = task.get_task_info()

            # 创建任务卡片
            card = TaskCard(
                task_id=task_id,
                process_name=task_info.process_name,
                pid=task_info.pid,
                metric_type=task_info.metric_type,
                parent=self
            )

            # 连接停止信号
            card.stop_clicked.connect(self._on_stop_task)

            # 添加到布局（在stretch之前）
            self.tasks_layout.insertWidget(self.tasks_layout.count() - 1, card)

            # 保存引用
            self.task_cards[task_id] = card

    def _on_task_started(self, task_id: str):
        """任务启动事件"""
        InfoBar.success(
            title="成功",
            content="监控任务已启动",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=1000
        )

    def _on_task_stopped(self, task_id: str, reason: str):
        """任务停止事件"""
        # 移除卡片
        if task_id in self.task_cards:
            card = self.task_cards[task_id]
            self.tasks_layout.removeWidget(card)
            card.deleteLater()
            del self.task_cards[task_id]

        # 移除任务
        self.manager.remove_task(task_id)

        InfoBar.info(
            title="任务已停止",
            content=f"原因: {reason}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _on_data_updated(self, task_id: str, value: float):
        """数据更新事件"""
        if task_id in self.task_cards:
            self.task_cards[task_id].update_value(value)

    def _on_error(self, task_id: str, error_msg: str):
        """错误事件"""
        InfoBar.error(
            title="错误",
            content=error_msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000
        )

    def _on_limit_reached(self):
        """任务数量达到上限"""
        InfoBar.warning(
            title="提示",
            content=f"最多只能同时监控{config.MAX_MONITOR_TASKS}个任务",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _on_stop_task(self, task_id: str):
        """停止任务"""
        self.manager.stop_task(task_id)
