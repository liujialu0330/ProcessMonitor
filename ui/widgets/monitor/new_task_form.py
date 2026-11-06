"""
新建监控任务表单

进程选择 + 采集周期 + 测试备注 + 指标摘要/选指标 + 开始按钮。表单只收集输入、
发出请求，不调用 manager，也不弹对话框：选指标、校验文案、重复 PID 确认等流程
都在 MonitorPresenter 里。

已有任务时表单可折叠成一行（只剩进程选择与开始按钮），把纵向空间让给任务卡片；
行尾的展开按钮可随时展开其余字段。
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel, CardWidget, FluentIcon, LineEdit, PrimaryPushButton, PushButton,
    SpinBox, StrongBodyLabel, TransparentToolButton,
)

from ui.services.preferences import MonitorPreferences
from ui.services.process_query import ProcessQuery
from ui.widgets.monitor.process_picker import ProcessPicker, ResolveError
from utils.metrics import LEAK_CHECK_PRESET, get_metric_display_name

NOTE_MAX_LENGTH = 100


@dataclass(frozen=True)
class TaskRequest:
    """一次"开始监控"请求：目标进程 + 任务参数。"""
    pid: int
    name: str
    create_time: Optional[float]
    metrics: Tuple[str, ...]
    interval: float
    note: str = ''


def metric_summary(metrics: Sequence[str]) -> Tuple[str, str]:
    """已选指标的摘要文案与完整列表（tooltip）。"""
    names = [get_metric_display_name(m) for m in metrics]
    summary = f"已选 {len(names)} 项" if names else "未选择指标"
    if names and set(metrics) == set(LEAK_CHECK_PRESET):
        summary += "（泄漏排查预设）"
    details = "、".join(names) if names else "请选择至少一项监控指标"
    return summary, details


class NewTaskForm(CardWidget):
    """新建监控任务表单。

    信号：
        start_requested(object): 点击开始（或备注框回车）且目标进程有效，参数为 TaskRequest
        target_invalid(object): 点击开始但无法确定目标进程，参数为 ResolveError
        choose_metrics_requested(): 点击"选择指标"
    """

    start_requested = pyqtSignal(object)
    target_invalid = pyqtSignal(object)
    choose_metrics_requested = pyqtSignal()

    def __init__(self, process_query: ProcessQuery, preferences: MonitorPreferences,
                 parent=None):
        super().__init__(parent)
        self._metrics: List[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.title_label = StrongBodyLabel("新建监控任务", self)
        layout.addWidget(self.title_label)

        # 第一行：进程选择 | 展开/收起 | 开始。折叠后只剩这一行
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.picker = ProcessPicker(process_query, self)
        top_row.addWidget(self.picker, 1)

        self.expand_button = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self)
        self.expand_button.setToolTip("展开周期、指标与备注")
        self.expand_button.clicked.connect(self._toggle_details)
        self.expand_button.hide()
        top_row.addWidget(self.expand_button, 0, Qt.AlignTop)

        self.start_button = PrimaryPushButton("开始监控", self, FluentIcon.PLAY)
        self.start_button.setFixedWidth(116)
        self.start_button.clicked.connect(self.submit)
        top_row.addWidget(self.start_button, 0, Qt.AlignTop)
        layout.addLayout(top_row)

        # 其余字段：周期、指标；备注
        self.details = QWidget(self)
        grid = QGridLayout(self.details)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(12)

        grid.addWidget(CaptionLabel("周期（秒）", self.details), 0, 0)
        self.interval_spinbox = SpinBox(self.details)
        # 取值范围只在应用配置里定义一份，经偏好服务读取
        self.interval_spinbox.setRange(*preferences.interval_range())
        self.interval_spinbox.setValue(preferences.default_interval())
        # 宽度交给 SpinBox 的尺寸提示：紧凑布局压缩它会把数值编辑区压成 0
        grid.addWidget(self.interval_spinbox, 0, 1)

        self.metric_button = PushButton("选择指标", self.details, FluentIcon.CHECKBOX)
        self.metric_button.setFixedWidth(112)
        self.metric_button.clicked.connect(self.choose_metrics_requested)
        grid.addWidget(self.metric_button, 0, 2)
        self.metric_summary_label = CaptionLabel(self.details)
        grid.addWidget(self.metric_summary_label, 0, 3)
        grid.setColumnStretch(3, 1)

        # 测试备注：随任务落库，历史页/导出页用它区分各次测试
        grid.addWidget(CaptionLabel("测试备注", self.details), 1, 0)
        self.note_input = LineEdit(self.details)
        self.note_input.setPlaceholderText("测试备注（可选，如：建模 200 次）；回车开始监控")
        self.note_input.setMaxLength(NOTE_MAX_LENGTH)
        self.note_input.setClearButtonEnabled(True)
        self.note_input.returnPressed.connect(self.submit)
        grid.addWidget(self.note_input, 1, 1, 1, 3)
        layout.addWidget(self.details)

        self.set_metrics_summary([])

    # ========== 指标 ==========

    def set_metrics_summary(self, metrics: Sequence[str]) -> None:
        """设置本表单的已选指标（新建任务将使用它们）并刷新摘要。"""
        self._metrics = list(metrics)
        summary, details = metric_summary(self._metrics)
        self.metric_summary_label.setText(summary)
        self.metric_summary_label.setToolTip(details)

    def metrics(self) -> List[str]:
        return list(self._metrics)

    # ========== 其他字段 ==========

    def interval(self) -> float:
        return float(self.interval_spinbox.value())

    def set_default_interval(self, seconds: int) -> None:
        """设置页的默认采集间隔变了：直接覆盖输入框的值（不影响已创建的任务）。"""
        self.interval_spinbox.setValue(seconds)

    def note(self) -> str:
        return self.note_input.text().strip()

    # ========== 提交 ==========

    def submit(self) -> None:
        """按当前输入发出开始请求；目标进程无效时发出 target_invalid。"""
        target = self.picker.resolve()
        if isinstance(target, ResolveError):
            self.target_invalid.emit(target)
            return
        self.start_requested.emit(TaskRequest(
            pid=target.pid, name=target.name, create_time=target.create_time,
            metrics=tuple(self._metrics), interval=self.interval(), note=self.note()))

    def show_error(self, text: str) -> None:
        self.picker.set_error(text)

    def forget_process_if(self, pid: int) -> bool:
        """被测进程退出后，若进程选择仍指向它则清掉并刷新列表（见 ProcessPicker.forget_if）。"""
        return self.picker.forget_if(pid)

    def fill_search(self, text: str) -> None:
        """把文本填进进程搜索框并聚焦（按名找不到唯一进程时交给用户自己选）。"""
        self.picker.set_text(text)
        self.picker.focus_search()

    # ========== 折叠 ==========

    def set_collapsed(self, collapsed: bool) -> None:
        """有任务时折叠成一行（只显示进程选择与开始），行尾的展开按钮可展开其余字段；
        没有任务时完整展开且不显示展开按钮。"""
        self.expand_button.setVisible(collapsed)
        self._show_details(not collapsed)

    def is_collapsed(self) -> bool:
        return self.details.isHidden()

    def _toggle_details(self) -> None:
        self._show_details(self.details.isHidden())

    def _show_details(self, visible: bool) -> None:
        self.details.setVisible(visible)
        self.title_label.setVisible(visible)
        self.expand_button.setIcon(
            FluentIcon.UP if visible else FluentIcon.CHEVRON_DOWN_MED)
        self.expand_button.setToolTip(
            "收起" if visible else "展开周期、指标与备注")
