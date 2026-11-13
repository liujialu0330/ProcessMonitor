"""
报告图片的离屏渲染

报告 PNG 与"复制报告图片"走同一条管线：build_report_text 组装报告的全部文字与表格数据，
render_report_image 用 QPainter 自上而下画出：

1. 标题块：进程名 · PID · 备注（超长自动换行），下一行是范围与起止时间；
2. 元信息网格（两列）：core.report.metadata_items；
3. 结论表：每个指标一行，判定胶囊 / 基线 / 当前 / 较基线 / 每轮均增 / 增速 ±CI / R²；
4. 打点区间增量表：至少 2 轮打点时才有，数据见 core.report.round_delta_rows；
5. 小倍数趋势图：只画"持续上涨"与"波动"的指标（与界面同一 ChartView，离屏渲染）；
6. 页脚：工具版本、生成时间、降采样说明。

配色：默认固定浅色，与界面当前主题无关（报告常被贴进文档、邮件，深色底不合适）；设置
"报告配色 = 跟随主题"时才用当前主题。报告控件按显式 palette 取色、不跟随 themeChanged，
也不改全局主题。

尺寸：输出 PNG 的像素宽恒为 width（默认 1600），逻辑布局宽 = width / 设备像素比；
设备像素比由参数显式给出（默认 1），所有控件经 QWidget.render 画进同一设备像素比的
QImage，与屏幕缩放（QT_SCALE_FACTOR、系统缩放）无关。

渲染必须在主线程（控件）；得到的 QImage 可交给导出线程保存（QImage 可跨线程）。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QCoreApplication, QEvent, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen
from PyQt5.QtWidgets import QGridLayout, QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme, qconfig

import config
from app_config import REPORT_SCHEME_FOLLOW, cfg
from core.history_query import TaskAnalysis
from core.report import (analysis_span, downsample_footnote, metadata_items, metric_names,
                         quality_text, round_delta_rows)
from ui import theme_tokens as tokens
from ui.chart_theme import chart_palette, make_transparent
from ui.color_contrast import to_qcolor
from ui.formatting import (STAT_EMPTY, build_summary_row, format_duration, format_rate_text,
                           object_usage_text, overall_summary, task_overall_verdict,
                           verdict_summary)
from ui.typography import data_font, ui_font
from ui.widgets.analysis.report_layout import (
    block_height, chunk, column_widths, grid_shape, logical_width, pixel_size, row_height,
    wrap_text)
from ui.widgets.analysis.trend_panel import draw_snapshot
from ui.widgets.history.chart_view import ChartView
from utils.metrics import VALUE_CLASS_LEVEL, format_metric_delta, get_metric_display_name

# ---------- 尺寸（逻辑像素） ----------

REPORT_WIDTH = 1600
REPORT_PIXEL_RATIO = 1.0
REPORT_MARGIN = 40
REPORT_COLUMNS = 2
# 每张小倍数图（含图上方的标题行）的高度与图间距
REPORT_CHART_HEIGHT = 300
CHART_TITLE_HEIGHT = 34
CHART_GAP = 16
CHART_CARD_RADIUS = 8

TITLE_PX = 24
SECTION_PX = 17
BODY_PX = 14
SMALL_PX = 12
LINE_FACTOR = 1.5
SECTION_GAP = 26
HEADING_GAP = 10
META_LABEL_WIDTH = 84
META_COLUMN_GAP = 32
CELL_PAD_X = 10
CELL_PAD_Y = 7
TABLE_ROW_MIN = 36
PILL_PAD_X = 10
PILL_HEIGHT = 22
# 分隔线与表格边线：坐标轴色按此不透明度淡化（只作版面分隔，不承载信息）
DIVIDER_ALPHA = 0.22

TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
CLOCK_FORMAT = '%H:%M:%S'

# ---------- 文案 ----------

SECTION_META = "概况"
SECTION_CONCLUSION = "结论"
SECTION_ROUNDS = "打点区间增量"
SECTION_CHARTS = "趋势图"
OVERALL_LINE = "整体判定：{verdict}。{sentence}"
ROUNDS_HINT = "每行是相邻两个打点之间的一轮；Δ = 本轮终值 − 起值，与上轮差 = 本轮 Δ − 上一轮 Δ"
CHARTS_HINT = "只画持续上涨与波动的指标，平稳等其余指标见结论表"
NO_CHART_TEXT = "没有持续上涨或波动的指标，未附趋势图"
RANGE_TEMPLATE = "范围：{range} · {start} → {end}"
RANGE_ONLY_TEMPLATE = "范围：{range}"
FOOTER_TEMPLATE = "{app} {version} · 生成于 {time} · {downsample}"

CONCLUSION_HEADERS = ("指标", "判定", "基线", "当前", "较基线", "每轮均增", "增速 ±CI", "R²")
CONCLUSION_WEIGHTS = (1.7, 1.0, 1.2, 1.2, 1.7, 1.2, 2.0, 0.6)
VERDICT_COLUMN = 1
ROUND_LABEL_HEADER = "区间"
ROUND_DURATION_HEADER = "时长"
ROUND_DELTA_HEADER = "{name} Δ"
ROUND_VS_HEADER = "与上轮差"
ROUND_VS_SINGLE_HEADER = "与上轮差 · {name}"
# 至少这么多轮（相邻打点围出的区间）才出区间增量表：只有一轮时没有"与上轮差"可比
ROUND_TABLE_MIN_ROUNDS = 2
# 进小倍数图的判定；其余（平稳、数据不足、累计量、峰值）只在结论表出现
CHART_VERDICTS = ("持续上涨", "波动")



# ---------------------------------------------------------------- 数据


@dataclass(frozen=True)
class ConclusionRow:
    """结论表一行：cells 依次为 基线 / 当前 / 较基线 / 每轮均增 / 增速 ±CI / R²。
    note 是指标名下方的小字（GDI/USER 的上限占比），dimmed 表示样本不足整行置灰。"""
    metric: str
    name: str
    verdict: str
    cells: Tuple[str, ...]
    note: str = ''
    dimmed: bool = False


@dataclass(frozen=True)
class RoundTable:
    """打点区间增量表：表头与各行文本（单元格内可含换行）。"""
    headers: Tuple[str, ...]
    rows: Tuple[Tuple[str, ...], ...]


@dataclass(frozen=True)
class ReportText:
    """报告图片上的文字与表格数据。

    title / info / conclusions 是一行式摘要（复制文本、旧调用方用）；版式 2.0 用 heading、
    time_range、metadata、conclusion_rows、round_table、chart_metrics 与 footer。"""
    title: str
    info: str
    conclusions: List[str]
    footnote: str
    heading: str = ''
    time_range: str = ''
    metadata: Tuple[Tuple[str, str], ...] = ()
    conclusion_rows: Tuple[ConclusionRow, ...] = ()
    round_table: Optional[RoundTable] = None
    chart_metrics: Tuple[str, ...] = ()
    footer: str = ''
    # 整体判定行：被测进程重启续接过、中途崩溃过或提前退出时才有，与分析页结论条同一口径
    overall_line: str = ''


def report_is_dark(scheme: Optional[str] = None) -> bool:
    """报告是否用深色配色：只有设置为"跟随主题"且当前是深色主题时为 True。
    scheme 为 None 时读设置项 report_color_scheme。"""
    if scheme is None:
        scheme = cfg.get(cfg.report_color_scheme)
    return scheme == REPORT_SCHEME_FOLLOW and isDarkTheme()


def _rgba(color: str, alpha: float) -> str:
    qc = QColor(color)
    return f"rgba({qc.red()}, {qc.green()}, {qc.blue()}, {alpha:g})"


def report_palette(dark: bool = False) -> Dict[str, str]:
    """报告版面配色（均取自 theme_tokens / chart_theme）：
    page 页面底（= 卡片常态底，与图表导出底一致）、text 正文、secondary 次要文字（与图表
    刻度文字同色）、dimmed 置灰行文字、divider 分隔线、header 表头底（中性胶囊底）。"""
    i = 1 if dark else 0
    chart = chart_palette(dark)
    return {
        'page': tokens.SURFACE[i],
        'text': tokens.PILL_TEXT[i],
        'secondary': chart['text'],
        'dimmed': tokens.DIMMED_TEXT[i],
        'divider': _rgba(chart['axis'], DIVIDER_ALPHA),
        'header': tokens.PILL_BACKGROUNDS['neutral'][i],
    }


def _time_range_text(analysis: TaskAnalysis, range_desc: str) -> str:
    span = analysis_span(analysis)
    if span is None:
        return RANGE_ONLY_TEMPLATE.format(range=range_desc)
    start, end = span
    end_format = CLOCK_FORMAT if end.date() == start.date() else TIME_FORMAT
    return RANGE_TEMPLATE.format(range=range_desc, start=start.strftime(TIME_FORMAT),
                                 end=end.strftime(end_format))


def _conclusion_row(metric: str, snapshot) -> ConclusionRow:
    row = build_summary_row(metric, snapshot)
    judgement = snapshot.judgement
    rate, r2 = row.text('rate'), STAT_EMPTY
    if (snapshot.value_class == VALUE_CLASS_LEVEL and snapshot.rate is not None
            and judgement is not None and judgement.fit is not None):
        rate, r2 = format_rate_text(metric, snapshot.rate), f"{judgement.fit.r2:.2f}"
    usage = object_usage_text(metric, snapshot.stats['last'] if snapshot.stats else None)
    cells = (row.text('baseline'), row.text('current'), row.text('delta'),
             row.text('per_round'), rate, r2)
    # 基线在别的启动段（"基线""较基线"写 —）：把原因写在指标名下方，报告里没有悬停可看
    note = " · ".join(t for t in (usage, snapshot.baseline_note) if t)
    return ConclusionRow(metric, get_metric_display_name(metric), row.text('verdict'),
                         cells, note, row.dimmed)


def _round_table(analysis: TaskAnalysis,
                 verdicts: Dict[str, str]) -> Optional[RoundTable]:
    rows = round_delta_rows(analysis)
    if len(rows) < ROUND_TABLE_MIN_ROUNDS:
        return None
    metrics = list(rows[0].deltas)
    # "与上轮差"只列关注的指标（持续上涨、波动），没有时列第一个指标，避免一格塞满所有指标
    focus = [m for m in metrics if verdicts.get(m) in CHART_VERDICTS] or metrics[:1]
    if len(focus) == 1:
        vs_header = ROUND_VS_SINGLE_HEADER.format(name=get_metric_display_name(focus[0]))
    else:
        vs_header = ROUND_VS_HEADER
    headers = ((ROUND_LABEL_HEADER, ROUND_DURATION_HEADER)
               + tuple(ROUND_DELTA_HEADER.format(name=get_metric_display_name(m))
                       for m in metrics)
               + (vs_header,))
    body = []
    for item in rows:
        deltas = tuple(format_metric_delta(m, item.deltas[m]) for m in metrics)
        if len(focus) == 1:
            vs = format_metric_delta(focus[0], item.vs_previous[focus[0]])
        else:
            vs = "\n".join(f"{get_metric_display_name(m)} "
                           f"{format_metric_delta(m, item.vs_previous[m])}" for m in focus)
        body.append((item.label, format_duration(item.duration_seconds)) + deltas + (vs,))
    return RoundTable(headers, tuple(body))


def _overall_line(task, analysis: TaskAnalysis) -> str:
    """整体判定行（与结论条同一函数得出）；没有分段 / 崩溃 / 提前退出的信息时为空串。"""
    base = verdict_summary([(m, s if s.points else None)
                            for m, s in analysis.snapshots.items() if m])
    overall = overall_summary(base, analysis, task_overall_verdict(task, analysis))
    if overall is base:
        return ''
    return OVERALL_LINE.format(verdict=overall.verdict, sentence=overall.sentence)


def build_report_text(task, analysis: TaskAnalysis, range_desc: str,
                      generated_at: datetime) -> ReportText:
    """报告的全部文字：一行式摘要（title / info / conclusions）与版式 2.0 的各区块数据。"""
    names = metric_names(task.metric_types)
    heading_parts = [task.process_name, f"PID {task.pid}"]
    note = (task.note or '').strip()
    if note:
        heading_parts.append(note)
    title = " · ".join(heading_parts + [f"{len(names)} 项指标：{'、'.join(names)}"])

    span = analysis_span(analysis)
    span_seconds = (span[1] - span[0]).total_seconds() if span else 0.0
    points = max((s.total_count for s in analysis.snapshots.values()), default=0)
    raw = sum(s.total_count for s in analysis.snapshots.values())
    info = (f"范围：{range_desc} · 时长 {format_duration(span_seconds)} · "
            f"采集间隔 {task.interval:g} 秒 · {points:,} 点")
    # 数据质量（采集完成率、跳过的周期、调度延迟）只有收尾写过质量列的任务才有
    quality = quality_text(getattr(task, 'quality', None))
    if quality:
        info += f" · {quality}"

    conclusions, rows = [], []
    for metric, snapshot in analysis.snapshots.items():
        if not metric:
            continue
        summary = build_summary_row(metric, snapshot)
        parts = [f"{get_metric_display_name(metric)}：{summary.text('verdict')}",
                 f"较基线 {summary.text('delta')}", summary.text('rate')]
        if summary.text('per_round') != STAT_EMPTY:
            parts.append(f"每轮 {summary.text('per_round')}")
        usage = object_usage_text(metric, snapshot.stats['last'] if snapshot.stats else None)
        if usage:
            parts.append(usage)
        conclusions.append("，".join(parts))
        rows.append(_conclusion_row(metric, snapshot))

    overall_line = _overall_line(task, analysis)
    if overall_line:
        conclusions.insert(0, overall_line)
    verdicts = {row.metric: row.verdict for row in rows}
    charted = tuple(row.metric for row in rows if row.verdict in CHART_VERDICTS
                    and analysis.snapshots[row.metric].points)
    footnote = f"{downsample_footnote(raw)} · 生成于 {generated_at.strftime(TIME_FORMAT)}"
    footer = FOOTER_TEMPLATE.format(app=config.APP_NAME, version=config.APP_VERSION,
                                    time=generated_at.strftime(TIME_FORMAT),
                                    downsample=downsample_footnote(raw))
    return ReportText(
        title, info, conclusions, footnote,
        heading=" · ".join(heading_parts), time_range=_time_range_text(analysis, range_desc),
        metadata=tuple(metadata_items(task, analysis)), conclusion_rows=tuple(rows),
        round_table=_round_table(analysis, verdicts), chart_metrics=charted, footer=footer,
        overall_line=overall_line)


# ---------------------------------------------------------------- 绘制工具


def _font(px: int, weight: int = QFont.Normal) -> QFont:
    return ui_font(px, weight)


def _measure(font: QFont) -> Callable[[str], float]:
    metrics = QFontMetricsF(font)
    return metrics.horizontalAdvance


def _line_height(font: QFont) -> float:
    return round(font.pixelSize() * LINE_FACTOR)


def paint_pill(painter: QPainter, x: float, center_y: float, text: str,
               dark: bool) -> float:
    """在 (x, 垂直居中 center_y) 画一个判定胶囊（底色按判定语气，文字同判定摘要表），返回宽度。"""
    font = _font(SMALL_PX, QFont.DemiBold)
    width = _measure(font)(text) + 2 * PILL_PAD_X
    rect = QRectF(x, center_y - PILL_HEIGHT / 2, width, PILL_HEIGHT)
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(to_qcolor(tokens.pill_background(tokens.verdict_tone(text), dark)))
    painter.drawRoundedRect(rect, PILL_HEIGHT / 2, PILL_HEIGHT / 2)
    painter.setPen(to_qcolor(tokens.pick(tokens.PILL_TEXT, dark)))
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignCenter, text)
    painter.restore()
    return width


def _draw_lines(painter: QPainter, lines: Sequence[str], font: QFont, color: str,
                x: float, y: float, width: float, align=Qt.AlignLeft) -> float:
    """逐行画文字，返回画完后的 y。"""
    painter.setFont(font)
    painter.setPen(to_qcolor(color))
    height = _line_height(font)
    for line in lines:
        painter.drawText(QRectF(x, y, width, height), align | Qt.AlignVCenter, line)
        y += height
    return y


def _draw_divider(painter: QPainter, palette: dict, x: float, y: float, width: float) -> None:
    painter.save()
    painter.setPen(QPen(to_qcolor(palette['divider']), 1))
    painter.drawLine(int(x), int(y), int(x + width), int(y))
    painter.restore()


# 一个版面区块：(高度, 绘制函数(painter, x, y, 宽度))
Block = Tuple[int, Callable[[QPainter, float, float, float], None]]


def _text_block(text: str, font: QFont, color: str, width: float) -> Block:
    lines = wrap_text(text, width, _measure(font))
    height = block_height(len(lines), _line_height(font))

    def draw(painter, x, y, w):
        _draw_lines(painter, lines, font, color, x, y, w)
    return height, draw


def _spacer(height: int) -> Block:
    return height, lambda *_args: None


def _divider_block(palette: dict) -> Block:
    def draw(painter, x, y, w):
        _draw_divider(painter, palette, x, y, w)
    return 1, draw


def _meta_grid(items: Sequence[Tuple[str, str]], palette: dict, width: float) -> Block:
    """元信息两列网格：每格"标签  值"，值超宽折行，一行的高度取两格中较高者。"""
    label_font, value_font = _font(BODY_PX), _font(BODY_PX)
    line = _line_height(value_font)
    col = (width - META_COLUMN_GAP) / 2
    value_width = col - META_LABEL_WIDTH
    measure = _measure(value_font)
    rows = []
    for pair in chunk(list(items), 2):
        wrapped = [(label, wrap_text(value, value_width, measure)) for label, value in pair]
        rows.append((wrapped, row_height([len(v) for _l, v in wrapped], line, 2)))
    height = sum(h for _w, h in rows)

    def draw(painter, x, y, _w):
        for wrapped, h in rows:
            for c, (label, values) in enumerate(wrapped):
                cx = x + c * (col + META_COLUMN_GAP)
                _draw_lines(painter, [label], label_font, palette['secondary'], cx, y + 2,
                            META_LABEL_WIDTH)
                _draw_lines(painter, values, value_font, palette['text'],
                            cx + META_LABEL_WIDTH, y + 2, value_width)
            y += h
    return height, draw


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], weights: Sequence[float],
           palette: dict, dark: bool, width: float, *, pill_column: Optional[int] = None,
           notes: Sequence[str] = (), dimmed: Sequence[bool] = ()) -> Block:
    """通用表格：表头淡底；首列左对齐，其余右对齐（数字列），pill_column 列画判定胶囊；
    单元格超宽折行，行高取该行最高的格。notes 为首列第二行的小字。"""
    widths = column_widths(width, weights)
    head_font, cell_font, num_font = _font(BODY_PX, QFont.DemiBold), _font(BODY_PX), \
        data_font(BODY_PX)
    note_font = _font(SMALL_PX)
    line = _line_height(cell_font)
    note_line = _line_height(note_font)

    def wrap_cells(texts, first_font, other_font):
        return [wrap_text(t, w - 2 * CELL_PAD_X, _measure(first_font if c == 0 else other_font))
                for c, (t, w) in enumerate(zip(texts, widths))]

    head_cells = wrap_cells(headers, head_font, head_font)
    head_h = row_height([len(c) for c in head_cells], line, CELL_PAD_Y, TABLE_ROW_MIN)
    body = []
    for r, texts in enumerate(rows):
        cells = wrap_cells(texts, cell_font, num_font)
        note = notes[r] if r < len(notes) else ''
        counts = [len(c) for c in cells]
        extra = note_line if note else 0
        h = row_height(counts, line, CELL_PAD_Y, TABLE_ROW_MIN)
        h = max(h, row_height([counts[0]], line, CELL_PAD_Y) + extra)
        body.append((cells, note, h, r < len(dimmed) and dimmed[r]))
    height = head_h + sum(h for *_rest, h, _d in body)

    def draw(painter, x, y, _w):
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(to_qcolor(palette['header']))
        painter.drawRect(QRectF(x, y, sum(widths), head_h))
        painter.restore()
        cx = x
        for c, (cell, w) in enumerate(zip(head_cells, widths)):
            align = Qt.AlignLeft if c == 0 else (
                Qt.AlignHCenter if c == pill_column else Qt.AlignRight)
            top = y + (head_h - len(cell) * line) / 2
            _draw_lines(painter, cell, head_font, palette['secondary'], cx + CELL_PAD_X, top,
                        w - 2 * CELL_PAD_X, align)
            cx += w
        y += head_h
        for cells, note, h, is_dimmed in body:
            color = palette['dimmed'] if is_dimmed else palette['text']
            cx = x
            for c, (cell, w) in enumerate(zip(cells, widths)):
                if c == pill_column:
                    text = "".join(cell)
                    pill_w = _measure(_font(SMALL_PX, QFont.DemiBold))(text) + 2 * PILL_PAD_X
                    paint_pill(painter, cx + (w - pill_w) / 2, y + h / 2, text, dark)
                else:
                    block = len(cell) * line + (note_line if c == 0 and note else 0)
                    top = y + (h - block) / 2
                    align = Qt.AlignLeft if c == 0 else Qt.AlignRight
                    end = _draw_lines(painter, cell, cell_font if c == 0 else num_font, color,
                                      cx + CELL_PAD_X, top, w - 2 * CELL_PAD_X, align)
                    if c == 0 and note:
                        _draw_lines(painter, [note], note_font, palette['secondary'],
                                    cx + CELL_PAD_X, end, w - 2 * CELL_PAD_X)
                cx += w
            y += h
            _draw_divider(painter, palette, x, y - 1, sum(widths))
    return height, draw


def _section(title: str, palette: dict, width: float, hint: str = '') -> List[Block]:
    blocks = [_text_block(title, _font(SECTION_PX, QFont.DemiBold), palette['text'], width)]
    if hint:
        blocks.append(_text_block(hint, _font(SMALL_PX), palette['secondary'], width))
    blocks.append(_spacer(HEADING_GAP))
    return blocks


# ---------------------------------------------------------------- 小倍数图


class ReportChartView(ChartView):
    """报告专用的趋势图卡片：按给定 palette 取色（不跟随全局主题、不连 themeChanged），
    卡片底与边线也按 palette 画（ChartView / CardWidget 按当前主题画，深色界面下导出的
    浅色报告里会出现深色卡片）。"""

    def __init__(self, palette: Dict[str, str], page: Dict[str, str], parent=None):
        self._report_palette = palette
        self._page_palette = page
        super().__init__(parent)
        try:
            qconfig.themeChanged.disconnect(self._on_theme_changed)
        except TypeError:
            pass
        self.set_tools_visible(False)
        self.hover_label.setVisible(False)

    def _colors(self) -> Dict[str, str]:
        return getattr(self, '_report_palette', None) or chart_palette(False)

    def apply_theme(self) -> None:
        colors = self._colors()
        make_transparent(self.chart_widget)
        for axis_name in ('bottom', 'left'):
            axis = self.chart_widget.getAxis(axis_name)
            axis.setPen(pg.mkPen(color=colors['axis']))
            axis.setTextPen(pg.mkPen(color=colors['text']))
        self.crosshair_line.setPen(pg.mkPen(color=colors['crosshair'], style=Qt.DashLine))
        self._redraw()

    def _redraw(self) -> None:
        # 与 ChartView._redraw 相同，只是取色来自本控件的 palette 而不是当前主题
        self.chart_widget.clear()
        self._marker_items = []
        self._gap_items = []
        self._band_items = []
        self.chart_widget.addItem(self.crosshair_line, ignoreBounds=True)
        self.hide_hover()
        if not self._x:
            return
        colors = self._colors()
        self._draw_gaps(colors)
        self._draw_band(colors)
        pen = pg.mkPen(color=colors['curve'], width=2)
        extra = {'connect': np.array(self.connect_array(), dtype=bool)} if self._gaps else {}
        self.chart_widget.plot(self._x, self._y, pen=pen, **extra)
        self._draw_markers(colors)

    def paintEvent(self, event) -> None:
        page = getattr(self, '_page_palette', None) or report_palette(False)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(to_qcolor(page['divider']), 1))
            painter.setBrush(to_qcolor(page['page']))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                    CHART_CARD_RADIUS, CHART_CARD_RADIUS)
        finally:
            painter.end()


class _ChartTitle(QWidget):
    """小倍数图上方的标题行：指标名 + 判定胶囊。"""

    def __init__(self, name: str, verdict: str, page: Dict[str, str], dark: bool,
                 parent=None):
        super().__init__(parent)
        self._name, self._verdict, self._page, self._dark = name, verdict, page, dark
        self.setFixedHeight(CHART_TITLE_HEIGHT)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.TextAntialiasing)
            font = _font(BODY_PX + 1, QFont.DemiBold)
            painter.setFont(font)
            painter.setPen(to_qcolor(self._page['text']))
            width = _measure(font)(self._name)
            painter.drawText(QRectF(2, 0, width + 4, self.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, self._name)
            if self._verdict:
                paint_pill(painter, width + 14, self.height() / 2, self._verdict, self._dark)
        finally:
            painter.end()


class _Sheet(QWidget):
    """小倍数图的离屏容器：显式画页面底色（网格间距处不再露出 Qt 默认的浅灰窗口色）。"""

    def __init__(self, page_color: str):
        super().__init__()
        self._page_color = page_color
        self.setAttribute(Qt.WA_DontShowOnScreen, True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), to_qcolor(self._page_color))
        finally:
            painter.end()


def _render_widget(widget: QWidget, dpr: float, fill: str) -> QImage:
    image = QImage(pixel_size(widget.width(), dpr), pixel_size(widget.height(), dpr),
                   QImage.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(to_qcolor(fill))
    widget.render(image)
    return image


def render_charts(analysis: TaskAnalysis, width: int = REPORT_WIDTH - 2 * REPORT_MARGIN,
                  columns: int = REPORT_COLUMNS,
                  origin: Optional[float] = None, *,
                  metrics: Optional[Sequence[str]] = None,
                  verdicts: Optional[Dict[str, str]] = None,
                  dark: bool = False,
                  device_pixel_ratio: float = REPORT_PIXEL_RATIO) -> Optional[QImage]:
    """离屏渲染小倍数图（与界面同一 ChartView 的画法），每张图上方是"指标名 + 判定胶囊"。
    metrics 限定要画的指标（None 为全部有数据的指标）；width 为逻辑宽，返回的 QImage 带
    device_pixel_ratio。没有可画的指标时为 None。"""
    chosen = [(m, s) for m, s in analysis.snapshots.items()
              if m and s.points and (metrics is None or m in metrics)]
    if not chosen:
        return None
    verdicts = verdicts or {}
    page = report_palette(dark)
    colors = chart_palette(dark)
    rows, used = grid_shape(len(chosen), columns)
    width = int(width)
    sheet = _Sheet(page['page'])
    grid = QGridLayout(sheet)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(CHART_GAP)
    charts = []
    for i, (metric, snapshot) in enumerate(chosen):
        cell = QWidget(sheet)
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(_ChartTitle(get_metric_display_name(metric), verdicts.get(metric, ''),
                                     page, dark, cell))
        chart = ReportChartView(colors, page, cell)
        chart.set_chart_min_height(REPORT_CHART_HEIGHT - CHART_TITLE_HEIGHT - 60)
        draw_snapshot(chart, metric, snapshot, origin)
        layout.addWidget(chart, 1)
        cell.setFixedHeight(REPORT_CHART_HEIGHT)
        grid.addWidget(cell, i // used, i % used)
        charts.append(chart)
    sheet.resize(width, rows * REPORT_CHART_HEIGHT + (rows - 1) * CHART_GAP)
    try:
        sheet.show()
        grid.activate()
        # 让挂起的尺寸/布局事件生效（不处理用户输入事件）
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        for chart in charts:
            chart.reset_view()
        # pyqtgraph 的坐标轴在首次绘制时才按刻度文字定宽（宽度变化再发布局请求）：
        # 先画一遍让轴宽到位、再布局一次，第二遍才是最终图，否则纵轴标题会压住刻度
        _render_widget(sheet, device_pixel_ratio, page['page'])
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        image = _render_widget(sheet, device_pixel_ratio, page['page'])
    finally:
        sheet.hide()
        sheet.deleteLater()
    return image


# ---------------------------------------------------------------- 整张报告


def _image_block(image: QImage) -> Block:
    ratio = image.devicePixelRatio() or 1.0
    height = int(round(image.height() / ratio))

    def draw(painter, x, y, _w):
        painter.drawImage(int(x), int(y), image)
    return height, draw


def _layout_blocks(text: ReportText, analysis: TaskAnalysis, inner: float, dark: bool,
                   dpr: float, origin: Optional[float]) -> List[Block]:
    page = report_palette(dark)
    blocks: List[Block] = []
    # 标题块
    blocks.append(_text_block(text.heading or text.title, _font(TITLE_PX, QFont.DemiBold),
                              page['text'], inner))
    blocks.append(_spacer(4))
    blocks.append(_text_block(text.time_range or text.info, _font(BODY_PX), page['secondary'],
                              inner))
    blocks += [_spacer(SECTION_GAP - 8), _divider_block(page), _spacer(SECTION_GAP - 8)]
    # 元信息网格
    if text.metadata:
        blocks += _section(SECTION_META, page, inner)
        blocks.append(_meta_grid(text.metadata, page, inner))
        blocks.append(_spacer(SECTION_GAP))
    # 结论表
    if text.conclusion_rows:
        blocks += _section(SECTION_CONCLUSION, page, inner)
        if text.overall_line:
            blocks.append(_text_block(text.overall_line, _font(BODY_PX, QFont.DemiBold),
                                      page['text'], inner))
            blocks.append(_spacer(HEADING_GAP))
        rows = [(row.name, row.verdict) + row.cells for row in text.conclusion_rows]
        blocks.append(_table(CONCLUSION_HEADERS, rows, CONCLUSION_WEIGHTS, page, dark, inner,
                             pill_column=VERDICT_COLUMN,
                             notes=[row.note for row in text.conclusion_rows],
                             dimmed=[row.dimmed for row in text.conclusion_rows]))
        blocks.append(_spacer(SECTION_GAP))
    elif text.conclusions:
        for line in text.conclusions:
            blocks.append(_text_block(line, _font(BODY_PX), page['text'], inner))
        blocks.append(_spacer(SECTION_GAP))
    # 打点区间增量表
    table = text.round_table
    if table is not None:
        blocks += _section(SECTION_ROUNDS, page, inner, ROUNDS_HINT)
        weights = (1.8, 1.0) + (1.3,) * (len(table.headers) - 3) + (1.8,)
        blocks.append(_table(table.headers, table.rows, weights, page, dark, inner))
        blocks.append(_spacer(SECTION_GAP))
    # 小倍数图
    blocks += _section(SECTION_CHARTS, page, inner, CHARTS_HINT)
    charts = (render_charts(analysis, int(inner), origin=origin, metrics=text.chart_metrics,
                            verdicts={row.metric: row.verdict for row in text.conclusion_rows},
                            dark=dark, device_pixel_ratio=dpr)
              if text.chart_metrics else None)
    if charts is not None:
        blocks.append(_image_block(charts))
    else:
        blocks.append(_text_block(NO_CHART_TEXT, _font(BODY_PX), page['secondary'], inner))
    # 页脚
    blocks += [_spacer(SECTION_GAP), _divider_block(page), _spacer(10)]
    blocks.append(_text_block(text.footer or text.footnote, _font(SMALL_PX), page['secondary'],
                              inner))
    return blocks


def render_report_image(text: ReportText, analysis: TaskAnalysis,
                        width: int = REPORT_WIDTH, origin: Optional[float] = None, *,
                        dark: Optional[bool] = None,
                        device_pixel_ratio: float = REPORT_PIXEL_RATIO) -> QImage:
    """拼出整张报告图片。width 是输出像素宽（恒定），逻辑布局宽 = width / device_pixel_ratio，
    高度随内容自适应。dark 为 None 时按设置项"报告配色"（默认固定浅色）。"""
    if dark is None:
        dark = report_is_dark()
    dpr = float(device_pixel_ratio) if device_pixel_ratio and device_pixel_ratio > 0 else 1.0
    page = report_palette(dark)
    total = logical_width(width, dpr)
    inner = int(total - 2 * REPORT_MARGIN)
    blocks = _layout_blocks(text, analysis, inner, dark, dpr, origin)
    height = REPORT_MARGIN * 2 + sum(h for h, _draw in blocks)

    image = QImage(int(width), pixel_size(height, dpr), QImage.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(to_qcolor(page['page']))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        y = REPORT_MARGIN
        for h, draw in blocks:
            draw(painter, REPORT_MARGIN, y, inner)
            y += h
    finally:
        painter.end()
    return image


def render_report_for(task, analysis: TaskAnalysis, generated_at: datetime,
                      range_desc: str) -> QImage:
    """按任务与分析结果渲染整张报告图片（命令行导出与阶段快照共用；须在界面线程调用）。"""
    return render_report_image(build_report_text(task, analysis, range_desc, generated_at),
                               analysis)
