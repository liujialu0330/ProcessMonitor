"""
历史页筛选条：任务选择槽位、指标、时间范围、趋势/明细切换、刷新与"更多"菜单
（显示原始值、删除此任务数据）。

布局（四列网格；列 1、3 拉伸）：
- 常规宽度：第 0 行「监控任务 + 任务选择器 · 趋势/明细 · 刷新 · 更多」（命令组在任务行内），
  第 1 行「指标 + 下拉 | 时间范围 + 分段」；
- 窄宽度：时间范围换到第 2 行独占，命令组移到第 1 行指标右侧，避免分段控件被裁切。
按控件自身内容宽度判断档位，只在档位变化时重排。

所有 set_* 方法都不发信号；信号只表示用户操作。
"""
from typing import Optional, Sequence, Tuple

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QWidget
from qfluentwidgets import (
    Action, BodyLabel, CardWidget, ComboBox, FluentIcon, PushButton, RoundMenu,
    SegmentedWidget, TransparentDropDownPushButton)

from ui.widgets.history.common import (
    CARD_PADDING_H, VIEWPORT_TO_CONTENT_OFFSET, content_width_of)

# 时间范围选项（routeKey, 显示文本）；秒数等查询语义由页面/查询层定义
RANGE_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ('10m', '10分钟'),
    ('1h', '1小时'),
    ('6h', '6小时'),
    ('24h', '24小时'),
    ('all', '全部'),
)
# 默认看全部数据：泄漏判断要看从开始至今的整体趋势，而不是最近一小时的局部
DEFAULT_RANGE_KEY = 'all'

VIEW_TREND = 'trend'
VIEW_DETAIL = 'detail'
VIEW_OPTIONS: Tuple[Tuple[str, str], ...] = ((VIEW_TREND, '趋势'), (VIEW_DETAIL, '明细'))

# 原规则按页面视口宽度 < 720 换行；换算成卡片内容宽度
COMPACT_BELOW_WIDTH = 720 - VIEWPORT_TO_CONTENT_OFFSET

# 卡片内边距：上下 14，左右 16；列间距 12、行间距 10；命令组内控件间距 8
CARD_PADDING_V = 14
GRID_SPACING_H = 12
GRID_SPACING_V = 10
COMMAND_SPACING = 8
TASK_SLOT_MIN_WIDTH = 220
METRIC_COMBO_MIN_WIDTH = 140
VIEW_SEGMENTED_WIDTH = 120

RAW_VALUES_SHOW_TEXT = "显示原始值"
RAW_VALUES_HIDE_TEXT = "隐藏原始值"
DELETE_TEXT = "删除此任务数据"


