"""
数据导出页面：选任务 -> 确认摘要 -> 选位置并导出 CSV（导出在后台线程中进行）。
"""
import os
from contextlib import suppress
from typing import TYPE_CHECKING, Optional

from PyQt5.QtCore import Qt, QStandardPaths
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from qfluentwidgets import (
    CardWidget, PrimaryPushButton, PushButton, FluentIcon,
    StrongBodyLabel, CaptionLabel, LineEdit, HorizontalSeparator
)

from core.export_worker import ExportWorker
from data.database import Database
from ui.formatting import default_export_filename, format_count, task_status_text
from ui.models.task_options import TaskOptionsModel
from ui.services import UiServices
from ui.typography import DataCaptionLabel
from ui.widgets.empty_state import EmptyStateCard
from ui.widgets.page_scaffold import PageScaffold
from ui.widgets.task_selector import TaskSelector
from ui.widgets.visible_timer import VisibleTimer
from utils.metrics import get_metric_display_name

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager

# 页面可见时刷新运行中任务的下拉时长与摘要的间隔（毫秒）；时长按分钟显示，30 秒足够
RUNNING_TASK_REFRESH_INTERVAL_MS = 30_000
DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
# 警告提示：(标题, 内容, 显示毫秒)
NO_TASK_WARNING = ("请选择任务", "请先选择要导出的监控任务", 2000)
MAINTENANCE_WARNING = ("暂时无法导出", "正在清理数据库，请稍后再导出", 4000)


