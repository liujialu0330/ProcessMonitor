"""
历史数据页面
显示监控任务的历史数据，包括图表和表格
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
                             QTableWidget, QTableWidgetItem, QHeaderView,
                             QSizePolicy)
from qfluentwidgets import (
    ComboBox, CardWidget, PushButton, FluentIcon,
    StrongBodyLabel, BodyLabel, InfoBar, InfoBarPosition
)
import pyqtgraph as pg
from datetime import datetime

from core.monitor_manager import MonitorManager
from data.database import Database
from utils.metrics import get_metric_display_name, format_metric_value


class HistoryPage(QScrollArea):
    """历史数据页面"""

    def __init__(self, parent=None):
        """初始化页面"""
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("historyPage")

        # 监控管理器和数据库
        self.manager = MonitorManager()
        self.db = Database()

        # 当前选中的任务ID
        self.current_task_id = None

        # 初始化UI
        self._init_ui()

        # 加载任务列表
        self._load_tasks()

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

        # ========== 任务选择区域 ==========
        select_card = CardWidget()
        select_layout = QHBoxLayout(select_card)
        select_layout.setContentsMargins(20, 20, 20, 20)
        select_layout.setSpacing(15)

        # 任务选择
        task_label = BodyLabel("选择任务:")
        self.task_combo = ComboBox()
        self.task_combo.setPlaceholderText("选择要查看的监控任务")
        self.task_combo.setMinimumWidth(400)
        self.task_combo.currentIndexChanged.connect(self._on_task_selected)

        # 刷新按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._load_tasks)

        select_layout.addWidget(task_label)
        select_layout.addWidget(self.task_combo, 1)
        select_layout.addWidget(self.refresh_button)

        main_layout.addWidget(select_card)

        # ========== 图表区域 ==========
        chart_label = StrongBodyLabel("数据趋势图")
        main_layout.addWidget(chart_label)

        # 创建图表
        self.chart_widget = pg.PlotWidget()
        self.chart_widget.setBackground('w')
        self.chart_widget.showGrid(x=True, y=True, alpha=0.3)
        self.chart_widget.setLabel('left', '值')
        self.chart_widget.setLabel('bottom', '时间')
        # 设置图表固定高度，确保X轴完整显示
        self.chart_widget.setFixedHeight(400)

        chart_card = CardWidget()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(10, 10, 10, 10)
        chart_layout.addWidget(self.chart_widget)

        main_layout.addWidget(chart_card)

        # ========== 数据表格区域 ==========
        table_label = StrongBodyLabel("详细数据")
        main_layout.addWidget(table_label)

        # 创建表格
        self.data_table = QTableWidget()
        self.data_table.setColumnCount(3)
        self.data_table.setHorizontalHeaderLabels(['时间', '值', '原始值'])
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.data_table.setSelectionBehavior(QTableWidget.SelectRows)
        # 设置表格固定高度，显示更多行
        self.data_table.setFixedHeight(500)

        table_card = CardWidget()
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(10, 10, 10, 10)
        table_layout.addWidget(self.data_table)

        main_layout.addWidget(table_card)
        # 添加底部伸缩空间
        main_layout.addStretch()

    def _load_tasks(self):
        """加载任务列表"""
        # 1. 保存当前选中的任务ID（如果有）
        current_task_id = self.current_task_id

        # 2. 清空ComboBox
        self.task_combo.clear()

        # 获取所有任务（包括正在运行的和历史的）
        all_tasks = self.db.get_all_tasks()

        # 过滤掉单元测试产生的任务（python.exe进程且运行时间很短的）
        tasks = []
        for task in all_tasks:
            # 过滤条件：如果是python.exe进程，检查是否有数据点
            if task.process_name.lower() == "python.exe" and task.status == "stopped":
                # 获取数据点数量
                data_count = self.db.get_data_point_count(task.task_id)
                # 如果数据点少于5个，很可能是单元测试，跳过
                if data_count < 5:
                    continue
            tasks.append(task)

        if not tasks:
            InfoBar.info(
                title="提示",
                content="暂无监控任务数据",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            # 清空显示和当前任务ID
            self._clear_display()
            self.current_task_id = None
            return

        # 3. 添加到下拉框（使用setItemData设置userData）
        for task in tasks:
            display_text = (
                f"{task.process_name} (PID: {task.pid}) - "
                f"{get_metric_display_name(task.metric_type)} - "
                f"{task.start_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            # 只传递文本，不传递第二个参数
            self.task_combo.addItem(display_text)
            # 使用setItemData单独设置userData
            index = self.task_combo.count() - 1
            self.task_combo.setItemData(index, task.task_id)

        # 4. 尝试恢复之前选中的任务并重新加载数据
        if current_task_id:
            for i in range(self.task_combo.count()):
                if self.task_combo.itemData(i) == current_task_id:
                    # 找到之前选中的任务，恢复选中
                    self.task_combo.setCurrentIndex(i)
                    # 重新加载该任务的数据（这是刷新的关键！）
                    self._load_task_data(current_task_id)
                    return

            # 之前的任务不在列表中了（可能被过滤），清空显示
            self._clear_display()
            self.current_task_id = None

        # 5. 如果没有恢复之前的选择，且ComboBox不为空
        # 强制设置currentIndex以确保触发信号
        if self.task_combo.count() > 0:
            # 先设置为-1，再设置为0，强制触发currentIndexChanged信号
            self.task_combo.setCurrentIndex(-1)
            self.task_combo.setCurrentIndex(0)

    def _on_task_selected(self, index: int):
        """任务选择事件"""
        if index < 0:
            return

        # 获取任务ID
        task_id = self.task_combo.itemData(index)
        if not task_id:
            return

        self.current_task_id = task_id

        # 加载数据
        self._load_task_data(task_id)

    def _load_task_data(self, task_id: str):
        """
        加载任务数据并显示

        Args:
            task_id: 任务ID
        """
        # 获取任务信息
        task_info = self.db.get_task(task_id)
        if not task_info:
            InfoBar.error(
                title="错误",
                content="无法加载任务信息",
                parent=self,
                position=InfoBarPosition.TOP
            )
            return

        # 获取数据点
        data_points = self.db.get_task_data_points(task_id)

        if not data_points:
            InfoBar.info(
                title="提示",
                content="该任务暂无数据",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            self._clear_display()
            return

        # 更新图表
        self._update_chart(data_points, task_info.metric_type)

        # 更新表格
        self._update_table(data_points, task_info.metric_type)

    def _update_chart(self, data_points, metric_type):
        """
        更新图表

        Args:
            data_points: 数据点列表
            metric_type: 指标类型
        """
        # 清空图表
        self.chart_widget.clear()

        if not data_points:
            return

        # 准备数据
        times = []
        values = []

        # 获取起始时间作为基准
        start_time = data_points[0].timestamp

        for dp in data_points:
            # 计算相对时间（秒）
            elapsed = (dp.timestamp - start_time).total_seconds()
            times.append(elapsed)
            values.append(dp.value)

        # 绘制曲线
        pen = pg.mkPen(color='#0078D4', width=2)
        self.chart_widget.plot(times, values, pen=pen)

        # 设置标签
        metric_name = get_metric_display_name(metric_type)
        self.chart_widget.setLabel('left', metric_name)
        self.chart_widget.setLabel('bottom', '时间 (秒)')

    def _update_table(self, data_points, metric_type):
        """
        更新表格

        Args:
            data_points: 数据点列表
            metric_type: 指标类型
        """
        # 设置行数
        self.data_table.setRowCount(len(data_points))

        # 填充数据（倒序显示，最新的在前）
        for i, dp in enumerate(reversed(data_points)):
            # 时间
            time_str = dp.timestamp.strftime('%H:%M:%S')
            time_item = QTableWidgetItem(time_str)
            self.data_table.setItem(i, 0, time_item)

            # 格式化的值
            formatted_value = format_metric_value(metric_type, dp.value)
            value_item = QTableWidgetItem(formatted_value)
            self.data_table.setItem(i, 1, value_item)

            # 原始值
            raw_value_item = QTableWidgetItem(f"{dp.value:.4f}")
            self.data_table.setItem(i, 2, raw_value_item)

    def _clear_display(self):
        """清空显示"""
        self.chart_widget.clear()
        self.data_table.setRowCount(0)

    def showEvent(self, event):
        """页面显示事件"""
        super().showEvent(event)
        # 每次显示时刷新任务列表
        self._load_tasks()
