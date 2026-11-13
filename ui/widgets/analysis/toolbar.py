"""
分析页工具条（卡片）

第 0 行：任务选择槽位 · [成员（进程组任务才显示；窄宽度时换到第 3 行）] · [对比任务] ·
        [导出报告 ▾ 报告/证据包/CSV/图片/复制图片] · [刷新] · [更多 ▾]
第 1 行：范围分段（自基线 / 10分钟 / 1小时 / 6小时 / 24小时 / 全部 / 自定义）
        · 视图 [小倍数 | 叠加Δ%] · [绝对 | 相对时间]；窄宽度时视图切换换到第 2 行。
compact 档位（ui.layout_breakpoints）下范围改用下拉（七段并排放不下，会撑出横向溢出），
下拉与分段控件同步同一份状态，视图切换回到范围同一行；任务槽位的最小宽度也收窄
（下拉里的任务名本来就会省略）。

"自定义"由概览条刷选产生：没有刷选时禁用，刷选后自动选中并在 tooltip 写明起止。
所有 set_* 方法都不发信号；信号只表示用户操作。
"""
from typing import Optional

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QGridLayout, QHBoxLayout, QScrollArea, QSizePolicy, QWidget
from qfluentwidgets import (
    Action, BodyLabel, CardWidget, ComboBox, DropDownPushButton, FluentIcon, PushButton,
    RoundMenu, SegmentedWidget, TogglePushButton, TransparentDropDownPushButton, TransparentToolButton)

from core.history_query import BASELINE_RANGE_KEY, CUSTOM_RANGE_KEY, DEFAULT_RANGE_KEY
from ui.layout_breakpoints import COMPACT, REGULAR
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

