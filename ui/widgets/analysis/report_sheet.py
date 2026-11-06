"""
报告图片的离屏渲染

报告 PNG 与"复制报告图片"走同一条管线：build_report_text 拼出标题行（进程 · PID · 备注 ·
指标）、信息行（范围 · 时长 · 采集间隔 · 点数）、各指标结论行与脚注；render_report_image
用与界面相同的 ChartView 画多指标小倍数图（离屏：WA_DontShowOnScreen 下 show 一次让
布局与 pyqtgraph 视图就位后 grab），再用 QPainter 把文字与图拼成一张 QImage。

渲染必须在主线程（控件）；得到的 QImage 可交给导出线程保存（QImage 可跨线程）。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from PyQt5.QtCore import QCoreApplication, QEvent, QRect, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtWidgets import QGridLayout, QWidget

from core.history_query import TaskAnalysis
from core.report import downsample_footnote, metric_names
from ui.chart_theme import chart_colors
from ui.formatting import build_summary_row, format_duration, object_usage_text
from ui.typography import ui_font
from ui.widgets.analysis.trend_panel import draw_snapshot
from ui.widgets.history.chart_view import ChartView
from utils.metrics import get_metric_display_name

REPORT_WIDTH = 1600
REPORT_MARGIN = 32
REPORT_COLUMNS = 2
REPORT_CHART_HEIGHT = 300
TITLE_PX = 22
BODY_PX = 15
LINE_GAP = 8
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'


@dataclass(frozen=True)
class ReportText:
    """报告图片上的文字。"""
    title: str
    info: str
    conclusions: List[str]
    footnote: str


def _span_seconds(analysis: TaskAnalysis) -> float:
    starts, ends = [], []
    for snapshot in analysis.snapshots.values():
        if snapshot.band:
            starts.append(snapshot.band[0].start)
            ends.append(snapshot.band[-1].end)
        elif snapshot.buckets:
            starts.append(snapshot.buckets[0].timestamp)
            ends.append(snapshot.buckets[-1].timestamp)
    if not starts:
        return 0.0
    return (max(ends) - min(starts)).total_seconds()


def build_report_text(task, analysis: TaskAnalysis, range_desc: str,
                      generated_at: datetime) -> ReportText:
    """报告文字：标题、信息行、每个指标一行结论、脚注。"""
    names = metric_names(task.metric_types)
    title_parts = [task.process_name, f"PID {task.pid}"]
    note = (task.note or '').strip()
    if note:
        title_parts.append(note)
    title_parts.append(f"{len(names)} 项指标：{'、'.join(names)}")

    points = max((s.total_count for s in analysis.snapshots.values()), default=0)
    raw = sum(s.total_count for s in analysis.snapshots.values())
    info = (f"范围：{range_desc} · 时长 {format_duration(_span_seconds(analysis))} · "
            f"采集间隔 {task.interval:g} 秒 · {points:,} 点")

    conclusions = []
    for metric, snapshot in analysis.snapshots.items():
        if not metric:
            continue
        row = build_summary_row(metric, snapshot)
        parts = [f"{get_metric_display_name(metric)}：{row.text('verdict')}",
                 f"较基线 {row.text('delta')}", row.text('rate')]
        if row.text('per_round') != '--':
            parts.append(f"每轮 {row.text('per_round')}")
        usage = object_usage_text(metric, snapshot.stats['last'] if snapshot.stats else None)
        if usage:
            parts.append(usage)
        conclusions.append("，".join(parts))
    footnote = (f"{downsample_footnote(raw)} · 生成于 {generated_at.strftime(TIME_FORMAT)}")
    return ReportText(" · ".join(title_parts), info, conclusions, footnote)


def render_charts(analysis: TaskAnalysis, width: int = REPORT_WIDTH - 2 * REPORT_MARGIN,
                  columns: int = REPORT_COLUMNS,
                  origin: Optional[float] = None) -> Optional[QImage]:
    """离屏渲染多指标小倍数图（与界面同一 ChartView）；没有可画的指标时为 None。"""
    snapshots = [(m, s) for m, s in analysis.snapshots.items() if m and s.points]
    if not snapshots:
        return None
    columns = max(1, min(columns, len(snapshots)))
    rows = (len(snapshots) + columns - 1) // columns
    sheet = QWidget()
    sheet.setAttribute(Qt.WA_DontShowOnScreen, True)
    grid = QGridLayout(sheet)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(12)
    for i, (metric, snapshot) in enumerate(snapshots):
        chart = ChartView(sheet)
        chart.set_title(get_metric_display_name(metric))
        chart.set_tools_visible(False)
        chart.set_chart_min_height(REPORT_CHART_HEIGHT - 60)
        draw_snapshot(chart, metric, snapshot, origin)
        grid.addWidget(chart, i // columns, i % columns)
    sheet.resize(width, rows * REPORT_CHART_HEIGHT + (rows - 1) * 12)
    try:
        sheet.show()
        grid.activate()
        # 让挂起的尺寸/布局事件生效（不处理用户输入事件）
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        for chart in sheet.findChildren(ChartView):
            chart.reset_view()
        # pyqtgraph 的坐标轴在首次绘制时才按刻度文字定宽（宽度变化再发布局请求）：
        # 先画一遍让轴宽到位、再布局一次，第二遍才是最终图，否则纵轴标题会压住刻度
        sheet.grab()
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        image = sheet.grab().toImage()
    finally:
        sheet.hide()
        sheet.deleteLater()
    return image


def _font(px: int, weight: int = QFont.Normal) -> QFont:
    return ui_font(px, weight)


def render_report_image(text: ReportText, analysis: TaskAnalysis,
                        width: int = REPORT_WIDTH, origin: Optional[float] = None) -> QImage:
    """拼出整张报告图片：标题 / 信息 / 结论 / 小倍数图 / 脚注。"""
    colors = chart_colors()
    inner = width - 2 * REPORT_MARGIN
    charts = render_charts(analysis, inner, origin=origin)
    lines = ([(text.title, _font(TITLE_PX, QFont.DemiBold))]
             + [(text.info, _font(BODY_PX))]
             + [(line, _font(BODY_PX)) for line in text.conclusions])
    line_heights = [int(font.pixelSize() * 1.5) for _text, font in lines]
    footnote_font = _font(12)
    header = sum(line_heights) + LINE_GAP * len(lines)
    chart_height = charts.height() if charts is not None else 0
    height = (REPORT_MARGIN + header + LINE_GAP + chart_height + 2 * LINE_GAP
              + int(12 * 1.6) + REPORT_MARGIN)

    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(QColor(colors['background']))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setPen(QColor(colors['text']))
        y = REPORT_MARGIN
        for (line, font), line_height in zip(lines, line_heights):
            painter.setFont(font)
            painter.drawText(QRect(REPORT_MARGIN, y, inner, line_height),
                             Qt.AlignLeft | Qt.AlignVCenter, line)
            y += line_height + LINE_GAP
        y += LINE_GAP
        if charts is not None:
            painter.drawImage(REPORT_MARGIN, y, charts)
            y += chart_height
        y += 2 * LINE_GAP
        painter.setFont(footnote_font)
        painter.drawText(QRect(REPORT_MARGIN, y, inner, int(12 * 1.6)),
                         Qt.AlignLeft | Qt.AlignVCenter, text.footnote)
    finally:
        painter.end()
    return image
