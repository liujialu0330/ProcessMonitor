"""
监控页 Presenter

开始 / 停止 / 暂停 / 恢复 / 打点 / 重新监控 / 查看历史 / 关闭卡片的业务流程，以及
manager 各信号（任务添加、启动、停止、数据更新、指标不可用、错误、达到上限）的
界面响应。依赖：
- manager：只用命令与只读快照接口，不接触任务线程对象；
- services：process_query / dialogs / notifier / clock / preferences；
- form：新建任务表单（NewTaskForm 或同接口的替身）；
- board：任务面板（TaskBoard 或同接口的替身），按 task_id 取卡片更新显示；
- host：提示挂靠的控件（页面），对话框挂在 host.window() 上。

中文文案（表单错误、提示）集中在这里；控件只发信号、显示给定内容。
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QWidget

import config
from core.contracts import StopReason, UnavailableReason
from ui.formatting import stop_reason_text, unavailable_reason_text
from ui.services import UiServices
from ui.widgets.monitor.new_task_form import TaskRequest
from ui.widgets.monitor.process_picker import (
    ERROR_EMPTY, ERROR_NO_MATCH, ERROR_NOT_A_NUMBER, ERROR_NOT_FOUND, ResolveError,
)
from utils.metrics import get_metric_display_name

# 异常结束提示的显示时长（毫秒）：被测进程崩溃是测试里最需要被注意到的事件，
# 不能像普通提示那样两秒就消失
ABNORMAL_STOP_INFOBAR_MS = 15000
UNAVAILABLE_INFOBAR_MS = 10000
TASK_STARTED_INFOBAR_MS = 1000
RESTART_HINT_INFOBAR_MS = 5000


def resolve_error_text(error: ResolveError) -> str:
    """进程选择错误的字段提示文案。"""
    if error.kind == ERROR_NOT_FOUND:
        return f"进程 PID {error.pid} 不存在或无法访问"
    if error.kind == ERROR_NOT_A_NUMBER:
        return "PID 必须是正整数"
    if error.kind == ERROR_NO_MATCH:
        return "输入的内容与进程列表不匹配，请从下拉列表中选择，或直接输入 PID"
    if error.kind == ERROR_EMPTY:
        return "请输入进程名或 PID，或从列表中选择进程"
    raise ValueError(f"未知的进程选择错误：{error.kind!r}")


@dataclass
class _TaskEntry:
    """面板上一张卡片对应的任务参数（取自建卡时的快照）与界面状态。"""
    task_id: str
    pid: int
    process_name: str
    metric_types: Tuple[str, ...]
    interval: Optional[float]
    note: str
    finished: bool = False
    # 打点默认标签的递增序号（"标记 N"），成功打点后加一
    marker_seq: int = 1

    @property
    def display_name(self) -> str:
        return f"{self.process_name}（PID {self.pid}）"


class MonitorPresenter(QObject):
    """监控页业务流程。

    信号：
        history_requested(str): 请求切到历史页并选中该任务（主窗口处理）
        task_ended_abnormally(str, str): 被测进程自行结束（不是用户停止）：(标题, 内容)，
            主窗口不在前台时据此弹托盘气泡
    """

    history_requested = pyqtSignal(str)
    task_ended_abnormally = pyqtSignal(str, str)

    def __init__(self, manager, services: UiServices, form, board, host: QWidget,
                 parent: Optional[QObject] = None, *,
                 task_limit: int = config.MAX_MONITOR_TASKS):
        super().__init__(parent)
        self.manager = manager
        self.services = services
        self.form = form
        self.board = board
        self.host = host
        self.task_limit = task_limit

        self._tasks: Dict[str, _TaskEntry] = {}
        # 当前显示中的错误提示 {(task_id, 内容): 提示对象}：错误提示不自动消失，
        # 同一任务同一错误重复上报时不再叠加
        self._error_bars: Dict[Tuple[str, str], Any] = {}

        # 新建任务沿用上次选择的指标（默认"泄漏排查"预设）
        self.form.set_metrics_summary(self.services.preferences.last_metrics())
        self.form.set_collapsed(False)
        self._sync_counts()

        self.form.start_requested.connect(self.on_start_requested)
        self.form.target_invalid.connect(self.on_target_invalid)
        self.form.choose_metrics_requested.connect(self.on_choose_metrics)

        self.board.stop_requested.connect(self.on_stop)
        self.board.pause_requested.connect(self.on_pause)
        self.board.resume_requested.connect(self.on_resume)
        self.board.marker_requested.connect(self.on_marker)
        self.board.history_requested.connect(self.history_requested)
        self.board.restart_requested.connect(self.on_restart)
        self.board.close_requested.connect(self.on_close)

        manager.task_added.connect(self.on_task_added)
        manager.task_started.connect(self.on_task_started)
        manager.task_stopped.connect(self.on_task_stopped)
        manager.data_updated.connect(self.on_data_updated)
        manager.error_occurred.connect(self.on_error)
        manager.metrics_unavailable.connect(self.on_metrics_unavailable)
        manager.task_limit_reached.connect(self.on_limit_reached)

    # ========== 查询（供页面与测试） ==========

    def task_ids(self):
        """面板上各卡片的任务 ID（按加入顺序）。"""
        return list(self._tasks)

    def shown_errors(self) -> Set[Tuple[str, str]]:
        """仍在显示中的错误提示键 (task_id, 内容)。"""
        return set(self._error_bars)

    def next_marker_label(self, task_id: str) -> Optional[str]:
        entry = self._tasks.get(task_id)
        return f"标记 {entry.marker_seq}" if entry is not None else None

    # ========== 公共小工具 ==========

    def _dialog_parent(self) -> QWidget:
        return self.host.window()

    def _warn(self, content: str, title: str = "提示", duration: int = 3000):
        self.services.notifier.warning(self.host, title, content, duration)

    def _sync_counts(self) -> None:
        self.board.set_counts(self.manager.running_task_count(), self.task_limit)

    def _set_has_tasks(self, had_tasks: bool) -> None:
        """面板从空到有卡片时折叠表单，从有到空时展开；其余时候不动用户的展开状态。"""
        has_tasks = bool(self._tasks)
        if has_tasks != had_tasks:
            self.form.set_collapsed(has_tasks)

    # ========== 新建任务 ==========

    def on_choose_metrics(self) -> None:
        """弹出指标多选对话框；确定后记住本次选择。"""
        selected = self.services.dialogs.choose_metrics(
            self._dialog_parent(), self.form.metrics())
        if selected is None:
            return
        metrics = list(selected)
        self.services.preferences.remember_last_metrics(metrics)
        self.form.set_metrics_summary(metrics)

    def on_target_invalid(self, error: ResolveError) -> None:
        self.form.show_error(resolve_error_text(error))

    def on_start_requested(self, request: TaskRequest) -> Optional[str]:
        if not request.metrics:
            self._warn("请先选择要监控的指标")
            return None
        return self.start_monitoring(request)

    def start_monitoring(self, request: TaskRequest) -> Optional[str]:
        """创建并启动任务；同一 PID 已有运行中任务时先确认。返回任务 ID，
        取消或创建失败返回 None。"""
        if (self.manager.has_running_task_for_pid(request.pid)
                and not self._confirm_duplicate_pid(request)):
            return None
        task_id = self.manager.create_task(
            request.pid, request.name, list(request.metrics), request.interval,
            note=request.note, create_time=request.create_time)
        if task_id:
            self.manager.start_task(task_id)
        return task_id

    def _confirm_duplicate_pid(self, request: TaskRequest) -> bool:
        return self.services.dialogs.confirm(
            self._dialog_parent(), "已在监控该进程",
            f"{request.name}（PID {request.pid}）已有一个运行中的监控任务。\n"
            "仍要再创建一个吗？",
            "继续创建")

    # ========== manager 信号 ==========

    def on_task_added(self, task_id: str) -> None:
        info = self.manager.get_task_info(task_id)
        if info is None:
            return
        had_tasks = bool(self._tasks)
        self._tasks[task_id] = _TaskEntry(
            task_id=task_id, pid=info.pid, process_name=info.process_name,
            metric_types=tuple(info.metric_types), interval=info.interval, note=info.note)
        self.board.create_card(info)
        self._sync_counts()
        self._set_has_tasks(had_tasks)

    def on_task_started(self, _task_id: str) -> None:
        self._sync_counts()
        self.services.notifier.success(
            self.host, "成功", "监控任务已启动", TASK_STARTED_INFOBAR_MS)

    def on_task_stopped(self, task_id: str, reason: StopReason) -> None:
        """卡片保留并转为"已结束"，同时请 manager 移除 core 侧任务。remove_task 不阻塞：
        线程若还没退出，manager 会在它 finished 后再释放对象。"""
        entry = self._tasks.get(task_id)
        if entry is not None:
            entry.finished = True
            self.board.finish_card(task_id, reason, self.services.clock.now())
            self.form.forget_process_if(entry.pid)

        self.manager.remove_task(task_id)
        self._sync_counts()

        reason_text = stop_reason_text(reason)
        if reason is StopReason.USER:
            self.services.notifier.info(self.host, "任务已停止", f"原因：{reason_text}", 2000)
            return

        # 被测进程崩溃/退出等自行结束：提示要显眼且停留更久，并通知主窗口在后台时补托盘气泡
        name = entry.display_name if entry is not None else task_id
        title = "监控任务异常结束"
        content = f"{name}：{reason_text}"
        self.services.notifier.warning(self.host, title, content, ABNORMAL_STOP_INFOBAR_MS)
        self.task_ended_abnormally.emit(title, content)

    def on_data_updated(self, task_id: str, values: dict) -> None:
        """每采集周期一次：卡片采集次数为内存自增，不查库。"""
        card = self.board.card(task_id)
        if card is not None:
            card.update_values(values)
            card.increment_count()

    def on_metrics_unavailable(self, task_id: str,
                               reasons: Dict[str, UnavailableReason]) -> None:
        """部分指标无法采集：卡片标"不可用"，并弹出一次警告。"""
        card = self.board.card(task_id)
        if card is None:
            return
        card.mark_unavailable(reasons)

        items = "、".join(
            f"{get_metric_display_name(metric)}（{unavailable_reason_text(reason)}）"
            for metric, reason in reasons.items())
        content = f"{items}，这些指标不会记录数据。"
        if UnavailableReason.ACCESS_DENIED in reasons.values():
            content += "以管理员身份运行本工具可采集全部指标。"
        self.services.notifier.warning(
            self.host, "部分指标无法采集", content, UNAVAILABLE_INFOBAR_MS)

    def on_error(self, task_id: str, error_msg: str) -> None:
        """错误提示不自动消失；同一任务的同一错误仍在显示时不再叠加
        （采集异常可能每个周期都上报一次）。"""
        key = (task_id, error_msg)
        if key in self._error_bars:
            return
        bar = self.services.notifier.error(self.host, "错误", error_msg)
        if bar is not None:
            self._error_bars[key] = bar
            bar.closedSignal.connect(lambda k=key: self._error_bars.pop(k, None))

    def on_limit_reached(self) -> None:
        self.services.notifier.warning(
            self.host, "提示", f"最多只能同时监控 {self.task_limit} 个任务", 2000)

    # ========== 卡片按钮 ==========

    def on_stop(self, task_id: str) -> None:
        """确认后发出停止请求（非阻塞），卡片等 task_stopped 再转为已结束。
        停止窗口期内 manager 可能返回 False（任务已在停止），界面容忍不报错。"""
        entry = self._tasks.get(task_id)
        if entry is None or entry.finished:
            return
        confirmed = self.services.dialogs.confirm(
            self._dialog_parent(), "停止监控",
            f"确定要停止对 {entry.display_name} 的监控吗？\n"
            "停止后本任务不可续采，如需继续请重新监控；已采集的数据会保留。",
            "停止")
        if not confirmed:
            return
        if self.manager.stop_task(task_id):
            card = self.board.card(task_id)
            if card is not None:
                card.set_stopping()

    def on_pause(self, task_id: str) -> None:
        """manager 确认暂停成功后才更新卡片；任务正在停止等导致失败时不做任何事。"""
        if self.manager.pause_task(task_id):
            card = self.board.card(task_id)
            if card is not None:
                card.set_paused(True)

    def on_resume(self, task_id: str) -> None:
        if self.manager.resume_task(task_id):
            card = self.board.card(task_id)
            if card is not None:
                card.set_paused(False)

    def on_marker(self, task_id: str) -> None:
        """打点：弹出标签输入框，经 manager 写库，成功后提示打点时间。"""
        entry = self._tasks.get(task_id)
        if entry is None or entry.finished:
            return
        # 打点时间取点击按钮的这一刻：输入标签要花好几秒，按确认时刻记录会让
        # 竖线落在事件之后，短周期下甚至把增长归到下一轮
        clicked_at = self.services.clock.now()
        label = self.services.dialogs.ask_marker_label(
            self._dialog_parent(), f"标记 {entry.marker_seq}", clicked_at)
        if label is None:
            return
        if not label.strip():
            self._warn("打点标签不能为空")
            return

        timestamp = self.manager.add_marker(task_id, label, clicked_at)
        if timestamp is None:
            self.services.notifier.error(self.host, "打点失败", "未能保存打点，请查看日志")
            return
        entry.marker_seq += 1
        self.services.notifier.success(
            self.host, "已打点", f"{label.strip()} · {timestamp.strftime('%H:%M:%S')}", 2000)

    def on_restart(self, task_id: str) -> Optional[str]:
        """已结束卡片的"重新监控"：按进程名找到唯一的运行进程时，以相同指标/间隔/
        备注新建任务；找不到或有多个时把进程名填入搜索框，由用户自己选。"""
        entry = self._tasks.get(task_id)
        if entry is None:
            return None

        # 精确匹配（不区分大小写）并排除本程序自身：被测的同名进程（如 python.exe）
        # 退出后，不能因为只剩本程序同名而自动监控起自己
        matches = self.services.process_query.find_pids_by_name(entry.process_name)
        if len(matches) == 1:
            pid, create_time = matches[0]
            return self.start_monitoring(TaskRequest(
                pid=pid, name=entry.process_name, create_time=create_time,
                metrics=entry.metric_types,
                interval=entry.interval or self.form.interval(), note=entry.note))

        self.form.fill_search(entry.process_name)
        if matches:
            self._warn(
                f"找到 {len(matches)} 个 {entry.process_name} 进程，"
                "请在上方列表中选择要监控的一个",
                title="请选择进程", duration=RESTART_HINT_INFOBAR_MS)
        else:
            self._warn(
                f"当前没有运行中的 {entry.process_name}，启动后点击刷新再选择",
                title="未找到进程", duration=RESTART_HINT_INFOBAR_MS)
        return None

    def on_close(self, task_id: str) -> None:
        """移除卡片（历史数据仍保留）。"""
        had_tasks = bool(self._tasks)
        self._tasks.pop(task_id, None)
        self.board.remove_card(task_id)
        self._sync_counts()
        self._set_has_tasks(had_tasks)
