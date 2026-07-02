"""
监控指标多选对话框
按类别分组展示指标复选框，支持分类三态全选/取消
"""
from typing import Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QScrollArea
from qfluentwidgets import MessageBoxBase, SubtitleLabel, CheckBox

from utils.metrics import AVAILABLE_METRICS, get_metric_display_name


class MetricSelectorDialog(MessageBoxBase):
    """监控指标多选对话框"""

    # 指标网格每行列数
    GRID_COLUMNS = 3

    def __init__(self, selected_metrics: List[str] = None, parent=None):
        """
        初始化对话框

        Args:
            selected_metrics: 预选中的指标类型列表
            parent: 父窗口
        """
        super().__init__(parent)

        # 分类复选框字典 {分类名: CheckBox}
        self.category_checkboxes: Dict[str, CheckBox] = {}
        # 指标复选框字典 {指标类型: CheckBox}
        self.metric_checkboxes: Dict[str, CheckBox] = {}

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
        for category, metrics in AVAILABLE_METRICS.items():
            # 分类复选框（三态：全选/部分选中/全不选）
            category_checkbox = CheckBox(category)
            category_checkbox.setTristate(True)
            category_checkbox.clicked.connect(
                lambda checked, c=category: self._on_category_clicked(c))
            self.category_checkboxes[category] = category_checkbox
            container_layout.addWidget(category_checkbox)

            # 指标复选框网格（每行3列）
            grid_layout = QGridLayout()
            grid_layout.setContentsMargins(28, 0, 0, 0)
            grid_layout.setHorizontalSpacing(15)
            grid_layout.setVerticalSpacing(8)
            for i, metric in enumerate(metrics):
                metric_checkbox = CheckBox(get_metric_display_name(metric))
                metric_checkbox.setChecked(metric in selected)
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
        for category in AVAILABLE_METRICS:
            self._update_category_state(category)
        self._update_yes_button()

    def _on_category_clicked(self, category: str):
        """
        分类复选框点击事件：全选/取消该分类下所有指标

        Args:
            category: 分类名称
        """
        category_checkbox = self.category_checkboxes[category]
        # 点击后处于部分选中态时视为全选
        checked = category_checkbox.checkState() != Qt.Unchecked
        category_checkbox.blockSignals(True)
        category_checkbox.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        category_checkbox.blockSignals(False)

        # 批量设置子项（阻断信号防止回环）
        for metric in AVAILABLE_METRICS[category]:
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
        根据子项勾选情况更新分类复选框的三态

        Args:
            category: 分类名称
        """
        metrics = AVAILABLE_METRICS[category]
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
        """更新确认按钮可用性：0个选中时禁用"""
        has_selection = any(
            checkbox.isChecked() for checkbox in self.metric_checkboxes.values())
        self.yesButton.setEnabled(has_selection)

    def get_selected(self) -> List[str]:
        """
        获取选中的指标类型列表

        Returns:
            List[str]: 选中的指标类型，按AVAILABLE_METRICS定义顺序排列
        """
        return [
            metric
            for metrics in AVAILABLE_METRICS.values()
            for metric in metrics
            if self.metric_checkboxes[metric].isChecked()
        ]
