"""
监控任务卡片
实时显示单个任务各指标的当前值、较基线（无基线时比开始时）的变化量与趋势；任务
结束后卡片保留并收成一行（进程名 · PID · 结束原因 · 结束时间 · 已记录 N 次），
供用户一键跳转分析或同配置再测。

卡片只负责显示：基线值由 MonitorPresenter 按"基线打点之后 5 个样本的中位数"
算好后交给卡片；"现在几点"也由 Presenter 经注入的时钟传入，卡片不自己读系统时间。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QGridLayout, QSizePolicy)
from qfluentwidgets import (
    PushButton, CardWidget, FluentIcon, CaptionLabel, StrongBodyLabel, Theme,
    TransparentToolButton, isDarkTheme
)
from qfluentwidgets.common.icon import FluentIconBase, SvgIconEngine, drawSvgIcon

from core.contracts import StopReason, UnavailableReason
from ui import theme_tokens as tokens
from ui.components.sparkline import SparklineWidget
from ui.formatting import (
    is_abnormal_stop, stop_reason_text, task_status_text, truncate_text, unavailable_reason_text,
)
from ui.typography import DataCaptionLabel, MetricValueLabel
from utils.metrics import (
    format_metric_delta, format_metric_value, get_metric_description,
    get_metric_display_name
)

# 状态胶囊：(文案, 浅色主题色, 深色主题色)。CaptionLabel.setTextColor 只需设置一次
# 即可自动跟随主题切换（FluentLabelBase 内部已连接 qconfig.themeChanged）。
# 颜色取自 ui.theme_tokens，与分析页判定胶囊同源（运行中=平稳=成功色，暂停=波动=注意色，
# 异常结束=持续上涨=危险色）
STATUS_RUNNING = 'running'
STATUS_PAUSED = 'paused'
STATUS_STOPPING = 'stopping'
STATUS_FINISHED = 'finished'      # 用户停止（正常结束）
STATUS_ABNORMAL = 'abnormal'      # 被测进程退出、内部错误等（异常结束）

STATUS_STYLES = {
    STATUS_RUNNING: (task_status_text(True), *tokens.colors(tokens.SUCCESS)),
    STATUS_PAUSED: (task_status_text(True, paused=True), *tokens.colors(tokens.CAUTION)),
    STATUS_STOPPING: ("正在停止…", *tokens.colors(tokens.TERTIARY)),
    STATUS_FINISHED: (task_status_text(False), *tokens.colors(tokens.TERTIARY)),
    STATUS_ABNORMAL: (task_status_text(False, abnormal=True), *tokens.colors(tokens.CRITICAL)),
}

# 卡片身份行里备注的最大显示字数（完整内容放 tooltip）
NOTE_DISPLAY_CHARS = 40
# 卡片悬停提示里各段（方案摘要、被测目标）之间空一行
TOOLTIP_SECTION_SEPARATOR = "\n\n"

BASELINE_SAMPLING_TEXT = "基线采样中"
# 6 列时指标格最窄："比开始时（12 分钟前）+85.23 MB"会被截断，改为"较开始 +85.23 MB"
COMPACT_START_PREFIX = "较开始"
STOP_TOOLTIP = "停止后不能继续记录到这个任务；要继续请用“同配置再测”"
PAUSE_TOOLTIP = "暂停采集（会记一个暂停打点），恢复后继续记录到这个任务"
MARKER_TOOLTIP = "记下当前时刻（默认名称依次为：基线、第 1 轮、第 2 轮…）"
ANALYSIS_TOOLTIP = "查看本任务的趋势与各轮增量"
RESTART_TOOLTIP = "按进程名查找进程，并以相同的指标、间隔和备注新建任务"

# 指标格列数随卡片宽度在 [MIN, MAX] 之间自适应；每格至少 CELL_MIN_WIDTH 宽
MIN_VALUE_COLUMNS = 3
MAX_VALUE_COLUMNS = 6
CELL_MIN_WIDTH = 150
CELL_SPACING = 16
CARD_PADDING = 16

# 停止图标：16px 实心圆角方块（媒体播放器通行的"停止"）。FluentIcon 里没有合适的：
# CANCEL 是撤销箭头、CLOSE 是关闭叉号，都会被误读成"取消/关掉卡片"
_STOP_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
             'viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="1.5" '
             'fill="{fill}"/></svg>')


class StopIcon(FluentIconBase):
    """自绘停止图标（不依赖图标文件，打包无需附带资源），颜色随主题切换。"""

    @staticmethod
    def _svg(theme: Theme, fill: str = None) -> str:
        if fill is None:
            dark = isDarkTheme() if theme == Theme.AUTO else theme == Theme.DARK
            fill = "#ffffff" if dark else "#000000"
        return _STOP_SVG.format(fill=fill)

    def path(self, theme=Theme.AUTO) -> str:
        # 没有图标文件；库只在 render/icon 里用到 path，二者都已覆盖
        return ""

    def icon(self, theme=Theme.AUTO, color: QColor = None) -> QIcon:
        fill = QColor(color).name() if color else None
        return QIcon(SvgIconEngine(self._svg(theme, fill)))

    def render(self, painter, rect, theme=Theme.AUTO, indexes=None, **attributes):
        drawSvgIcon(self._svg(theme, attributes.get('fill')).encode(), painter, rect)


# ========== 文案小工具（卡片专用） ==========

def format_clock(seconds: float) -> str:
    """已运行时长的钟表格式 hh:mm:ss（小时可超过 24）。"""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_elapsed(seconds: float) -> str:
    """已运行时长的口语格式：45 秒 / 2 分 15 秒 / 1 小时 3 分。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分 {total % 60} 秒"
    return f"{total // 3600} 小时 {total % 3600 // 60} 分"


