"""
趋势图卡片（分析页小倍数的单张图）：真实时间轴曲线、空洞断开与阴影、打点编号竖线、悬停十字线与读数、
主题配色、导出图片。

- 空洞（相邻采样间隔 > 2.5 × 周期）：曲线在空洞处断开（pyqtgraph connect 数组），
  空洞区间画灰色阴影并标注"无数据 12分钟"；
- 打点：用户打点画注意色虚线、顶部横排编号徽标 ①②③，基线画注意色实线、徽标"基线"，
  标签放在 tooltip 与悬停读数里；屏幕上挨得太近的徽标错开高度（badge_positions），
  缩放/平移/改变窗口大小后重新排；暂停/恢复是系统打点，画在底部的浅色短竖线、不编号；
- 系统事件打点（被测进程异常退出 crash / 重启续接 restart / 工具重启后续采 recover）：贯穿整个
  高度的点划线，红 / 绿 / 蓝三色（颜色取自 ui.theme_tokens）、底部带短文字标签、悬停提示写明事件与
  时刻；不编号，也不能在右键菜单里改名或删除；
- 累计类指标由调用方传入每秒速率序列（rate=True），纵轴标题为"每秒 X"。

分析页用到的扩展点：标题（小倍数各图的指标名）、小图模式（compact，不显示轴标题）、隐藏保存/复制
图标、可调最小高度、min/max 带（set_band，按时间分桶，空洞处断开）、相对时间横轴
（set_relative_origin）、跨图联动十字线（hover_moved / set_crosshair）、右键菜单请求
（enable_context_menu 后发 context_menu_requested(x, 附近打点或 None, 屏幕坐标)）。

绘图 x 数据统一用 epoch float（timestamp.timestamp()），仅用于绘图与悬停吸附，
严禁回流作为 SQL 查询参数（查询参数全链路走 datetime / ISO 字符串）。
"""
import bisect
from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from pyqtgraph import exporters
from PyQt5.QtCore import QEvent, QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import (CardWidget, FluentIcon, StrongBodyLabel, TransparentToolButton,
                            qconfig)

from core.analysis.series import connect_mask
from ui.chart_theme import (BAND_ALPHA, GAP_SHADE_ALPHA, GRID_ALPHA, chart_colors,
                             is_transparent, make_transparent)
from ui.components.chart_axes import ContextDateAxisItem, MetricAxisItem
from ui.components.sparkline import compute_y_range
from ui.formatting import (
    BADGE_BASELINE, format_duration, format_per_second, format_relative, marker_badge,
    marker_context)
from ui.typography import DataCaptionLabel, TypeScale, data_font, ui_font
from ui.widgets.history.common import CARD_PADDING_H, DETAIL_TIME_FORMAT
from utils.metrics import (
    VALUE_CLASS_RATE, format_metric_value, get_metric_display_name,
    get_metric_display_unit, is_integer_metric, split_member_key)

# 保留约 360px 的有效读图高度，同时允许高窗口自动拉伸
CHART_MIN_HEIGHT = 340
# 导出图片的像素宽度
EXPORT_WIDTH = 1600
# 悬停吸附计算的限流频率（Hz）
HOVER_RATE_LIMIT = 60

# 卡片内边距：上 12，左右 16，下 16；工具行与图表间距 8
CARD_PADDING_TOP = 12
CARD_PADDING_BOTTOM = 16
CONTENT_SPACING = 8

DEFAULT_VALUE_AXIS_TITLE = '值'
TIME_AXIS_TITLE = '时间'
RELATIVE_TIME_AXIS_TITLE = '相对时间'
# 右键时离打点竖线多少像素以内算"点在打点上"
MARKER_HIT_PX = 8

# 打点类型（与 data.models.MARKER_KINDS 一致）
MARKER_USER = 'user'
MARKER_BASELINE = 'baseline'
SYSTEM_MARKER_KINDS = ('pause', 'resume')
SYSTEM_MARKER_TEXTS = {'pause': '暂停', 'resume': '恢复'}
# 系统事件打点：线上的短标签、悬停提示里的事件名，以及图表配色表（ui.chart_theme）里的键
EVENT_MARKER_KINDS = ('crash', 'restart', 'recover')
EVENT_MARKER_LABELS = {'crash': '异常退出', 'restart': '重启', 'recover': '续采'}
EVENT_MARKER_TEXTS = {'crash': '被测进程异常退出', 'restart': '被测进程重启并续接',
                      'recover': '工具重启后继续监控'}
EVENT_MARKER_COLOR_KEYS = {'crash': 'crash_marker', 'restart': 'restart_marker',
                           'recover': 'recover_marker'}