# 卡片内边距上下 14、左右 16；控件间距 8。成员下拉按实际最小宽度换行；
# 视图切换换到第 2 行（范围是七段分段控件时要 860，是下拉时 600 就放得下）
CARD_PADDING_V = 14
SPACING = 8
VIEWS_INLINE_MIN_WIDTH = 860
VIEWS_INLINE_MIN_WIDTH_COMBO = 600
TASK_SLOT_MIN_WIDTH = 240
TASK_SLOT_MIN_WIDTH_COMPACT = 150
RANGE_COMBO_WIDTH = 160


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

        self.compare_button = TogglePushButton(FluentIcon.LAYOUT, "对比任务", self)
        self.compare_button.setToolTip("最多对比四个任务，按轮次或时间对齐并比较各指标")
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

        self.refresh_button = TransparentToolButton(FluentIcon.SYNC, self)
        self.refresh_button.setToolTip("刷新分析（F5）")
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

        # 成员下拉的槽位：只有进程组任务才显示，位置由 update_layout 按宽度安排
        self.member_box = QWidget(self)
        self._member_layout = QHBoxLayout(self.member_box)
        self._member_layout.setContentsMargins(0, 0, 0, 0)
        self._member_layout.setSpacing(SPACING)
        self._member_layout.addWidget(BodyLabel("成员", self.member_box))
        self.member_box.setVisible(False)

        self.top = QHBoxLayout()
        self.top.setSpacing(SPACING)
        self.top.addWidget(BodyLabel("任务", self))
        self.top.addWidget(self.task_slot, 1)
        for widget in (self.compare_button, self.export_button, self.refresh_button,
                       self.more_button):
            self.top.addWidget(widget)
        self.grid.addLayout(self.top, 0, 0, 1, 3)

        self.range_label = BodyLabel("范围", self)
        self.range_segmented = self._segmented(RANGE_OPTIONS, DEFAULT_RANGE_KEY, self.range_changed)
        # compact 档位用的范围下拉：与分段控件各存一份选中状态，由 set_range 等一起写
        self.range_combo = ComboBox(self)
        self.range_combo.setFixedWidth(RANGE_COMBO_WIDTH)
        for key, text in RANGE_OPTIONS:
            self.range_combo.addItem(text, userData=key)
        self.range_combo.setCurrentIndex(self._range_index(DEFAULT_RANGE_KEY))
        self.range_combo.currentIndexChanged.connect(self._on_range_combo_changed)
        self.range_combo.setVisible(False)
        self.range_slot = QWidget(self)
        range_layout = QHBoxLayout(self.range_slot)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(0)
        range_layout.addWidget(self.range_segmented)
        range_layout.addWidget(self.range_combo)
        self.view_segmented = self._segmented(VIEW_OPTIONS, VIEW_SMALL, self.view_mode_changed)
        self.time_segmented = self._segmented(TIME_OPTIONS, TIME_ABSOLUTE, self.time_mode_changed)
        self.views = QWidget(self)
        views_layout = QHBoxLayout(self.views)
        views_layout.setContentsMargins(0, 0, 0, 0)
        views_layout.setSpacing(SPACING)
        self.view_segmented.hide()
        self.time_segmented.hide()
        self.view_menu_button = TransparentDropDownPushButton(FluentIcon.VIEW, '视图', self.views)
        view_menu = RoundMenu(parent=self)
        self._view_actions = {}
        self._time_actions = {}
        for options, current, signal, actions in (
                (VIEW_OPTIONS, VIEW_SMALL, self.view_mode_changed, self._view_actions),
                (TIME_OPTIONS, TIME_ABSOLUTE, self.time_mode_changed, self._time_actions)):
            if actions is self._time_actions:
                view_menu.addSeparator()
            for key, label in options:
                action = Action(label, self)
                action.setCheckable(True)
                action.setChecked(key == current)
                action.triggered.connect(lambda _=False, k=key, sig=signal: sig.emit(k))
                view_menu.addAction(action)
                actions[key] = action
        self.view_menu_button.setMenu(view_menu)
        views_layout.addWidget(self.view_menu_button)
        self.grid.addWidget(self.range_label, 1, 0)
        self.grid.addWidget(self.range_slot, 1, 1)
        self.grid.setColumnStretch(1, 1)
        self.set_baseline_available(False)
        self.set_custom_range_text(None)
        self._tier = REGULAR
        self._views_inline: Optional[bool] = None
        self._member_inline: Optional[bool] = None
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
        self.update_layout()

    def set_member_selector(self, widget: QWidget) -> None:
        self._member_layout.addWidget(widget)
        self.update_layout()

    def set_members_visible(self, visible: bool) -> None:
        """进程组任务才显示成员下拉。"""
        self.member_box.setVisible(visible)
        self.update_layout()

    def members_visible(self) -> bool:
        return not self.member_box.isHidden()

    # ---------- 范围 / 视图 ----------

    @staticmethod
    def _range_index(key: str) -> int:
        return next((i for i, (k, _text) in enumerate(RANGE_OPTIONS) if k == key), 0)

    def set_range(self, key: str) -> None:
        self._set_silently(self.range_segmented, key)
        self.range_combo.blockSignals(True)
        try:
            self.range_combo.setCurrentIndex(self._range_index(key))
        finally:
            self.range_combo.blockSignals(False)
        self._sync_combo_tip()

    def current_range(self) -> str:
        return self.range_segmented.currentRouteKey()

    def _on_range_combo_changed(self, index: int) -> None:
        if not 0 <= index < len(RANGE_OPTIONS):
            return
        key = RANGE_OPTIONS[index][0]
        self._set_silently(self.range_segmented, key)
        self._sync_combo_tip()
        self.range_changed.emit(key)

    def _sync_combo_tip(self) -> None:
        """下拉里选中"自定义"时，悬停写明刷选的起止。"""
        item = self.range_segmented.items[CUSTOM_RANGE_KEY]
        self.range_combo.setToolTip(
            item.toolTip() if self.current_range() == CUSTOM_RANGE_KEY else "")

    def set_baseline_available(self, available: bool) -> None:
        item = self.range_segmented.items[BASELINE_RANGE_KEY]
        item.setEnabled(available)
        item.setToolTip(BASELINE_RANGE_TIP if available else BASELINE_RANGE_DISABLED_TIP)
        self.range_combo.setItemEnabled(self._range_index(BASELINE_RANGE_KEY), available)

    def set_custom_range_text(self, text: Optional[str]) -> None:
        """有刷选范围时启用"自定义"并写明起止；None 禁用。"""
        item = self.range_segmented.items[CUSTOM_RANGE_KEY]
        item.setEnabled(text is not None)
        item.setToolTip(text if text is not None else CUSTOM_RANGE_DISABLED_TIP)
        self.range_combo.setItemEnabled(self._range_index(CUSTOM_RANGE_KEY), text is not None)
        self._sync_combo_tip()

    def custom_available(self) -> bool:
        return self.range_segmented.items[CUSTOM_RANGE_KEY].isEnabled()

    def set_view_mode(self, key: str) -> None:
        self._set_silently(self.view_segmented, key)
        for name, action in self._view_actions.items():
            action.setChecked(name == key)

    def set_time_mode(self, key: str) -> None:
        self._set_silently(self.time_segmented, key)
        for name, action in self._time_actions.items():
            action.setChecked(name == key)

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

    def set_tier(self, tier: str) -> None:
        """切换档位（页面订阅 breakpoints.tier_changed 后调用）：compact 用范围下拉、
        收窄任务槽位。"""
        if tier == self._tier:
            return
        self._tier = tier
        compact = tier == COMPACT
        self.range_segmented.setVisible(not compact)
        self.range_combo.setVisible(compact)
        self.task_slot.setMinimumWidth(
            TASK_SLOT_MIN_WIDTH_COMPACT if compact else TASK_SLOT_MIN_WIDTH)
        self.update_layout()

    def range_as_combo(self) -> bool:
        """范围当前是下拉（compact 档位）而不是分段控件。"""
        return self._tier == COMPACT

    def views_inline(self) -> bool:
        """视图切换是否与范围同在一行（放不下时单占一行）。"""
        return bool(self._views_inline)

    def _available_content_width(self) -> int:
        width = content_width_of(self)
        margins = self.grid.contentsMargins()
        insets = margins.left() + margins.right()
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                # 容器可能已被旧布局撑宽，不能再用撑宽后的尺寸决定保持同行。
                return min(width, max(0, parent.viewport().width() - insets))
            layout = parent.layout()
            if layout is not None:
                margins = layout.contentsMargins()
                insets += margins.left() + margins.right()
            parent = parent.parentWidget()
        return width

    def _member_inline_width(self) -> int:
        items = [self.top.itemAt(i) for i in range(self.top.count())
                 if self.top.itemAt(i).widget() is not self.member_box]
        visible = [item for item in items if not item.isEmpty()]
        margins = self.top.contentsMargins()
        width = sum(item.minimumSize().width() for item in visible)
        member_width = max(self.member_box.minimumWidth(),
                           self._member_layout.minimumSize().width())
        return (width + member_width + self.top.spacing() * len(visible)
                + margins.left() + margins.right())

    def update_layout(self, content_width: Optional[int] = None) -> bool:
        """按卡片内容宽度摆放：放不下时视图切换换到第 2 行、成员下拉换到第 3 行；
        摆法没变不重排，返回是否重排。"""
        if content_width is None:
            content_width = self._available_content_width()
        views_min = (VIEWS_INLINE_MIN_WIDTH_COMBO if self.range_as_combo()
                     else VIEWS_INLINE_MIN_WIDTH)
        views_inline = content_width >= views_min
        member_inline = content_width >= self._member_inline_width()
        if (views_inline, member_inline) == (self._views_inline, self._member_inline):
            return False
        self._views_inline, self._member_inline = views_inline, member_inline
        self.grid.removeWidget(self.views)
        if views_inline:
            self.grid.addWidget(self.views, 1, 2)
        else:
            self.grid.addWidget(self.views, 2, 1)
        self.top.removeWidget(self.member_box)
        self.grid.removeWidget(self.member_box)
        if member_inline:
            self.top.insertWidget(2, self.member_box)
        else:
            self.grid.addWidget(self.member_box, 3, 0, 1, 3)
        return True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_layout()
