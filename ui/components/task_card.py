"""
监控任务卡片
实时显示单个任务各指标的当前值、较开始的变化量与趋势；任务结束后卡片保留，
切换为"已结束"形态，供用户回看最后的值并一键跳转历史页或重新监控。
"""
from datetime import datetime
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QGridLayout, QSizePolicy)
from qfluentwidgets import (
    PushButton, CardWidget, FluentIcon, CaptionLabel, StrongBodyLabel
)

from core.contracts import StopReason, UnavailableReason
from ui.components.sparkline import SparklineWidget
from ui.formatting import stop_reason_text, truncate_text, unavailable_reason_text
from ui.typography import DataCaptionLabel, DataLabel
from utils.metrics import (
    format_metric_delta, format_metric_value, get_metric_display_name
)

# "已暂停"/"异常结束"状态提示配色（橙色系浅/深双色）：CaptionLabel.setTextColor
# 只需设置一次即可自动跟随主题切换（FluentLabelBase 内部已连接 qconfig.themeChanged，
# 会自动用 lightColor/darkColor 重新套用样式），本文件无需再手动连接信号
PAUSED_LABEL_LIGHT_COLOR = QColor("#D83B01")
PAUSED_LABEL_DARK_COLOR = QColor("#FF9D42")

# 卡片身份行里备注的最大显示字数（完整内容放 tooltip）
NOTE_DISPLAY_CHARS = 40


