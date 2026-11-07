"""
分析页工具条（卡片）

第 0 行：任务选择槽位 · [对比任务] · [导出报告 ▾ 报告/证据包/CSV/图片/复制图片] · [刷新] · [更多 ▾]
第 1 行：范围分段（自基线 / 10分钟 / 1小时 / 6小时 / 24小时 / 全部 / 自定义）
        · 视图 [小倍数 | 叠加Δ%] · [绝对 | 相对时间]；窄宽度时视图切换换到第 2 行。

"自定义"由概览条刷选产生：没有刷选时禁用，刷选后自动选中并在 tooltip 写明起止。
所有 set_* 方法都不发信号；信号只表示用户操作。
"""
from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import (
    Action, BodyLabel, CardWidget, DropDownPushButton, FluentIcon, PushButton, RoundMenu,
    SegmentedWidget, TogglePushButton, TransparentDropDownPushButton)

from core.history_query import BASELINE_RANGE_KEY, CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY
from ui.viewmodels.analysis_vm import (
    TIME_ABSOLUTE, TIME_RELATIVE, VIEW_OVERLAY, VIEW_SMALL)
from ui.widgets.history.common import CARD_PADDING_H, content_width_of

RANGE_OPTIONS = (
    (BASELINE_RANGE_KEY, '自基线'), ('10m', '10分钟'), ('1h', '1小时'), ('6h', '6小时'),
    ('24h', '24小时'), ('all', '全部'), (CUSTOM_RANGE_KEY, '自定义'),
)
VIEW_OPTIONS = ((VIEW_SMALL, '小倍数'), (VIEW_OVERLAY, '叠加Δ%'))
TIME_OPTIONS = ((TIME_ABSOLUTE, '绝对'), (TIME_RELATIVE, '相对时间'))

BASELINE_RANGE_TIP = "只看基线打点之后的数据"
BASELINE_RANGE_DISABLED_TIP = "该任务还没有基线打点"
CUSTOM_RANGE_DISABLED_TIP = "在趋势图下方的概览条上拖动选择范围"

EXPORT_REPORT = 'report'
EXPORT_CSV = 'csv'
EXPORT_IMAGE = 'image'
EXPORT_COPY = 'copy'
EXPORT_EVIDENCE = 'evidence'
EXPORT_ITEMS = (
    (EXPORT_REPORT, FluentIcon.DOCUMENT, "报告（图片 + CSV + 摘要）"),
    (EXPORT_EVIDENCE, FluentIcon.ZIP_FOLDER, "证据包（.zip）"),
    (EXPORT_CSV, FluentIcon.SAVE, "CSV 明细"),
    (EXPORT_IMAGE, FluentIcon.PHOTO, "报告图片"),
    (EXPORT_COPY, FluentIcon.COPY, "复制报告图片"),
)

RAW_VALUES_SHOW_TEXT = "显示原始值"
RAW_VALUES_HIDE_TEXT = "隐藏原始值"
DELETE_TEXT = "删除此任务数据"

# 卡片内边距上下 14、左右 16；控件间距 8；窄于此内容宽度时视图切换换行
CARD_PADDING_V = 14
SPACING = 8
COMPACT_BELOW_WIDTH = 860
TASK_SLOT_MIN_WIDTH = 240


