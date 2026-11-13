"""
监控指标多选对话框
按类别分组展示指标复选框，支持分类三态全选/取消

进程组（进程树 / 同名进程）任务用 group_mode：对组不适用的指标（峰值类、基本优先级）置灰
并说明原因，多出"进程组"分类（进程总数，始终选中、不可取消）。
"""
from typing import Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QScrollArea)
from qfluentwidgets import (
    MessageBoxBase, SubtitleLabel, CheckBox, PushButton, CaptionLabel, FluentIcon, ComboBox, LineEdit
)

from utils.metrics import (
    AVAILABLE_METRICS, GROUP_REGISTRY, LEAK_CHECK_PRESET, PROCESS_COUNT,
    get_metric_description, get_metric_display_name, group_selectable_metrics
)

# 进程组模式的说明文案
GROUP_HINT = ("进程树 / 同名进程：峰值类与基本优先级对进程组不适用（已置灰）；"
              "各指标按成员求和或累计，并另记每种成员各自的曲线")
GROUP_UNSUPPORTED_TIP = ("对进程树 / 同名进程不适用：峰值与优先级是单个进程的瞬时属性，"
                         "把多个进程的值相加或取最大没有意义")
PROCESS_COUNT_TIP = "进程总数是进程组任务的基本指标，始终记录"


