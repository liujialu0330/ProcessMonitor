"""
带 SpinBox 的设置卡组件

qfluentwidgets 1.11.2 内置的设置卡家族里，数值型配置项默认由 RangeSettingCard
（滑块 Slider）承载；本项目"默认采集周期"这类小范围整数更适合直接输入数字，
与监控页现有的采集周期 SpinBox 交互保持一致，故照 RangeSettingCard 的双向绑定
写法（configItem.valueChanged 回写控件 / 控件 valueChanged 写回 qconfig）另外
实现一个 SpinBox 版本。
"""
from typing import Union

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from qfluentwidgets import SettingCard, SpinBox, qconfig, RangeConfigItem, FluentIconBase


class SpinBoxSettingCard(SettingCard):
    """右侧带 SpinBox 的设置卡，绑定 RangeConfigItem，双向同步"""

    valueChanged = pyqtSignal(int)

    def __init__(self, configItem: RangeConfigItem, icon: Union[str, QIcon, FluentIconBase],
                 title: str, content: str = None, parent=None, suffix: str = ""):
        """
        Args:
            configItem: 绑定的 RangeConfigItem（SpinBox 取值范围取自 configItem.range）
            icon: 卡片左侧图标
            title: 卡片标题
            content: 卡片说明文字
            parent: 父窗口
            suffix: 数值后缀，用于明确秒等单位
        """
        super().__init__(icon, title, content, parent)
        self.configItem = configItem

        self.spinBox = SpinBox(self)
        self.spinBox.setRange(*configItem.range)
        self.spinBox.setValue(qconfig.get(configItem))
        self.spinBox.setSuffix(suffix)
        self.spinBox.setMinimumWidth(120)

        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)

        configItem.valueChanged.connect(self._on_config_value_changed)
        self.spinBox.valueChanged.connect(self._on_spinbox_value_changed)

    def _on_spinbox_value_changed(self, value: int):
        """SpinBox 值变更：写入配置（双向同步的"控件 -> 配置"方向）"""
        qconfig.set(self.configItem, value)
        self.valueChanged.emit(value)

    def _on_config_value_changed(self, value: int):
        """配置项被外部修改（如设置页之外的代码直接调用 qconfig.set，或多个
        设置卡绑定同一配置项）：回写 SpinBox；blockSignals 阻断"配置 -> SpinBox
        -> 配置"的信号回环"""
        self.spinBox.blockSignals(True)
        self.spinBox.setValue(value)
        self.spinBox.blockSignals(False)

    def setValue(self, value: int):
        """外部编程方式设置值（对齐 SettingCard.setValue 约定）"""
        qconfig.set(self.configItem, value)
        self._on_config_value_changed(value)