class AnalysisToolbar(CardWidget):
    """分析页工具条。"""

    range_changed = pyqtSignal(str)
    view_mode_changed = pyqtSignal(str)
    time_mode_changed = pyqtSignal(str)
    refresh_clicked = pyqtSignal()
    compare_toggled = pyqtSignal(bool)
    export_requested = pyqtSignal(str)
    raw_values_toggled = pyqtSignal(bool)
    delete_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(CARD_PADDING_H, CARD_PADDING_V, CARD_PADDING_H, CARD_PADDING_V)
        self.grid.setHorizontalSpacing(SPACING)
        self.grid.setVerticalSpacing(10)

        self.task_slot = QWidget(self)
        self.task_slot.setMinimumWidth(TASK_SLOT_MIN_WIDTH)
        self.task_slot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._task_slot_layout = QHBoxLayout(self.task_slot)
        self._task_slot_layout.setContentsMargins(0, 0, 0, 0)

        self.compare_button = TogglePushButton(FluentIcon.SYNC, "对比任务", self)
        self.compare_button.setToolTip("选第二个任务，叠加曲线并排比较分轮增量")
        self.compare_button.toggled.connect(self.compare_toggled)

        self.export_button = DropDownPushButton(FluentIcon.SHARE, "导出报告", self)
        self.export_menu = RoundMenu(parent=self)
        self.export_actions = {}
        for key, icon, text in EXPORT_ITEMS:
            action = Action(icon, text, self)
            action.triggered.connect(lambda _=False, k=key: self.export_requested.emit(k))
            self.export_menu.addAction(action)
            self.export_actions[key] = action
        self.export_button.setMenu(self.export_menu)

        self.refresh_button = PushButton(FluentIcon.SYNC, "刷新", self)
        self.refresh_button.clicked.connect(self.refresh_clicked)

        self.more_button = TransparentDropDownPushButton(FluentIcon.MORE, "更多", self)
        self.more_menu = RoundMenu(parent=self)
        self.raw_value_action = Action(FluentIcon.DOCUMENT, RAW_VALUES_SHOW_TEXT, self)
        self.raw_value_action.setCheckable(True)
        self.raw_value_action.toggled.connect(self._on_raw_values_toggled)
        self.more_menu.addAction(self.raw_value_action)
        self.more_menu.addSeparator()
        self.delete_action = Action(FluentIcon.DELETE, DELETE_TEXT, self)
        self.delete_action.setEnabled(False)
        self.delete_action.triggered.connect(self._on_delete_triggered)
        self.more_menu.addAction(self.delete_action)
        self.more_button.setMenu(self.more_menu)

        top = QHBoxLayout()
        top.setSpacing(SPACING)
        top.addWidget(BodyLabel("任务", self))
        top.addWidget(self.task_slot, 1)
        for widget in (self.compare_button, self.export_button, self.refresh_button,
                       self.more_button):
            top.addWidget(widget)
        self.grid.addLayout(top, 0, 0, 1, 3)

        self.range_label = BodyLabel("范围", self)
        self.range_segmented = self._segmented(RANGE_OPTIONS, DEFAULT_RANGE_KEY, self.range_changed)
        self.view_segmented = self._segmented(VIEW_OPTIONS, VIEW_SMALL, self.view_mode_changed)
        self.time_segmented = self._segmented(TIME_OPTIONS, TIME_ABSOLUTE, self.time_mode_changed)
        self.views = QWidget(self)
        views_layout = QHBoxLayout(self.views)
        views_layout.setContentsMargins(0, 0, 0, 0)
        views_layout.setSpacing(SPACING)
        views_layout.addWidget(BodyLabel("视图", self.views))
        views_layout.addWidget(self.view_segmented)
        views_layout.addWidget(self.time_segmented)
        self.grid.addWidget(self.range_label, 1, 0)
        self.grid.addWidget(self.range_segmented, 1, 1)
        self.grid.setColumnStretch(1, 1)
        self.set_baseline_available(False)
        self.set_custom_range_text(None)
        self._compact: Optional[bool] = None
        self.update_layout()

    def _segmented(self, options, current: str, signal) -> SegmentedWidget:
        widget = SegmentedWidget(self)
        for key, text in options:
            widget.addItem(routeKey=key, text=text)
        widget.setCurrentItem(current)
        widget.currentItemChanged.connect(signal)
        return widget

    @staticmethod
    def _set_silently(segmented: SegmentedWidget, key: str) -> None:
        segmented.blockSignals(True)
        try:
            segmented.setCurrentItem(key)
        finally:
            segmented.blockSignals(False)

    # ---------- 任务选择器 ----------

    def set_task_selector(self, widget: QWidget) -> None:
        widget.setSizePolicy(QSizePolicy.Expanding, widget.sizePolicy().verticalPolicy())
        self._task_slot_layout.addWidget(widget)

    # ---------- 范围 / 视图 ----------

    def set_range(self, key: str) -> None:
        self._set_silently(self.range_segmented, key)

    def current_range(self) -> str:
        return self.range_segmented.currentRouteKey()

    def set_baseline_available(self, available: bool) -> None:
        item = self.range_segmented.items[BASELINE_RANGE_KEY]
        item.setEnabled(available)
        item.setToolTip(BASELINE_RANGE_TIP if available else BASELINE_RANGE_DISABLED_TIP)

    def set_custom_range_text(self, text: Optional[str]) -> None:
        """有刷选范围时启用"自定义"并写明起止；None 禁用。"""
        item = self.range_segmented.items[CUSTOM_RANGE_KEY]
        item.setEnabled(text is not None)
        item.setToolTip(text if text is not None else CUSTOM_RANGE_DISABLED_TIP)

    def custom_available(self) -> bool:
        return self.range_segmented.items[CUSTOM_RANGE_KEY].isEnabled()

    def set_view_mode(self, key: str) -> None:
        self._set_silently(self.view_segmented, key)

    def set_time_mode(self, key: str) -> None:
        self._set_silently(self.time_segmented, key)

    # ---------- 命令 ----------

    def set_compare_checked(self, checked: bool) -> None:
        self.compare_button.blockSignals(True)
        try:
            self.compare_button.setChecked(checked)
        finally:
            self.compare_button.blockSignals(False)

    def set_export_enabled(self, enabled: bool) -> None:
        self.export_button.setEnabled(enabled)

    def set_delete_enabled(self, enabled: bool, tooltip: str = "") -> None:
        self.delete_action.setEnabled(enabled)
        self.delete_action.setToolTip(tooltip)

    def _on_delete_triggered(self) -> None:
        # QAction.trigger() 在禁用状态下仍会发 triggered：禁用时不得转发删除请求
        if self.delete_action.isEnabled():
            self.delete_clicked.emit()

    def _on_raw_values_toggled(self, checked: bool) -> None:
        self.raw_value_action.setText(RAW_VALUES_HIDE_TEXT if checked else RAW_VALUES_SHOW_TEXT)
        self.raw_values_toggled.emit(checked)

    # ---------- 响应式布局 ----------

    def is_compact(self) -> bool:
        return bool(self._compact)

    def update_layout(self, content_width: Optional[int] = None) -> bool:
        """内容宽度不足时把视图切换放到第 2 行；档位未变不重排。"""
        if content_width is None:
            content_width = content_width_of(self)
        compact = content_width < COMPACT_BELOW_WIDTH
        if compact == self._compact:
            return False
        self._compact = compact
        self.grid.removeWidget(self.views)
        if compact:
            self.grid.addWidget(self.views, 2, 1)
        else:
            self.grid.addWidget(self.views, 1, 2)
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_layout()
