"""
迷你趋势图组件（Sparkline）
无坐标轴、无文字的极简折线图，用于任务卡片内联展示某个指标从任务开始至今的走势。
每个指标各有一条抽稀缓冲区，切换显示对象时直接换用对应缓冲区，曲线不必从头积累。

长时间监控的点数会远超控件像素宽度，因此缓冲区超过上限时把相邻点成对合并
（取平均）并让后续每个点覆盖两倍的采集次数：点数始终有界，而曲线覆盖的时间
范围始终是"任务开始至今"，各点代表的时间跨度保持一致。
纵轴按数据 min-max 自适应，但跨度有下限，避免微小抖动被画成满幅波动。
采集点带时间时还能画打点刻度：基线为注意色实线，普通打点为注意色虚线。
刻度可悬停（"第 3 轮 · 10:21:05 · 备注"）；用户打点与基线还能右键（发 marker_menu_requested，
菜单由监控页的 Presenter 弹出），异常退出 / 重启 / 续采这类系统打点只能悬停。
"""
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from PyQt5.QtCore import QEvent, Qt, QPointF, pyqtSignal
from PyQt5.QtGui import QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QToolTip, QWidget, QSizePolicy

from ui.formatting import marker_tick_text
from ui.theme_tokens import ATTENTION, DATA, EVENT_MARKER_COLORS, qcolor

# 最小跨度规则按指标 kind 定义在 utils.metrics；常量在此再导出，保持原有导入路径可用
from utils.metrics import (  # noqa: F401
    MIN_SPAN_BYTES_KB, MIN_SPAN_BYTES_RATIO, MIN_SPAN_COUNT, MIN_SPAN_PERCENT,
    min_span_for, rate_min_span_for
)

# 固定高度（像素），横向随父布局拉伸
FIXED_HEIGHT = 36

# 缓冲区点数上限（必须为偶数，保证成对合并后没有落单点）
DEFAULT_MAX_POINTS = 240

# 取色：在 paintEvent 内按当前主题从 ui.theme_tokens 取，不连接 qconfig.themeChanged——
# 任务卡片随监控任务动态创建/销毁，这样不必在卡片销毁时手动断开主题信号连接，零信号残留。
# 背景不画（透明），底色来自所在卡片；折线用数据主色（对卡片底 ≥ 4.5:1），
# 打点刻度用注意色（与分析页图表的打点竖线同色）。
LINE_COLOR = DATA
TICK_COLOR = ATTENTION

# 画在迷你图上的打点类型：暂停/恢复系统打点不画，小图上只留用户关心的轮次边界与系统事件
# （被测进程异常退出 / 重启续接 / 工具重启后续采，点划线，颜色与分析页图表一致）
EVENT_TICK_KINDS = tuple(EVENT_MARKER_COLORS)
TICK_KINDS = ('baseline', 'user') + EVENT_TICK_KINDS
# 能右键编辑的刻度类型（系统事件只读）
EDITABLE_TICK_KINDS = ('baseline', 'user')
# 鼠标离刻度多少像素以内算指到它
TICK_HIT_RADIUS = 5.0


def merge_pairs(values: Sequence[float]) -> List[float]:
    """相邻两点取平均成对合并（点数减半）。

    选平均而不是最大值：趋势图表达的是走势而非尖峰（尖峰在分析页图表里看），
    取平均让缓慢上涨的曲线在多次合并后依然保持平滑单调。
    元素个数为奇数时末尾落单的点原样保留。
    """
    merged = [(values[i] + values[i + 1]) / 2.0 for i in range(0, len(values) - 1, 2)]
    if len(values) % 2:
        merged.append(values[-1])
    return merged


