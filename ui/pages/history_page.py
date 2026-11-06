"""
历史数据页面
显示监控任务的历史数据：时间范围筛选、统计条（起点/当前/变化/增速/最小/最大）、
真实时间轴图表（悬停十字线、打点竖线）、采样明细表，以及图表导出与主题联动。
运行中任务在页面可见时自动刷新（间隔按数据量自适应，见 AUTO_REFRESH_* 常量）。
"""
import bisect
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QScrollArea, QTableWidgetItem, QHeaderView,
                             QSizePolicy, QStackedWidget)
from qfluentwidgets import (
    ComboBox, CardWidget, PushButton, FluentIcon,
    StrongBodyLabel, BodyLabel, CaptionLabel,
    TableWidget, SegmentedWidget, TransparentToolButton, qconfig,
    TransparentDropDownPushButton, RoundMenu, Action,
    IconWidget, setCustomStyleSheet
)
import pyqtgraph as pg
from pyqtgraph import exporters

from core.contracts import TaskStatus
from core.history_refresh_worker import (
    HistoryRefreshResult, HistoryRefreshWorker, query_history_data)
from data.errors import StorageError
from ui.chart_theme import chart_colors
from ui.components.chart_axes import ContextDateAxisItem, MetricAxisItem
from ui.components.sparkline import compute_y_range
from ui.services import UiServices
from ui.thread_shutdown import BackgroundThread
from ui.formatting import (
    build_stat_texts, format_task_option_text, format_task_tooltip,
    marker_context
)
from ui.typography import (
    DataCaptionLabel, PageTitleLabel, StatValueLabel, TypeScale,
    data_font, ui_font
)
from utils.metrics import (
    format_metric_value, get_metric_display_name, get_metric_display_unit,
    is_integer_metric
)

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager
    from data.database import Database

logger = logging.getLogger(__name__)

# 表格取数上限：只取最近 N 条采集，避免大数据量下界面卡顿
TABLE_POINT_LIMIT = 2000
# 图表分桶上限：按行号分桶后每桶取 MIN/MAX 两点，故图表最多 2*CHART_MAX_BUCKETS 个点
CHART_MAX_BUCKETS = 2000

# 时间范围选项：(SegmentedWidget routeKey, 显示文本, 范围秒数)；
# 秒数为 None 表示"全部"（不做时间过滤）。语义锚点见 _compute_since：
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
# 默认看全部数据：泄漏判断要看从开始至今的整体趋势，而不是最近一小时的局部
DEFAULT_TIME_RANGE_KEY = 'all'

# 运行中任务的自动刷新间隔（毫秒）：按当前范围内数据点数自适应，数据越多每次刷新
# （统计聚合 + 分桶在后台线程，填充在主线程）越重，间隔相应拉长，避免长时间监控后资源占用过高。
# 数据点数 < AUTO_REFRESH_TIER1_COUNT 用 AUTO_REFRESH_INTERVAL_MS（5 秒）；
# < AUTO_REFRESH_TIER2_COUNT 用 AUTO_REFRESH_INTERVAL_MID_MS（15 秒）；更大用
# AUTO_REFRESH_INTERVAL_SLOW_MS（30 秒）。
AUTO_REFRESH_INTERVAL_MS = 5000
AUTO_REFRESH_INTERVAL_MID_MS = 15000
AUTO_REFRESH_INTERVAL_SLOW_MS = 30000
AUTO_REFRESH_TIER1_COUNT = 200_000
AUTO_REFRESH_TIER2_COUNT = 1_000_000

# 统计条各格的名称（顺序即显示顺序）
STAT_KEYS = ("起点", "当前", "变化", "增速", "最小", "最大")
_STAT_TOOLTIPS = {
    "起点": "所选范围内最早一次采集的值",
    "当前": "所选范围内最新一次采集的值",
    "变化": "当前值相对起点的变化量（绝对值与百分比）",
    "增速": "对所选范围内全部数据做线性回归得到的每小时变化量，持续为正说明在持续上涨",
    "最小": "所选范围内的最小值",
    "最大": "所选范围内的最大值",
}

# 表格时间列显示格式：含月日避免跨天歧义
TABLE_TIME_FORMAT = '%m-%d %H:%M:%S'
# 明细表是单指标采样账本，而不是需要占满窗口的数据网格。限制阅读轨道宽度可避免
# 宽屏下时间和值被拉到卡片两端；窄窗口仍由 Expanding sizePolicy 自动填满可用空间。
DETAIL_TABLE_MAX_WIDTH = 960
DETAIL_TABLE_MIN_COLUMN_WIDTH = 144
DETAIL_TABLE_HEADER_HEIGHT = 40
DETAIL_TABLE_ROW_HEIGHT = 38

