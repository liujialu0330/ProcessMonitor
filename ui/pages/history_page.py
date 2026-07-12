"""
历史数据页面
显示监控任务的历史数据，包括图表和表格（v1.3.0 批2：时间范围筛选、统计摘要、
真实时间轴、悬停十字线、图表导出、主题联动）
"""
import bisect
import logging
from datetime import datetime, timedelta
from typing import Optional

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
                             QTableWidgetItem, QHeaderView, QSizePolicy,
                             QFileDialog, QApplication)
from qfluentwidgets import (
    ComboBox, CardWidget, PushButton, FluentIcon,
    StrongBodyLabel, BodyLabel, CaptionLabel, InfoBar, InfoBarPosition,
    TableWidget, MessageBox, SegmentedWidget, TransparentToolButton, qconfig
)
import pyqtgraph as pg
from pyqtgraph import exporters

from data.database import Database
from ui.chart_theme import chart_colors
from utils.metrics import get_metric_display_name, format_metric_value

logger = logging.getLogger(__name__)

# 表格取数上限：只取最近 N 条采集（v1.2.0 批3 性能优化，避免大数据量下界面卡顿）
TABLE_POINT_LIMIT = 2000
# 图表分桶上限：按行号分桶后每桶取 MIN/MAX 两点，故图表最多 2*CHART_MAX_BUCKETS 个点
CHART_MAX_BUCKETS = 2000

# 时间范围选项（A2）：(SegmentedWidget routeKey, 显示文本, 范围秒数)；
# 秒数为 None 表示"全部"（不做时间过滤）。语义锚点见 _compute_since_iso：
# 锚定该任务最后一个数据点的时间，而非当前时刻——停止已久的任务选"最近1小时"
# 仍应能看到其最后一小时的数据。
TIME_RANGE_OPTIONS = [
    ('10m', '10分钟', 600),
    ('1h', '1小时', 3600),
    ('6h', '6小时', 21600),
    ('24h', '24小时', 86400),
    ('all', '全部', None),
]
TIME_RANGE_SECONDS = {key: seconds for key, _text, seconds in TIME_RANGE_OPTIONS}
DEFAULT_TIME_RANGE_KEY = 'all'

# 表格时间列显示格式（A4）：真实时间轴替代原相对秒数展示，含月日避免跨天歧义
TABLE_TIME_FORMAT = '%m-%d %H:%M:%S'