class MetricSelectorDialog(MessageBoxBase):
    """监控指标多选对话框"""

    # 指标网格每行列数
    GRID_COLUMNS = 3

    def __init__(self, selected_metrics: List[str] = None, parent=None, *,
                 group_mode: bool = False):
        """
        初始化对话框

        Args:
            selected_metrics: 预选中的指标类型列表
            parent: 父窗口
            group_mode: 进程组任务的选择：对组不适用的指标置灰并说明原因，另有"进程组"
                分类（进程总数，始终选中）
        """
        super().__init__(parent)
        self.group_mode = group_mode

        # 分类复选框字典 {分类名: CheckBox}
        self.category_checkboxes: Dict[str, CheckBox] = {}
        # 指标复选框字典 {指标类型: CheckBox}
        self.metric_checkboxes: Dict[str, CheckBox] = {}
        # 每个分类里"可勾选"的指标（组模式下置灰的不算）：分类三态与整类勾选只看它们
        selectable = set(group_selectable_metrics()) if group_mode else None
        self._categories: Dict[str, List[str]] = {
            category: [m for m in metrics if selectable is None or m in selectable]
            for category, metrics in AVAILABLE_METRICS.items()}
        # 要显示的全部分类（组模式多出"进程组"）
        self._shown: Dict[str, List[str]] = dict(AVAILABLE_METRICS)
        if group_mode:
            for spec in GROUP_REGISTRY.values():
                self._shown[spec.category] = [spec.key]
                self._categories[spec.category] = [spec.key]

        self._init_ui(set(selected_metrics or []))

    def _init_ui(self, selected: set):
        """
        初始化UI

        Args:
            selected: 预选中的指标类型集合
        """
        # 标题
        title_label = SubtitleLabel("选择监控指标", self)
        self.viewLayout.addWidget(title_label)

        # 一键勾选"泄漏排查"预设；悬停每个指标可看含义与适用场景
        preset_row = QHBoxLayout()
        preset_row.setSpacing(12)
        self.preset_button = PushButton("应用预设：泄漏排查", self, FluentIcon.FLAG)
        self.preset_button.setToolTip(
            "私有字节、句柄数、GDI对象数、USER对象数、线程数：判断内存/句柄/界面资源泄漏的最小指标集")
        self.preset_button.clicked.connect(self.select_leak_check_preset)
        preset_row.addWidget(self.preset_button)
        preset_row.addWidget(CaptionLabel("鼠标悬停在指标上可查看含义"))
        preset_row.addStretch()
        self.viewLayout.addLayout(preset_row)
        from ui.metric_presets import metric_presets
        self.preset_combo = ComboBox(self)
        self.preset_combo.addItems(list(metric_presets()))
        self.preset_combo.currentTextChanged.connect(
            lambda name: self.select_metrics(metric_presets().get(name, [])))
        self.preset_name = LineEdit(self)
        self.preset_name.setPlaceholderText('自定义预设名称')
        self.preset_name.setMaximumWidth(180)
        self.save_preset_button = PushButton('存为预设', self)
        self.save_preset_button.clicked.connect(self._save_preset)
        saved_row = QHBoxLayout()
        for widget in (self.preset_combo, self.preset_name, self.save_preset_button):
            saved_row.addWidget(widget)
        self.viewLayout.addLayout(saved_row)
        self.group_hint_label = None
        if self.group_mode:
            self.group_hint_label = CaptionLabel(GROUP_HINT, self)
            self.group_hint_label.setWordWrap(True)
            self.viewLayout.addWidget(self.group_hint_label)

        # 滚动区域
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFixedHeight(420)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet("QScrollArea{background: transparent; border: none}")
        scroll_area.viewport().setStyleSheet("background: transparent")

        # 滚动内容容器
        container = QWidget()
        container.setStyleSheet("background: transparent")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 10, 0)
        container_layout.setSpacing(12)

        # 按分类构建复选框
        for category, metrics in self._shown.items():
            usable = self._categories[category]
            # 分类复选框（三态：全选/部分选中/全不选）
            category_checkbox = CheckBox(category)
            category_checkbox.setTristate(True)
            category_checkbox.clicked.connect(
                lambda checked, c=category: self._on_category_clicked(c))
            category_checkbox.setEnabled(bool(usable) and usable != [PROCESS_COUNT])
            self.category_checkboxes[category] = category_checkbox
            container_layout.addWidget(category_checkbox)

            # 指标复选框网格（每行3列）
            grid_layout = QGridLayout()
            grid_layout.setContentsMargins(28, 0, 0, 0)
            grid_layout.setHorizontalSpacing(15)
            grid_layout.setVerticalSpacing(8)
            for i, metric in enumerate(metrics):
                metric_checkbox = CheckBox(get_metric_display_name(metric))
                description = get_metric_description(metric)
                if metric == PROCESS_COUNT:
                    # 进程总数始终记录（创建任务时缺了也会补上），不让用户取消
                    metric_checkbox.setChecked(True)
                    metric_checkbox.setEnabled(False)
                    metric_checkbox.setToolTip(f"{description}\n{PROCESS_COUNT_TIP}")
                elif metric in usable:
                    metric_checkbox.setChecked(metric in selected)
                    metric_checkbox.setToolTip(description)
                else:
                    metric_checkbox.setEnabled(False)
                    metric_checkbox.setToolTip(f"{description}\n{GROUP_UNSUPPORTED_TIP}")
                metric_checkbox.stateChanged.connect(
                    lambda state, c=category: self._on_metric_changed(c))
                self.metric_checkboxes[metric] = metric_checkbox
                grid_layout.addWidget(
                    metric_checkbox, i // self.GRID_COLUMNS, i % self.GRID_COLUMNS)
            container_layout.addLayout(grid_layout)

        container_layout.addStretch()
        scroll_area.setWidget(container)
        self.viewLayout.addWidget(scroll_area)

        # 按钮文案
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")

        # 对话框最小宽度
        self.widget.setMinimumWidth(600)

        # 初始化分类三态和确认按钮可用性
        for category in self._shown:
            self._update_category_state(category)
        self._update_yes_button()

    def select_metrics(self, metrics: List[str]) -> None:
        """把勾选状态整体设为给定指标集合，并联动分类三态与确认按钮
        （组模式下置灰的指标保持不选，进程总数保持选中）。"""
        wanted = set(metrics)
        for usable in self._categories.values():
            for metric in usable:
                checkbox = self.metric_checkboxes[metric]
                checkbox.blockSignals(True)
                checkbox.setChecked(metric in wanted or metric == PROCESS_COUNT)
                checkbox.blockSignals(False)
        for category in self._shown:
            self._update_category_state(category)
        self._update_yes_button()

    def select_leak_check_preset(self) -> None:
        """一键勾选"泄漏排查"预设（覆盖当前勾选）。"""
        self.select_metrics(LEAK_CHECK_PRESET)

    def _save_preset(self):
        from ui.metric_presets import metric_presets, save_metric_preset
        try:
            save_metric_preset(self.preset_name.text(), self.get_selected())
        except ValueError as exc:
            self.preset_name.setToolTip(str(exc))
            self.preset_name.setFocus()
            return
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(list(metric_presets()))
        self.preset_combo.setCurrentText(f'自定义：{self.preset_name.text().strip()}')
        self.preset_combo.blockSignals(False)

    def _on_category_clicked(self, category: str):
        """
        分类复选框点击事件：全选/取消该分类下所有可选指标

        Args:
            category: 分类名称
        """
        category_checkbox = self.category_checkboxes[category]
        # 点击后处于部分选中态时视为全选
        checked = category_checkbox.checkState() != Qt.Unchecked
        category_checkbox.blockSignals(True)
        category_checkbox.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        category_checkbox.blockSignals(False)

        # 批量设置子项（阻断信号防止回环）；置灰的指标不参与
        for metric in self._categories[category]:
            metric_checkbox = self.metric_checkboxes[metric]
            metric_checkbox.blockSignals(True)
            metric_checkbox.setChecked(checked)
            metric_checkbox.blockSignals(False)

        self._update_yes_button()

    def _on_metric_changed(self, category: str):
        """
        指标复选框状态变化事件：联动分类三态和确认按钮

        Args:
            category: 该指标所属分类名称
        """
        self._update_category_state(category)
        self._update_yes_button()

    def _update_category_state(self, category: str):
        """
        根据子项勾选情况更新分类复选框的三态（只看可选的子项）

        Args:
            category: 分类名称
        """
        metrics = self._categories[category]
        checked_count = sum(
            1 for metric in metrics if self.metric_checkboxes[metric].isChecked())

        category_checkbox = self.category_checkboxes[category]
        category_checkbox.blockSignals(True)
        if checked_count == 0:
            category_checkbox.setCheckState(Qt.Unchecked)
        elif checked_count == len(metrics):
            category_checkbox.setCheckState(Qt.Checked)
        else:
            category_checkbox.setCheckState(Qt.PartiallyChecked)
        category_checkbox.blockSignals(False)

    def _update_yes_button(self):
        """更新确认按钮可用性：0个选中时禁用（组模式下始终选中的进程总数不算）"""
        has_selection = any(
            checkbox.isChecked() for metric, checkbox in self.metric_checkboxes.items()
            if metric != PROCESS_COUNT)
        self.yesButton.setEnabled(has_selection)

    def get_selected(self) -> List[str]:
        """
        获取选中的指标类型列表

        Returns:
            List[str]: 选中的指标类型，按AVAILABLE_METRICS定义顺序排列；组模式下末尾
                带进程总数
        """
        selected = [
            metric
            for metrics in AVAILABLE_METRICS.values()
            for metric in metrics
            if self.metric_checkboxes[metric].isChecked()
        ]
        if self.group_mode:
            selected.append(PROCESS_COUNT)
        return selected