# 事件标签在竖线上的纵向位置（0 底、1 顶）：贴近底部，避开顶部的编号徽标。相邻事件（崩溃后
# 几十秒就重启）的标签在屏幕上不足 EVENT_LABEL_MIN_GAP_PX 像素时依次上移 EVENT_LABEL_STEP 错开，
# 最多 EVENT_LABEL_ROWS 层
EVENT_LABEL_POSITION = 0.10
EVENT_LABEL_TOP = 0.25
EVENT_LABEL_STEP = 0.075
EVENT_LABEL_ROWS = 3
EVENT_LABEL_MIN_GAP_PX = 64
# 右键与悬停读数都不把这些打点当作可编辑的"轮次边界"
NON_EDITABLE_MARKER_KINDS = SYSTEM_MARKER_KINDS + EVENT_MARKER_KINDS
# 徽标在竖线上的纵向位置（0 底、1 顶）；系统打点短竖线占绘图区高度的比例
BADGE_POSITION = 0.94
SYSTEM_MARKER_SPAN = 0.08
# 相邻徽标横向相距不足 BADGE_MIN_GAP_PX 像素（约一个徽标宽）时视为挤在一起，
# 依次下移 BADGE_STEP 错开，最多 BADGE_ROWS 层后回到顶层
BADGE_MIN_GAP_PX = 22
BADGE_STEP = 0.07
BADGE_ROWS = 3
# 空洞说明文字的纵向位置（阴影不透明度见 ui.chart_theme.GAP_SHADE_ALPHA）
GAP_LABEL_POSITION = 0.85


def value_axis_title(metric_type: Optional[str], display_unit: str,
                     rate: bool = False) -> str:
    """纵轴标题：指标名（显示单位）；速率序列为"每秒指标名（单位）"；无指标时为 "值"。"""
    if not metric_type:
        return DEFAULT_VALUE_AXIS_TITLE
    # 成员序列（指标@成员键）的轴标题只写指标名：成员名放在图的标题里，轴上写不下
    name = get_metric_display_name(split_member_key(metric_type)[0])
    if rate:
        name = f"每秒{name}"
    return f"{name}（{display_unit}）" if display_unit else name


def chart_display_unit(metric_type: Optional[str], values: Sequence[float]) -> str:
    """整张图（以及同屏统计、明细、悬停）共用的显示单位：按绝对值最大的点选取。"""
    if not metric_type:
        return ''
    reference_value = max((abs(value) for value in values), default=0.0)
    return get_metric_display_unit(metric_type, reference_value)


def gap_label_text(start: datetime, end: datetime) -> str:
    """空洞阴影上的说明，如 "无数据 12分钟"。"""
    return f"无数据 {format_duration((end - start).total_seconds())}"


def marker_badges(markers: Sequence) -> List[Optional[str]]:
    """与 markers 等长的徽标：用户打点按时间顺序编号 ①②③，基线为"基线"，
    系统打点（暂停/恢复）与系统事件（异常退出/重启/续采）为 None。markers 须按时间升序。"""
    badges: List[Optional[str]] = []
    number = 0
    for marker in markers:
        kind = getattr(marker, 'kind', MARKER_USER)
        if kind == MARKER_BASELINE:
            badges.append(BADGE_BASELINE)
        elif kind in NON_EDITABLE_MARKER_KINDS:
            badges.append(None)
        else:
            number += 1
            badges.append(marker_badge(number))
    return badges


def event_marker_tooltip(kind: str, label: str, time_text: str) -> str:
    """系统事件竖线的悬停提示："被测进程异常退出：异常退出 0xC0000005（访问冲突） · 11-07 10:30:00"。
    打点名称里已经带事件名（如"异常退出 …"）时不重复写。"""
    text = EVENT_MARKER_TEXTS.get(kind, kind)
    if label and label.startswith(EVENT_MARKER_LABELS.get(kind, text)):
        return f"{label} · {time_text}"
    return f"{text}：{label} · {time_text}" if label else f"{text} · {time_text}"


def badge_positions(pixel_xs: Sequence[float], min_gap_px: float, top: float,
                    step: float, rows: int) -> List[float]:
    """各徽标的纵向位置（0 底、1 顶）。pixel_xs 为按时间升序的屏幕 x 像素；
    相邻两个相距 < min_gap_px 归入同一簇，簇内第 k 个位于 top - (k % rows) * step，
    簇间互不影响（每簇都从 top 开始）。"""
    positions: List[float] = []
    k = 0
    previous: Optional[float] = None
    for x in pixel_xs:
        k = k + 1 if previous is not None and abs(x - previous) < min_gap_px else 0
        positions.append(top - (k % rows) * step)
        previous = x
    return positions


def nearest_index(x_array: Sequence[float], x: float) -> int:
    """在升序 x_array 中二分查找与 x 最接近的下标（距离相等取前一个）。"""
    idx = bisect.bisect_left(x_array, x)
    if idx <= 0:
        return 0
    if idx >= len(x_array):
        return len(x_array) - 1
    before = x_array[idx - 1]
    after = x_array[idx]
    return idx - 1 if (x - before) <= (after - x) else idx