class HistoryPage(QScrollArea):
    """历史数据页面"""

    def __init__(self, db: "Database", manager: "MonitorManager",
                 services: UiServices, parent=None):
        """初始化页面

        Args:
            db: 数据库实例（只读查询；与主窗口共用同一实例）
            manager: 监控管理器：删除任务数据经它执行（UI 写库一律走 manager）
            services: 界面层服务（提示、对话框、时钟、任务运行状态等）
            parent: 父窗口
        """
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("historyPage")

        self.db = db
        self.manager = manager
        self.services = services

        # 当前选中的任务ID、指标类型与任务状态（用于删除按钮的运行中保护）
        self.current_task_id = None
        # 当前选中是否只是列表默认选中（用户没主动选过）：是的话，列表里出现更新的任务
        # （如刚开始的监控）时自动切到最新任务，而不是一直停在启动时的旧任务上
        self._selection_is_auto = False
        self.current_metric = None
        self.current_task_status = None
        # 下拉列表里的任务对象与载入时的运行状态：运行中任务的时长标签按定时器原地刷新
        self._task_by_id = {}
        self._task_running = {}

        # 时间范围选择：默认"全部"，切任务/切指标时保持不变，只有用户主动
        # 切换 SegmentedWidget 才会改变
        self.current_range_key = DEFAULT_TIME_RANGE_KEY

        # last_dt 按任务缓存：切范围/切指标复用缓存值，避免重复查询 MAX(timestamp)；
        # 点击刷新、重新载入任务列表与自动刷新时失效，保证锚点跟上新数据
        self._last_dt_cache_task_id = None
        self._last_dt_cache_value = None

        # 当前图表缓存的 x（epoch float）/y 数组，供悬停吸附与主题切换重绘复用，
        # 避免重新查库；仅用于绘图相关计算，严禁回流作为 SQL 查询参数
        # （SQL 过滤参数全链路走 ISO 字符串）
        self._chart_x = []
        self._chart_y = []
        self._chart_metric_type = None
        self._chart_display_unit = ''

        # 当前范围内的打点（按时间升序）：图上画竖线，悬停读数据此提示所处区间
        self._markers: list = []

        # 任务下拉项 tooltip {task_id: 文本}（ComboBox 没有逐项 tooltip，
        # 选中某项时把它设到下拉框上）
        self._task_tooltips = {}

        # 运行中任务自动刷新：页面可见时才计时，每次触发先检查所选任务是否仍在运行
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(AUTO_REFRESH_INTERVAL_MS)
        self._auto_refresh_timer.timeout.connect(self._on_auto_refresh)
        self._auto_refresh_was_running = False

        # 自动刷新的后台查询线程：同一时刻最多一个（在跑时跳过新的 tick）。请求序号
        # 单调递增，任何"用户主动加载/整体重载/页面隐藏"都会推进它，使在途结果过期被丢弃
        self._refresh_worker: Optional[HistoryRefreshWorker] = None
        self._refresh_request_seq = 0
        # 自动刷新连续读库失败时只提示一次；成功应用一次结果后复位
        self._refresh_error_notified = False

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
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(16)

        # ========== 页面标题与低频操作 ==========
        title_layout = QHBoxLayout()
        title_layout.setSpacing(8)
        title_layout.addWidget(PageTitleLabel("历史数据"))
        title_layout.addStretch()

        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._on_refresh_clicked)
        title_layout.addWidget(self.refresh_button)

        # 删除是低频且高风险的操作，收纳到「更多」菜单，避免与筛选控件
        # 等权常驻。隐藏的 delete_button 保留旧属性与 click() 能力，
        # 不破坏已有调用方；界面实际入口是 delete_action。
        self.more_button = TransparentDropDownPushButton(
            "更多", self, FluentIcon.MORE)
        self.more_menu = RoundMenu(parent=self)

        self.raw_value_action = Action(FluentIcon.DOCUMENT, "显示原始值", self)
        self.raw_value_action.setCheckable(True)
        self.raw_value_action.toggled.connect(self._toggle_raw_values)
        self.more_menu.addAction(self.raw_value_action)
        self.more_menu.addSeparator()

        self.delete_button = PushButton("删除此任务数据", self, FluentIcon.DELETE)
        self.delete_button.hide()
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._on_delete_task_clicked)
        self.delete_action = Action(FluentIcon.DELETE, "删除此任务数据", self)
        self.delete_action.setEnabled(False)
        self.delete_action.triggered.connect(self.delete_button.click)
        self.more_menu.addAction(self.delete_action)
        self.more_button.setMenu(self.more_menu)
        title_layout.addWidget(self.more_button)
        main_layout.addLayout(title_layout)

        # ========== 任务与展示范围 ==========
        select_card = CardWidget()
        self.select_layout = QGridLayout(select_card)
        self.select_layout.setContentsMargins(16, 14, 16, 14)
        self.select_layout.setHorizontalSpacing(12)
        self.select_layout.setVerticalSpacing(10)

        # 任务选择独占一行，长进程名和时间不再与其他控件抢宽度
        self.task_label = BodyLabel("监控任务")
        self.task_combo = ComboBox()
        self.task_combo.setPlaceholderText("选择要查看的监控任务")
        self.task_combo.setMinimumWidth(220)
        self.task_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.task_combo.currentIndexChanged.connect(self._on_task_selected)

        # 指标选择（多指标任务切换查看不同指标）
        self.metric_label = BodyLabel("指标")
        self.metric_combo = ComboBox()
        self.metric_combo.setPlaceholderText("选择指标")
        self.metric_combo.setMinimumWidth(140)
        self.metric_combo.currentIndexChanged.connect(self._on_metric_selected)

        self.range_label = BodyLabel("时间范围")
        self.range_segmented = SegmentedWidget()
        for route_key, text, _seconds in TIME_RANGE_OPTIONS:
            self.range_segmented.addItem(routeKey=route_key, text=text)
        self.range_segmented.currentItemChanged.connect(self._on_time_range_changed)
        self.range_segmented.setCurrentItem(DEFAULT_TIME_RANGE_KEY)

        self.select_layout.addWidget(self.task_label, 0, 0)
        self.select_layout.addWidget(self.task_combo, 0, 1, 1, 3)
        self.select_layout.addWidget(self.metric_label, 1, 0)
        self.select_layout.addWidget(self.metric_combo, 1, 1)
        self.select_layout.addWidget(self.range_label, 1, 2)
        self.select_layout.addWidget(self.range_segmented, 1, 3)
        self.select_layout.setColumnStretch(1, 2)
        self.select_layout.setColumnStretch(3, 3)
        self._filters_compact = None

        main_layout.addWidget(select_card)

        # 十字线：随图表数据一起在 _redraw_chart 中重新加入 PlotItem——
        # chart_widget.clear() 会清空 PlotItem 下全部 item，含本对象
        self.crosshair_line = pg.InfiniteLine(angle=90, movable=False)
        self.crosshair_line.setVisible(False)

        # 真实时间轴：x 轴改用 DateAxisItem 显示实际日期时间刻度；绘图 x 数据
        # 统一用 epoch float（dp.timestamp.timestamp()），仅用于绘图，严禁回流到
        # SQL 查询参数（SQL 过滤参数全链路走 ISO 字符串）
        self._value_axis = MetricAxisItem(orientation='left')
        self._value_axis.enableAutoSIPrefix(False)
        self._time_axis = ContextDateAxisItem(orientation='bottom')
        self.chart_widget = pg.PlotWidget(axisItems={
            'left': self._value_axis,
            'bottom': self._time_axis,
        })
        self.chart_widget.showGrid(x=False, y=True, alpha=0.18)
        self.chart_widget.setLabel('left', '值')
        self.chart_widget.setLabel('bottom', '时间')
        for axis in (self._value_axis, self._time_axis):
            axis.setTickFont(data_font(TypeScale.CHART_TICK))
            axis.setStyle(maxTickLevel=0, tickTextOffset=6)
            axis.setTickDensity(0.55)
            axis.label.setFont(ui_font(TypeScale.CAPTION))
        # 保留约 360px 的有效读图高度，同时允许高窗口自动拉伸
        self.chart_widget.setMinimumHeight(340)
        self.chart_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 渲染优化：分桶数据已保留尖峰，这里叠加 pyqtgraph 自身的峰值抽稀与视口裁剪，
        # 双保险应对缩放/大量点渲染时的卡顿。pyqtgraph 0.14 中 PlotWidget.setDownsampling
        # 转发到 PlotItem.setDownsampling，形参名是 mode（PlotDataItem 的才叫 method）。
        self.chart_widget.setDownsampling(auto=True, mode='peak')
        self.chart_widget.setClipToView(True)
        # 捕获鼠标离开图表区域事件，清空十字线与悬停读数
        self.chart_widget.installEventFilter(self)

        # 悬停十字线：SignalProxy 限流到 60Hz，避免每个原始鼠标移动事件都
        # 触发一次吸附计算+重绘；需持有引用防止被 GC
        self._mouse_proxy = pg.SignalProxy(
            self.chart_widget.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)

        chart_card = CardWidget()
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(16, 12, 16, 16)
        chart_layout.setSpacing(8)

        # 图表工具行只保留当前读数和图片快捷操作；两个透明图标
        # 是低强调入口，不再与时间范围争夺注意力。
        chart_toolbar = QHBoxLayout()
        self.hover_label = DataCaptionLabel("")
        chart_toolbar.addWidget(self.hover_label)
        chart_toolbar.addStretch()

        self.save_png_button = TransparentToolButton(FluentIcon.SAVE, chart_card)
        self.save_png_button.setToolTip("保存图表为图片")
        self.save_png_button.clicked.connect(self._save_chart_as_png)

        self.copy_chart_button = TransparentToolButton(FluentIcon.COPY, chart_card)
        self.copy_chart_button.setToolTip("复制图表到剪贴板")
        self.copy_chart_button.clicked.connect(self._copy_chart_image)

        chart_toolbar.addWidget(self.save_png_button)
        chart_toolbar.addWidget(self.copy_chart_button)
        chart_layout.addLayout(chart_toolbar)
        chart_layout.addWidget(self.chart_widget)

        # ========== 数据表格区域 ==========
        # 创建表格（使用Fluent-Widgets的TableWidget）
        self.data_table = TableWidget()
        self._detail_data_font = data_font(TypeScale.BODY)
        self.data_table.setFont(self._detail_data_font)
        self.data_table.setColumnCount(3)
        self.data_table.setHorizontalHeaderLabels(['采样时间', '指标值', '原始值'])
        table_header_view = self.data_table.horizontalHeader()
        table_header_view.setSectionResizeMode(QHeaderView.Stretch)
        table_header_view.setDefaultAlignment(Qt.AlignCenter)
        table_header_view.setMinimumSectionSize(DETAIL_TABLE_MIN_COLUMN_WIDTH)
        table_header_view.setFixedHeight(DETAIL_TABLE_HEADER_HEIGHT)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setEditTriggers(TableWidget.NoEditTriggers)
        self.data_table.setSelectionBehavior(TableWidget.SelectRows)
        self.data_table.setMinimumHeight(340)
        self.data_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 设置Fluent Design样式
        self.data_table.setBorderVisible(True)
        self.data_table.setBorderRadius(8)
        self.data_table.setWordWrap(False)
        self.data_table.setColumnHidden(2, True)
        # 隐藏行号
        self.data_table.verticalHeader().hide()
        self.data_table.verticalHeader().setDefaultSectionSize(
            DETAIL_TABLE_ROW_HEIGHT)
        header_font = ui_font(TypeScale.CAPTION, QFont.DemiBold)
        table_header_view.setFont(header_font)
        header_families = ','.join(
            f"'{family}'" for family in header_font.families())
        header_qss = (
            "QHeaderView::section {"
            f"font-family: {header_families};"
            f"font-size: {TypeScale.CAPTION}px;"
            "font-weight: 600;"
            "}"
        )
        setCustomStyleSheet(self.data_table, header_qss, header_qss)

        table_card = CardWidget()
        table_outer_layout = QHBoxLayout(table_card)
        table_outer_layout.setContentsMargins(16, 12, 16, 16)
        table_outer_layout.setSpacing(0)

        # 卡片保持与趋势视图同宽，标题和表格则共享一条居中的阅读轨道：
        # 窄窗口填满卡片，宽屏最多 960px，避免一条记录被撕到屏幕两端。
        self.detail_table_content = QWidget(table_card)
        self.detail_table_content.setMaximumWidth(DETAIL_TABLE_MAX_WIDTH)
        self.detail_table_content.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding)
        table_layout = QVBoxLayout(self.detail_table_content)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)
        table_header = QHBoxLayout()
        table_header.addWidget(StrongBodyLabel("采样明细"))
        table_header.addStretch()
        self.table_hint_label = CaptionLabel("最新数据优先")
        table_header.addWidget(self.table_hint_label)
        table_layout.addLayout(table_header)
        table_layout.addWidget(self.data_table)
        table_outer_layout.addStretch()
        table_outer_layout.addWidget(self.detail_table_content, 1)
        table_outer_layout.addStretch()

        # ========== 统计带 + 趋势/明细子视图 ==========
        self.analysis_page = QWidget()
        analysis_layout = QVBoxLayout(self.analysis_page)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.setSpacing(12)

        # 统计条：六格，窄窗口下自动换行（见 _update_stats_layout）
        self.stats_card = CardWidget()
        self.stats_layout = QGridLayout(self.stats_card)
        self.stats_layout.setContentsMargins(16, 10, 16, 10)
        self.stats_layout.setHorizontalSpacing(8)
        self.stats_layout.setVerticalSpacing(10)
        self.stat_value_labels = {}
        self._stat_cells = []
        for key in STAT_KEYS:
            stat_widget = QWidget(self.stats_card)
            stat_layout = QVBoxLayout(stat_widget)
            stat_layout.setContentsMargins(12, 0, 12, 0)
            stat_layout.setSpacing(3)
            caption = CaptionLabel(key)
            caption.setToolTip(_STAT_TOOLTIPS[key])
            stat_layout.addWidget(caption)
            value_label = StatValueLabel("--")
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            # 长文本（如变化量 + 百分比）宁可被裁剪也不撑宽整行，完整内容见 tooltip
            value_label.setMinimumWidth(1)
            value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            stat_layout.addWidget(value_label)
            self.stat_value_labels[key] = value_label
            self._stat_cells.append(stat_widget)
        self._stats_columns = None
        self._update_stats_layout(self.viewport().width())
        analysis_layout.addWidget(self.stats_card)

        view_header = QHBoxLayout()
        self.view_segmented = SegmentedWidget()
        self.view_segmented.setFixedWidth(180)
        self.view_segmented.addItem(routeKey='trend', text='趋势')
        self.view_segmented.addItem(routeKey='detail', text='明细')
        self.view_segmented.currentItemChanged.connect(self._on_view_changed)
        view_header.addWidget(self.view_segmented)
        view_header.addStretch()
        analysis_layout.addLayout(view_header)

        self.view_stack = QStackedWidget()
        self.view_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view_stack.addWidget(chart_card)
        self.view_stack.addWidget(table_card)
        self.view_segmented.setCurrentItem('trend')
        analysis_layout.addWidget(self.view_stack, 1)

        # 空状态作为内容区的一种状态，不再反复用浮层提示打断操作。
        self.empty_state_page = CardWidget()
        empty_layout = QVBoxLayout(self.empty_state_page)
        empty_layout.setContentsMargins(24, 48, 24, 48)
        empty_layout.setSpacing(8)
        empty_layout.addStretch()
        empty_icon = IconWidget(FluentIcon.HISTORY, self.empty_state_page)
        empty_icon.setFixedSize(32, 32)
        empty_layout.addWidget(empty_icon, 0, Qt.AlignHCenter)
        self.empty_title_label = StrongBodyLabel("暂无历史数据")
        self.empty_title_label.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.empty_title_label)
        self.empty_detail_label = CaptionLabel("完成一次监控后，数据会显示在这里")
        self.empty_detail_label.setAlignment(Qt.AlignCenter)
        self.empty_detail_label.setWordWrap(True)
        empty_layout.addWidget(self.empty_detail_label)
        empty_layout.addStretch()

        self.content_stack = QStackedWidget()
        self.content_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 不要显式设置最小高度：显式值会覆盖布局算出的最小尺寸（统计条 + 图表），
        # 小窗口下滚动区不再出现滚动条，图表被压出卡片、横轴被裁掉
        self.content_stack.addWidget(self.empty_state_page)
        self.content_stack.addWidget(self.analysis_page)
        self.content_stack.setCurrentWidget(self.empty_state_page)
        main_layout.addWidget(self.content_stack, 1)

        self._update_filter_layout(self.viewport().width())

        # 图表主题适配：构造时应用一次当前主题配色，并随全局主题切换实时重绘
        self._apply_chart_theme()
        qconfig.themeChanged.connect(self._on_theme_changed)

    def _update_filter_layout(self, available_width: int):
        """在窄窗口下将时间范围自动换到下一行。

        常规宽度仍是「任务独占一行，指标+时间范围第二行」；
        当内容区不足时让时间范围换行，避免分段控件被裁切。
        """
        compact = available_width < 720
        if self._filters_compact == compact:
            return

        self._filters_compact = compact
        self.select_layout.removeWidget(self.range_label)
        self.select_layout.removeWidget(self.range_segmented)
        if compact:
            self.select_layout.addWidget(self.range_label, 2, 0)
            self.select_layout.addWidget(self.range_segmented, 2, 1, 1, 3)
        else:
            self.select_layout.addWidget(self.range_label, 1, 2)
            self.select_layout.addWidget(self.range_segmented, 1, 3)

    @staticmethod
    def _stats_column_count(available_width: int) -> int:
        """统计条的列数：宽屏一行六格，中等宽度两行三列，窄窗口三行两列。"""
        if available_width >= 1100:
            return 6
        if available_width >= 560:
            return 3
        return 2

    def _update_stats_layout(self, available_width: int):
        """按可用宽度重排统计条，避免长文本在窄窗口下互相挤压。"""
        columns = self._stats_column_count(available_width)
        if columns == self._stats_columns:
            return
        self._stats_columns = columns
        for cell in self._stat_cells:
            self.stats_layout.removeWidget(cell)
        for index, cell in enumerate(self._stat_cells):
            self.stats_layout.addWidget(cell, index // columns, index % columns)
        for column in range(len(STAT_KEYS)):
            self.stats_layout.setColumnStretch(column, 1 if column < columns else 0)

    def _toggle_raw_values(self, checked: bool):
        """按需显示原始值列，默认只保留用户最常用的时间与格式化值。"""
        self.data_table.setColumnHidden(2, not checked)
        self.raw_value_action.setText("隐藏原始值" if checked else "显示原始值")

    def _on_view_changed(self, route_key: str):
        """切换趋势/明细子视图，避免图表与长表格纵向堆叠。"""
        self.view_stack.setCurrentIndex(0 if route_key == 'trend' else 1)

    def _show_empty_state(self, title: str, detail: str):
        """在页面内展示空状态，不使用短暂且重复的浮层提示。"""
        self.empty_title_label.setText(title)
        self.empty_detail_label.setText(detail)
        self.content_stack.setCurrentWidget(self.empty_state_page)

    def _invalidate_last_dt_cache(self):
        """使最后时间戳缓存失效：下次计算时间范围锚点时重新查询 MAX(timestamp)。"""
        self._last_dt_cache_task_id = None
        self._last_dt_cache_value = None

    def _is_task_running(self, task_id: str) -> bool:
        """所选任务当前是否仍有采集线程在运行（以任务状态服务为准，而非数据库状态列；
        停止收尾中仍算运行，收尾写完结束时间后才转为已停止）。"""
        return self.services.task_status.is_running(task_id)

    def _on_refresh_clicked(self):
        """刷新按钮：重新载入任务列表与当前任务数据（_load_tasks 会使最后时间戳缓存失效）。"""
        self._load_tasks()

    def select_task(self, task_id: str):
        """选中指定任务并载入其数据（供主窗口从监控页"查看历史"跳转时调用）。

        任务尚无采样数据时不在列表中：清空界面并给出提示后直接返回，
        不再静默选中列表里的第一个任务。
        """
        self.current_task_id = task_id
        self.current_metric = None
        self._selection_is_auto = False
        self._load_tasks()
        if self.current_task_id != task_id:
            self.services.notifier.info(self, "提示", "该任务暂无采样数据")

    def _load_tasks(self):
        """加载任务列表"""
        # 新数据可能已经写入：任何整体重载都让最后时间戳缓存失效
        self._invalidate_last_dt_cache()
        self._cancel_pending_refresh()

        # 1. 保存当前选中的任务ID和指标（如果有）
        current_task_id = self.current_task_id
        current_metric = self.current_metric

        # 2. 清空ComboBox（blockSignals：清空期间不触发选择事件）
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        self.task_combo.blockSignals(False)
        self._task_tooltips = {}
        self._task_by_id = {}
        self._task_running = {}
        self.task_combo.setToolTip("")

        # 单条查询取出"至少有一次采样"的任务（与导出页口径一致，不再逐任务 COUNT）
        # 读库失败要与"没有任务"区分开：显示"读取数据失败"而非"暂无历史数据"
        load_error = None
        try:
            tasks = self.db.tasks.list_with_data()
        except StorageError as e:
            logger.error("历史页读取任务列表失败", exc_info=True)
            tasks, load_error = [], str(e)

        if not tasks:
            # 清空显示和当前任务ID、指标
            if load_error is not None:
                self._show_load_failed(load_error)
            else:
                self._clear_display(
                    "暂无历史数据",
                    "完成一次监控后，数据会显示在这里")
            self._clear_metric_combo()
            self.current_task_id = None
            self.current_metric = None
            self.current_task_status = None
            self.task_combo.setEnabled(False)
            self._update_delete_button_state()
            return

        self.task_combo.setEnabled(True)

        # 3. 添加到下拉框（使用setItemData设置userData）
        now = self.services.clock.now()
        for task in tasks:
            # 下拉项承载任务身份与上下文：进程 · PID · 开始时间 · 时长 · 备注；
            # 指标由下一行专门的选择器展示
            running = self._is_task_running(task.task_id)
            self.task_combo.addItem(format_task_option_text(task, running, now))
            index = self.task_combo.count() - 1
            self.task_combo.setItemData(index, task.task_id)
            self._task_tooltips[task.task_id] = format_task_tooltip(task, running, now)
            self._task_by_id[task.task_id] = task
            self._task_running[task.task_id] = running

        # 4. 尝试恢复之前选中的任务和指标并重新加载数据。
        # 之前只是默认选中、且列表最前面（最新）的任务已经换了：改为选中最新任务
        if (current_task_id and self._selection_is_auto
                and tasks[0].task_id != current_task_id):
            current_task_id = None
            self.current_metric = None
        if current_task_id:
            for i in range(self.task_combo.count()):
                if self.task_combo.itemData(i) == current_task_id:
                    # 找到之前选中的任务，恢复选中（blockSignals防止级联触发重复加载）
                    self.task_combo.blockSignals(True)
                    self.task_combo.setCurrentIndex(i)
                    self.task_combo.blockSignals(False)
                    self.current_task_id = current_task_id
                    self.task_combo.setToolTip(
                        self._task_tooltips.get(current_task_id, ""))
                    # 恢复任务状态并刷新删除按钮可用性（blockSignals跳过了_on_task_selected）
                    task_info = self.db.get_task(current_task_id)
                    self.current_task_status = task_info.status if task_info else None
                    self._update_delete_button_state()
                    # 恢复指标下拉及之前选中的指标
                    self._populate_metric_combo(current_task_id, current_metric)
                    # 重新加载该任务的数据（这是刷新的关键！）
                    self._load_task_data(current_task_id, self.current_metric)
                    return

            # 之前的任务不在列表中了（可能被删除或尚无数据），清空显示
            self._clear_display(
                "请选择监控任务",
                "选定任务和指标后即可查看历史数据")
            self._clear_metric_combo()
            self.current_task_id = None
            self.current_metric = None
            self.current_task_status = None
            self._update_delete_button_state()
            # 找不到目标任务：保持清空后的"请选择监控任务"状态直接返回，
            # 不再静默选中列表里的第一个任务
            self.task_combo.blockSignals(True)
            self.task_combo.setCurrentIndex(-1)
            self.task_combo.blockSignals(False)
            self.task_combo.setToolTip("")
            return

        # 5. 如果没有恢复之前的选择，且ComboBox不为空
        # 强制设置currentIndex以确保触发信号
        if self.task_combo.count() > 0:
            # 先设置为-1，再设置为0，强制触发currentIndexChanged信号
            self.task_combo.setCurrentIndex(-1)
            self.task_combo.setCurrentIndex(0)
            self._selection_is_auto = True

    def _refresh_task_option_texts(self):
        """原地刷新下拉项的时长标签：运行中任务的时长随时间增长；任务刚结束时
        从数据库取回结束时间，标签改为最终时长。不重建列表，不影响当前选择与图表。"""
        now = self.services.clock.now()
        for index in range(self.task_combo.count()):
            task_id = self.task_combo.itemData(index)
            task = self._task_by_id.get(task_id)
            if task is None or not self._task_running.get(task_id):
                continue
            running = self._is_task_running(task_id)
            if not running:
                task = self.db.get_task(task_id) or task
                self._task_by_id[task_id] = task
                self._task_running[task_id] = False
            self.task_combo.setItemText(index, format_task_option_text(task, running, now))
            self._task_tooltips[task_id] = format_task_tooltip(task, running, now)
            if task_id == self.current_task_id:
                self.task_combo.setToolTip(self._task_tooltips[task_id])

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
        self._selection_is_auto = False
        self._auto_refresh_was_running = False
        self.task_combo.setToolTip(self._task_tooltips.get(task_id, ""))

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
        时间范围切换事件：重查图表+表格+统计条；只影响当前任务的展示，
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
            self.metric_combo.setEnabled(True)
        else:
            self.current_metric = None
            self.metric_combo.setEnabled(False)

        self.metric_combo.blockSignals(False)

    def _clear_metric_combo(self):
        """清空指标下拉框（blockSignals防止误触发）"""
        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()
        self.metric_combo.setEnabled(False)
        self.metric_combo.blockSignals(False)

    def _get_cached_last_dt(self, task_id: str, task_info=None) -> Optional[datetime]:
        """
        按任务缓存 last_dt：切范围/切指标复用缓存值；缓存在刷新、重载任务列表与
        自动刷新时失效（_invalidate_last_dt_cache）。读库失败抛 StorageError。

        Args:
            task_id: 任务ID
            task_info: 调用方已取到的 MonitorTask（可选，省一次 get_task）
        """
        if self._last_dt_cache_task_id != task_id:
            # 优先读 tasks.last_sample_at（主键查询，随数据点写入同事务维护），
            # 不再对大表执行 MAX(timestamp)；仅其为空（旧数据/维护缺失）时才回退旧查询
            if task_info is None:
                task_info = self.db.tasks.get(task_id)
            last_dt = task_info.last_sample_at if task_info else None
            if last_dt is None:
                last_dt = self.db.analytics.last_point_timestamp(task_id)
            self._last_dt_cache_value = last_dt
            self._last_dt_cache_task_id = task_id
        return self._last_dt_cache_value

    def _compute_since(self, task_id: str, task_info=None) -> Optional[datetime]:
        """
        把当前选中的时间范围换算成查询起点（datetime，仓储只收 datetime）。

        锚点为该任务最后一个数据点的时间（tasks.last_sample_at，为空才回退
        analytics.last_point_timestamp），而非当前时刻——停止已久的任务选"最近1小时"
        仍应能看到其最后一小时的数据。绘图用的 epoch float 不得回流到这里。

        Returns:
            Optional[datetime]: 选中"全部"或该任务尚无数据点时返回 None（不过滤）
        """
        range_seconds = TIME_RANGE_SECONDS.get(self.current_range_key)
        if range_seconds is None:
            return None
        last_dt = self._get_cached_last_dt(task_id, task_info)
        if last_dt is None:
            return None
        return last_dt - timedelta(seconds=range_seconds)

    def _load_task_data(self, task_id: str, metric_type: str = None,
                        keep_view: bool = False):
        """
        加载任务指定指标的数据并显示：表格与图表均按当前选中的时间范围过滤、限流
        查询，避免大数据量任务把整张表拖进内存/渲染导致界面卡顿；统计条统一取自
        legacy_stats（覆盖所选范围内全部数据）；导出页仍走全量数据。
        查询与自动刷新共用 query_history_data，保证同口径。

        读库失败（StorageError）显示"读取数据失败"并弹错误提示，而不是"暂无数据"。

        Args:
            task_id: 任务ID
            metric_type: 指标类型
            keep_view: True 时保留用户当前的缩放/平移（自动刷新用）；False 时让图表
                重新自适应范围，避免切指标后曲线跑出视野
        """
        # 同步加载比在途的后台刷新更新：让在途结果过期，避免它回来覆盖本次显示
        self._cancel_pending_refresh()

        try:
            task_info = self.db.tasks.get(task_id)
            if not task_info:
                self.services.notifier.error(self, "错误", "无法加载任务信息")
                return
            since = self._compute_since(task_id, task_info)
            table_points, stats, chart_points, markers = query_history_data(
                self.db, task_id, metric_type, since, TABLE_POINT_LIMIT, CHART_MAX_BUCKETS)
        except StorageError as e:
            logger.error("历史页读取数据失败: task_id=%s", task_id, exc_info=True)
            self._show_load_failed(str(e))
            return

        if not table_points:
            self._show_no_data()
            return

        self._apply_loaded_data(
            metric_type, table_points, stats, chart_points, markers, keep_view)

    def _show_load_failed(self, detail: str):
        """读库失败：空状态显示"读取数据失败"（区别于"暂无数据"），并弹错误提示。"""
        self._clear_display("读取数据失败", "数据库读取出错，可稍后点击刷新重试")
        self.services.notifier.error(self, "读取数据失败", detail)

    def _show_no_data(self):
        """所选范围内没有数据点：按当前所选范围判断提示文案（而不是与默认范围比较）。"""
        if self.current_range_key != 'all':
            self._clear_display(
                "所选时间范围内暂无数据",
                "尝试切换到「全部」查看完整历史")
        else:
            self._clear_display(
                "该任务暂无数据",
                "产生新的采样数据后此处会自动更新")

    def _apply_loaded_data(self, metric_type, table_points, stats, chart_points,
                           markers, keep_view: bool):
        """把已查好的数据应用到界面（图表/表格/统计条/刷新间隔）。纯主线程渲染，不查库；
        同步加载与后台自动刷新共用，保证两条路径的显示行为一致。"""
        # 打点：先于绘图设置，_redraw_chart 会一并画出竖线
        self._markers = markers

        self._update_chart(chart_points, metric_type)
        self._update_table(
            table_points, metric_type,
            total=stats['count'] if stats else None)
        self._update_stats(stats, metric_type)
        if stats:
            self._update_auto_refresh_interval(stats['count'])
        if not keep_view:
            self.chart_widget.enableAutoRange()
            self._apply_min_y_span(metric_type)
        self.content_stack.setCurrentWidget(self.analysis_page)

    def _apply_min_y_span(self, metric_type):
        """数据几乎是平线时，纵轴跨度扩到下限（与迷你趋势图同一规则），
        避免把几字节的抖动放大成满幅、刻度出现多位小数。跨度足够时保持自动范围。"""
        if not self._chart_y:
            return
        low, high = compute_y_range(self._chart_y, metric_type)
        if high - low > max(self._chart_y) - min(self._chart_y):
            self.chart_widget.setYRange(low, high, padding=0.05)

    def _update_chart(self, data_points, metric_type):
        """
        更新图表：缓存 x（epoch float）/y 数组供悬停吸附与主题重绘复用，
        再统一交给 _redraw_chart 完成实际绘制

        Args:
            data_points: 分桶降采样后的数据点列表（按 timestamp 升序）
            metric_type: 指标类型
        """
        self._chart_metric_type = metric_type
        if data_points:
            # 绘图 x 数据统一用 epoch float，仅用于绘图，严禁回流到 SQL 查询参数
            self._chart_x = [dp.timestamp.timestamp() for dp in data_points]
            self._chart_y = [dp.value for dp in data_points]
        else:
            self._chart_x = []
            self._chart_y = []

        reference_value = max((abs(value) for value in self._chart_y), default=0.0)
        self._chart_display_unit = (
            get_metric_display_unit(metric_type, reference_value)
            if metric_type else '')
        # 计数类指标强制整数刻度；字节类的刻度间距按显示单位选取
        self._value_axis.set_display_unit(
            self._chart_display_unit,
            integer_only=bool(metric_type) and is_integer_metric(metric_type))

        self._redraw_chart()

        if metric_type:
            metric_name = get_metric_display_name(metric_type)
            unit = self._chart_display_unit
            axis_text = f"{metric_name}（{unit}）" if unit else metric_name
        else:
            axis_text = '值'
        self.chart_widget.setLabel('left', axis_text)
        self.chart_widget.setLabel('bottom', '时间')

    def _redraw_chart(self):
        """
        按当前缓存的 _chart_x/_chart_y 数组重绘曲线与打点竖线。

        chart_widget.clear() 会清空 PlotItem 下全部 item（含十字线），因此每次
        都要重新加入；主题切换时也复用缓存数组走这里重绘，不重新查库。
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

        # 打点竖线：颜色随主题；ignoreBounds 保证它不参与自动范围计算
        marker_pen = pg.mkPen(color=colors['marker'], width=1, style=Qt.DashLine)
        for marker in self._markers:
            line = pg.InfiniteLine(
                pos=marker.timestamp.timestamp(), angle=90, movable=False,
                pen=marker_pen, label=marker.label,
                labelOpts={
                    'position': 0.94, 'color': colors['marker'],
                    'movable': False, 'rotateAxis': (1, 0),
                })
            line.setToolTip(marker.label)
            self.chart_widget.addItem(line, ignoreBounds=True)

    @staticmethod
    def _nearest_index(x_array: list, x: float) -> int:
        """在按升序排列的 x_array 中二分查找与 x 最接近的下标（悬停吸附用）"""
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
        悬停十字线跟随鼠标并吸附到最近的绘制点；读数里附带所处的打点区间。

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
        formatted_value = format_metric_value(
            self._chart_metric_type, y_val,
            display_unit=self._chart_display_unit)
        text = f"{time_str} · {formatted_value}"
        context = marker_context(
            [m.timestamp.timestamp() for m in self._markers],
            [m.label for m in self._markers], x_val)
        if context:
            text += f" · {context}"
        self.hover_label.setText(text)

    def eventFilter(self, obj, event):
        """
        鼠标离开图表区域时清空十字线与悬停读数。

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

    def _update_stats(self, stats: Optional[dict], metric_type: Optional[str]):
        """
        更新统计条：起点、当前、变化量、增速、最小、最大。

        Args:
            stats: analytics.legacy_stats 的结果（覆盖所选范围内全部数据）；
                None 时全部显示 "--"
            metric_type: 指标类型
        """
        texts = build_stat_texts(metric_type, stats, self._chart_display_unit)
        self._set_stat_values(texts)

    def _set_stat_values(self, texts: dict):
        """把各格文本写入统计条（完整文本同时放进 tooltip，防止被窄列裁剪）。"""
        for key in STAT_KEYS:
            text = texts.get(key, "--")
            label = self.stat_value_labels[key]
            label.setText(text)
            label.setToolTip(text if text != "--" else "")

    def _update_table(self, data_points, metric_type, total: Optional[int] = None):
        """
        更新表格

        Args:
            data_points: 数据点列表（limit 子查询已按 timestamp 升序返回；
                         这里仍需 reversed() 翻转填充，使表格保持"倒序显示，最新的
                         在前"的既有用户可见行为，勿删）
            metric_type: 指标类型
            total: 所选范围内的数据点总数（来自统计的 count）；None 时按已显示条数
        """
        shown = len(data_points)
        total = shown if total is None else max(total, shown)
        self.table_hint_label.setText(
            f"最新数据优先 · 显示最近 {shown:,} 条 / 共 {total:,} 条")

        # 大批量 setItem 前关闭界面更新与排序，避免逐行触发重排/重绘拖慢填表
        self.data_table.setUpdatesEnabled(False)
        self.data_table.setSortingEnabled(False)
        try:
            # 设置行数
            self.data_table.setRowCount(len(data_points))

            # 倒序显示，最新的在前
            for i, dp in enumerate(reversed(data_points)):
                # 时间：含月日的绝对时间，避免跨天数据歧义
                time_str = dp.timestamp.strftime(TABLE_TIME_FORMAT)
                time_item = QTableWidgetItem(time_str)
                time_item.setFont(self._detail_data_font)
                time_item.setTextAlignment(Qt.AlignCenter)
                self.data_table.setItem(i, 0, time_item)

                # 格式化的值
                formatted_value = format_metric_value(
                    metric_type, dp.value,
                    display_unit=self._chart_display_unit)
                value_item = QTableWidgetItem(formatted_value)
                value_item.setFont(self._detail_data_font)
                value_item.setTextAlignment(Qt.AlignCenter)
                self.data_table.setItem(i, 1, value_item)

                # 原始值
                raw_value_item = QTableWidgetItem(f"{dp.value:.4f}")
                raw_value_item.setFont(self._detail_data_font)
                raw_value_item.setTextAlignment(Qt.AlignCenter)
                self.data_table.setItem(i, 2, raw_value_item)
        finally:
            # 表格本就未开启排序（TableWidget 默认不排序），此处只需恢复界面更新
            self.data_table.setUpdatesEnabled(True)

    def _clear_display(
            self,
            empty_title: str = "请选择监控任务",
            empty_detail: str = "选定任务和指标后即可查看历史数据"):
        """清空图表、表格和统计值，并显示对应的内联空状态。"""
        self._chart_x = []
        self._chart_y = []
        self._chart_metric_type = None
        self._chart_display_unit = ''
        self._markers = []
        self._value_axis.set_display_unit('')
        self._redraw_chart()
        self.data_table.setRowCount(0)
        self.table_hint_label.setText("最新数据优先")
        self._update_stats(None, None)
        self._show_empty_state(empty_title, empty_detail)

    def _update_delete_button_state(self):
        """
        根据当前选中任务状态刷新删除按钮可用性：运行中任务禁止直接删除（需先停止），
        避免删掉正在写入的任务数据引发竞态
        """
        if not self.current_task_id:
            self.delete_button.setEnabled(False)
            self.delete_button.setToolTip("")
            self.delete_action.setEnabled(False)
            self.delete_action.setToolTip("")
        elif self.current_task_status == TaskStatus.RUNNING:
            self.delete_button.setEnabled(False)
            self.delete_button.setToolTip("任务正在运行中，请先停止任务再删除")
            self.delete_action.setEnabled(False)
            self.delete_action.setToolTip("任务正在运行中，请先停止任务再删除")
        else:
            self.delete_button.setEnabled(True)
            self.delete_button.setToolTip("")
            self.delete_action.setEnabled(True)
            self.delete_action.setToolTip("")

    def _on_delete_task_clicked(self):
        """删除此任务数据按钮点击事件：确认对话框 -> manager.delete_task_data -> 本页刷新"""
        if not self.current_task_id:
            return

        notifier = self.services.notifier
        if self.current_task_status == TaskStatus.RUNNING:
            notifier.warning(self, "提示", "任务正在运行中，请先停止任务再删除",
                             duration=2000)
            return

        task = self.db.get_task(self.current_task_id)
        task_desc = f"{task.process_name} (PID: {task.pid})" if task else self.current_task_id

        confirmed = self.services.dialogs.confirm(
            self.window(),
            "确认删除",
            f"确定要删除任务「{task_desc}」的全部历史数据吗？此操作不可恢复。",
            "删除",
        )
        if not confirmed:
            return

        if self.manager.delete_task_data(self.current_task_id):
            notifier.success(self, "删除成功", "该任务的历史数据已删除")
            self._load_tasks()
        else:
            notifier.error(self, "删除失败", "删除任务数据失败，请查看日志")

    def _save_chart_as_png(self):
        """保存当前图表为 PNG 图片"""
        notifier = self.services.notifier
        if not self.current_task_id or not self._chart_x:
            notifier.warning(self, "提示", "当前无图表数据可导出", duration=2000)
            return

        task_info = self.db.get_task(self.current_task_id)
        process_name = (task_info.process_name if task_info else "task").replace('.exe', '')
        metric_name = get_metric_display_name(self.current_metric) if self.current_metric else "指标"
        stamp = self.services.clock.now().strftime('%Y%m%d%H%M%S')
        default_name = f"{process_name}_{metric_name}_{stamp}.png"

        save_path = self.services.dialogs.ask_save_path(
            self, "保存图表为图片", default_name, "PNG 图片 (*.png)")
        if not save_path:
            return

        try:
            exporter = exporters.ImageExporter(self.chart_widget.getPlotItem())
            exporter.parameters()['width'] = 1600
            exporter.export(save_path)
        except Exception:
            logger.error("保存图表图片失败", exc_info=True)
            notifier.error(self, "保存失败", "保存图表图片失败，请查看日志")
            return

        notifier.success(self, "保存成功", f"图表已保存到 {save_path}")

    def _copy_chart_image(self):
        """复制当前图表为图片到剪贴板"""
        notifier = self.services.notifier
        if not self.current_task_id or not self._chart_x:
            notifier.warning(self, "提示", "当前无图表数据可复制", duration=2000)
            return

        try:
            pixmap = self.chart_widget.grab()
            self.services.dialogs.copy_pixmap(pixmap)
        except Exception:
            logger.error("复制图表到剪贴板失败", exc_info=True)
            notifier.error(self, "复制失败", "复制图表到剪贴板失败，请查看日志")
            return

        notifier.success(self, "复制成功", "图表已复制到剪贴板")

    def _apply_chart_theme(self):
        """
        应用图表配色：背景、坐标轴、十字线均按 chart_colors() 取色；曲线
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
        """qconfig.themeChanged 回调：全局主题切换时重新取色重绘图表"""
        self._apply_chart_theme()

    def resizeEvent(self, event):
        """页面宽度改变时更新筛选区与统计条的响应式排列。"""
        super().resizeEvent(event)
        if getattr(self, 'select_layout', None) is not None:
            self._update_filter_layout(self.viewport().width())
        if getattr(self, '_stat_cells', None):
            self._update_stats_layout(self.viewport().width())

    def showEvent(self, event):
        """页面显示事件：每次显示都刷新任务列表，并开始运行中任务的自动刷新计时"""
        super().showEvent(event)
        self._load_tasks()
        self._auto_refresh_timer.start()

    def hideEvent(self, event):
        """页面隐藏时停止自动刷新计时，避免后台页面白白查库"""
        super().hideEvent(event)
        self._auto_refresh_timer.stop()
        # 不阻塞等待在途查询：只让它的结果过期，线程自行跑完后由 finished 释放
        self._cancel_pending_refresh()

    # ========== 运行中任务自动刷新 ==========

    @staticmethod
    def _auto_refresh_interval_for(count: int) -> int:
        """按范围内数据点数返回自动刷新间隔（毫秒），分档见模块顶部常量。"""
        if count < AUTO_REFRESH_TIER1_COUNT:
            return AUTO_REFRESH_INTERVAL_MS
        if count < AUTO_REFRESH_TIER2_COUNT:
            return AUTO_REFRESH_INTERVAL_MID_MS
        return AUTO_REFRESH_INTERVAL_SLOW_MS

    def _update_auto_refresh_interval(self, count: int):
        """依据当前统计的数据点数调整定时器间隔（仅在变化时设置，避免重置计时）。"""
        interval = self._auto_refresh_interval_for(count)
        if self._auto_refresh_timer.interval() != interval:
            self._auto_refresh_timer.setInterval(interval)

    def _should_skip_auto_refresh(self) -> bool:
        """本次自动刷新是否应跳过（定时器继续）：
        - 当前在"明细"视图（图表/统计不可见，刷新白耗主线程）；
        - 鼠标正悬停在图表上（十字线可见，刷新会重绘并打断读数）；
        - 主窗口已最小化。
        """
        if self.view_stack.currentIndex() != 0:
            return True
        if self.crosshair_line.isVisible():
            return True
        window = self.window()
        if window is not None and window.isMinimized():
            return True
        return False

    def _on_auto_refresh(self):
        """
        定时器回调：页面可见且所选任务仍在运行时，复用现有加载流程刷新数据
        （保留用户的任务/指标/范围选择与图表缩放）；任务刚结束时整体重载一次，
        让"运行中"标记、删除按钮与最终数据同步到位。明细视图、鼠标悬停图表、
        窗口最小化时跳过本次刷新。
        """
        if not self.isVisible():
            self._auto_refresh_timer.stop()
            return
        self._refresh_task_option_texts()
        if not self.current_task_id:
            return
        if self._should_skip_auto_refresh():
            return

        if self._is_task_running(self.current_task_id):
            self._auto_refresh_was_running = True
            self._invalidate_last_dt_cache()
            self._start_auto_refresh_worker()
        elif self._auto_refresh_was_running:
            self._auto_refresh_was_running = False
            self._load_tasks()

    # ========== 自动刷新：后台查询 + 主线程应用 ==========

    def background_threads(self) -> list:
        """退出时需要收尾的后台线程（由主窗口统一 shutdown）：自动刷新查询只读、
        无取消机制，仅带超时等待，避免退出时线程仍在读库。"""
        return [BackgroundThread("历史自动刷新", self._refresh_worker, timeout_ms=3000)]

    def _cancel_pending_refresh(self):
        """使在途的后台刷新结果过期（推进请求序号）；不等待线程，线程自行跑完。"""
        self._refresh_request_seq += 1

    def _start_auto_refresh_worker(self):
        """
        在后台线程执行自动刷新的全部查询。同一时刻最多一个 worker：上一个未结束时
        跳过本次 tick（下一次 tick 再来）。结果带请求序号与 (任务, 指标, 范围) 标识，
        回到主线程后若已过期则丢弃。
        """
        if self._refresh_worker is not None:
            return
        task_id = self.current_task_id
        if not task_id:
            return

        self._refresh_request_seq += 1
        worker = HistoryRefreshWorker(
            self.db, self._refresh_request_seq, task_id, self.current_metric,
            self.current_range_key, TIME_RANGE_SECONDS.get(self.current_range_key),
            TABLE_POINT_LIMIT, CHART_MAX_BUCKETS)
        worker.result_ready.connect(self._on_refresh_result)
        # 先清引用、再 deleteLater（同一信号按连接顺序执行）：保证 background_threads()
        # 交给主窗口的 _refresh_worker 要么仍有效、要么已是 None，不会拿到已删除的对象
        worker.finished.connect(lambda w=worker: self._on_refresh_worker_finished(w))
        worker.finished.connect(worker.deleteLater)
        self._refresh_worker = worker
        worker.start()

    def _on_refresh_worker_finished(self, worker):
        """线程结束：释放引用，允许下一次 tick 启动新的 worker。"""
        if self._refresh_worker is worker:
            self._refresh_worker = None

    def _on_refresh_result(self, result: HistoryRefreshResult):
        """
        主线程槽：应用后台查询结果（保留用户当前缩放，keep_view=True）。
        请求序号过期、或用户已切换任务/指标/范围时丢弃；此刻已进入应跳过刷新的状态
        （明细视图/悬停图表/窗口最小化）时同样丢弃，等下一次 tick。
        """
        if result.request_id != self._refresh_request_seq:
            return
        if (result.task_id != self.current_task_id
                or result.metric_type != self.current_metric
                or result.range_key != self.current_range_key):
            return
        if result.error is not None:
            logger.warning("历史页自动刷新失败，保持当前显示: %s", result.error)
            # 存储故障：保持已显示的数据，但让用户知道读取失败；连续失败只提示一次，
            # 避免每个刷新周期弹一条
            if result.storage_error and not self._refresh_error_notified:
                self._refresh_error_notified = True
                self.services.notifier.error(self, "读取数据失败", result.error)
            return
        self._refresh_error_notified = False
        if result.task_missing:
            return
        if self._should_skip_auto_refresh():
            return

        # 回填锚点缓存：与同步路径一致，后续切范围/指标可直接复用
        self._last_dt_cache_task_id = result.task_id
        self._last_dt_cache_value = result.last_dt

        if not result.table_points:
            self._show_no_data()
            return
        self._apply_loaded_data(
            result.metric_type, result.table_points, result.stats,
            result.chart_points, result.markers, keep_view=True)
