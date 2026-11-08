"""
打点的即时回显与撤销（监控页）

打点成功后经 Notifier 回显"第 7 轮 · 句柄数 +18 · GDI对象 ±0 · 私有字节 +1.2 MB（较上轮）"并带
"撤销"按钮（弹窗打点、托盘与全局快捷键打点都走这里）；Ctrl+Z 撤销最近一次新增的打点（含定时
与日志触发的自动打点）。撤销的时限、基线恢复与编号回退都在 MonitorManager.undo_marker /
undo_last_marker；这里只负责把结果说给用户听。

卡片迷你图上打点刻度的右键菜单复用分析页的 MarkerEditFlow，这里提供它需要的视图模型接口
（CardMarkerCommands）。
"""
import logging
from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Tuple

from core.monitor_manager import (
    UNDO_EXPIRED, UNDO_NOTHING, UNDO_OK, UNDO_WINDOW_SECONDS, UndoOutcome,
)
from ui.formatting import ECHO_METRIC_LIMIT, marker_echo_text
from ui.services.notifier import (
    EVENT_MARKER_ADDED, EVENT_MARKER_UNDONE, NoticeAction,
)

logger = logging.getLogger(__name__)

UNDO_BUTTON_TEXT = "撤销"
UNDONE_TITLE = "已撤销打点"
UNDO_EXPIRED_TITLE = "无法撤销"
UNDO_EXPIRED_TEXT = f"已超过 {UNDO_WINDOW_SECONDS} 秒，无法撤销"
UNDO_NOTHING_TITLE = "没有可撤销的打点"
UNDO_NOTHING_TEXT = f"只能撤销最近 {UNDO_WINDOW_SECONDS} 秒内新增的打点"
UNDO_FAILED_TITLE = "撤销失败"
UNDO_FAILED_TEXT = "未能撤销打点，请查看日志"

# 打点回显里参与"上一个打点"的类型：用户打点与基线（暂停 / 恢复 / 事件类是系统记录）
_ROUND_KINDS = ('user', 'baseline')


class MarkerFeedback:
    """打点回显与撤销。entry_of(task_id) 返回面板上该任务的条目（需有 process_name、
    metric_types 属性），任务不在面板上时返回 None。"""

    def __init__(self, manager, services, host, entry_of: Callable[[str], Optional[Any]]):
        self._manager = manager
        self._services = services
        self._host = host
        self._entry_of = entry_of

    # ---------- 回显 ----------

    def echo_text(self, task_id: str, marker) -> str:
        """回显文案：本次打点时刻与上一个打点（基线是第一个，故第一次打点之后总有参照）
        时刻的变化量，指标取任务的前 ECHO_METRIC_LIMIT 个；没有参照或值缺失时只写名称。"""
        entry = self._entry_of(task_id)
        metrics = list(entry.metric_types)[:ECHO_METRIC_LIMIT] if entry is not None else []
        previous = None
        for item in self._manager.list_markers(task_id):
            if item.id == marker.id:
                break
            if item.kind in _ROUND_KINDS:
                previous = item
        if previous is None or not metrics:
            return marker.label
        current = self._manager.sample_values_at(task_id, metrics, marker.timestamp)
        before = self._manager.sample_values_at(task_id, metrics, previous.timestamp)
        return marker_echo_text(marker.label, metrics, current, before,
                                previous_is_baseline=previous.kind == 'baseline')

    def announce(self, added: List[Tuple[str, Any]]) -> None:
        """回显一次打点 [(任务 ID, 打点)]，带"撤销"按钮（一次操作的几个任务一起撤销）。"""
        if not added:
            return
        lines = []
        for task_id, marker in added:
            entry = self._entry_of(task_id)
            name = entry.process_name if entry is not None else task_id
            lines.append((name, self.echo_text(task_id, marker)))
        if len(lines) == 1:
            title, content = f"已打点 · {lines[0][0]}", lines[0][1]
        else:
            title = f"已打点：{len(lines)} 个任务"
            content = "；".join(f"{name}：{text}" for name, text in lines)

        bar_ref: List[Any] = []
        marker_id = added[0][1].id
        action = NoticeAction(UNDO_BUTTON_TEXT, lambda: self._undo_clicked(marker_id, bar_ref))
        bar = self._services.notifier.route(
            EVENT_MARKER_ADDED, 'success', title, content, action=action,
            key=EVENT_MARKER_ADDED, anchor=self._host)
        if bar is not None:
            bar_ref.append(bar)

    # ---------- 撤销 ----------

    def undo_last(self) -> UndoOutcome:
        """Ctrl+Z：撤销最近一次新增的打点，并把结果提示给用户。"""
        outcome = self._manager.undo_last_marker()
        self._report(outcome)
        return outcome

    def _undo_clicked(self, marker_id: int, bar_ref: List[Any]) -> None:
        outcome = self._manager.undo_marker(marker_id)
        # 无论成败，回显提示都不该再挂着一个失效的按钮
        for bar in bar_ref:
            try:
                bar.close()
            except RuntimeError:
                pass   # 已随窗口销毁
        self._report(outcome)

    def _report(self, outcome: UndoOutcome) -> None:
        notify = self._services.notifier.route
        if outcome.status == UNDO_OK:
            parts = []
            for marker in outcome.markers:
                entry = self._entry_of(marker.task_id)
                name = entry.process_name if entry is not None else marker.task_id
                parts.append(f"{marker.label}（{name}）")
            notify(EVENT_MARKER_UNDONE, 'success', UNDONE_TITLE, "；".join(parts),
                   key=EVENT_MARKER_UNDONE, anchor=self._host)
        elif outcome.status == UNDO_EXPIRED:
            notify(EVENT_MARKER_UNDONE, 'warning', UNDO_EXPIRED_TITLE, UNDO_EXPIRED_TEXT,
                   key=EVENT_MARKER_UNDONE, anchor=self._host)
        elif outcome.status == UNDO_NOTHING:
            notify(EVENT_MARKER_UNDONE, 'info', UNDO_NOTHING_TITLE, UNDO_NOTHING_TEXT,
                   key=EVENT_MARKER_UNDONE, anchor=self._host)
        else:
            notify(EVENT_MARKER_UNDONE, 'error', UNDO_FAILED_TITLE, UNDO_FAILED_TEXT,
                   key=EVENT_MARKER_UNDONE, anchor=self._host)


class CardMarkerCommands:
    """卡片迷你图右键菜单用的"视图模型"：满足 MarkerEditFlow 对 vm 的接口（当前任务、
    改名 / 删除 / 设为基线 / 默认名），命令直接交给 manager。菜单没有"在此处打点"，所以
    不提供 add_marker_at。"""

    def __init__(self, manager, task_id: str, entry: Optional[Any]):
        self._manager = manager
        self.current_task_id = task_id
        self.current_task = SimpleNamespace(
            process_name=getattr(entry, 'process_name', ''),
            pid=getattr(entry, 'marker_pid', None),
            start_time=None, last_sample_at=None, end_time=None)

    def next_marker_label(self) -> str:
        return self._manager.next_marker_label(self.current_task_id)

    def set_baseline_marker(self, marker) -> bool:
        return self._manager.set_baseline(self.current_task_id, marker.id)

    def rename_marker(self, marker, label: str) -> bool:
        return self._manager.rename_marker(marker.id, label, self.current_task_id)

    def delete_marker(self, marker) -> bool:
        return self._manager.delete_marker(marker.id, self.current_task_id)
