"""
趋势图右键菜单：在此处打点 / 设为基线 / 重命名 / 删除

右键位置附近（几个像素内）有打点时，后三项作用于该打点；否则只能"在此处打点"。
输入名称、确认删除经 services.dialogs，写库经视图模型 → manager（成功后 manager 发
markers_changed，视图模型静默重查，图表与各表随之更新，因此成功时不另弹提示）。
"""
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from PyQt5.QtCore import QObject
from qfluentwidgets import Action, FluentIcon, RoundMenu

from ui.widgets.history.common import DETAIL_TIME_FORMAT

ADD_TEXT = "在此处打点"
BASELINE_TEXT = "设为基线"
RENAME_TEXT = "重命名"
DELETE_TEXT = "删除"
SYSTEM_KINDS = ('pause', 'resume')

MenuItem = Tuple[str, Callable[[], bool], bool]


class MarkerEditFlow(QObject):
    """右键打点编辑：生成菜单项并执行（对话框 + 视图模型命令 + 失败提示）。"""

    def __init__(self, vm, services, host, parent=None):
        super().__init__(parent)
        self._vm = vm
        self._services = services
        self._host = host
        self.last_menu: Optional[RoundMenu] = None

    def items(self, x: float, marker) -> List[MenuItem]:
        """(文本, 执行函数, 是否可用) 列表，顺序即菜单顺序。"""
        editable = marker is not None and getattr(marker, 'kind', 'user') not in SYSTEM_KINDS
        is_baseline = editable and marker.kind == 'baseline'
        return [
            (ADD_TEXT, lambda: self.add_at(x), self._vm.current_task_id is not None),
            (BASELINE_TEXT, lambda: self.set_baseline(marker), editable and not is_baseline),
            (RENAME_TEXT, lambda: self.rename(marker), editable),
            (DELETE_TEXT, lambda: self.delete(marker), editable),
        ]

    def show_menu(self, x: float, marker, global_pos) -> RoundMenu:
        menu = RoundMenu(parent=self._host)
        icons = (FluentIcon.PIN, FluentIcon.FLAG, FluentIcon.EDIT, FluentIcon.DELETE)
        for (text, run, enabled), icon in zip(self.items(x, marker), icons):
            action = Action(icon, text, menu)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _=False, fn=run: fn())
            menu.addAction(action)
            if text == ADD_TEXT:
                menu.addSeparator()
        self.last_menu = menu
        menu.exec(global_pos)
        return menu

    # ---------- 动作 ----------

    def _clamped_time(self, x: float) -> datetime:
        """右键位置换成时刻，限制在任务的采集时间内（打点落在数据之外没有意义）。"""
        ts = datetime.fromtimestamp(x)
        task = self._vm.current_task
        if task is None:
            return ts
        low = task.start_time
        high = task.last_sample_at or task.end_time
        if low is not None and ts < low:
            ts = low
        if high is not None and ts > high:
            ts = high
        return ts

    def add_at(self, x: float) -> bool:
        task = self._vm.current_task
        if task is None:
            return False
        ts = self._clamped_time(x)
        default = self._vm.next_marker_label()
        label = self._services.dialogs.ask_marker_label(
            self._host.window(), default, ts, process_name=task.process_name, pid=task.pid)
        if label is None or not label.strip():
            return False
        if self._vm.add_marker_at(ts, label, default):
            return True
        self._services.notifier.error(self._host, "打点失败", "添加打点失败，请查看日志")
        return False

    def set_baseline(self, marker) -> bool:
        if self._vm.set_baseline_marker(marker):
            return True
        self._services.notifier.error(self._host, "设置失败", "设为基线失败，请查看日志")
        return False

    def rename(self, marker) -> bool:
        label = self._services.dialogs.ask_marker_label(
            self._host.window(), marker.label, marker.timestamp,
            process_name=getattr(self._vm.current_task, 'process_name', ''),
            pid=getattr(self._vm.current_task, 'pid', None))
        if label is None or not label.strip() or label.strip() == marker.label:
            return False
        if self._vm.rename_marker(marker, label):
            return True
        self._services.notifier.error(self._host, "重命名失败", "修改打点名称失败，请查看日志")
        return False

    def delete(self, marker) -> bool:
        stamp = marker.timestamp.strftime(DETAIL_TIME_FORMAT)
        if not self._services.dialogs.confirm(
                self._host.window(), "删除打点",
                f"确定删除打点「{marker.label}」（{stamp}）吗？", "删除"):
            return False
        if self._vm.delete_marker(marker):
            return True
        self._services.notifier.error(self._host, "删除失败", "删除打点失败，请查看日志")
        return False
