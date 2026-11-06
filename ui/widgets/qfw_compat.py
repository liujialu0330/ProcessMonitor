"""
qfluentwidgets 兼容封装

本项目依赖 PyQt-Fluent-Widgets 1.x。库里有几处与 Qt 原生控件不同、容易踩坑的
行为；能封装的在这里（或对应控件里）封装一次，页面不再各自打补丁。

已知特殊行为清单：

1. ComboBox / EditableComboBox 的 addItem 在加入第一项时会自动选中它并发出
   currentIndexChanged（此时 itemData 往往还没设置）。
   → 已封装：ui.widgets.task_selector.TaskSelector.set_options（填充期间屏蔽信号、
     填完回到 -1 再恢复选中）；可搜索下拉见 ui.widgets.searchable_combo。
2. ComboBox.clear() 只清条目并把按钮文字置空，不恢复占位文字；
   ComboBox.setCurrentIndex(-1) 才会重新显示占位文字（且不发信号）。
   EditableComboBox.clear() 在没有选中项时不清输入框里的文本（用户打了字却
   没选中任何项的情形），需要另行 setText("")。
   → 已封装：同上两个下拉控件。
3. ComboBoxBase.setCurrentIndex 对越界下标与"已是当前项"直接返回、不发信号；
   ComboBox 重写后对负数下标置为 -1（显示占位文字）而不是忽略；
   EditableComboBox 重写后的 setCurrentIndex 完全不发 currentIndexChanged。
4. MessageBoxBase 构造时把焦点给"确定"按钮：带输入框的对话框弹出后，用户直接
   打字会落空、回车按默认值提交。
   → 已封装：InputDialogBase（显示后把焦点交给指定输入控件并全选）。
5. SwitchSettingCard.setValue 每次都把开关文案重置为英文 On/Off（用户点击与
   配置值被外部改动两条路径都会经过它）。
   → 已封装：CnSwitchSettingCard（setValue 之后改回中文"开/关"）。
6. 对控件显式 setMinimumHeight 会覆盖布局算出的最小尺寸：放在滚动区里的内容
   因此不再撑出滚动条，小窗口下子控件被压出卡片、坐标轴被裁掉。
   → 不封装：需要最小高度时改给真正需要的子控件设，或用布局的最小尺寸；
     现有位置保留注释与回归测试。
"""
from typing import Optional

from PyQt5.QtCore import QEvent, QTimer
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import MessageBoxBase, SwitchSettingCard

SWITCH_ON_TEXT = "开"
SWITCH_OFF_TEXT = "关"


class InputDialogBase(MessageBoxBase):
    """带输入控件的对话框基类：显示后把焦点交给指定输入控件。

    MessageBoxBase 默认把焦点给"确定"按钮（见模块说明第 4 条）。子类建好输入
    控件后调用 set_focus_widget（或构造时传入）；输入控件有 selectAll 时一并全选
    默认内容，用户打字即覆盖。
    """

    def __init__(self, parent=None, focus_widget: Optional[QWidget] = None):
        super().__init__(parent)
        self._focus_widget: Optional[QWidget] = None
        self._select_all = True
        if focus_widget is not None:
            self.set_focus_widget(focus_widget)

    def set_focus_widget(self, widget: Optional[QWidget], *,
                         select_all: bool = True) -> None:
        """指定显示后获得焦点的控件；select_all 为 True 时显示后全选其内容。"""
        if self._focus_widget is not None:
            self._focus_widget.removeEventFilter(self)
        self._focus_widget = widget
        self._select_all = select_all
        if widget is not None:
            # 监听焦点进入：窗口可能在显示之后才被激活并把焦点交给输入控件
            # （焦点原因不同，QLineEdit 会自动全选），选区状态要在那之后再定
            widget.installEventFilter(self)

    def focus_widget(self) -> Optional[QWidget]:
        return self._focus_widget

    def _apply_selection(self) -> None:
        widget = self._focus_widget
        if widget is None:
            return
        if self._select_all:
            if hasattr(widget, "selectAll"):
                widget.selectAll()
        elif hasattr(widget, "deselect"):
            # select_all=False 必须确定地表示"不选中"
            widget.deselect()

    def eventFilter(self, watched, event):
        # 基类构造期间就会以本对象为过滤器收到事件，此时属性尚未赋值
        focus_widget = getattr(self, "_focus_widget", None)
        if focus_widget is not None and watched is focus_widget and event.type() == QEvent.FocusIn:
            # 等 QLineEdit 自己的 focusInEvent（可能全选）处理完再统一设定选区
            QTimer.singleShot(0, self._apply_selection)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        widget = self._focus_widget
        if widget is None:
            return
        widget.setFocus()
        self._apply_selection()


class CnSwitchSettingCard(SwitchSettingCard):
    """开关文案为中文"开/关"的设置卡片。

    库的 setValue 每次都会把文案重置为英文 On/Off（见模块说明第 5 条），所以在
    setValue 之后统一改回中文；基类构造时就会调用一次 setValue，届时开关按钮
    已创建，覆盖同样生效。
    """

    def setValue(self, isChecked: bool):  # noqa: N802,N803 - 覆盖库方法
        super().setValue(isChecked)
        self.switchButton.setText(SWITCH_ON_TEXT if isChecked else SWITCH_OFF_TEXT)