def format_ago(seconds: float) -> str:
    """相对时间：45 秒前 / 12 分钟前 / 2 小时 5 分钟前。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} 秒前"
    if total < 3600:
        return f"{total // 60} 分钟前"
    return f"{total // 3600} 小时 {total % 3600 // 60} 分钟前"


def format_interval(interval: Optional[float]) -> str:
    """采集间隔：每 1 秒 / 每 0.5 秒；未知时返回空串。"""
    if not interval:
        return ""
    return f"每 {interval:g} 秒"


def format_end_time(end: datetime, now: Optional[datetime] = None) -> str:
    """结束时间：当天只写 HH:mm:ss，跨天写 MM-DD HH:mm（完整年份放 tooltip）。"""
    if now is None or now.date() == end.date():
        return end.strftime('%H:%M:%S')
    return end.strftime('%m-%d %H:%M')


def value_columns_for(width: int, metric_count: int) -> int:
    """指标格列数：按可用宽度在 3–6 之间取，指标少时不留多余的空列。"""
    available = max(0, width - 2 * CARD_PADDING)
    columns = (available + CELL_SPACING) // (CELL_MIN_WIDTH + CELL_SPACING)
    columns = max(MIN_VALUE_COLUMNS, min(MAX_VALUE_COLUMNS, columns))
    return min(columns, max(metric_count, MIN_VALUE_COLUMNS))


@dataclass
class CardBaseline:
    """卡片上的基线：基线打点时刻与各指标基线值（尚在采样的指标不在 values 里）。"""
    timestamp: datetime
    values: Dict[str, float] = field(default_factory=dict)


class MetricCell(QWidget):
    """指标格：名 / 值（20px Semibold）/ 较基线变化。点击即把迷你趋势图切到该指标。"""

    clicked = pyqtSignal(str)

    def __init__(self, metric_type: str, parent=None):
        super().__init__(parent)
        self.metric_type = metric_type
        self._selected = False
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        display_name = get_metric_display_name(metric_type)
        description = get_metric_description(metric_type)
        tooltip = f"{display_name}：{description}" if description else display_name
        self.setToolTip(f"{tooltip}\n点击在下方趋势图查看它的走势")

        self.name_label = CaptionLabel(display_name, self)
        self.name_label.setMinimumWidth(1)
        self.name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.value_label = MetricValueLabel("--", self)
        self.value_label.setMinimumWidth(1)
        self.value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.delta_label = DataCaptionLabel("", self)
        self.delta_label.setMinimumWidth(1)
        self.delta_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.name_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.delta_label)

    def is_selected(self) -> bool:
        return self._selected

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit(self.metric_type)

    def paintEvent(self, event):
        if not (self._selected or self._hover):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(tokens.qcolor(tokens.CELL_HIGHLIGHT))
        painter.drawRoundedRect(QRectF(self.rect()), 4, 4)
        if self._selected:
            # 选中态：左缘强调色竖条（强调色只用于选中态与主按钮；对底色不足 3:1 时自动加深）
            painter.setBrush(tokens.selection_bar_color())
            painter.drawRoundedRect(QRectF(0, 6, 3, max(0, self.height() - 12)), 1.5, 1.5)
        painter.end()


class TaskCard(CardWidget):
    """监控任务卡片组件"""

    # 按钮信号一律只携带 task_id：TaskCard 不持有 manager，只 emit 信号，经
    # TaskBoard 转发给 MonitorPresenter，后者调用 manager 并在成功后回调
    # set_paused() 等方法更新显示
    stop_requested = pyqtSignal(str)
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    marker_requested = pyqtSignal(str)
    history_requested = pyqtSignal(str)    # "分析"/"查看分析"（运行中与已结束卡片都有）
    restart_requested = pyqtSignal(str)    # 已结束卡片："同配置再测"
    close_requested = pyqtSignal(str)      # 已结束卡片：行尾 ✕（从列表移除）

    def __init__(self, task_id: str, process_name: str, pid: int,
                 metric_types: List[str], parent=None, *,
                 interval: Optional[float] = None, note: str = ''):
        """
        初始化任务卡片

        Args:
            task_id: 任务ID
            process_name: 进程名称
            pid: 进程ID
            metric_types: 监控指标类型列表
            parent: 父窗口
            interval: 采集间隔（秒），用于身份行显示与"同配置再测"时沿用
            note: 测试备注
        """
        super().__init__(parent)

        self.task_id = task_id
        self.process_name = process_name
        self.pid = pid
        self.metric_types = list(metric_types)
        self.interval = interval
        self.note = note or ''

        # 指标格与其中的值/变化量标签 {指标类型: 控件}
        self.cells: Dict[str, MetricCell] = {}
        self.value_labels: Dict[str, QLabel] = {}
        self.delta_labels: Dict[str, QLabel] = {}
        self._columns = 0

        # 本任务各指标首个有效值（"比开始时"的参照）与最新值 {指标类型: 值}
        self.start_values: Dict[str, float] = {}
        self.last_values: Dict[str, float] = {}
        # 当前无法采集的指标：变化量显示 "--"，之后采到值即恢复
        self._unavailable: Set[str] = set()

        # 基线（时间与各指标基线值），没有基线时为 None
        self.baseline: Optional[CardBaseline] = None

        # 时间锚点：任务开始、首个样本、最近一次已知的"现在"、结束时间
        self.started_at: Optional[datetime] = None
        self.first_sample_at: Optional[datetime] = None
        self.now: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.stop_reason: Optional[StopReason] = None

        # 采集次数：UI 内存计数，卡片创建时置 0，每次收到 data_updated 加 1，
        # 不查库（当前没有"重启后恢复运行中任务"的路径）
        self.count = 0

        # 暂停/结束状态；卡片创建时任务必为运行态
        self.is_paused = False
        # 暂停的原因说明（目前只有"磁盘空间不足"由存储守卫自动暂停），None 表示用户自己暂停
        self.pause_reason: Optional[str] = None
        self.is_finished = False
        self.status = STATUS_RUNNING

        # 迷你趋势图当前显示的指标（默认首指标，点击指标格切换）
        self.trend_metric: Optional[str] = self.metric_types[0] if self.metric_types else None

        # 卡片悬停提示 = 调用方给的内容（如自动化方案摘要）+ 被测目标摘要行
        self._base_tooltip = ''
        self._target_lines: List[str] = []

        self._init_ui()
        self._apply_status(STATUS_RUNNING)
        self._refresh_running_texts()
        self._refresh_deltas()

    # ========== 构建界面 ==========

    def _init_ui(self):
        """初始化UI"""
        # 卡片随指标数量自适应增高，水平方向由页面宽度决定。不要显式设置最小高度：
        # 显式值会覆盖布局算出的最小尺寸，卡片多时会被滚动区压扁、文字重叠
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING)
        layout.setSpacing(12)

        # 顶部一行：身份（进程名 ●状态 · PID · 每 N 秒 · 已运行 · 备注）与操作按钮；
        # 结束后这一行就是整张卡片（· 结束原因 · 结束时间 · 已记录 N 次）
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        self.title_label = StrongBodyLabel(self.process_name)
        self.title_label.setToolTip(self.process_name)
        # 长进程名允许被压缩，不与右侧操作按钮共同撑宽卡片
        self.title_label.setMinimumWidth(1)
        self.title_label.setMaximumWidth(260)
        header_layout.addWidget(self.title_label)

        self.status_label = CaptionLabel("")
        header_layout.addWidget(self.status_label)

        self.meta_label = DataCaptionLabel("")
        self.meta_label.setMinimumWidth(1)
        header_layout.addWidget(self.meta_label)

        # 结束原因 · 结束时间 · 已记录 N 次：任务结束后才显示
        self.finished_detail_label = CaptionLabel("")
        self.finished_detail_label.setMinimumWidth(1)
        self.finished_detail_label.setVisible(False)
        header_layout.addWidget(self.finished_detail_label)

        # 测试备注（可选）：只在有备注时显示，排在身份行最后，空间不够时先被压缩
        self.note_label = CaptionLabel(
            f"· {truncate_text(self.note, NOTE_DISPLAY_CHARS)}")
        self.note_label.setToolTip(f"备注：{self.note}")
        self.note_label.setMinimumWidth(1)
        self.note_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.note_label.setVisible(bool(self.note))
        header_layout.addWidget(self.note_label, 1)
        if not self.note:
            header_layout.addStretch(1)

        # 运行中的操作：打点 / 分析 / 暂停 | 停止。停止是不可逆操作，与暂停拉开间距
        self.running_actions = QWidget(self)
        running_layout = QHBoxLayout(self.running_actions)
        running_layout.setContentsMargins(0, 0, 0, 0)
        running_layout.setSpacing(8)

        self.marker_button = PushButton("打点", self, FluentIcon.TAG)
        self.marker_button.setToolTip(MARKER_TOOLTIP)
        self.marker_button.clicked.connect(
            lambda: self.marker_requested.emit(self.task_id))
        self.marker_button.setFixedWidth(88)
        running_layout.addWidget(self.marker_button)

        self.analysis_button = PushButton("分析", self, FluentIcon.MARKET)
        self.analysis_button.setToolTip(ANALYSIS_TOOLTIP)
        self.analysis_button.clicked.connect(
            lambda: self.history_requested.emit(self.task_id))
        self.analysis_button.setFixedWidth(88)
        running_layout.addWidget(self.analysis_button)

        self.pause_button = PushButton("暂停", self, FluentIcon.PAUSE)
        self.pause_button.setToolTip(PAUSE_TOOLTIP)
        self.pause_button.clicked.connect(self._on_pause_button_clicked)
        self.pause_button.setFixedWidth(88)
        running_layout.addWidget(self.pause_button)

        running_layout.addSpacing(12)

        self.stop_button = PushButton("停止", self, StopIcon())
        self.stop_button.setToolTip(STOP_TOOLTIP)
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.task_id))
        self.stop_button.setFixedWidth(88)
        running_layout.addWidget(self.stop_button)
        header_layout.addWidget(self.running_actions, 0, Qt.AlignRight | Qt.AlignVCenter)

        # 已结束后的操作：查看分析 / 同配置再测 / 行尾 ✕
        self.finished_actions = QWidget(self)
        finished_layout = QHBoxLayout(self.finished_actions)
        finished_layout.setContentsMargins(0, 0, 0, 0)
        finished_layout.setSpacing(8)

        self.history_button = PushButton("查看分析", self, FluentIcon.MARKET)
        self.history_button.setToolTip(ANALYSIS_TOOLTIP)
        self.history_button.clicked.connect(
            lambda: self.history_requested.emit(self.task_id))
        finished_layout.addWidget(self.history_button)

        self.restart_button = PushButton("同配置再测", self, FluentIcon.PLAY)
        self.restart_button.setToolTip(RESTART_TOOLTIP)
        self.restart_button.clicked.connect(
            lambda: self.restart_requested.emit(self.task_id))
        finished_layout.addWidget(self.restart_button)

        self.close_button = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_button.setToolTip("从列表移除")
        self.close_button.clicked.connect(
            lambda: self.close_requested.emit(self.task_id))
        finished_layout.addWidget(self.close_button)
        self.finished_actions.setVisible(False)
        header_layout.addWidget(self.finished_actions, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(header_layout)

        # 中部：指标格，列数随卡片宽度自适应（resizeEvent 里重排）
        self.values_box = QWidget(self)
        self.values_layout = QGridLayout(self.values_box)
        self.values_layout.setContentsMargins(0, 0, 0, 0)
        self.values_layout.setHorizontalSpacing(CELL_SPACING - 8)
        self.values_layout.setVerticalSpacing(8)
        for metric_type in self.metric_types:
            cell = MetricCell(metric_type, self.values_box)
            cell.clicked.connect(self.select_trend_metric)
            self.cells[metric_type] = cell
            self.value_labels[metric_type] = cell.value_label
            self.delta_labels[metric_type] = cell.delta_label
        self._relayout_cells(MIN_VALUE_COLUMNS)
        layout.addWidget(self.values_box)

        # 底部：迷你趋势图（标题行注明对象、变化量与运行概况）
        self.trend_box = QWidget(self)
        trend_layout = QVBoxLayout(self.trend_box)
        trend_layout.setContentsMargins(0, 0, 0, 0)
        trend_layout.setSpacing(4)
        trend_header = QHBoxLayout()
        trend_header.setSpacing(8)
        self.sparkline_label = CaptionLabel("")
        self.sparkline_label.setMinimumWidth(1)
        self.sparkline_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        trend_header.addWidget(self.sparkline_label, 1)

        self.trend_delta_label = DataCaptionLabel("")
        trend_header.addWidget(self.trend_delta_label)

        # 已运行 · 间隔 · 采集次数（内存计数，非落库条数）作为趋势的辅助上下文
        self.count_label = DataCaptionLabel("")
        self.count_label.setToolTip("采集次数为本次运行收到的采集周期数（非落库条数）")
        trend_header.addWidget(self.count_label)
        trend_layout.addLayout(trend_header)

        self.sparkline = SparklineWidget(metric_type=self.trend_metric)
        trend_layout.addWidget(self.sparkline)
        layout.addWidget(self.trend_box)
        self._refresh_trend_header()

    def _relayout_cells(self, columns: int) -> None:
        """按给定列数重排指标格（列数不变时不动）。"""
        if columns == self._columns:
            return
        was_compact = self._compact_deltas()
        for cell in self.cells.values():
            self.values_layout.removeWidget(cell)
        for i, metric_type in enumerate(self.metric_types):
            self.values_layout.addWidget(self.cells[metric_type], i // columns, i % columns)
        for column in range(MAX_VALUE_COLUMNS):
            self.values_layout.setColumnStretch(column, 1 if column < columns else 0)
        self._columns = columns
        # 列数跨过 6 列时变化量文案要在长短两种之间切换
        if self._compact_deltas() != was_compact:
            self._refresh_deltas()

    def value_columns(self) -> int:
        """指标格当前的列数。"""
        return self._columns

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout_cells(value_columns_for(event.size().width(), len(self.metric_types)))

    # ========== 迷你趋势对象 ==========

    def select_trend_metric(self, metric_type: str) -> None:
        """把迷你趋势图切到某个指标（点击指标格时调用）；不是本卡片的指标时忽略。"""
        if metric_type not in self.cells:
            return
        self.trend_metric = metric_type
        self.sparkline.set_metric(metric_type)
        self._refresh_trend_header()

    def _refresh_trend_header(self) -> None:
        for metric_type, cell in self.cells.items():
            cell.set_selected(metric_type == self.trend_metric)
        name = (get_metric_display_name(self.trend_metric)
                if self.trend_metric else "指标")
        self.sparkline_label.setText(f"{name} · 任务开始至今趋势")
        self.sparkline_label.setToolTip(
            f"{name}：从任务开始到现在的走势（点多时自动合并抽稀；竖线为打点，实线是基线）。"
            "点击上方其他指标格可切换")
        self.sparkline.setToolTip(f"任务开始至今的趋势（{name}）")
        if self.trend_metric:
            self.trend_delta_label.setText(self.delta_text(self.trend_metric))
            self.trend_delta_label.setToolTip(self._delta_tooltip())

    # ========== 文案计算 ==========

    def running_seconds(self) -> Optional[float]:
        """已运行秒数：开始时刻到结束时刻（未结束时到最近一次已知的"现在"）。"""
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else self.now
        if end is None:
            return None
        return max(0.0, (end - self.started_at).total_seconds())

    def _start_ago_text(self) -> str:
        """"比开始时"后面的相对时间"（12 分钟前）"；时间未知时为空串。"""
        if self.first_sample_at is None or self.now is None:
            return ""
        return f"（{format_ago((self.now - self.first_sample_at).total_seconds())}）"

    def delta_text(self, metric_type: str, compact: bool = False) -> str:
        """某指标的变化量文案：有基线时"较基线 +x"（基线值未采够时"基线采样中"），
        无基线时"比开始时（12 分钟前）+x"；compact=True（6 列时格子窄）改短为
        "较开始 +x"，完整文案放 tooltip。"""
        value = self.last_values.get(metric_type)
        available = value is not None and metric_type not in self._unavailable
        if self.baseline is not None:
            base = self.baseline.values.get(metric_type)
            if base is None and metric_type not in self._unavailable:
                return BASELINE_SAMPLING_TEXT
            if base is None or not available:
                return "较基线 --"
            return f"较基线 {format_metric_delta(metric_type, value - base)}"
        if not available:
            return f"{COMPACT_START_PREFIX} --" if compact else "比开始时 --"
        delta = format_metric_delta(metric_type, value - self.start_values[metric_type])
        if compact:
            return f"{COMPACT_START_PREFIX} {delta}"
        ago = self._start_ago_text()
        # 全角括号自带间隔，括号后不再加空格："比开始时（12 分钟前）+85.23 MB"
        return f"比开始时{ago}{delta}" if ago else f"比开始时 {delta}"

    def _delta_tooltip(self) -> str:
        if self.baseline is not None:
            return (f"相对基线（{self.baseline.timestamp.strftime('%H:%M:%S')} 打点后 "
                    "5 个样本的中位数）的变化量")
        return "相对本任务首个有效值的变化量"

    def _compact_deltas(self) -> bool:
        """指标格排到最多列时格子最窄，"比开始时（N 分钟前）"放不下，改用短文案。"""
        return self._columns >= MAX_VALUE_COLUMNS

    def _refresh_deltas(self) -> None:
        tooltip = self._delta_tooltip()
        compact = self._compact_deltas()
        for metric_type, label in self.delta_labels.items():
            text = self.delta_text(metric_type, compact)
            full = self.delta_text(metric_type)
            label.setText(text)
            label.setToolTip(tooltip if text == full else f"{full}\n{tooltip}")
        if self.trend_metric:
            self.trend_delta_label.setText(self.delta_text(self.trend_metric))
            self.trend_delta_label.setToolTip(tooltip)

    def _refresh_running_texts(self) -> None:
        """身份行"PID · 每 N 秒 · 已运行 hh:mm:ss"与底部"已运行 · 每 N 秒 · N 次采集"；
        结束后身份行只留 PID，其余信息放进结束说明。"""
        seconds = self.running_seconds()
        interval_text = format_interval(self.interval)

        meta = [f"PID {self.pid}"]
        if not self.is_finished:
            if interval_text:
                meta.append(interval_text)
            if seconds is not None:
                meta.append(f"已运行 {format_clock(seconds)}")
        self.meta_label.setText("· " + " · ".join(meta))

        footer = []
        if seconds is not None:
            footer.append(f"已运行 {format_elapsed(seconds)}")
        if interval_text:
            footer.append(interval_text)
        footer.append(f"{self.count} 次采集")
        self.count_label.setText(" · ".join(footer))

        if self.is_finished:
            self._refresh_finished_detail()

    def _refresh_finished_detail(self) -> None:
        reason = stop_reason_text(self.stop_reason) if self.stop_reason is not None else ""
        end = self.finished_at
        parts = [p for p in (reason, format_end_time(end, self.now) if end else "") if p]
        parts.append(f"已记录 {self.count} 次")
        self.finished_detail_label.setText("· " + " · ".join(parts))
        tips = []
        if end is not None:
            tips.append(f"结束于 {end.strftime('%Y-%m-%d %H:%M:%S')}")
        seconds = self.running_seconds()
        if seconds is not None:
            tips.append(f"已运行 {format_clock(seconds)}")
        interval_text = format_interval(self.interval)
        if interval_text:
            tips.append(interval_text)
        self.finished_detail_label.setToolTip(" · ".join(tips))

    def _apply_status(self, status: str) -> None:
        text, light, dark = STATUS_STYLES[status]
        self.status = status
        if status == STATUS_PAUSED and self.pause_reason:
            text = f"{text}（{self.pause_reason}）"
        self.status_label.setText(f"● {text}")
        self.status_label.setTextColor(light, dark)

    # ========== 数据更新 ==========

    def update_values(self, values: dict, now: Optional[datetime] = None):
        """
        批量更新指标显示值与变化量，并把这批值追加到迷你趋势图（每个指标各一条曲线，
        显示的是当前选中的那条）

        Args:
            values: 指标值字典 {指标类型: 指标值}
            now: 收到这批数据的时刻（Presenter 经注入时钟取得）；首次收到时同时作为
                "开始时"与（未另行设置时的）开始运行时刻。缺省时不更新时间相关文案
        """
        if now is not None:
            self.now = now
            if self.first_sample_at is None:
                self.first_sample_at = now
            if self.started_at is None:
                self.started_at = now
        own = {}
        for metric_type, value in values.items():
            value_label = self.value_labels.get(metric_type)
            if value_label is None:
                continue
            own[metric_type] = value
            self.start_values.setdefault(metric_type, value)
            self.last_values[metric_type] = value
            self._unavailable.discard(metric_type)

            formatted_value = format_metric_value(metric_type, value)
            value_label.setText(formatted_value)
            value_label.setToolTip(
                f"{get_metric_display_name(metric_type)}：{formatted_value}")

        # 暂停期间 core 不采集、不 emit data_updated，本方法根本不会被调用，
        # 趋势图自然随之静止，无需额外判断暂停状态
        if own:
            self.sparkline.append_values(own, now)
        self._refresh_deltas()
        self._refresh_running_texts()

    def tick(self, now: datetime) -> None:
        """时间推进（Presenter 每秒调用）：刷新已运行时长与"（N 分钟前）"。"""
        if self.is_finished:
            return
        self.now = now
        self._refresh_running_texts()
        self._refresh_deltas()

    def set_started_at(self, when: datetime) -> None:
        """任务开始运行的时刻（已运行时长从这里算起）。"""
        self.started_at = when
        if self.now is None:
            self.now = when
        self._refresh_running_texts()

    def mark_unavailable(self, reasons: Dict[str, UnavailableReason]):
        """
        把无法采集的指标标为"不可用"（本指标不记录数据，不会记为 0）；
        之后若该指标又采到值，update_values 会正常覆盖显示。

        Args:
            reasons: {指标类型: UnavailableReason}
        """
        for metric_type, reason in reasons.items():
            value_label = self.value_labels.get(metric_type)
            if value_label is None:
                continue
            self._unavailable.add(metric_type)
            value_label.setText("不可用")
            value_label.setToolTip(
                f"{get_metric_display_name(metric_type)}："
                f"{unavailable_reason_text(reason)}，"
                "本指标不记录数据（不会记为 0）")
        self._refresh_deltas()

    def increment_count(self):
        """采集次数+1并刷新文案（内存计数，每次收到 data_updated 调用一次）"""
        self.count += 1
        self._refresh_running_texts()

    # ========== 基线与打点 ==========

    def set_baseline(self, timestamp: datetime,
                     values: Optional[Dict[str, float]] = None) -> None:
        """进入"较基线"模式。values 为已算好的基线值；缺的指标显示"基线采样中"，
        等 set_baseline_value 补上。"""
        self.baseline = CardBaseline(timestamp, dict(values or {}))
        self._refresh_deltas()

    def set_baseline_value(self, metric_type: str, value: float) -> None:
        """某指标的基线值采够了（基线打点后 5 个样本的中位数）。"""
        if self.baseline is None:
            return
        self.baseline.values[metric_type] = value
        self._refresh_deltas()

    def clear_baseline(self) -> None:
        """基线被删除：回到"比开始时"模式。"""
        self.baseline = None
        self._refresh_deltas()

    def set_markers(self, markers: Iterable) -> None:
        """在迷你趋势图上画打点刻度。markers 为带 timestamp/kind 属性的打点对象。"""
        self.sparkline.set_markers([(m.timestamp, m.kind) for m in markers])

    # ========== 状态切换 ==========

    def setToolTip(self, text: str) -> None:
        """设置卡片自身的悬停提示（如自动化方案摘要）；被测目标摘要行始终追加在其后，
        两者谁先到都不会互相覆盖。"""
        self._base_tooltip = text or ''
        self._apply_card_tooltip()

    def set_target_summary(self, lines: Iterable[str]) -> None:
        """被测目标元数据的摘要行（TargetInfo.summary_lines()），追加到悬停提示末尾。"""
        self._target_lines = [line for line in lines if line]
        self._apply_card_tooltip()

    def target_summary(self) -> List[str]:
        return list(self._target_lines)

    def _apply_card_tooltip(self) -> None:
        parts = [self._base_tooltip, '\n'.join(self._target_lines)]
        super().setToolTip(TOOLTIP_SECTION_SEPARATOR.join(p for p in parts if p))

    def _on_pause_button_clicked(self):
        """
        暂停/恢复按钮点击：只负责按当前状态 emit 对应信号，不直接调用 manager。

        按钮图标/文案与状态胶囊不在这里乐观更新——留给 MonitorPresenter 在确认
        manager.pause_task/resume_task 调用成功后回调 set_paused()，避免任务
        已不在运行（如正在停止）导致调用失败时，卡片显示与真实状态不一致。
        """
        if self.is_paused:
            self.resume_requested.emit(self.task_id)
        else:
            self.pause_requested.emit(self.task_id)

    def set_paused(self, is_paused: bool) -> None:
        """
        更新暂停/恢复按钮的图标文案与状态胶囊。

        暂停期间 core 的主循环不采集，被监控进程退出不会被感知：卡片一直显示
        "已暂停"，恢复后下一次采集才发现进程已消失，以 StopReason.PROCESS_EXITED 结束。

        Args:
            is_paused: 任务是否处于暂停状态
        """
        self.is_paused = is_paused
        if not is_paused:
            self.pause_reason = None
        if is_paused:
            self.pause_button.setText("恢复")
            self.pause_button.setIcon(FluentIcon.PLAY)
            self.pause_button.setToolTip(self._resume_tooltip())
        else:
            self.pause_button.setText("暂停")
            self.pause_button.setIcon(FluentIcon.PAUSE)
            self.pause_button.setToolTip(PAUSE_TOOLTIP)
        self._apply_status(STATUS_PAUSED if is_paused else STATUS_RUNNING)

    def _resume_tooltip(self) -> str:
        if self.pause_reason:
            return f"因{self.pause_reason}自动暂停；问题解决后点“恢复”继续记录到这个任务"
        return "恢复采集，继续记录到这个任务"

    def set_pause_reason(self, reason: Optional[str]) -> None:
        """标注当前暂停的原因（状态胶囊显示"已暂停（原因）"）；只在已暂停时有意义，
        None 或任务没在暂停时清除标注"""
        reason = reason or None
        if reason is not None and not self.is_paused:
            reason = None
        if reason == self.pause_reason:
            return
        self.pause_reason = reason
        if self.is_paused:
            self.pause_button.setToolTip(self._resume_tooltip())
            self._apply_status(STATUS_PAUSED)

    def set_stopping(self) -> None:
        """已发出停止请求、等待任务收尾（task_stopped）期间：禁用全部操作按钮，
        避免用户在收尾窗口内重复点击（"分析"不改变任务，保持可用）。"""
        for button in (self.marker_button, self.pause_button, self.stop_button):
            button.setEnabled(False)
        self._apply_status(STATUS_STOPPING)

    def set_finished(self, reason: StopReason, end_time: datetime) -> None:
        """
        切换为"已结束"形态：收成一行（进程名 ●状态 · PID · 结束原因 · 结束时间 ·
        已记录 N 次 [查看分析][同配置再测] ✕）。各指标的最后值与趋势仍保存在卡片里，
        只是不再显示；已运行时长定格在结束时刻。

        Args:
            reason: 任务停止原因
            end_time: 结束时间（由调用方传入）
        """
        self.is_finished = True
        self.finished_at = end_time
        self.stop_reason = reason
        self._apply_status(STATUS_ABNORMAL if is_abnormal_stop(reason)
                           else STATUS_FINISHED)

        self.finished_detail_label.setVisible(True)
        self.values_box.setVisible(False)
        self.trend_box.setVisible(False)
        self.running_actions.setVisible(False)
        self.finished_actions.setVisible(True)
        self._refresh_running_texts()