class TrendDownsampler:
    """等跨度抽稀缓冲区：始终覆盖"开始至今"，点数不超过 max_points。

    每个已完成的点是 stride 次采集的平均值；达到上限时全体成对合并、stride 翻倍。
    尚未攒满 stride 次的最新采集暂存为 pending，并以其平均值作为末尾点参与绘制，
    因此最新一次采集的走势能立即反映到曲线末端。
    每个点另记其首个采集的时间（可为 None），合并时取两点中较早的，用于把打点
    时刻换算到横轴位置。
    """

    def __init__(self, max_points: int = DEFAULT_MAX_POINTS):
        # 取偶数，保证合并后没有落单点
        self.max_points = max(2, max_points - max_points % 2)
        self.stride = 1
        self._points: List[float] = []
        self._times: List[Optional[datetime]] = []
        self._pending: List[float] = []
        self._pending_time: Optional[datetime] = None

    def append(self, value: float, timestamp: Optional[datetime] = None) -> None:
        if not self._pending:
            self._pending_time = timestamp
        self._pending.append(value)
        if len(self._pending) < self.stride:
            return
        self._points.append(sum(self._pending) / len(self._pending))
        self._times.append(self._pending_time)
        self._pending = []
        self._pending_time = None
        if len(self._points) >= self.max_points:
            self._points = merge_pairs(self._points)
            self._times = self._times[::2]
            self.stride *= 2

    def clear(self) -> None:
        self.stride = 1
        self._points = []
        self._times = []
        self._pending = []
        self._pending_time = None

    def series(self) -> List[float]:
        """当前用于绘制的序列（含尚未攒满一个点的最新采集）。"""
        if self._pending:
            return self._points + [sum(self._pending) / len(self._pending)]
        return list(self._points)

    def times(self) -> List[Optional[datetime]]:
        """与 series() 一一对应的各点起始时间。"""
        if self._pending:
            return self._times + [self._pending_time]
        return list(self._times)


def tick_fraction(times: Sequence[datetime], when: datetime) -> float:
    """打点时刻在横轴上的位置（0 = 第一个点，1 = 最后一个点）。

    落在两点之间时按时间线性插值；早于第一个点或晚于最后一个点时贴边。
    只有一个点时返回 0.5（单点画在中间）。
    """
    count = len(times)
    if count <= 1:
        return 0.5
    if when <= times[0]:
        return 0.0
    if when >= times[-1]:
        return 1.0
    i = bisect_right(times, when) - 1
    span = (times[i + 1] - times[i]).total_seconds()
    offset = (when - times[i]).total_seconds() / span if span > 0 else 0.0
    return (i + offset) / (count - 1)


def min_span(metric_type: Optional[str], reference: float) -> float:
    """纵轴最小跨度（原始单位）：低于该跨度的起伏不应被拉伸成满幅波动。"""
    return min_span_for(metric_type, reference)


def compute_y_range(
        values: Sequence[float], metric_type: Optional[str], *,
        rate: bool = False) -> Tuple[float, float]:
    """按数据 min-max 求纵轴范围；跨度小于下限时围绕数据中点扩展到下限。

    rate=True 表示 values 是累计量的每秒速率，跨度下限按 rate_min_span_for。
    """
    low = min(values)
    high = max(values)
    required = (rate_min_span_for(metric_type, values[-1]) if rate
                else min_span(metric_type, values[-1]))
    if high - low < required:
        middle = (high + low) / 2.0
        low = middle - required / 2.0
        high = middle + required / 2.0
    return low, high


def cosmetic_pen(color, width: float, ratio: float = 1.0) -> QPen:
    """cosmetic 画笔：笔宽按设备像素计，不随 QPainter 的缩放变换放大。
    width 是逻辑像素宽，乘设备像素比 ratio 后落在整数 / 半整数设备像素上，
    125% / 150% 缩放下 1 px 的刻度线不会被缩放糊成两条半透明的线。"""
    pen = QPen(color)
    pen.setCosmetic(True)
    pen.setWidthF(max(1.0, round(width * max(ratio, 1.0) * 2.0) / 2.0))
    return pen


def tick_pen(kind: str, ratio: float = 1.0) -> QPen:
    """打点刻度的画笔：基线实线，普通打点虚线，系统事件（异常退出 / 重启 / 续采）点划线并取各自
    语义色，让它们在迷你图上一眼与用户打点区分开。ratio 为控件的设备像素比（devicePixelRatioF）。"""
    if kind in EVENT_TICK_KINDS:
        pen = cosmetic_pen(qcolor(EVENT_MARKER_COLORS[kind]), 1.5, ratio)
        pen.setStyle(Qt.DashDotLine)
        return pen
    if kind == 'baseline':
        return cosmetic_pen(qcolor(TICK_COLOR), 1.5, ratio)
    pen = cosmetic_pen(qcolor(TICK_COLOR), 1.0, ratio)
    pen.setStyle(Qt.DashLine)
    return pen