class ChartView(CardWidget):
    """趋势图卡片：工具行（悬停读数 + 保存/复制图标）+ pyqtgraph 绘图区。

    保存/复制只发信号：选择路径、提示结果由页面负责（需要对话框与提示服务），
    控件提供 export_png / grab_pixmap 完成实际导出。
    """

    save_requested = pyqtSignal()
    copy_requested = pyqtSignal()
    # 悬停位置变化：吸附后的 x（epoch 秒），离开图表时为 None（供多图联动十字线）
    hover_moved = pyqtSignal(object)
    # 右键：(x epoch 秒, 附近的打点或 None, 屏幕坐标 QPoint)；需先 enable_context_menu
    context_menu_requested = pyqtSignal(float, object, object)

    def __init__(self, parent=None, *, compact: bool = False):
        """compact=True 为小倍数里的小图：不显示纵轴、横轴标题（单位由调用方写进卡片标题），
        省出的空间留给曲线；其余行为与大图相同。"""
        super().__init__(parent)
        self._compact = compact
        # min/max 带 [(epoch 秒, 下沿, 上沿)]，与空洞一起决定分段
        self._band: List[Tuple[float, float, float]] = []
        self._band_items: list = []
        self._relative_origin: Optional[float] = None
        self._context_menu = False

        # 绘图缓存：悬停吸附与主题重绘复用，不重新查库
        self._x: List[float] = []
        self._y: List[float] = []
        self._metric_type: Optional[str] = None
        self._display_unit = ''
        # 打点（按时间升序，含基线与暂停/恢复）：图上画竖线，悬停读数据此提示所处区间
        self._markers: list = []
        # 空洞 (前一点, 后一点)，epoch 秒；曲线在此断开并画阴影
        self._gaps: List[Tuple[float, float]] = []
        # 当前曲线是否为每秒速率序列（累计类指标）
        self._rate = False
        # 本次重绘画出的打点竖线 [(kind, 徽标或 None, 竖线)] 与空洞阴影 [(区域, 说明)]
        self._marker_items: list = []
        self._gap_items: list = []

        self._pressed = False
        self._interacting = False
        self._interaction_callback: Optional[Callable[[bool], None]] = None

        # 十字线：chart_widget.clear() 会清空 PlotItem 下全部 item，故每次重绘重新加入
        self.crosshair_line = pg.InfiniteLine(angle=90, movable=False)
        self.crosshair_line.setVisible(False)

        self.value_axis = MetricAxisItem(orientation='left')
        self.value_axis.enableAutoSIPrefix(False)
        self.time_axis = ContextDateAxisItem(orientation='bottom')
        self.chart_widget = pg.PlotWidget(axisItems={
            'left': self.value_axis,
            'bottom': self.time_axis,
        })
        self.chart_widget.showGrid(x=False, y=True, alpha=GRID_ALPHA)
        self._set_axis_label('left', DEFAULT_VALUE_AXIS_TITLE)
        self._set_axis_label('bottom', TIME_AXIS_TITLE)
        for axis in (self.value_axis, self.time_axis):
            axis.setTickFont(data_font(TypeScale.CHART_TICK))
            axis.setStyle(maxTickLevel=0, tickTextOffset=6)
            axis.setTickDensity(0.55)
            axis.label.setFont(ui_font(TypeScale.CAPTION))
        self.chart_widget.setMinimumHeight(CHART_MIN_HEIGHT)
        self.chart_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 分桶数据已保留尖峰，这里叠加 pyqtgraph 的峰值抽稀与视口裁剪应对缩放卡顿。
        # pyqtgraph 0.14 中 PlotWidget.setDownsampling 转发到 PlotItem，形参名是 mode；
        # 两者都能正确处理 connect 数组（空洞断开）
        self.chart_widget.setDownsampling(auto=True, mode='peak')
        self.chart_widget.setClipToView(True)
        # 徽标错开取决于屏幕上的像素距离：缩放、平移、改变大小后重新排。
        # chart_widget.clear() 不会替换 ViewBox，连一次即可
        view_box = self.chart_widget.getPlotItem().vb
        view_box.sigXRangeChanged.connect(self._layout_badges)
        view_box.sigResized.connect(self._layout_badges)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            CARD_PADDING_H, CARD_PADDING_TOP, CARD_PADDING_H, CARD_PADDING_BOTTOM)
        layout.setSpacing(CONTENT_SPACING)

        # 工具行只保留当前读数和图片快捷操作；透明图标是低强调入口
        toolbar = QHBoxLayout()
        self.title_label = StrongBodyLabel("", self)
        self.title_label.setVisible(False)
        toolbar.addWidget(self.title_label)
        self.hover_label = DataCaptionLabel("", self)
        toolbar.addWidget(self.hover_label)
        toolbar.addStretch()
        self.save_png_button = TransparentToolButton(FluentIcon.SAVE, self)
        self.save_png_button.setToolTip("保存图表为图片")
        self.save_png_button.clicked.connect(self.save_requested)
        self.copy_chart_button = TransparentToolButton(FluentIcon.COPY, self)
        self.copy_chart_button.setToolTip("复制图表到剪贴板")
        self.copy_chart_button.clicked.connect(self.copy_requested)
        toolbar.addWidget(self.save_png_button)
        toolbar.addWidget(self.copy_chart_button)
        layout.addLayout(toolbar)
        layout.addWidget(self.chart_widget)

        # 鼠标离开图表时清空十字线与读数；视口上的按下/松开用于判断"正在拖动"
        self.chart_widget.installEventFilter(self)
        self.chart_widget.viewport().installEventFilter(self)
        # SignalProxy 限流，避免每个原始鼠标移动事件都触发吸附与重绘；需持有引用防 GC
        self._mouse_proxy = pg.SignalProxy(
            self.chart_widget.scene().sigMouseMoved,
            rateLimit=HOVER_RATE_LIMIT, slot=self._on_mouse_moved)

        # 构造时应用一次当前主题，并随全局主题切换重新取色重绘
        self.apply_theme()
        qconfig.themeChanged.connect(self._on_theme_changed)

    # ---------- 只读状态 ----------

    @property
    def x_values(self) -> List[float]:
        """当前绘制的 x（epoch 秒）。"""
        return self._x

    @property
    def y_values(self) -> List[float]:
        return self._y

    @property
    def metric_type(self) -> Optional[str]:
        return self._metric_type

    @property
    def display_unit(self) -> str:
        """当前图固定的显示单位；统计条与明细表应使用同一单位。"""
        return self._display_unit

    @property
    def markers(self) -> list:
        return self._markers

    @property
    def gaps(self) -> List[Tuple[float, float]]:
        """当前空洞（epoch 秒）。"""
        return self._gaps

    @property
    def is_rate(self) -> bool:
        """当前曲线是否为每秒速率序列。"""
        return self._rate

    def marker_items(self) -> list:
        """本次绘制的打点竖线：[(kind, 徽标或 None, InfiniteLine)]，按时间升序。"""
        return list(self._marker_items)

    def event_label_layout(self) -> List[float]:
        """系统事件短标签当前的纵向位置（0 底、1 顶），按时间升序（紧凑图没有标签，不计）。"""
        return [line.label.orthoPos for kind, _badge, line in self._marker_items
                if kind in EVENT_MARKER_KINDS and getattr(line, 'label', None) is not None]

    def badge_layout(self) -> List[float]:
        """带徽标竖线的标签当前纵向位置（0 底、1 顶），按时间升序（系统打点不计）。"""
        return [line.label.orthoPos for _kind, badge, line in self._marker_items
                if badge is not None]

    def gap_items(self) -> list:
        """本次绘制的空洞阴影：[(LinearRegionItem, 说明文本)]。"""
        return list(self._gap_items)

    @property
    def band(self) -> List[Tuple[float, float, float]]:
        """当前 min/max 带 [(epoch 秒, 下沿, 上沿)]。"""
        return self._band

    def band_items(self) -> list:
        """本次绘制的带状填充（每个连续段一个 FillBetweenItem）。"""
        return list(self._band_items)

    @property
    def relative_origin(self) -> Optional[float]:
        return self._relative_origin

    def connect_array(self) -> List[bool]:
        """当前曲线的连线掩码（第 i 项为 False 表示第 i 点与下一点之间断开）。"""
        return connect_mask(self._x, self._gaps)

    def has_data(self) -> bool:
        return bool(self._x)

    def axis_title(self) -> str:
        """纵轴当前标题文本。"""
        return self.chart_widget.getPlotItem().getAxis('left').labelText

    # ---------- 数据 ----------

    def set_snapshot(self, snapshot, keep_view: bool = False) -> None:
        """按历史查询快照绘制：累计类画每秒速率序列，其余画分桶点；带打点与空洞。"""
        rate = snapshot.value_class == VALUE_CLASS_RATE
        self.set_series(
            snapshot.rate_series if rate else snapshot.buckets, snapshot.request.metric,
            snapshot.markers, keep_view=keep_view, gaps=snapshot.gaps, rate=rate)

    def set_series(self, points: Sequence, metric_type: Optional[str],
                   markers: Optional[Sequence] = None, keep_view: bool = False, *,
                   gaps: Optional[Sequence] = None, rate: bool = False) -> None:
        """
        设置曲线数据并重绘。

        Args:
            points: 分桶降采样后的数据点（需有 timestamp / value，按时间升序）
            metric_type: 指标类型
            markers: 打点列表（需有 timestamp / label，可有 kind，按时间升序）；
                None 表示保持现有打点
            keep_view: True 保留用户当前缩放/平移（自动刷新用）；False 重新自适应范围
            gaps: 空洞 [(前一点 datetime, 后一点 datetime)]；None 表示没有空洞
            rate: points 是否为每秒速率序列（纵轴标题与读数带"每秒"）
        """
        if markers is not None:
            self._markers = list(markers)
        self._metric_type = metric_type
        self._rate = rate
        self._gaps = [(a.timestamp(), b.timestamp()) for a, b in (gaps or ())]
        if points:
            self._x = [dp.timestamp.timestamp() for dp in points]
            self._y = [dp.value for dp in points]
        else:
            self._x = []
            self._y = []

        self._display_unit = chart_display_unit(metric_type, self._y)
        # 计数类指标强制整数刻度（速率是小数，不强制）；字节类的刻度间距按显示单位选取
        self.value_axis.set_display_unit(
            self._display_unit,
            integer_only=bool(metric_type) and not rate and is_integer_metric(metric_type))

        self._redraw()
        self._set_axis_label('left', value_axis_title(metric_type, self._display_unit, rate))
        self._set_axis_label('bottom', TIME_AXIS_TITLE)

        if not keep_view:
            self.reset_view()

    def set_band(self, buckets: Optional[Sequence]) -> None:
        """设置 min/max 带（需有 start / end / min / max 的时间桶，按时间升序；None 或空
        清除）。每桶在其首末点之间画平台，空洞处断开。"""
        self._band = []
        for bucket in buckets or ():
            stamps = ((bucket.start, bucket.end) if bucket.end != bucket.start
                      else (bucket.start,))
            for ts in stamps:
                self._band.append((ts.timestamp(), bucket.min, bucket.max))
        self._redraw()

    def set_title(self, text: str) -> None:
        """卡片左上角标题（小倍数图的指标名）；空串隐藏。"""
        self.title_label.setText(text)
        self.title_label.setVisible(bool(text))

    def set_tools_visible(self, visible: bool) -> None:
        """显示/隐藏保存、复制图标（小倍数图由页面统一导出）。"""
        self.save_png_button.setVisible(visible)
        self.copy_chart_button.setVisible(visible)

    def set_chart_min_height(self, height: int) -> None:
        self.chart_widget.setMinimumHeight(height)

    def set_relative_origin(self, origin: Optional[float]) -> None:
        """横轴改为相对 origin（epoch 秒）的 T+mm:ss；None 恢复绝对时间。"""
        self._relative_origin = origin
        self.time_axis.set_relative_origin(origin)
        self._set_axis_label(
            'bottom', TIME_AXIS_TITLE if origin is None else RELATIVE_TIME_AXIS_TITLE)

    @property
    def compact(self) -> bool:
        """是否为不显示轴标题的小图。"""
        return self._compact

    def axis_label_visible(self, name: str) -> bool:
        """name（'left' / 'bottom'）轴的标题是否显示。"""
        return self.chart_widget.getPlotItem().getAxis(name).label.isVisible()

    def _set_axis_label(self, name: str, text: str) -> None:
        # pyqtgraph 的 setLabel 会顺带把标题显示出来；小图记下文本（axis_title 仍可读）后再藏起
        self.chart_widget.setLabel(name, text)
        if self._compact:
            self.chart_widget.getPlotItem().getAxis(name).showLabel(False)

    def enable_context_menu(self) -> None:
        """改用自己的右键菜单：关掉 pyqtgraph 自带菜单，右键时发 context_menu_requested。"""
        if self._context_menu:
            return
        self._context_menu = True
        self.chart_widget.getPlotItem().setMenuEnabled(False)
        self.chart_widget.scene().sigMouseClicked.connect(self._on_scene_clicked)

    def marker_near(self, x: float, tolerance_px: float = MARKER_HIT_PX):
        """横向距 x 在 tolerance_px 像素以内、最近的用户打点或基线；没有返回 None。"""
        pixel = self.chart_widget.getPlotItem().vb.viewPixelSize()[0] or 0.0
        best, best_dist = None, None
        for marker in self._markers:
            if getattr(marker, 'kind', MARKER_USER) in NON_EDITABLE_MARKER_KINDS:
                continue
            dist = abs(marker.timestamp.timestamp() - x)
            if dist <= tolerance_px * pixel and (best_dist is None or dist < best_dist):
                best, best_dist = marker, dist
        return best

    def _on_scene_clicked(self, event) -> None:
        if event.button() != Qt.RightButton:
            return
        plot_item = self.chart_widget.getPlotItem()
        pos = event.scenePos()
        if not plot_item.vb.sceneBoundingRect().contains(pos):
            return
        x = plot_item.vb.mapSceneToView(pos).x()
        event.accept()
        self.context_menu_requested.emit(
            float(x), self.marker_near(x), event.screenPos().toPoint())

    def set_markers(self, markers: Sequence) -> None:
        """替换打点并重绘（曲线数据不变）。"""
        self._markers = list(markers)
        self._redraw()

    def clear(self) -> None:
        """清空曲线、打点、空洞与单位；纵轴标题保持上一次的文本。"""
        self._x = []
        self._y = []
        self._metric_type = None
        self._display_unit = ''
        self._markers = []
        self._gaps = []
        self._rate = False
        self._band = []
        self.value_axis.set_display_unit('')
        self._redraw()

    def reset_view(self, follow_x: bool = False) -> None:
        """重新自适应范围；数据几乎是平线时把纵向跨度扩到下限。follow_x 为真时横轴不自适应
        （由外部同步，如小倍数图跟随焦点大图），只自适应纵轴。"""
        if follow_x:
            self.chart_widget.enableAutoRange(axis='y')
        else:
            self.chart_widget.enableAutoRange()
        self._apply_min_y_span()

    def _apply_min_y_span(self) -> None:
        """数据几乎是平线时，纵轴跨度扩到下限（与迷你趋势图同一规则），
        避免把几字节的抖动放大成满幅、刻度出现多位小数。跨度足够时保持自动范围。
        速率序列另用速率的跨度下限（参考值 5%，至少 1 个单位/秒），水位量的下限不适用。"""
        if not self._y:
            return
        low, high = compute_y_range(self._y, self._metric_type, rate=self._rate)
        if high - low > max(self._y) - min(self._y):
            self.chart_widget.setYRange(low, high, padding=0.05)

    def _redraw(self) -> None:
        """按缓存数据重绘曲线、空洞阴影与打点竖线（主题切换也走这里，不重新查库）。"""
        self.chart_widget.clear()
        self._marker_items = []
        self._gap_items = []
        self._band_items = []
        self.chart_widget.addItem(self.crosshair_line, ignoreBounds=True)
        self.hide_hover()

        if not self._x:
            return

        colors = chart_colors()
        self._draw_gaps(colors)
        self._draw_band(colors)
        pen = pg.mkPen(color=colors['curve'], width=2)
        extra = {'connect': np.array(self.connect_array(), dtype=bool)} if self._gaps else {}
        self.chart_widget.plot(self._x, self._y, pen=pen, **extra)
        self._draw_markers(colors)

    def _draw_gaps(self, colors: dict) -> None:
        """空洞：灰色阴影 + "无数据 N" 说明；ignoreBounds 不参与自动范围。"""
        shade = QColor(colors['gap_shade'])
        shade.setAlpha(GAP_SHADE_ALPHA)
        for start, end in self._gaps:
            region = pg.LinearRegionItem(
                values=(start, end), orientation='vertical', movable=False,
                brush=pg.mkBrush(shade), pen=pg.mkPen(None))
            region.setZValue(-10)
            text = gap_label_text(datetime.fromtimestamp(start), datetime.fromtimestamp(end))
            label = pg.InfLineLabel(
                region.lines[0], text=text, position=GAP_LABEL_POSITION,
                anchors=[(0, 0.5), (0, 0.5)], color=colors['text'])
            # pyqtgraph 默认字号比刻度大一号，与刻度同为说明文字
            label.setFont(ui_font(TypeScale.CAPTION))
            label.setToolTip(text)
            region.setToolTip(text)
            self.chart_widget.addItem(region, ignoreBounds=True)
            self._gap_items.append((region, text))

    def _band_segments(self) -> List[List[Tuple[float, float, float]]]:
        """把带按空洞切成连续段（跨空洞的相邻两点不相连）。"""
        mask = connect_mask([t for t, _lo, _hi in self._band], self._gaps)
        segments, current = [], []
        for item, linked in zip(self._band, mask):
            current.append(item)
            if not linked:
                segments.append(current)
                current = []
        if current:
            segments.append(current)
        return segments

    def _draw_band(self, colors: dict) -> None:
        """min/max 带：半透明填充，空洞处断开。"""
        if not self._band:
            return
        fill = QColor(colors['curve'])
        fill.setAlpha(BAND_ALPHA)
        no_pen = pg.mkPen(None)
        for segment in self._band_segments():
            xs = [t for t, _lo, _hi in segment]
            low = pg.PlotDataItem(xs, [lo for _t, lo, _hi in segment], pen=no_pen)
            high = pg.PlotDataItem(xs, [hi for _t, _lo, hi in segment], pen=no_pen)
            item = pg.FillBetweenItem(low, high, brush=pg.mkBrush(fill))
            item.setZValue(-5)
            for curve in (low, high, item):
                self.chart_widget.addItem(curve)
            self._band_items.append(item)

    def band_range_at(self, x: float) -> Optional[Tuple[float, float]]:
        """x 处最近的带点的 (min, max)；没有带时为 None。"""
        if not self._band:
            return None
        _t, low, high = self._band[nearest_index([t for t, _lo, _hi in self._band], x)]
        return low, high

    def _draw_markers(self, colors: dict) -> None:
        """打点竖线：用户打点虚线 + 编号徽标，基线实线 + "基线"，暂停/恢复底部浅色短线，
        系统事件（异常退出/重启/续采）彩色点划线 + 底部短标签。"""
        marker_color = colors['marker']
        user_pen = pg.mkPen(color=marker_color, width=1, style=Qt.DashLine)
        baseline_pen = pg.mkPen(color=marker_color, width=2)
        system_pen = pg.mkPen(color=colors['system_marker'], width=2)
        for marker, badge in zip(self._markers, marker_badges(self._markers)):
            kind = getattr(marker, 'kind', MARKER_USER)
            pos = marker.timestamp.timestamp()
            time_text = marker.timestamp.strftime(DETAIL_TIME_FORMAT)
            if kind in EVENT_MARKER_KINDS:
                color = colors[EVENT_MARKER_COLOR_KEYS[kind]]
                tooltip = event_marker_tooltip(kind, marker.label, time_text)
                opts = {'position': EVENT_LABEL_POSITION, 'color': color, 'movable': False,
                        'anchors': [(0, 1), (0, 1)]}
                line = pg.InfiniteLine(
                    pos=pos, angle=90, movable=False,
                    pen=pg.mkPen(color=color, width=2, style=Qt.DashDotLine),
                    label=None if self._compact else EVENT_MARKER_LABELS[kind], labelOpts=opts)
                tag = getattr(line, 'label', None)
                if tag is not None:
                    tag.setFont(ui_font(TypeScale.CAPTION))
                    tag.setToolTip(tooltip)
            elif badge is None:
                tooltip = f"{SYSTEM_MARKER_TEXTS.get(kind, kind)} · {time_text}"
                line = pg.InfiniteLine(pos=pos, angle=90, movable=False, pen=system_pen,
                                       span=(0, SYSTEM_MARKER_SPAN))
            else:
                tooltip = f"{badge} {marker.label} · {time_text}"
                line = pg.InfiniteLine(
                    pos=pos, angle=90, movable=False,
                    pen=baseline_pen if kind == MARKER_BASELINE else user_pen,
                    label=badge, labelOpts={
                        'position': BADGE_POSITION, 'color': marker_color,
                        'movable': False,
                    })
                line.label.setToolTip(tooltip)
            line.setToolTip(tooltip)
            self.chart_widget.addItem(line, ignoreBounds=True)
            self._marker_items.append((kind, badge, line))
        self._layout_badges()

    def _layout_badges(self, *_args) -> None:
        """按徽标在屏幕上的横向距离错开高度，避免相隔几秒的打点徽标重叠；系统事件的短标签
        同理（各自一组，互不影响）。只改标签位置，不改视图范围，不会反过来触发 sigXRangeChanged。"""
        badge_lines = [line for _kind, badge, line in self._marker_items if badge is not None]
        event_lines = [line for kind, _badge, line in self._marker_items
                       if kind in EVENT_MARKER_KINDS and getattr(line, 'label', None) is not None]
        self._stagger(badge_lines, BADGE_MIN_GAP_PX, BADGE_POSITION, BADGE_STEP, BADGE_ROWS)
        self._stagger(event_lines, EVENT_LABEL_MIN_GAP_PX, EVENT_LABEL_TOP, EVENT_LABEL_STEP,
                      EVENT_LABEL_ROWS)

    def _stagger(self, lines: list, min_gap_px: float, top: float, step: float,
                 rows: int) -> None:
        if not lines:
            return
        view_box = self.chart_widget.getPlotItem().vb
        pixel_xs = [view_box.mapViewToScene(QPointF(line.value(), 0)).x() for line in lines]
        if not all(np.isfinite(pixel_xs)):
            # 视图尚未布局（尺寸为零等）时映射无意义，先放回顶层，sigResized 后再排
            positions = [top] * len(lines)
        else:
            positions = badge_positions(pixel_xs, min_gap_px, top, step, rows)
        for line, position in zip(lines, positions):
            line.label.setPosition(position)

    # ---------- 悬停十字线 ----------

    def hover_text(self, index: int) -> str:
        """第 index 个绘制点的悬停读数：时间 · 值 · 所处打点区间（若有，带编号徽标）。"""
        x_val = self._x[index]
        if self._relative_origin is not None:
            time_str = format_relative(x_val - self._relative_origin)
        else:
            time_str = datetime.fromtimestamp(x_val).strftime(DETAIL_TIME_FORMAT)
        if self._rate:
            formatted_value = format_per_second(self._metric_type, self._y[index])
        else:
            formatted_value = format_metric_value(
                self._metric_type, self._y[index], display_unit=self._display_unit)
        text = f"{time_str} · {formatted_value}"
        band = self.band_range_at(x_val)
        if band is not None and not self._rate:
            low, high = (format_metric_value(self._metric_type, v, display_unit=self._display_unit)
                         for v in band)
            text += f"（{low} – {high}）"
        # 区间说明只看用户打点与基线（暂停/恢复不是一轮操作的边界）
        labelled = [(m, badge) for m, badge in zip(self._markers, marker_badges(self._markers))
                    if badge is not None]
        context = marker_context(
            [m.timestamp.timestamp() for m, _ in labelled],
            [f"{badge} {m.label}" for m, badge in labelled], x_val)
        if context:
            text += f" · {context}"
        return text

    def show_hover_at(self, x: float) -> Optional[int]:
        """把十字线吸附到与 x（epoch 秒）最近的绘制点并显示读数；无数据返回 None。"""
        if not self._x:
            return None
        idx = self.set_crosshair(x)
        self._update_interaction()
        self.hover_moved.emit(self._x[idx])
        return idx

    def set_crosshair(self, x: Optional[float]) -> Optional[int]:
        """只移动十字线与读数（联动其他图时用，不发 hover_moved）；x 为 None 时隐藏。"""
        if x is None or not self._x:
            self.crosshair_line.setVisible(False)
            self.hover_label.setText("")
            return None
        idx = nearest_index(self._x, x)
        self.crosshair_line.setPos(self._x[idx])
        self.crosshair_line.setVisible(True)
        self.hover_label.setText(self.hover_text(idx))
        return idx

    def hide_hover(self) -> None:
        """隐藏十字线并清空读数。"""
        was_visible = self.crosshair_line.isVisible()
        self.set_crosshair(None)
        self._update_interaction()
        if was_visible:
            self.hover_moved.emit(None)

    def _on_mouse_moved(self, evt) -> None:
        """SignalProxy 回调（evt 是单元素元组）：鼠标在绘图区内时吸附，离开时隐藏。"""
        if not self._x:
            return
        pos = evt[0]
        plot_item = self.chart_widget.getPlotItem()
        if not plot_item.sceneBoundingRect().contains(pos):
            self.hide_hover()
            return
        self.show_hover_at(plot_item.vb.mapSceneToView(pos).x())

    # ---------- 交互状态（供自动刷新判断是否跳过） ----------

    def is_interacting(self) -> bool:
        """用户是否正在看图：悬停读数中，或按住鼠标拖动/框选中。"""
        return self._pressed or self.crosshair_line.isVisible()

    def set_interaction_callback(self, callback: Optional[Callable[[bool], None]]) -> None:
        """交互状态变化时回调 callback(is_interacting)；传 None 取消。"""
        self._interaction_callback = callback

    def _update_interaction(self) -> None:
        state = self.is_interacting()
        if state == self._interacting:
            return
        self._interacting = state
        if self._interaction_callback is not None:
            self._interaction_callback(state)

    def eventFilter(self, obj, event):
        """鼠标离开图表时清空十字线与读数；记录视口上的按下/松开。"""
        event_type = event.type()
        if obj is self.chart_widget and event_type == QEvent.Leave:
            self._pressed = False
            self.hide_hover()
        elif obj is self.chart_widget.viewport():
            if event_type == QEvent.MouseButtonPress:
                self._pressed = True
                self._update_interaction()
            elif event_type == QEvent.MouseButtonRelease:
                self._pressed = False
                self._update_interaction()
        return super().eventFilter(obj, event)

    # ---------- 主题 ----------

    def renders_offscreen(self) -> bool:
        """是否在离屏窗口里渲染（报告图片：WA_DontShowOnScreen 下 show 一次再 grab）。
        这时没有主窗口的底色可透，卡片的半透明底会叠在 Qt 默认的浅灰窗口色上。"""
        return self.window().testAttribute(Qt.WA_DontShowOnScreen)

    def paintEvent(self, event) -> None:
        # 离屏导出时先垫一层当前主题的窗口底色（卡片的半透明底叠上去正好是屏幕上的卡片色），
        # 深色报告里的图表与标题才不会是浅底浅字
        if self.renders_offscreen():
            painter = QPainter(self)
            try:
                painter.fillRect(self.rect(), QColor(chart_colors()['window']))
            finally:
                painter.end()
        super().paintEvent(event)

    def is_background_transparent(self) -> bool:
        """绘图区背景是否透明（由卡片提供底色）。"""
        return is_transparent(self.chart_widget)

    def apply_theme(self) -> None:
        """按 chart_colors() 设置坐标轴、十字线配色，并用缓存数据重绘曲线。
        绘图区背景透明，由卡片提供底色（写死的底色在 Mica 与悬停下会露出色差矩形）。"""
        colors = chart_colors()
        make_transparent(self.chart_widget)
        axis_pen = pg.mkPen(color=colors['axis'])
        text_pen = pg.mkPen(color=colors['text'])
        for axis_name in ('bottom', 'left'):
            axis = self.chart_widget.getAxis(axis_name)
            axis.setPen(axis_pen)
            axis.setTextPen(text_pen)
        self.crosshair_line.setPen(pg.mkPen(color=colors['crosshair'], style=Qt.DashLine))
        self._redraw()

    def _on_theme_changed(self, _theme) -> None:
        # 无父对象的 ChartView（测试里常见）可能已被 Python 端回收了 pyqtgraph 的场景，
        # 而控件本身还在：此时重新取色会碰到已删除的 C++ 对象。遇到就退订，不再跟随主题
        try:
            self.apply_theme()
        except RuntimeError:
            try:
                qconfig.themeChanged.disconnect(self._on_theme_changed)
            except TypeError:
                pass

    # ---------- 导出 ----------

    def export_png(self, path: str, width: int = EXPORT_WIDTH) -> None:
        """把绘图区导出为 PNG；失败时异常原样抛出，由调用方提示。
        屏幕上背景透明，导出时垫当前主题的卡片底色，图片离开卡片后文字仍可读。"""
        exporter = exporters.ImageExporter(self.chart_widget.getPlotItem())
        exporter.parameters()['width'] = width
        exporter.parameters()['background'] = QColor(chart_colors()['background'])
        exporter.export(path)

    def grab_pixmap(self) -> QPixmap:
        """绘图区截图（复制到剪贴板用），垫当前主题的卡片底色（同 export_png）。"""
        ratio = self.chart_widget.devicePixelRatioF()
        pixmap = QPixmap(self.chart_widget.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor(chart_colors()['background']))
        painter = QPainter(pixmap)
        try:
            self.chart_widget.render(painter)
        finally:
            painter.end()
        return pixmap