class HistoryPage(QScrollArea):
    """历史数据页面"""

    # 统计摘要行占位文案（未选任务/所选范围内无数据时展示）
    _EMPTY_STATS_TEXT = "当前 -- ｜ 最小 -- ｜ 最大 -- ｜ 平均 --"

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

        self.db = db if db is not None else Database()

        # 当前选中的任务ID、指标类型与任务状态（用于删除按钮的运行中保护）
        self.current_task_id = None
        self.current_metric = None
        self.current_task_status = None

        # 时间范围选择（A2）：默认"全部"，切任务/切指标时保持不变，只有用户主动
        # 切换 SegmentedWidget 才会改变
        self.current_range_key = DEFAULT_TIME_RANGE_KEY

        # last_dt 按任务缓存（评审修订 M3）：仅切任务时重新查询 MAX(timestamp)，
        # 切范围/切指标复用缓存值
        self._last_dt_cache_task_id = None
        self._last_dt_cache_value = None

        # 当前图表缓存的 x（epoch float）/y 数组，供悬停吸附（A1）与主题切换重绘
        # （A6）复用，避免重新查库；仅用于绘图相关计算，严禁回流作为 SQL 查询参数
        # （评审修订 B1：SQL 过滤参数全链路走 ISO 字符串）
        self._chart_x = []
        self._chart_y = []
        self._chart_metric_type = None

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

        # 数据量提示（表格与图表均按当前时间范围+最近 N 次采集限流展示，导出仍为全量）
        # 沿用 CaptionLabel 默认主题色（浅/深色自适应），不再用内联样式硬编码固定灰色
        limit_hint = CaptionLabel(
            f"表格显示所选时间范围内最近 {TABLE_POINT_LIMIT} 次采集，图表按数据分桶展示"
            "（保留尖峰），导出数据仍为全量")
        main_layout.addWidget(limit_hint)

        # ========== 图表区域 ==========
        chart_label = StrongBodyLabel("数据趋势图")
        main_layout.addWidget(chart_label)

        # 十字线（A1）：随图表数据一起在 _redraw_chart 中重新加入 PlotItem——
        # chart_widget.clear() 会清空 PlotItem 下全部 item，含本对象
        self.crosshair_line = pg.InfiniteLine(angle=90, movable=False)
        self.crosshair_line.setVisible(False)

        # 真实时间轴（A4）：x 轴改用 DateAxisItem 显示实际日期时间刻度；绘图 x 数据
        # 统一用 epoch float（dp.timestamp.timestamp()），仅用于绘图，严禁回流到
        # SQL 查询参数（评审修订 B1，SQL 过滤参数全链路走 ISO 字符串）
        self.chart_widget = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem()})
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
        # 捕获鼠标离开图表区域事件，清空十字线与悬停读数（A1）
        self.chart_widget.installEventFilter(self)

        # 悬停十字线（A1）：SignalProxy 限流到 60Hz，避免每个原始鼠标移动事件都
        # 触发一次吸附计算+重绘；需持有引用防止被 GC
        self._mouse_proxy = pg.SignalProxy(
            self.chart_widget.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)

        chart_card = CardWidget()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(10, 10, 10, 10)
        chart_layout.setSpacing(8)

        # 图表工具行：时间范围切换（A2）+ 导出按钮（A5）
        chart_toolbar = QHBoxLayout()
        self.range_segmented = SegmentedWidget()
        for route_key, text, _seconds in TIME_RANGE_OPTIONS:
            self.range_segmented.addItem(routeKey=route_key, text=text)
        self.range_segmented.currentItemChanged.connect(self._on_time_range_changed)
        self.range_segmented.setCurrentItem(DEFAULT_TIME_RANGE_KEY)

        self.save_png_button = TransparentToolButton(FluentIcon.SAVE, chart_card)
        self.save_png_button.setToolTip("保存图表为图片")
        self.save_png_button.clicked.connect(self._save_chart_as_png)

        self.copy_chart_button = TransparentToolButton(FluentIcon.COPY, chart_card)
        self.copy_chart_button.setToolTip("复制图表到剪贴板")
        self.copy_chart_button.clicked.connect(self._copy_chart_to_clipboard)

        chart_toolbar.addWidget(self.range_segmented)
        chart_toolbar.addStretch()
        chart_toolbar.addWidget(self.save_png_button)
        chart_toolbar.addWidget(self.copy_chart_button)
        chart_layout.addLayout(chart_toolbar)

        # 统计摘要行（A3）+ 悬停读数（A1，右侧独立 label）
        stats_row = QHBoxLayout()
        self.stats_label = BodyLabel(self._EMPTY_STATS_TEXT)
        self.hover_label = CaptionLabel("")
        stats_row.addWidget(self.stats_label)
        stats_row.addStretch()
        stats_row.addWidget(self.hover_label)
        chart_layout.addLayout(stats_row)

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

        # 图表主题适配（A6）：构造时应用一次当前主题配色，并随全局主题切换实时重绘
        self._apply_chart_theme()
        qconfig.themeChanged.connect(self._on_theme_changed)

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

    def _on_time_range_changed(self, route_key: str):
        """
        时间范围切换事件（A2）：重查图表+表格+统计摘要；只影响当前任务的展示，
        不影响任务/指标下拉的选中状态（切任务/切指标时保持当前范围选择不变）
        """
        self.current_range_key = route_key
        if not self.current_task_id:
            return
        self._load_task_data(self.current_task_id, self.current_metric)

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

    def _get_cached_last_dt(self, task_id: str) -> Optional[datetime]:
        """
        按任务缓存 last_dt（评审修订 M3）：仅切任务时重新查询 MAX(timestamp)，
        切范围/切指标复用缓存值，避免在现有 (task_id, metric_type) 索引不含
        timestamp 的前提下重复触发一次全表 filter。
        """
        if self._last_dt_cache_task_id != task_id:
            self._last_dt_cache_value = self.db.get_last_point_timestamp(task_id)
            self._last_dt_cache_task_id = task_id
        return self._last_dt_cache_value

    def _compute_since_iso(self, task_id: str) -> Optional[str]:
        """
        把当前选中的时间范围换算成 SQL 过滤用的 ISO 字符串。

        【评审修订 B1】SQL 过滤参数全链路用 ISO 字符串，与绘图用的 epoch float
        严禁混用。锚点为该任务最后一个数据点的时间（get_last_point_timestamp），
        而非当前时刻——停止已久的任务选"最近1小时"仍应能看到其最后一小时的数据。

        Returns:
            Optional[str]: 选中"全部"或该任务尚无数据点时返回 None（不过滤）
        """
        range_seconds = TIME_RANGE_SECONDS.get(self.current_range_key)
        if range_seconds is None:
            return None
        last_dt = self._get_cached_last_dt(task_id)
        if last_dt is None:
            return None
        since_dt = last_dt - timedelta(seconds=range_seconds)
        return since_dt.isoformat()

    def _load_task_data(self, task_id: str, metric_type: str = None):
        """
        加载任务指定指标的数据并显示（v1.2.0 批3 性能优化 + v1.3.0 批2 时间范围
        筛选：表格与图表均按当前选中的时间范围过滤、限流查询，避免大数据量任务把
        整张表拖进内存/渲染导致界面卡顿；导出页仍走全量数据）

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

        since_iso = self._compute_since_iso(task_id)

        # 表格：所选范围内最近 TABLE_POINT_LIMIT 条，新语义下子查询已按时间升序返回，
        # 无需再翻转
        table_points = self.db.get_task_data_points(
            task_id, metric_type=metric_type, limit=TABLE_POINT_LIMIT, since_iso=since_iso)

        if not table_points:
            if self.current_range_key != DEFAULT_TIME_RANGE_KEY:
                InfoBar.info(
                    title="提示",
                    content="所选时间范围内暂无数据，可切换到「全部」查看完整历史",
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2500
                )
            else:
                InfoBar.info(
                    title="提示",
                    content="该任务暂无数据",
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=2000
                )
            self._clear_display()
            return

        # 图表：SQL 分桶降采样（每桶 MIN/MAX 两点，保留尖峰），按时间升序返回，
        # 同样按当前范围过滤
        chart_points = self.db.get_task_data_points_bucketed(
            task_id, metric_type=metric_type, max_buckets=CHART_MAX_BUCKETS, since_iso=since_iso)

        # 更新图表
        self._update_chart(chart_points, metric_type)

        # 更新表格
        self._update_table(table_points, metric_type)

        # 更新统计摘要行（A3）
        self._update_stats(table_points, metric_type, since_iso)

    def _update_chart(self, data_points, metric_type):
        """
        更新图表（A4 真实时间轴）：缓存 x（epoch float）/y 数组供悬停吸附与主题
        重绘复用，再统一交给 _redraw_chart 完成实际绘制

        Args:
            data_points: 分桶降采样后的数据点列表（按 timestamp 升序）
            metric_type: 指标类型
        """
        self._chart_metric_type = metric_type
        if data_points:
            # 绘图 x 数据统一用 epoch float，仅用于绘图，严禁回流到 SQL 查询参数
            # （评审修订 B1）
            self._chart_x = [dp.timestamp.timestamp() for dp in data_points]
            self._chart_y = [dp.value for dp in data_points]
        else:
            self._chart_x = []
            self._chart_y = []

        self._redraw_chart()

        self.chart_widget.setLabel(
            'left', get_metric_display_name(metric_type) if metric_type else '值')
        self.chart_widget.setLabel('bottom', '时间')

    def _redraw_chart(self):
        """
        按当前缓存的 _chart_x/_chart_y 数组重绘曲线。

        chart_widget.clear() 会清空 PlotItem 下全部 item（含十字线），因此每次
        都要重新加入；主题切换（A6）时也复用缓存数组走这里重绘，不重新查库。
        """
        self.chart_widget.clear()
        self.chart_widget.addItem(self.crosshair_line, ignoreBounds=True)
        self.crosshair_line.setVisible(False)
        self.hover_label.setText("")

        if not self._chart_x:
            return

        colors = chart_colors()
        pen = pg.mkPen(color=colors['curve'], width=2)
        self.chart_widget.plot(self._chart_x, self._chart_y, pen=pen)

    @staticmethod
    def _nearest_index(x_array: list, x: float) -> int:
        """在按升序排列的 x_array 中二分查找与 x 最接近的下标（悬停吸附用，A1）"""
        idx = bisect.bisect_left(x_array, x)
        if idx <= 0:
            return 0
        if idx >= len(x_array):
            return len(x_array) - 1
        before = x_array[idx - 1]
        after = x_array[idx]
        return idx - 1 if (x - before) <= (after - x) else idx

    def _on_mouse_moved(self, evt):
        """
        悬停十字线跟随鼠标并吸附到最近的绘制点（A1）。

        SignalProxy 限流到 60Hz 后回调，evt 是 SignalProxy 包装后的单元素元组。
        """
        if not self._chart_x:
            return

        pos = evt[0]
        plot_item = self.chart_widget.getPlotItem()
        if not plot_item.sceneBoundingRect().contains(pos):
            self.crosshair_line.setVisible(False)
            self.hover_label.setText("")
            return

        mouse_point = plot_item.vb.mapSceneToView(pos)
        idx = self._nearest_index(self._chart_x, mouse_point.x())

        x_val = self._chart_x[idx]
        y_val = self._chart_y[idx]
        self.crosshair_line.setPos(x_val)
        self.crosshair_line.setVisible(True)

        time_str = datetime.fromtimestamp(x_val).strftime(TABLE_TIME_FORMAT)
        formatted_value = format_metric_value(self._chart_metric_type, y_val)
        self.hover_label.setText(f"{time_str} · {formatted_value}")

    def eventFilter(self, obj, event):
        """
        鼠标离开图表区域时清空十字线与悬停读数（A1）。

        HistoryPage 本身是 QScrollArea：QAbstractScrollArea 内部会把 self 安装为
        其 viewport 的事件过滤器（Qt 内部机制，与本类下面 self.chart_widget.
        installEventFilter(self) 是两回事），因此本方法在 self.chart_widget 尚未
        赋值的构造早期就可能先被 Qt 内部调用一次（obj 是 viewport，不是
        chart_widget）。用 getattr 兜底避免属性尚不存在时抛 AttributeError；
        不满足条件的调用统一落到 super().eventFilter 保持 Qt 内置行为不变。
        """
        if getattr(self, 'chart_widget', None) is obj and event.type() == QEvent.Leave:
            self.crosshair_line.setVisible(False)
            self.hover_label.setText("")
        return super().eventFilter(obj, event)

    def _update_stats(self, table_points, metric_type, since_iso):
        """
        更新统计摘要行（A3）：当前 {last} ｜ 最小 {min} ｜ 最大 {max} ｜ 平均 {avg}

        Args:
            table_points: 表格查询结果（按 timestamp 升序），末元素即"当前"
                          （评审修订 M3：范围内最新值直接复用该结果，不单独查询）
            metric_type: 指标类型
            since_iso: 当前范围过滤参数，透传给 get_metric_stats 保持与图表/表格
                       口径一致
        """
        if not table_points or not self.current_task_id or not metric_type:
            self.stats_label.setText(self._EMPTY_STATS_TEXT)
            return

        current_text = format_metric_value(metric_type, table_points[-1].value)
        stats = self.db.get_metric_stats(self.current_task_id, metric_type, since_iso=since_iso)
        if stats:
            min_text = format_metric_value(metric_type, stats['min'])
            max_text = format_metric_value(metric_type, stats['max'])
            avg_text = format_metric_value(metric_type, stats['avg'])
        else:
            min_text = max_text = avg_text = "--"

        self.stats_label.setText(
            f"当前 {current_text} ｜ 最小 {min_text} ｜ 最大 {max_text} ｜ 平均 {avg_text}")

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
                # 时间（A4 真实时间轴：表格时间列改为含月日的绝对时间，替代原
                # 仅 HH:MM:SS 展示，避免跨天数据歧义）
                time_str = dp.timestamp.strftime(TABLE_TIME_FORMAT)
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
        """清空显示（图表、表格、统计摘要与悬停读数）"""
        self._chart_x = []
        self._chart_y = []
        self._chart_metric_type = None
        self._redraw_chart()
        self.data_table.setRowCount(0)
        self.stats_label.setText(self._EMPTY_STATS_TEXT)

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

    def _save_chart_as_png(self):
        """保存当前图表为 PNG 图片（A5）"""
        if not self.current_task_id or not self._chart_x:
            InfoBar.warning(
                title="提示",
                content="当前无图表数据可导出",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        task_info = self.db.get_task(self.current_task_id)
        process_name = (task_info.process_name if task_info else "task").replace('.exe', '')
        metric_name = get_metric_display_name(self.current_metric) if self.current_metric else "指标"
        default_name = f"{process_name}_{metric_name}_{datetime.now().strftime('%Y%m%d%H%M%S')}.png"

        save_path, _ = QFileDialog.getSaveFileName(
            self, "保存图表为图片", default_name, "PNG 图片 (*.png)")
        if not save_path:
            return

        try:
            exporter = exporters.ImageExporter(self.chart_widget.getPlotItem())
            exporter.parameters()['width'] = 1600
            exporter.export(save_path)
        except Exception:
            logger.error("保存图表图片失败", exc_info=True)
            InfoBar.error(
                title="保存失败",
                content="保存图表图片失败，请查看日志",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000
            )
            return

        InfoBar.success(
            title="保存成功",
            content=f"图表已保存到 {save_path}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _copy_chart_to_clipboard(self):
        """复制当前图表为图片到剪贴板（A5）"""
        if not self.current_task_id or not self._chart_x:
            InfoBar.warning(
                title="提示",
                content="当前无图表数据可复制",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        try:
            pixmap = self.chart_widget.grab()
            QApplication.clipboard().setPixmap(pixmap)
        except Exception:
            logger.error("复制图表到剪贴板失败", exc_info=True)
            InfoBar.error(
                title="复制失败",
                content="复制图表到剪贴板失败，请查看日志",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        InfoBar.success(
            title="复制成功",
            content="图表已复制到剪贴板",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _apply_chart_theme(self):
        """
        应用图表配色（A6）：背景、坐标轴、十字线均按 chart_colors() 取色；曲线
        颜色随缓存的 x/y 数组一并在 _redraw_chart 中重绘。构造时调用一次，
        qconfig.themeChanged 时重新调用。
        """
        colors = chart_colors()
        self.chart_widget.setBackground(colors['background'])

        axis_pen = pg.mkPen(color=colors['axis'])
        text_pen = pg.mkPen(color=colors['text'])
        for axis_name in ('bottom', 'left'):
            axis = self.chart_widget.getAxis(axis_name)
            axis.setPen(axis_pen)
            axis.setTextPen(text_pen)

        self.crosshair_line.setPen(pg.mkPen(color=colors['crosshair'], style=Qt.DashLine))

        # 曲线颜色随主题变化，用缓存的 x/y 数组重绘，不重新查库
        self._redraw_chart()

    def _on_theme_changed(self, theme):
        """qconfig.themeChanged 回调：全局主题切换时重新取色重绘图表（A6）"""
        self._apply_chart_theme()

    def showEvent(self, event):
        """页面显示事件"""
        super().showEvent(event)
        # 每次显示时刷新任务列表
        self._load_tasks()