@dataclass(frozen=True)
class Tick:
    """迷你图上的一个打点刻度。marker 为原打点对象（只给了时间与类型时为 None）；
    text 是悬停文字，没有 marker 时为空串。"""
    when: datetime
    kind: str
    marker: Optional[object] = None
    text: str = ""


class SparklineWidget(QWidget):
    """迷你趋势图：固定高度 36px，横向拉伸；覆盖任务开始至今的走势

    信号：
        marker_menu_requested(object, QPoint): 在可编辑的刻度（基线 / 用户打点）上右键，
            参数为打点对象与全局坐标
    """

    marker_menu_requested = pyqtSignal(object, object)

    def __init__(self, max_points: int = DEFAULT_MAX_POINTS,
                 metric_type: Optional[str] = None, parent=None):
        """
        Args:
            max_points: 缓冲区点数上限，达到后成对合并抽稀（见 TrendDownsampler）
            metric_type: 趋势对应的指标，用于决定纵轴最小跨度；None 表示不设下限
            parent: 父窗口
        """
        super().__init__(parent)
        self._max_points = max_points
        self._metric_type = metric_type
        # 每个指标一条缓冲区（键为指标类型；未指定指标时键为 None）
        self._samplers: Dict[Optional[str], TrendDownsampler] = {}
        self._sampler = self._sampler_for(metric_type)
        # 打点刻度（按时间升序），只保留 TICK_KINDS
        self._ticks: List[Tick] = []

        self.setMouseTracking(True)
        self.setFixedHeight(FIXED_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _sampler_for(self, metric_type: Optional[str]) -> TrendDownsampler:
        sampler = self._samplers.get(metric_type)
        if sampler is None:
            sampler = self._samplers[metric_type] = TrendDownsampler(self._max_points)
        return sampler

    def append(self, value: float, timestamp: Optional[datetime] = None) -> None:
        """向当前显示的指标追加一个采集点并触发重绘；timestamp 为采集时间（画打点刻度要用）"""
        self._sampler.append(value, timestamp)
        self.update()

    def append_values(self, values: Mapping[str, float],
                      timestamp: Optional[datetime] = None) -> None:
        """一批采集值分别追加到各指标的缓冲区（之后切换显示对象时曲线已是完整的）。"""
        for metric_type, value in values.items():
            self._sampler_for(metric_type).append(value, timestamp)
        self.update()

    def metric_type(self) -> Optional[str]:
        """当前显示的指标。"""
        return self._metric_type

    def set_metric(self, metric_type: Optional[str]) -> None:
        """切换显示的指标（纵轴最小跨度随之切换）。"""
        self._metric_type = metric_type
        self._sampler = self._sampler_for(metric_type)
        self.update()

    def clear(self) -> None:
        """清空全部缓冲区并触发重绘"""
        for sampler in self._samplers.values():
            sampler.clear()
        self.update()

    def series(self) -> List[float]:
        """当前绘制序列（测试与调试用）"""
        return self._sampler.series()

    def set_markers(self, markers: Sequence) -> None:
        """设置打点刻度；非 TICK_KINDS 的打点忽略。markers 的元素可以是 (时间, kind)，
        也可以是带 timestamp / kind / label 的打点对象（后者才有悬停文字与右键）。"""
        pairs = []
        for item in markers:
            if isinstance(item, tuple):
                pairs.append((item[0], item[1], None))
            else:
                pairs.append((item.timestamp, item.kind, item))
        pairs.sort(key=lambda p: (p[0], getattr(p[2], 'id', None) or 0))
        # 用户打点的序号按时间先后数（与"第 N 轮"的默认命名同一口径）
        rounds = 0
        ticks: List[Tick] = []
        for when, kind, marker in pairs:
            number = None
            if kind == 'user':
                rounds += 1
                number = rounds
            if kind in TICK_KINDS:
                text = marker_tick_text(marker, number) if marker is not None else ""
                ticks.append(Tick(when, kind, marker, text))
        self._ticks = ticks
        self.update()

    def _tick_fractions(self) -> List[Tuple[float, Tick]]:
        times = self._sampler.times()
        if not times or any(t is None for t in times):
            return []
        return [(tick_fraction(times, tick.when), tick) for tick in self._ticks]

    def tick_positions(self) -> List[Tuple[float, str]]:
        """各打点刻度的横轴位置 [(0~1 的比例, kind)]。还没有点、或有点缺少时间时
        无法换算，返回空列表（不画刻度）。"""
        return [(fraction, tick.kind) for fraction, tick in self._tick_fractions()]

    def _tick_x(self, fraction: float) -> float:
        width = self.width()
        return min(max(fraction * width, 0.5), max(width - 0.5, 0.5))

    def tick_at(self, x: float) -> Optional[Tick]:
        """横坐标 x（控件像素）附近的打点刻度，取最近的一个；没有返回 None。"""
        best: Optional[Tick] = None
        best_distance = TICK_HIT_RADIUS + 1e-9
        for fraction, tick in self._tick_fractions():
            distance = abs(self._tick_x(fraction) - x)
            if distance <= best_distance:
                best, best_distance = tick, distance
        return best

    def event(self, event) -> bool:
        if event.type() == QEvent.ToolTip:
            tick = self.tick_at(event.pos().x())
            if tick is not None and tick.text:
                QToolTip.showText(event.globalPos(), tick.text, self)
                return True
        return super().event(event)

    def mouseMoveEvent(self, event) -> None:
        tick = self.tick_at(event.pos().x())
        editable = tick is not None and tick.kind in EDITABLE_TICK_KINDS
        self.setCursor(Qt.PointingHandCursor if editable else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def contextMenuEvent(self, event) -> None:
        tick = self.tick_at(event.pos().x())
        if tick is None or tick.marker is None or tick.kind not in EDITABLE_TICK_KINDS:
            event.ignore()
            return
        event.accept()
        self.marker_menu_requested.emit(tick.marker, event.globalPos())

    def paintEvent(self, event):
        """
        QPainter 抗锯齿折线：纵向按 min-max 自适应并套用最小跨度（平线画在中线）；
        0 个点不画任何内容，1 个点画一个圆点；无坐标轴、无文字。

        全部分支均已避开除零（step 的分母固定在 count>=2 分支内，恒 >=1；
        span==0 时单独走中线分支不做除法），widget 尚未布局（width/height 为0）
        时也只会画出退化的点/线，不会抛异常——paintEvent 里的异常在 PyQt5 下
        无法被安全捕获后优雅降级，必须在源头保证不发生。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        values = self._sampler.series()
        count = len(values)
        width = self.width()
        height = self.height()
        color = qcolor(LINE_COLOR)

        if count == 1:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(width / 2, height / 2), 2.0, 2.0)
        elif count >= 2:
            painter.setPen(cosmetic_pen(color, 1.5, self.devicePixelRatioF()))

            low, high = compute_y_range(values, self._metric_type)
            span = high - low
            step = width / (count - 1)

            points = []
            for i, value in enumerate(values):
                x = i * step
                if span <= 0:
                    y = height / 2
                else:
                    ratio = (value - low) / span
                    y = height - ratio * height
                points.append(QPointF(x, y))

            painter.drawPolyline(QPolygonF(points))
        # count == 0：不画任何内容

        self._paint_ticks(painter, width, height)
        painter.end()

    def _paint_ticks(self, painter: QPainter, width: int, height: int) -> None:
        """打点刻度：基线注意色实线，普通打点注意色虚线，系统事件各自的语义色点划线，
        都贯穿整个高度。"""
        positions = self.tick_positions()
        if not positions:
            return
        ratio = self.devicePixelRatioF()
        for fraction, kind in positions:
            painter.setPen(tick_pen(kind, ratio))
            # 竖线对齐到设备像素中心：逻辑坐标 x 在 150% 缩放下常落在两个设备像素之间
            x = (math.floor(self._tick_x(fraction) * ratio) + 0.5) / ratio
            painter.drawLine(QPointF(x, 0), QPointF(x, height))
