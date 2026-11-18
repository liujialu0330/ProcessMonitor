"""监控与分析共用的内存容量显示偏好。"""
from PyQt5.QtCore import QEvent, QSignalBlocker, Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox

from app_config import cfg, update_values
from utils.metrics import MEMORY_DISPLAY_UNITS

UNIT_TOOLTIP = "设置监控和分析页的内存容量显示单位。报告与 CSV 保持原有格式。"


class MemoryUnitSelector(QWidget):
    unit_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.combo = ComboBox(self)
        self.combo.setAccessibleName("内存单位")
        self.combo.setToolTip(UNIT_TOOLTIP)
        self.combo.installEventFilter(self)
        label = BodyLabel("内存单位", self)
        label.setToolTip(UNIT_TOOLTIP)
        label.setBuddy(self.combo)
        for unit in MEMORY_DISPLAY_UNITS:
            self.combo.addItem("自动" if unit == "auto" else unit, userData=unit)
        self.combo.setMinimumWidth(self.combo.sizeHint().width())
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(label)
        layout.addWidget(self.combo)
        self._sync(cfg.get(cfg.memory_display_unit))
        self.combo.currentIndexChanged.connect(self._select)
        cfg.memory_display_unit.valueChanged.connect(self._sync)

    def unit(self) -> str:
        return self.combo.currentData()

    def eventFilter(self, watched, event):
        if (watched is self.combo and event.type() == QEvent.KeyPress
                and event.modifiers() == Qt.NoModifier and event.key() in (Qt.Key_Up, Qt.Key_Down)):
            step = 1 if event.key() == Qt.Key_Down else -1
            index = max(0, min(self.combo.count() - 1, self.combo.currentIndex() + step))
            self.combo.setCurrentIndex(index)
            return True
        return super().eventFilter(watched, event)

    def _select(self, _index: int) -> None:
        update_values([(cfg.memory_display_unit, self.unit())])

    def _sync(self, unit: str) -> None:
        with QSignalBlocker(self.combo):
            self.combo.setCurrentIndex(self.combo.findData(unit))
        self.unit_changed.emit(unit)