class FilterBar(CardWidget):
    """历史页筛选条（卡片）。"""

    metric_selected = pyqtSignal(str)
    range_changed = pyqtSignal(str)
    view_changed = pyqtSignal(str)
    refresh_clicked = pyqtSignal()
    delete_clicked = pyqtSignal()
    raw_values_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.select_layout = QGridLayout(self)
        self.select_layout.setContentsMargins(
            CARD_PADDING_H, CARD_PADDING_V, CARD_PADDING_H, CARD_PADDING_V)
        self.select_layout.setHorizontalSpacing(GRID_SPACING_H)
        self.select_layout.setVerticalSpacing(GRID_SPACING_V)

        # 任务选择：槽位由页面放入选择器，长进程名与时间独占一行不与其他控件抢宽度
        self.task_label = BodyLabel("监控任务", self)
        self.task_slot = QWidget(self)
        self.task_slot.setMinimumWidth(TASK_SLOT_MIN_WIDTH)
        self.task_slot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._task_slot_layout = QHBoxLayout(self.task_slot)
        self._task_slot_layout.setContentsMargins(0, 0, 0, 0)
        self._task_selector: Optional[QWidget] = None
        # 任务行：选择器槽位 + 常规宽度下跟在右侧的命令组。命令组放在行内而不是网格
        # 第 3 列，避免它与下一行的时间范围分段争同一列的最小宽度
        self.task_row = QWidget(self)
        self._task_row_layout = QHBoxLayout(self.task_row)
        self._task_row_layout.setContentsMargins(0, 0, 0, 0)
        self._task_row_layout.setSpacing(GRID_SPACING_H)
        self._task_row_layout.addWidget(self.task_slot, 1)

        self.metric_label = BodyLabel("指标", self)
        self.metric_combo = ComboBox(self)
        self.metric_combo.setPlaceholderText("选择指标")
        self.metric_combo.setMinimumWidth(METRIC_COMBO_MIN_WIDTH)
        self.metric_combo.setEnabled(False)
        self.metric_combo.currentIndexChanged.connect(self._on_metric_index_changed)

        self.range_label = BodyLabel("时间范围", self)
        self.range_segmented = SegmentedWidget(self)
        for route_key, text in RANGE_OPTIONS:
            self.range_segmented.addItem(routeKey=route_key, text=text)
        self.range_segmented.setCurrentItem(DEFAULT_RANGE_KEY)
        self.range_segmented.currentItemChanged.connect(self.range_changed)

        # 命令组：趋势/明细、刷新、更多（低频且高风险的删除收纳进菜单）
        self.command_bar = QWidget(self)
        command_layout = QHBoxLayout(self.command_bar)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(COMMAND_SPACING)
        command_layout.addStretch()

        self.view_segmented = SegmentedWidget(self.command_bar)
        self.view_segmented.setFixedWidth(VIEW_SEGMENTED_WIDTH)
        for route_key, text in VIEW_OPTIONS:
            self.view_segmented.addItem(routeKey=route_key, text=text)
        self.view_segmented.setCurrentItem(VIEW_TREND)
        self.view_segmented.currentItemChanged.connect(self.view_changed)
        command_layout.addWidget(self.view_segmented)

        self.refresh_button = PushButton("刷新", self.command_bar, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self.refresh_clicked)
        command_layout.addWidget(self.refresh_button)

        self.more_button = TransparentDropDownPushButton(
            "更多", self.command_bar, FluentIcon.MORE)
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
        command_layout.addWidget(self.more_button)

        self.select_layout.addWidget(self.task_label, 0, 0)
        self.select_layout.addWidget(self.task_row, 0, 1, 1, 3)
        self.select_layout.addWidget(self.metric_label, 1, 0)
        self.select_layout.addWidget(self.metric_combo, 1, 1)
        self.select_layout.setColumnStretch(1, 2)
        self.select_layout.setColumnStretch(3, 3)
        self._compact: Optional[bool] = None
        self.update_layout()

    # ---------- 任务选择器槽位 ----------

    def set_task_selector(self, widget: QWidget) -> None:
        """放入任务选择器（替换已有的）；FilterBar 不关心其信号，由页面直连。"""
        if self._task_selector is widget:
            return
        if self._task_selector is not None:
            self._task_slot_layout.removeWidget(self._task_selector)
            self._task_selector.setParent(None)
        self._task_selector = widget
        widget.setSizePolicy(QSizePolicy.Expanding, widget.sizePolicy().verticalPolicy())
        self._task_slot_layout.addWidget(widget)

    def task_selector(self) -> Optional[QWidget]:
        return self._task_selector

    # ---------- 指标 ----------

    def set_metrics(self, metrics: Sequence[Tuple[str, str]],
                    current: Optional[str] = None) -> Optional[str]:
        """
        填充指标下拉（不发 metric_selected）。

        Args:
            metrics: [(指标键, 显示名), ...]
            current: 优先选中的指标；不在列表中时选首项

        Returns:
            实际选中的指标键；列表为空返回 None（下拉禁用）
        """
        self.metric_combo.blockSignals(True)
        try:
            self.metric_combo.clear()
            keys = []
            for key, name in metrics:
                self.metric_combo.addItem(name)
                self.metric_combo.setItemData(self.metric_combo.count() - 1, key)
                keys.append(key)
            if not keys:
                self.metric_combo.setEnabled(False)
                return None
            index = keys.index(current) if current in keys else 0
            self.metric_combo.setCurrentIndex(index)
            self.metric_combo.setEnabled(True)
            return keys[index]
        finally:
            self.metric_combo.blockSignals(False)

    def clear_metrics(self) -> None:
        """清空并禁用指标下拉（不发信号）。"""
        self.set_metrics([])

    def current_metric(self) -> Optional[str]:
        index = self.metric_combo.currentIndex()
        if index < 0:
            return None
        return self.metric_combo.itemData(index) or None

    def _on_metric_index_changed(self, index: int) -> None:
        if index < 0:
            return
        key = self.metric_combo.itemData(index)
        if key:
            self.metric_selected.emit(key)

    # ---------- 时间范围 / 视图 ----------

    def set_range(self, key: str) -> None:
        """选中时间范围（不发 range_changed）。"""
        self.range_segmented.blockSignals(True)
        try:
            self.range_segmented.setCurrentItem(key)
        finally:
            self.range_segmented.blockSignals(False)

    def current_range(self) -> str:
        return self.range_segmented.currentRouteKey()

    def set_view(self, key: str) -> None:
        """选中趋势/明细（不发 view_changed）。"""
        self.view_segmented.blockSignals(True)
        try:
            self.view_segmented.setCurrentItem(key)
        finally:
            self.view_segmented.blockSignals(False)

    def current_view(self) -> str:
        return self.view_segmented.currentRouteKey()

    # ---------- 更多菜单 ----------

    def set_delete_enabled(self, enabled: bool, tooltip: str = "") -> None:
        """删除入口可用性与说明（如运行中任务禁止删除的原因）。"""
        self.delete_action.setEnabled(enabled)
        self.delete_action.setToolTip(tooltip)

    def _on_delete_triggered(self) -> None:
        # QAction.trigger() 在禁用状态下仍会发 triggered：禁用时不得转发删除请求
        if self.delete_action.isEnabled():
            self.delete_clicked.emit()

    def set_raw_values_checked(self, checked: bool) -> None:
        """同步"显示原始值"勾选状态与文案（不发 raw_values_toggled）。"""
        self.raw_value_action.blockSignals(True)
        try:
            self.raw_value_action.setChecked(checked)
        finally:
            self.raw_value_action.blockSignals(False)
        self._sync_raw_values_text(checked)

    def _sync_raw_values_text(self, checked: bool) -> None:
        self.raw_value_action.setText(RAW_VALUES_HIDE_TEXT if checked else RAW_VALUES_SHOW_TEXT)

    def _on_raw_values_toggled(self, checked: bool) -> None:
        self._sync_raw_values_text(checked)
        self.raw_values_toggled.emit(checked)

    # ---------- 响应式布局 ----------

    @staticmethod
    def is_compact_width(content_width: int) -> bool:
        """内容宽度不足以让「指标 + 时间范围」同处一行时换行。"""
        return content_width < COMPACT_BELOW_WIDTH

    def is_compact(self) -> bool:
        return bool(self._compact)

    def update_layout(self, content_width: Optional[int] = None) -> bool:
        """按内容宽度切换档位；档位未变化时不动布局。返回是否发生了重排。"""
        if content_width is None:
            content_width = content_width_of(self)
        compact = self.is_compact_width(content_width)
        if compact == self._compact:
            return False
        self._compact = compact
        layout = self.select_layout
        for widget in (self.range_label, self.range_segmented):
            layout.removeWidget(widget)
        layout.removeWidget(self.command_bar)
        self._task_row_layout.removeWidget(self.command_bar)
        if compact:
            layout.addWidget(self.command_bar, 1, 3)
            layout.addWidget(self.range_label, 2, 0)
            layout.addWidget(self.range_segmented, 2, 1, 1, 3)
        else:
            self._task_row_layout.addWidget(self.command_bar)
            layout.addWidget(self.range_label, 1, 2)
            layout.addWidget(self.range_segmented, 1, 3)
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_layout()
