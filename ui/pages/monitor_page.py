"""
实时监控页面
显示进程选择、监控任务列表等
"""
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QGridLayout, QScrollArea, QSizePolicy)
from qfluentwidgets import (
    LineEdit, ComboBox, PushButton, PrimaryPushButton, SpinBox,
    CardWidget, FluentIcon, InfoBar, InfoBarPosition,
    BodyLabel, CaptionLabel, StrongBodyLabel
)

from core.monitor_manager import MonitorManager
from core.process_collector import ProcessCollector
from data.database import Database
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

        # 记录条数
        self.count_label = CaptionLabel("已记录: 0 条")
        left_layout.addWidget(self.count_label)

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

    def update_count(self, count: int):
        """
        更新记录条数

        Args:
            count: 记录条数
        """
        self.count_label.setText(f"已记录: {count} 条")


class MonitorPage(QScrollArea):
    """实时监控页面"""

    def __init__(self, parent=None):
        """初始化页面"""
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("monitorPage")

        # 监控管理器和数据库
        self.manager = MonitorManager()
        self.db = Database()

        # 任务卡片字典 {task_id: TaskCard}
        self.task_cards = {}

        # 进程列表缓存 {pid: name}
        self.process_dict = {}

        # 同步标志，防止循环触发
        self._syncing = False

        # 初始化UI
        self._init_ui()

        # 连接信号
        self._connect_signals()

        # 启动时自动加载进程列表
        self._refresh_process_list()

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

        # 第一行：进程选择（PID输入框、进程下拉框、刷新按钮）
        process_label = BodyLabel("选择进程:")
        select_layout.addWidget(process_label, 1, 0)

        # PID输入框
        self.pid_input = LineEdit()
        self.pid_input.setPlaceholderText("输入进程PID")
        self.pid_input.setFixedWidth(150)
        self.pid_input.textChanged.connect(self._on_pid_changed)
        select_layout.addWidget(self.pid_input, 1, 1)

        # 进程选择下拉框
        self.process_combo = ComboBox()
        self.process_combo.setPlaceholderText("从列表中选择进程")
        self.process_combo.setMinimumWidth(300)
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)
        select_layout.addWidget(self.process_combo, 1, 2)

        # 刷新进程列表按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._refresh_process_list)
        self.refresh_button.setFixedWidth(80)
        select_layout.addWidget(self.refresh_button, 1, 3)

        # 监控指标选择
        metric_label = BodyLabel("监控指标:")
        self.metric_combo = ComboBox()
        self.metric_combo.setPlaceholderText("选择要监控的指标")
        self.metric_combo.setMinimumWidth(250)
        select_layout.addWidget(metric_label, 2, 0)
        select_layout.addWidget(self.metric_combo, 2, 1, 1, 2)

        # 填充指标选项
        self._fill_metric_options()

        # 采集周期（改为SpinBox）
        interval_label = BodyLabel("采集周期(秒):")
        self.interval_spinbox = SpinBox()
        self.interval_spinbox.setRange(1, 3600)  # 1-3600秒
        self.interval_spinbox.setValue(1)  # 默认1秒
        self.interval_spinbox.setFixedWidth(150)
        select_layout.addWidget(interval_label, 3, 0)
        select_layout.addWidget(self.interval_spinbox, 3, 1)

        # 开始监控按钮
        self.start_button = PrimaryPushButton("开始监控", self, FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.start_button.setFixedWidth(150)
        select_layout.addWidget(self.start_button, 4, 1, Qt.AlignRight)

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

    def _fill_metric_options(self):
        """填充监控指标选项"""
        for category, metrics in AVAILABLE_METRICS.items():
            for metric in metrics:
                display_name = get_metric_display_name(metric)
                # 修复：使用setItemData单独设置userData
                self.metric_combo.addItem(f"{category} - {display_name}")
                # 设置最后一项的userData
                index = self.metric_combo.count() - 1
                self.metric_combo.setItemData(index, metric)

    def _refresh_process_list(self):
        """刷新进程列表"""
        # 1. 断开信号，避免中间状态触发不必要的事件
        try:
            self.process_combo.currentIndexChanged.disconnect(self._on_process_selected)
        except:
            pass  # 如果未连接则忽略

        # 2. 保存当前选中的PID（如果有）
        current_pid = None
        current_index = self.process_combo.currentIndex()
        if current_index >= 0:
            item_data = self.process_combo.itemData(current_index)
            if item_data:
                current_pid = item_data[0]

        # 3. 完全重置ComboBox
        self.process_combo.clear()
        self.process_combo.setCurrentIndex(-1)
        self.process_dict.clear()

        # 4. 获取并添加进程
        processes = ProcessCollector.get_all_processes()

        for pid, name in processes:
            # 保存到字典
            self.process_dict[pid] = name
            # 添加到ComboBox
            self.process_combo.addItem(f"{name} (PID: {pid})")
            # 为最后添加的项设置userData
            index = self.process_combo.count() - 1
            self.process_combo.setItemData(index, (pid, name))

        # 5. 重新连接信号
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)

        # 6. 恢复之前的选择或选择第一个进程
        if current_pid and current_pid in self.process_dict:
            # 尝试恢复之前选中的进程
            for i in range(self.process_combo.count()):
                item_data = self.process_combo.itemData(i)
                if item_data and item_data[0] == current_pid:
                    self.process_combo.setCurrentIndex(i)
                    return

        # 如果无法恢复或首次加载，默认选择第一个进程
        if self.process_combo.count() > 0:
            self.process_combo.setCurrentIndex(0)

    def _on_pid_changed(self, text: str):
        """PID输入框内容变化"""
        if self._syncing:
            return

        if not text.strip():
            return

        try:
            pid = int(text)
            # 查找对应的进程并同步到下拉框
            if pid in self.process_dict:
                self._syncing = True
                # 在下拉框中查找并选中该进程
                for i in range(self.process_combo.count()):
                    item_data = self.process_combo.itemData(i)
                    if item_data and item_data[0] == pid:
                        self.process_combo.setCurrentIndex(i)
                        break
                self._syncing = False
        except ValueError:
            pass

    def _on_process_selected(self, index: int):
        """进程下拉框选择变化"""
        if self._syncing or index < 0:
            return

        item_data = self.process_combo.itemData(index)
        if item_data:
            pid, name = item_data
            # 同步到PID输入框
            self._syncing = True
            self.pid_input.setText(str(pid))
            self._syncing = False

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
                item_data = self.process_combo.itemData(index)
                if item_data:
                    pid, process_name = item_data
                else:
                    InfoBar.warning(
                        title="提示",
                        content="请输入PID或从列表中选择进程",
                        parent=self,
                        position=InfoBarPosition.TOP
                    )
                    return
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

        # 获取采集周期（从SpinBox）
        interval = float(self.interval_spinbox.value())

        # 创建并启动任务
        task_id = self.manager.create_task(pid, process_name, metric_type, interval)
        if task_id:
            self.manager.start_task(task_id)

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
            # 同时更新记录条数
            count = self.db.get_data_point_count(task_id)
            self.task_cards[task_id].update_count(count)

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