class TaskCard(CardWidget):
    """监控任务卡片组件"""

    # 按钮信号一律只携带 task_id：TaskCard 不持有 manager，只 emit 信号，由
    # MonitorPage 调用 manager 并在确认成功后回调 set_paused() 等方法更新显示
    stop_clicked = pyqtSignal(str)
    pause_clicked = pyqtSignal(str)
    resume_clicked = pyqtSignal(str)
    marker_clicked = pyqtSignal(str)
    history_clicked = pyqtSignal(str)    # 已结束卡片："查看历史"
    restart_clicked = pyqtSignal(str)    # 已结束卡片："重新监控"
    close_clicked = pyqtSignal(str)      # 已结束卡片："关闭"（移除卡片）

    # 指标值网格每行列数
    VALUE_COLUMNS = 3

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
            interval: 采集周期（秒），仅用于"重新监控"时沿用
            note: 测试备注
        """
        super().__init__(parent)

        self.task_id = task_id
        self.process_name = process_name
        self.pid = pid
        self.metric_types = list(metric_types)
        self.interval = interval
        self.note = note or ''

        # 指标值标签与"较开始"标签 {指标类型: 标签}
        self.value_labels: Dict[str, QLabel] = {}
        self.delta_labels: Dict[str, QLabel] = {}

        # 基线（本任务首个有效值）与最新值 {指标类型: 值}
        self.baselines: Dict[str, float] = {}
        self.last_values: Dict[str, float] = {}

        # 采集次数：UI 内存计数，卡片创建时置 0，每次收到 data_updated 加 1，
        # 不查库（当前没有"重启后恢复运行中任务"的路径）
        self.count = 0

        # 暂停/结束状态；卡片创建时任务必为运行态
        self.is_paused = False
        self.is_finished = False

        # 打点标签的递增序号（默认标签"标记 N"）
        self.marker_seq = 1

        self._init_ui()

    # ========== 构建界面 ==========

    def _init_ui(self):
        """初始化UI"""
        # 卡片随指标数量自适应增高，水平方向由页面宽度决定。不要显式设置最小高度：
        # 显式值会覆盖布局算出的最小尺寸，卡片多时会被滚动区压扁、文字重叠
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # 顶部：进程身份、状态和任务操作保持在同一视觉层级
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)

        identity_layout = QVBoxLayout()
        identity_layout.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title_label = StrongBodyLabel(self.process_name)
        self.title_label.setToolTip(self.process_name)
        # 长进程名允许被布局压缩，不与右侧操作按钮共同撑宽卡片
        self.title_label.setMinimumWidth(1)
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title_row.addWidget(self.title_label, 1)

        self.running_label = CaptionLabel("运行中")
        title_row.addWidget(self.running_label)
        self.paused_label = CaptionLabel("已暂停")
        self.paused_label.setTextColor(PAUSED_LABEL_LIGHT_COLOR, PAUSED_LABEL_DARK_COLOR)
        self.paused_label.setVisible(False)
        title_row.addWidget(self.paused_label)
        self.finished_label = CaptionLabel("已结束")
        self.finished_label.setVisible(False)
        title_row.addWidget(self.finished_label)
        identity_layout.addLayout(title_row)

        identity_caption = DataCaptionLabel(
            f"PID {self.pid} · {len(self.metric_types)} 项指标")
        identity_caption.setToolTip(
            f"PID {self.pid} · 监控 {len(self.metric_types)} 项指标")
        identity_caption.setMinimumWidth(1)
        identity_caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        identity_layout.addWidget(identity_caption)

        # 测试备注（可选）：只在有备注时显示
        self.note_label = CaptionLabel(
            f"备注：{truncate_text(self.note, NOTE_DISPLAY_CHARS)}")
        self.note_label.setToolTip(self.note)
        self.note_label.setMinimumWidth(1)
        self.note_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.note_label.setVisible(bool(self.note))
        identity_layout.addWidget(self.note_label)

        # 结束原因与时间：任务结束后才显示
        self.finished_detail_label = CaptionLabel("")
        self.finished_detail_label.setMinimumWidth(1)
        self.finished_detail_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.finished_detail_label.setVisible(False)
        identity_layout.addWidget(self.finished_detail_label)
        header_layout.addLayout(identity_layout, 1)

        # 运行中的操作：打点 / 暂停 | 停止。停止是不可逆操作，与暂停拉开间距
        self.running_actions = QWidget(self)
        running_layout = QHBoxLayout(self.running_actions)
        running_layout.setContentsMargins(0, 0, 0, 0)
        running_layout.setSpacing(8)

        self.marker_button = PushButton("打点", self, FluentIcon.TAG)
        self.marker_button.setToolTip("在当前时刻记录一个事件标记（如：开始第 3 轮建模）")
        self.marker_button.clicked.connect(
            lambda: self.marker_clicked.emit(self.task_id))
        self.marker_button.setFixedWidth(88)
        running_layout.addWidget(self.marker_button)

        self.pause_button = PushButton("暂停", self, FluentIcon.PAUSE)
        self.pause_button.clicked.connect(self._on_pause_button_clicked)
        self.pause_button.setFixedWidth(88)
        running_layout.addWidget(self.pause_button)

        running_layout.addSpacing(20)

        self.stop_button = PushButton("停止", self, FluentIcon.CANCEL)
        self.stop_button.setToolTip("停止后本任务不可续采")
        self.stop_button.clicked.connect(lambda: self.stop_clicked.emit(self.task_id))
        self.stop_button.setFixedWidth(88)
        running_layout.addWidget(self.stop_button)
        header_layout.addWidget(self.running_actions, 0, Qt.AlignRight | Qt.AlignTop)

        # 已结束后的操作：查看历史 / 重新监控 / 关闭
        self.finished_actions = QWidget(self)
        finished_layout = QHBoxLayout(self.finished_actions)
        finished_layout.setContentsMargins(0, 0, 0, 0)
        finished_layout.setSpacing(8)

        self.history_button = PushButton("查看历史", self, FluentIcon.HISTORY)
        self.history_button.clicked.connect(
            lambda: self.history_clicked.emit(self.task_id))
        finished_layout.addWidget(self.history_button)

        self.restart_button = PushButton("重新监控", self, FluentIcon.PLAY)
        self.restart_button.setToolTip("按进程名查找进程，并以相同的指标、周期和备注新建任务")
        self.restart_button.clicked.connect(
            lambda: self.restart_clicked.emit(self.task_id))
        finished_layout.addWidget(self.restart_button)

        self.close_button = PushButton("关闭", self, FluentIcon.CLOSE)
        self.close_button.setToolTip("从列表移除此卡片（历史数据仍保留）")
        self.close_button.clicked.connect(
            lambda: self.close_clicked.emit(self.task_id))
        finished_layout.addWidget(self.close_button)
        self.finished_actions.setVisible(False)
        header_layout.addWidget(self.finished_actions, 0, Qt.AlignRight | Qt.AlignTop)
        layout.addLayout(header_layout)

        # 中部：指标名、值与较开始变化量分行，用等宽列增强快速比较能力
        values_layout = QGridLayout()
        values_layout.setHorizontalSpacing(20)
        values_layout.setVerticalSpacing(8)
        for i, metric_type in enumerate(self.metric_types):
            metric_widget = QWidget(self)
            metric_layout = QVBoxLayout(metric_widget)
            metric_layout.setContentsMargins(0, 0, 0, 0)
            metric_layout.setSpacing(2)

            display_name = get_metric_display_name(metric_type)
            metric_name = CaptionLabel(display_name)
            metric_name.setToolTip(display_name)
            metric_name.setMinimumWidth(1)
            metric_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            value_label = DataLabel("--")
            value_label.setToolTip(display_name)
            value_label.setMinimumWidth(1)
            value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            delta_label = DataCaptionLabel("较开始 --")
            delta_label.setToolTip("相对本任务首个有效值的变化量")
            delta_label.setMinimumWidth(1)
            delta_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            metric_layout.addWidget(metric_name)
            metric_layout.addWidget(value_label)
            metric_layout.addWidget(delta_label)

            self.value_labels[metric_type] = value_label
            self.delta_labels[metric_type] = delta_label
            values_layout.addWidget(
                metric_widget, i // self.VALUE_COLUMNS, i % self.VALUE_COLUMNS)
        for column in range(self.VALUE_COLUMNS):
            values_layout.setColumnStretch(column, 1)
        layout.addLayout(values_layout)

        # 底部：在图表上方明示标注趋势对应的首指标及其较开始变化
        trend_header = QHBoxLayout()
        trend_header.setSpacing(8)
        trend_metric_name = (
            get_metric_display_name(self.metric_types[0])
            if self.metric_types else "指标"
        )
        self.sparkline_label = CaptionLabel(f"{trend_metric_name} · 任务开始至今趋势")
        self.sparkline_label.setToolTip(
            f"{trend_metric_name}：从任务开始到现在的走势（点多时自动合并抽稀）")
        self.sparkline_label.setMinimumWidth(1)
        self.sparkline_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        trend_header.addWidget(self.sparkline_label, 1)

        self.trend_delta_label = DataCaptionLabel("较开始 --")
        trend_header.addWidget(self.trend_delta_label)

        # 采集次数（内存计数，非落库条数）作为趋势的辅助上下文
        self.count_label = DataCaptionLabel("已记录：0 次采集")
        self.count_label.setToolTip("采集次数（非落库条数）")
        trend_header.addWidget(self.count_label)
        layout.addLayout(trend_header)

        self.sparkline = SparklineWidget(
            metric_type=self.metric_types[0] if self.metric_types else None)
        if self.metric_types:
            self.sparkline.setToolTip(
                f"任务开始至今的趋势（{get_metric_display_name(self.metric_types[0])}）")
        layout.addWidget(self.sparkline)

    # ========== 数据更新 ==========

    @staticmethod
    def _delta_text(metric_type: str, value: float, baseline: float) -> str:
        """"较开始 +x"文案；基线为本任务首个有效值。"""
        return f"较开始 {format_metric_delta(metric_type, value - baseline)}"

    def update_values(self, values: dict):
        """
        批量更新指标显示值与"较开始"变化量，并在包含首指标时向趋势图追加一点

        Args:
            values: 指标值字典 {指标类型: 指标值}
        """
        for metric_type, value in values.items():
            value_label = self.value_labels.get(metric_type)
            if value_label is None:
                continue
            baseline = self.baselines.setdefault(metric_type, value)
            self.last_values[metric_type] = value

            formatted_value = format_metric_value(metric_type, value)
            value_label.setText(formatted_value)
            value_label.setToolTip(
                f"{get_metric_display_name(metric_type)}：{formatted_value}")

            delta_text = self._delta_text(metric_type, value, baseline)
            self.delta_labels[metric_type].setText(delta_text)
            if metric_type == self.metric_types[0]:
                self.trend_delta_label.setText(delta_text)

        # 趋势图只跟踪首指标。暂停期间 core 不采集、不 emit data_updated，本方法
        # 根本不会被调用，趋势图自然随之静止，无需额外判断暂停状态
        if self.metric_types:
            first_metric = self.metric_types[0]
            if first_metric in values:
                self.sparkline.append(values[first_metric])

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
            value_label.setText("不可用")
            value_label.setToolTip(
                f"{get_metric_display_name(metric_type)}："
                f"{unavailable_reason_text(reason)}，"
                "本指标不记录数据（不会记为 0）")
            self.delta_labels[metric_type].setText("较开始 --")

    def increment_count(self):
        """采集次数+1并刷新文案（内存计数，每次收到 data_updated 调用一次）"""
        self.count += 1
        self.count_label.setText(f"已记录：{self.count} 次采集")

    # ========== 状态切换 ==========

    def _on_pause_button_clicked(self):
        """
        暂停/恢复按钮点击：只负责按当前状态 emit 对应信号，不直接调用 manager。

        按钮图标/文案与"已暂停"提示不在这里乐观更新——留给 MonitorPage 在确认
        manager.pause_task/resume_task 调用成功后回调 set_paused()，避免任务
        已不在运行（如正在停止）导致调用失败时，卡片显示与真实状态不一致。
        """
        if self.is_paused:
            self.resume_clicked.emit(self.task_id)
        else:
            self.pause_clicked.emit(self.task_id)

    def set_paused(self, is_paused: bool) -> None:
        """
        更新暂停/恢复按钮的图标文案与"已暂停"状态提示的可见性。

        暂停期间 core 的主循环不采集，被监控进程退出不会被感知：卡片一直显示
        "已暂停"，恢复后下一次采集才发现进程已消失，以 StopReason.PROCESS_EXITED 结束。

        Args:
            is_paused: 任务是否处于暂停状态
        """
        self.is_paused = is_paused
        if is_paused:
            self.pause_button.setText("恢复")
            self.pause_button.setIcon(FluentIcon.PLAY)
        else:
            self.pause_button.setText("暂停")
            self.pause_button.setIcon(FluentIcon.PAUSE)
        self.running_label.setVisible(not is_paused)
        self.paused_label.setVisible(is_paused)

    def set_stopping(self) -> None:
        """已发出停止请求、等待任务收尾（task_stopped）期间：禁用全部操作按钮，
        避免用户在收尾窗口内重复点击。"""
        for button in (self.marker_button, self.pause_button, self.stop_button):
            button.setEnabled(False)
        self.running_label.setText("正在停止…")
        self.running_label.setVisible(True)
        self.paused_label.setVisible(False)

    def set_finished(self, reason: StopReason, end_time: datetime) -> None:
        """
        切换为"已结束"形态：隐藏运行中操作，显示结束原因与时间和后续操作按钮。
        各指标的最后值、"较开始"与趋势图原样保留。

        Args:
            reason: 任务停止原因
            end_time: 结束时间（由调用方传入）
        """
        self.is_finished = True

        self.running_label.setVisible(False)
        self.paused_label.setVisible(False)
        self.finished_label.setVisible(True)
        if reason is not StopReason.USER:
            self.finished_label.setTextColor(
                PAUSED_LABEL_LIGHT_COLOR, PAUSED_LABEL_DARK_COLOR)

        self.finished_detail_label.setText(
            f"结束原因：{stop_reason_text(reason)} · "
            f"{end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.finished_detail_label.setVisible(True)

        self.running_actions.setVisible(False)
        self.finished_actions.setVisible(True)

    def next_marker_label(self) -> str:
        """下一个打点的默认标签（"标记 N"，N 随成功打点递增）。"""
        return f"标记 {self.marker_seq}"

    def note_marker_added(self) -> None:
        """打点成功后递增默认标签序号。"""
        self.marker_seq += 1