class ExportPage(PageScaffold):
    """数据导出页面（db 与导出线程共用；manager 登记导出，与清理数据库互斥）"""

    def __init__(self, db: Database, manager: "MonitorManager",
                 services: UiServices, parent=None):
        super().__init__("导出数据", parent=parent)
        self.setObjectName("exportPage")
        self.db, self.manager, self.services = db, manager, services

        self.current_task_id: Optional[str] = None
        self.current_task = None
        # 导出摘要场景下年份有意义：下拉项的开始时间带年份
        self._task_options = TaskOptionsModel(full_date=True)
        # 导出期间非 None；线程对象的释放与退出收尾由 services.threads 登记表负责
        self._export_worker: Optional[ExportWorker] = None
        # 上次使用的保存目录：切换任务后默认文件名仍放在这里；首次默认"文档"目录
        self.last_dir = (
            QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
            or os.path.expanduser('~'))

        self._init_ui()
        # 页面可见时定时刷新运行中任务的时长与摘要；页面隐藏时自动停止
        self.running_timer = VisibleTimer(
            self, RUNNING_TASK_REFRESH_INTERVAL_MS, self.refresh_running_tasks)

    @property
    def export_worker(self) -> Optional[ExportWorker]:
        """进行中的导出线程（没有导出时为 None）。"""
        return self._export_worker

    def _init_ui(self):
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self.reload_tasks)
        self.add_header_widget(self.refresh_button)

        # 导出是一个连续任务，收敛到同一张卡片，避免几张等权大卡片打断操作流
        self.export_card = CardWidget()
        card_layout = QVBoxLayout(self.export_card)
        card_layout.setContentsMargins(20, 18, 20, 20)
        card_layout.setSpacing(14)
        card_layout.addWidget(StrongBodyLabel("选择要导出的任务"))
        self.task_selector = TaskSelector(placeholder="选择监控任务")
        self.task_selector.task_selected.connect(self._on_task_selected)
        card_layout.addWidget(self.task_selector)
        card_layout.addWidget(HorizontalSeparator())
        self.summary_widget = self._build_summary()
        card_layout.addWidget(self.summary_widget)
        card_layout.addWidget(HorizontalSeparator())

        path_layout = QHBoxLayout()
        path_layout.setSpacing(8)
        self.path_edit = LineEdit()
        self.path_edit.setPlaceholderText("尚未选择保存位置")
        self.path_edit.setReadOnly(True)
        self.browse_button = PushButton("选择位置", self, FluentIcon.FOLDER)
        self.browse_button.clicked.connect(self._browse_save_path)
        self.export_button = PrimaryPushButton("导出 CSV", self, FluentIcon.DOWNLOAD)
        self.export_button.setMinimumWidth(128)
        self.export_button.clicked.connect(self._export_data)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.browse_button)
        path_layout.addWidget(self.export_button)
        card_layout.addLayout(path_layout)
        card_layout.addWidget(CaptionLabel(
            "CSV · UTF-8 · 可用 Excel 打开 · 末列「打点」记录事件标记"))
        self.add_section(self.export_card)

        # 没有可导出任务时以空状态卡代替整张导出卡，不让用户点出"未选任务"提示
        self.empty_state_card = EmptyStateCard(
            FluentIcon.DOWNLOAD, "暂无可导出的数据",
            "完成一次监控后，即可在这里把数据导出为 CSV")
        self.empty_state_card.setVisible(False)
        self.add_section(self.empty_state_card)

        self.export_status_label = CaptionLabel("")   # 导出期间显示已处理行数
        self.export_status_label.setAlignment(Qt.AlignCenter)
        self.add_section(self.export_status_label)
        self.content_layout.addStretch()

    def _build_summary(self) -> QWidget:
        """紧凑任务摘要：进程 · PID · 状态 / 指标 / 时间范围 · 数据量。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        def row(*items):
            # 字符串为说明标签，整数为额外间距，其余为控件
            line = QHBoxLayout()
            line.setSpacing(8)
            for item in items:
                if isinstance(item, int):
                    line.addSpacing(item)
                else:
                    line.addWidget(CaptionLabel(item) if isinstance(item, str) else item)
            line.addStretch()
            layout.addLayout(line)

        self.process_name_label = StrongBodyLabel("未选择任务")
        self.pid_label = DataCaptionLabel("—")
        self.status_label = CaptionLabel("—")
        row(self.process_name_label, 12, "PID", self.pid_label, 12, "状态",
            self.status_label)
        self.metric_label = CaptionLabel("—")
        row("指标", self.metric_label)
        self.start_time_label = DataCaptionLabel("—")
        self.end_time_label = DataCaptionLabel("—")
        self.data_count_label = DataCaptionLabel("—")
        row("时间", self.start_time_label, "—", self.end_time_label, 12, "数据",
            self.data_count_label)
        return widget

    def reload_tasks(self):
        """重新载入任务列表：恢复之前选中的任务，找不到时默认选最新任务。"""
        # 单条查询取出至少有一次采样的任务（与历史页口径一致，不逐任务 COUNT）
        tasks = self.db.get_tasks_with_data()
        self.export_card.setVisible(bool(tasks))
        self.empty_state_card.setVisible(not tasks)
        running_ids = {t.task_id for t in tasks if self._is_task_running(t.task_id)}
        options = self._task_options.rebuild(tasks, running_ids, self.services.clock.now())
        keep_id = self.current_task_id
        self.task_selector.set_options(options, keep_id=keep_id)
        if not options:
            self.current_task_id = self.current_task = None
            self._clear_task_info()
            self.path_edit.clear()
        elif keep_id is not None and self.task_selector.current_id() == keep_id:
            # 同一任务重新载入：只刷新摘要，保留用户已选的保存路径
            self._load_task_info(keep_id)
        else:
            self.task_selector.select_by_id(options[0].task_id)

    def refresh_running_tasks(self):
        """原地刷新运行中任务：下拉项时长随时间增长，结束后改为最终时长；当前选中的
        是运行中任务时一并刷新摘要。不重建列表，不影响当前选择与已选的保存路径。"""
        tracked = self._task_options.tracked_running_ids()
        if not tracked:
            return
        # 已在别处被删除的任务取回 None：模型沿用旧记录，下次显示页面时整体重载
        latest = {task_id: self.db.get_task(task_id) for task_id in tracked}
        running_ids = {task_id for task_id in tracked if self._is_task_running(task_id)}
        changed = self._task_options.refresh_running(
            self.services.clock.now(), running_ids, latest)
        options = self._task_options.options()
        for index in changed:
            self.task_selector.update_option(index, options[index])
        if self.current_task_id in tracked:
            self._load_task_info(self.current_task_id)

    def _on_task_selected(self, task_id: Optional[str], _explicit: bool):
        if task_id is None:
            self._clear_task_info()
            return
        task_changed = task_id != self.current_task_id
        self.current_task_id = task_id
        self._load_task_info(task_id)
        # 换了任务：保存路径重置为该任务的默认文件名，避免沿用上一个任务的文件名
        # 而覆盖或误导；同一任务重新载入列表（如页面再次显示）时保留用户已选的路径
        if task_changed and self.current_task is not None:
            self.path_edit.setText(self._default_save_path())

    def _load_task_info(self, task_id: str):
        """加载并显示任务摘要。"""
        task = self.db.get_task(task_id)
        if not task:
            self.services.notifier.error(self, "任务加载失败", "无法读取所选任务")
            self._clear_task_info()
            return
        self.current_task = task
        self.process_name_label.setText(task.process_name)
        self.pid_label.setText(str(task.pid))
        # 多指标显示"N 项 · 名1、名2…"，超过3个截断，tooltip 显示全部
        names = [get_metric_display_name(m) for m in task.metric_types]
        shown = '、'.join(names[:3]) + ('…' if len(names) > 3 else '')
        self.metric_label.setText(f"{len(names)} 项 · {shown}")
        self.metric_label.setToolTip('、'.join(names))
        # 未在运行却没有结束时间（库里仍标 running 的孤儿任务，启动时 reconcile
        # 会补写）：结束时间退回最后采样时间，与下拉时长同口径
        running = self._is_task_running(task_id)
        self.start_time_label.setText(task.start_time.strftime(DATETIME_FORMAT))
        end_time = None if running else (task.end_time or task.last_sample_at)
        self.end_time_label.setText("进行中" if running else (
            end_time.strftime(DATETIME_FORMAT) if end_time is not None else "—"))
        sample_count = self.db.get_sample_count(task_id)
        data_count = self.db.get_data_point_count(task_id)
        self.data_count_label.setText(
            f"{format_count(sample_count)} 次采集 · {format_count(data_count)} 条数据")
        self.status_label.setText(task_status_text(running))

    def _is_task_running(self, task_id: str) -> bool:
        """以任务状态服务为准（与历史页同口径，含停止收尾中），不看库里的 status
        列——异常退出遗留的孤儿任务在库里仍是 running。"""
        return self.services.task_status.is_running(task_id)

    def _clear_task_info(self):
        self.process_name_label.setText("未选择任务")
        for label in (self.pid_label, self.metric_label, self.start_time_label,
                      self.end_time_label, self.data_count_label, self.status_label):
            label.setText("—")
        self.metric_label.setToolTip("")

    def _default_save_path(self) -> str:
        """上次使用的目录 + 进程名_PID_开始时间.csv"""
        # normpath：上次目录来自文件对话框（正斜杠），统一成 Windows 反斜杠显示
        return os.path.normpath(
            os.path.join(self.last_dir, default_export_filename(self.current_task)))

    def _browse_save_path(self):
        if not self.current_task:
            self._warn(NO_TASK_WARNING)
            return
        default_path = self.path_edit.text().strip() or self._default_save_path()
        file_path = self.services.dialogs.ask_save_path(
            self, "选择保存位置", default_path, "CSV 文件 (*.csv)")
        if file_path:
            self.path_edit.setText(file_path)
            self.last_dir = os.path.dirname(file_path) or self.last_dir

    def _export_data(self):
        """校验、禁用按钮、登记并启动导出线程（分批读库与流式写文件都在线程内）。"""
        if self._export_worker is not None and self._export_worker.isRunning():
            return   # 已有导出在进行中，忽略重复点击
        # 正在清理/压缩数据库：导出读库会和 VACUUM 互相抢锁，先拒绝（不弹保存对话框）
        if self.manager.is_maintenance_running():
            self._warn(MAINTENANCE_WARNING)
            return
        if not self.current_task_id or not self.current_task:
            self._warn(NO_TASK_WARNING)
            return
        # 路径为空（如手动清空）时直接打开保存对话框，不必先点「选择位置」再点「导出」
        save_path = self.path_edit.text().strip()
        if not save_path:
            self._browse_save_path()
            save_path = self.path_edit.text().strip()
            if not save_path:
                return
        # 目标已存在：确认覆盖（导出线程先写临时文件再原子替换，取消不会损坏原文件）
        if os.path.exists(save_path) and not self.services.dialogs.confirm(
                self.window(), "文件已存在", f"{save_path}\n已存在，是否覆盖？", "覆盖"):
            return
        # 轻量存在性检查（COUNT 查询，不拉取全部数据）
        if self.db.get_data_point_count(self.current_task_id) == 0:
            self.services.notifier.warning(
                self, "暂无可导出数据", "该任务没有可导出的采集数据", duration=2000)
            return
        # 登记导出（确认框期间维护可能已开始，这里以登记结果为准）
        if not self.manager.begin_export():
            self._warn(MAINTENANCE_WARNING)
            return

        self.export_button.setEnabled(False)
        self.export_status_label.setText("正在导出…")
        worker = ExportWorker(self.db, self.current_task, save_path, metric_type=None)
        self._export_worker = worker
        worker.export_progress.connect(self._on_export_progress)
        worker.export_finished.connect(self._on_export_finished)
        worker.error_occurred.connect(self._on_export_error)
        # 注销挂在 QThread.finished 上：完成/失败信号发出时线程尚未真正结束；取消导出
        # （退出时）不发完成/失败信号，finished 仍会触发，取消后线程自行删除临时文件
        worker.finished.connect(lambda w=worker: self._on_export_thread_finished(w))
        self.services.threads.register(
            worker, name="导出数据", cancel=worker.cancel, timeout_ms=2000)
        worker.start()

    def _warn(self, notice):
        title, content, duration = notice
        self.services.notifier.warning(self, title, content, duration=duration)

    def _on_export_progress(self, processed: int):
        self.export_status_label.setText(f"正在导出… 已处理 {format_count(processed)} 条数据")

    def _on_export_finished(self, save_path: str, row_count: int, point_count: int):
        self.export_status_label.setText("")
        self.last_dir = os.path.dirname(save_path) or self.last_dir
        # 运行中任务在导出期间仍在采集：刷新摘要，避免与"已导出 N 次采集"对不上
        if self.current_task is not None:
            self._load_task_info(self.current_task.task_id)
        info_bar = self.services.notifier.success(self, "导出成功", (
            f"已导出 {format_count(row_count)} 次采集（{format_count(point_count)} 条数据）"),
            duration=8000)
        # 导出完成不自动抢焦点打开资源管理器；保留显式快捷操作
        self.open_folder_button = PushButton("打开文件夹")
        self.open_folder_button.clicked.connect(
            lambda: self._open_folder(os.path.dirname(save_path)))
        info_bar.addWidget(self.open_folder_button)

    def _on_export_error(self, msg: str):
        self.export_status_label.setText("")
        self.services.notifier.error(self, "导出失败", msg)

    def _on_export_thread_finished(self, worker):
        """导出线程真正结束（QThread.finished）：注销导出、释放引用并恢复导出按钮。"""
        # 每个已启动的 worker 都只在这里注销一次，即使引用已被新 worker 替换
        self.manager.end_export()
        if self._export_worker is worker:
            self._export_worker = None
        if self._export_worker is None:
            self.export_button.setEnabled(True)

    def _open_folder(self, folder_path: str):
        # 打不开（目录已被删除等）时不再打扰，文件本身已经导出成功
        with suppress(OSError):
            self.services.dialogs.open_folder(folder_path)

    def showEvent(self, event):
        # 每次显示时重新载入任务列表（运行中刷新定时器由 running_timer 自行启停）
        super().showEvent(event)
        self.reload_tasks()
