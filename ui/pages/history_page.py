"""
历史数据页面
显示监控任务的历史数据，包括图表和表格
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
                             QTableWidgetItem, QHeaderView, QSizePolicy)
from qfluentwidgets import (
    ComboBox, CardWidget, PushButton, FluentIcon,
    StrongBodyLabel, BodyLabel, CaptionLabel, InfoBar, InfoBarPosition,
    TableWidget, MessageBox
)
import pyqtgraph as pg
from datetime import datetime

from core.monitor_manager import MonitorManager
from data.database import Database
from utils.metrics import get_metric_display_name, format_metric_value

# 表格取数上限：只取最近 N 条采集（v1.2.0 批3 性能优化，避免大数据量下界面卡顿）
TABLE_POINT_LIMIT = 2000
# 图表分桶上限：按行号分桶后每桶取 MIN/MAX 两点，故图表最多 2*CHART_MAX_BUCKETS 个点
CHART_MAX_BUCKETS = 2000


class HistoryPage(QScrollArea):
    """历史数据页面"""

    def __init__(self, parent=None, db=None):
        """初始化页面

        Args:
            parent: 父窗口
            db: 数据库实例（可选，默认回退新建 Database()；生产路径必须由
                MainWindow 注入，回退仅为兼容兜底）
        """
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("historyPage")

        # 监控管理器（单例：仅首次构造时传入的 db 生效）和数据库
        self.manager = MonitorManager()
        self.db = db if db is not None else Database()

        # 当前选中的任务ID、指标类型与任务状态（用于删除按钮的运行中保护）
        self.current_task_id = None
        self.current_metric = None
        self.current_task_status = None

        # 初始化UI
        self._init_ui()

        # 加载任务列表
        self._load_tasks()

    def _init_ui(self):
        """初始化UI"""
        # 设置滚动区域属性
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 移除边框，设置透明背景
        self.setStyleSheet("QScrollArea{background: transparent; border: none}")
        self.viewport().setStyleSheet("background: transparent")

        # 创建主容器
        container = QWidget()
        container.setStyleSheet("background: transparent")
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

        # 指标选择（多指标任务切换查看不同指标）
        metric_label = BodyLabel("指标:")
        self.metric_combo = ComboBox()
        self.metric_combo.setPlaceholderText("选择指标")
        self.metric_combo.setMinimumWidth(160)
        self.metric_combo.currentIndexChanged.connect(self._on_metric_selected)

        # 刷新按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._load_tasks)

        # 删除此任务数据按钮
        self.delete_button = PushButton("删除此任务数据", self, FluentIcon.DELETE)
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._on_delete_task_clicked)

        select_layout.addWidget(task_label)
        select_layout.addWidget(self.task_combo, 1)
        select_layout.addWidget(metric_label)
        select_layout.addWidget(self.metric_combo)
        select_layout.addWidget(self.refresh_button)
        select_layout.addWidget(self.delete_button)

        main_layout.addWidget(select_card)

        # 数据量提示（表格与图表均按最近 N 次采集限流展示，导出仍为全量）
        limit_hint = CaptionLabel(f"表格显示最近 {TABLE_POINT_LIMIT} 次采集，图表按数据分桶展示（保留尖峰），导出数据仍为全量")
        limit_hint.setStyleSheet("color: #666;")
        main_layout.addWidget(limit_hint)

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
        # 渲染优化：分桶数据已保留尖峰，这里叠加 pyqtgraph 自身的峰值抽稀与视口裁剪，
        # 双保险应对缩放/大量点渲染时的卡顿。
        # 注：架构方案文本写的关键字是 method='peak'，但本项目实际安装的 pyqtgraph
        # 0.14.0 中 PlotWidget.setDownsampling 转发到 PlotItem.setDownsampling，
        # 其形参名是 mode 而非 method（PlotDataItem.setDownsampling 才是 method），
        # 这里按本项目实际依赖版本的真实签名改用 mode='peak'，效果（峰值保留抽稀）等价。
        self.chart_widget.setDownsampling(auto=True, mode='peak')
        self.chart_widget.setClipToView(True)

        chart_card = CardWidget()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(10, 10, 10, 10)
        chart_layout.addWidget(self.chart_widget)

        main_layout.addWidget(chart_card)

        # ========== 数据表格区域 ==========
        table_label = StrongBodyLabel("详细数据")
        main_layout.addWidget(table_label)

        # 创建表格（使用Fluent-Widgets的TableWidget）
        self.data_table = TableWidget()
        self.data_table.setColumnCount(3)
        self.data_table.setHorizontalHeaderLabels(['时间', '值', '原始值'])
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setEditTriggers(TableWidget.NoEditTriggers)
        self.data_table.setSelectionBehavior(TableWidget.SelectRows)
        # 设置表格固定高度，显示更多行
        self.data_table.setFixedHeight(500)
        # 设置Fluent Design样式
        self.data_table.setBorderVisible(True)
        self.data_table.setBorderRadius(8)
        self.data_table.setWordWrap(False)
        # 隐藏行号
        self.data_table.verticalHeader().hide()

        table_card = CardWidget()
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(10, 10, 10, 10)
        table_layout.addWidget(self.data_table)

        main_layout.addWidget(table_card)
        # 添加底部伸缩空间
        main_layout.addStretch()

    def _load_tasks(self):
        """加载任务列表"""
        # 1. 保存当前选中的任务ID和指标（如果有）
        current_task_id = self.current_task_id
        current_metric = self.current_metric

        # 2. 清空ComboBox
        self.task_combo.clear()

        # 获取所有任务（包括正在运行的和历史的）
        all_tasks = self.db.get_all_tasks()

        # 任务可见性与导出页统一为"有数据即显示"（不再按进程名过滤）
        tasks = []
        for task in all_tasks:
            data_count = self.db.get_data_point_count(task.task_id)
            if data_count > 0:
                tasks.append(task)

        if not tasks:
            InfoBar.info(
                title="提示",
                content="暂无监控任务数据",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            # 清空显示和当前任务ID、指标
            self._clear_display()
            self._clear_metric_combo()
            self.current_task_id = None
            self.current_metric = None
            self.current_task_status = None
            self._update_delete_button_state()
            return

        # 3. 添加到下拉框（使用setItemData设置userData）
        for task in tasks:
            # 单指标保持原格式（显示指标名），多指标显示"N项指标"
            if len(task.metric_types) == 1:
                metric_text = get_metric_display_name(task.metric_types[0])
            else:
                metric_text = f"{len(task.metric_types)}项指标"
            display_text = (
                f"{task.process_name} (PID: {task.pid}) - "
                f"{metric_text} - "
                f"{task.start_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            # 只传递文本，不传递第二个参数
            self.task_combo.addItem(display_text)
            # 使用setItemData单独设置userData
            index = self.task_combo.count() - 1
            self.task_combo.setItemData(index, task.task_id)

        # 4. 尝试恢复之前选中的任务和指标并重新加载数据
        if current_task_id:
            for i in range(self.task_combo.count()):
                if self.task_combo.itemData(i) == current_task_id:
                    # 找到之前选中的任务，恢复选中（blockSignals防止级联触发重复加载）
                    self.task_combo.blockSignals(True)
                    self.task_combo.setCurrentIndex(i)
                    self.task_combo.blockSignals(False)
                    self.current_task_id = current_task_id
                    # 恢复任务状态并刷新删除按钮可用性（blockSignals跳过了_on_task_selected）
                    task_info = self.db.get_task(current_task_id)
                    self.current_task_status = task_info.status if task_info else None
                    self._update_delete_button_state()
                    # 恢复指标下拉及之前选中的指标
                    self._populate_metric_combo(current_task_id, current_metric)
                    # 重新加载该任务的数据（这是刷新的关键！）
                    self._load_task_data(current_task_id, self.current_metric)
                    return

            # 之前的任务不在列表中了（可能被过滤/被删除），清空显示
            self._clear_display()
            self._clear_metric_combo()
            self.current_task_id = None
            self.current_metric = None
            self.current_task_status = None
            self._update_delete_button_state()

        # 5. 如果没有恢复之前的选择，且ComboBox不为空
        # 强制设置currentIndex以确保触发信号
        if self.task_combo.count() > 0:
            # 先设置为-1，再设置为0，强制触发currentIndexChanged信号
            self.task_combo.setCurrentIndex(-1)
            self.task_combo.setCurrentIndex(0)

    def _on_task_selected(self, index: int):
        """任务选择事件"""
        if index < 0:
            self.current_task_status = None
            self._update_delete_button_state()
            return

        # 获取任务ID
        task_id = self.task_combo.itemData(index)
        if not task_id:
            return

        self.current_task_id = task_id

        # 记录任务状态，用于删除按钮的运行中保护
        task_info = self.db.get_task(task_id)
        self.current_task_status = task_info.status if task_info else None
        self._update_delete_button_state()

        # 填充指标下拉（默认选中首指标）
        self._populate_metric_combo(task_id)

        # 加载数据
        self._load_task_data(task_id, self.current_metric)

    def _on_metric_selected(self, index: int):
        """指标选择事件"""
        if index < 0 or not self.current_task_id:
            return

        # 获取指标类型
        metric_type = self.metric_combo.itemData(index)
        if not metric_type:
            return

        self.current_metric = metric_type

        # 重新加载当前任务在该指标下的数据
        self._load_task_data(self.current_task_id, metric_type)

    def _populate_metric_combo(self, task_id: str, preferred_metric: str = None):
        """
        填充指标下拉框（全程blockSignals，避免级联触发数据加载）

        Args:
            task_id: 任务ID
            preferred_metric: 优先选中的指标（不在任务指标列表中则回退首指标）
        """
        task_info = self.db.get_task(task_id)
        metric_types = task_info.metric_types if task_info else []

        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()

        # 添加指标项（使用setItemData设置userData）
        for metric_type in metric_types:
            self.metric_combo.addItem(get_metric_display_name(metric_type))
            index = self.metric_combo.count() - 1
            self.metric_combo.setItemData(index, metric_type)

        if metric_types:
            # 恢复之前选中的指标，否则默认首指标
            select_index = 0
            if preferred_metric in metric_types:
                select_index = metric_types.index(preferred_metric)
            self.metric_combo.setCurrentIndex(select_index)
            self.current_metric = metric_types[select_index]
        else:
            self.current_metric = None

        self.metric_combo.blockSignals(False)

    def _clear_metric_combo(self):
        """清空指标下拉框（blockSignals防止误触发）"""
        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()
        self.metric_combo.blockSignals(False)

    def _load_task_data(self, task_id: str, metric_type: str = None):
        """
        加载任务指定指标的数据并显示（v1.2.0 批3 性能优化：表格与图表均限流查询，
        避免大数据量任务把整张表拖进内存/渲染导致界面卡顿；导出页仍走全量数据）

        Args:
            task_id: 任务ID
            metric_type: 指标类型（首指标查询自动包含metric_type为NULL的旧数据）
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

        # 表格：最近 TABLE_POINT_LIMIT 条，新语义下子查询已按时间升序返回，无需再翻转
        table_points = self.db.get_task_data_points(
            task_id, metric_type=metric_type, limit=TABLE_POINT_LIMIT)

        if not table_points:
            InfoBar.info(
                title="提示",
                content="该任务暂无数据",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            self._clear_display()
            return

        # 图表：SQL 分桶降采样（每桶 MIN/MAX 两点，保留尖峰），按时间升序返回
        chart_points = self.db.get_task_data_points_bucketed(
            task_id, metric_type=metric_type, max_buckets=CHART_MAX_BUCKETS)

        # 更新图表
        self._update_chart(chart_points, metric_type)

        # 更新表格
        self._update_table(table_points, metric_type)

    def _update_chart(self, data_points, metric_type):
        """
        更新图表

        Args:
            data_points: 分桶降采样后的数据点列表（按 timestamp 升序）
            metric_type: 指标类型
        """
        # 清空图表
        self.chart_widget.clear()

        if not data_points:
            return

        # 准备数据（分桶查询已按 timestamp 升序返回，直接用，无需再排序/翻转）
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
            data_points: 数据点列表（limit 子查询新语义下已按 timestamp 升序返回；
                         这里仍需 reversed() 翻转填充，使表格保持"倒序显示，最新的
                         在前"的既有用户可见行为——方案原文"表格 reversed() 零改动"
                         指的是保留这一步，之前误删已恢复，v1.2.0 批3 修正）
            metric_type: 指标类型
        """
        # 大批量 setItem 前关闭界面更新与排序，避免逐行触发重排/重绘拖慢填表
        self.data_table.setUpdatesEnabled(False)
        self.data_table.setSortingEnabled(False)
        try:
            # 设置行数
            self.data_table.setRowCount(len(data_points))

            # 倒序显示，最新的在前
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
        finally:
            # 表格本就未开启排序（TableWidget 默认不排序），此处只需恢复界面更新
            self.data_table.setUpdatesEnabled(True)

    def _clear_display(self):
        """清空显示"""
        self.chart_widget.clear()
        self.data_table.setRowCount(0)

    def _update_delete_button_state(self):
        """
        根据当前选中任务状态刷新删除按钮可用性：运行中任务禁止直接删除（需先停止），
        避免删掉正在写入的任务数据引发竞态
        """
        if not self.current_task_id:
            self.delete_button.setEnabled(False)
            self.delete_button.setToolTip("")
        elif self.current_task_status == 'running':
            self.delete_button.setEnabled(False)
            self.delete_button.setToolTip("任务正在运行中，请先停止任务再删除")
        else:
            self.delete_button.setEnabled(True)
            self.delete_button.setToolTip("")

    def _on_delete_task_clicked(self):
        """删除此任务数据按钮点击事件：确认对话框 -> delete_task -> 本页刷新"""
        if not self.current_task_id:
            return

        if self.current_task_status == 'running':
            InfoBar.warning(
                title="提示",
                content="任务正在运行中，请先停止任务再删除",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        task = self.db.get_task(self.current_task_id)
        task_desc = f"{task.process_name} (PID: {task.pid})" if task else self.current_task_id

        dialog = MessageBox(
            "确认删除",
            f"确定要删除任务「{task_desc}」的全部历史数据吗？此操作不可恢复。",
            self.window()
        )
        if not dialog.exec():
            return

        if self.db.delete_task(self.current_task_id):
            InfoBar.success(
                title="删除成功",
                content="该任务的历史数据已删除",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            self._load_tasks()
        else:
            InfoBar.error(
                title="删除失败",
                content="删除任务数据失败，请查看日志",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )

    def showEvent(self, event):
        """页面显示事件"""
        super().showEvent(event)
        # 每次显示时刷新任务列表
        self._load_tasks()
