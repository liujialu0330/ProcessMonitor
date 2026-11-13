"""
监控页面

只做组装：页面骨架（页头：标题 · 配额摘要 · [刷新进程]）+ 新建任务表单（NewTaskForm）
+ 任务面板（TaskBoard），业务流程（开始/等待进程启动/停止/打点/同配置再测、manager
信号响应、提示与确认框）全部在 MonitorPresenter。

键盘：F5 刷新进程列表、Ctrl+F 聚焦进程搜索框（只在本页可见时生效，不与其他页的
同名快捷键冲突）。进程列表在页面显示、窗口重新激活时自动刷新（两秒节流）。
构造函数不读取进程列表：首屏只发起一次后台读取（线程登记到 services.threads）。
"""
from typing import List

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QShortcut
from qfluentwidgets import CaptionLabel, FluentIcon, TransparentToolButton, SegmentedWidget, TeachingTip
from app_config import cfg, update_values

from core.monitor_manager import MonitorManager
from ui.presenters.monitor_presenter import MonitorPresenter
from ui.services import UiServices
from ui.widgets.monitor.new_task_form import NewTaskForm
from ui.widgets.monitor.task_board import TaskBoard
from ui.widgets.page_scaffold import PageScaffold

PAGE_TITLE = "监控"
PAGE_SUBTITLE = "选择被测进程开始记录；每轮操作后点“打点”即可量化泄漏"
REFRESH_TEXT = "刷新进程"


class MonitorPage(PageScaffold):
    """监控页面"""

    # 请求切换到分析页并选中该任务（由 MainWindow 处理，页面之间不互相引用）
    history_requested = pyqtSignal(str)

    def __init__(self, db, manager: MonitorManager, services: UiServices, parent=None, *,
                 hotkey_text: str = ''):
        """初始化页面

        Args:
            db: 数据库实例（本页面所有数据读写都经由 manager 完成，不直接使用；
                与其他页面保持统一的构造签名）
            manager: 监控管理器
            services: 界面服务（提示、对话框、时钟、进程查询、用户偏好）
            parent: 父窗口
            hotkey_text: 全局打点快捷键（空状态引导里提到；为空表示不可用）
        """
        super().__init__(PAGE_TITLE, PAGE_SUBTITLE, parent)
        self.setObjectName("monitorPage")

        self.db = db
        self.manager = manager
        self.services = services

        # 页头命令区：配额摘要 · [刷新进程]
        self.summary_label = CaptionLabel("", self.container)
        self.add_header_widget(self.summary_label)
        self.command_layout.setAlignment(self.summary_label, Qt.AlignVCenter)
        self.refresh_button = TransparentToolButton(FluentIcon.SYNC, self.container)
        self.refresh_button.setToolTip("重新读取进程列表（F5）")

        self.form = NewTaskForm(services.process_query, services.preferences, self.container)
        self.refresh_button.setText('')
        self.refresh_button.setFixedWidth(36)
        self.form.top_row.insertWidget(2, self.refresh_button)
        self.add_section(self.form)
        self.board = TaskBoard(self.container, hotkey_text=hotkey_text)
        self.add_section(self.board)
        self.content_layout.addStretch(1)
        self.view_switch = SegmentedWidget(self)
        self.view_switch.addItem('cards', '卡片')
        self.view_switch.addItem('list', '列表')
        self.view_switch.setCurrentItem(cfg.get(cfg.monitor_view))
        self.view_switch.currentItemChanged.connect(self._set_view_mode)
        self.add_header_widget(self.view_switch)
        self.board.set_view_mode(cfg.get(cfg.monitor_view))

        self.board.summary_changed.connect(self.summary_label.setText)
        self.refresh_button.clicked.connect(self.refresh_processes)

        self.presenter = MonitorPresenter(manager, services, self.form, self.board, self, self)
        self.presenter.history_requested.connect(self.history_requested)
        from ui.presenters.plan_flow import PlanFlow
        self.plan_flow = PlanFlow(db, self.presenter, self.form, services, self)
        self.board.export_selected_requested.connect(self.export_selected)
        self.board.close_selected_requested.connect(self.close_selected)
        manager.task_started.connect(self._guide_started)
        manager.markers_changed.connect(self._guide_baseline)
        self._guide_request = None
        self._guide_timer = QTimer(self)
        self._guide_timer.setSingleShot(True)
        self._guide_timer.timeout.connect(self._show_pending_guide)
        self.summary_label.setText(self.board.summary())

        self._shortcuts: List[QShortcut] = []
        self._add_shortcut("F5", self.refresh_processes)
        self._add_shortcut("Ctrl+F", self.form.picker.focus_search)

        # 进程列表一律在后台线程读取（psutil 冷启动遍历约 2 秒，不能推迟首屏）；
        # 读取期间刷新按钮置灰，结束后恢复
        self.form.picker.set_thread_registry(services.threads)
        self.form.picker.loading_changed.connect(self._on_process_list_loading)
        # 启动时发起一次读取（不默认选中任何进程）；直接输入 PID 不必等列表
        self.form.picker.refresh()

    def _set_view_mode(self, mode):
        self.board.set_view_mode(mode)
        update_values([(cfg.monitor_view, mode)])

    def export_selected(self):
        ids = self.board.selected_finished_ids()
        if not ids:
            self.services.notifier.info(self, '请选择任务', '勾选已结束任务后再批量导出')
            return
        directory = self.services.dialogs.ask_directory(
            self, '导出选中任务的明细 CSV', cfg.get(cfg.default_export_dir))
        if directory:
            for task_id in ids:
                self.presenter.on_export_requested(task_id, directory)

    def close_selected(self):
        for task_id in self.board.selected_finished_ids():
            self.presenter.on_close(task_id)

    def _guide_started(self, task_id):
        self._guide_request = task_id, False
        self._guide_timer.start(0)

    def _guide_baseline(self, task_id):
        if not cfg.get(cfg.guide_baseline) and self.manager.get_baseline(task_id) is not None:
            self._guide_request = task_id, True
            self._guide_timer.start(0)

    def _show_pending_guide(self):
        if self._guide_request is not None:
            request, self._guide_request = self._guide_request, None
            self._show_guide(*request)

    def _show_guide(self, task_id, baseline):
        item = cfg.guide_baseline if baseline else cfg.guide_started
        card = self.board.card(task_id)
        if cfg.get(item) or not self.isVisible() or card is None or card.is_finished:
            return
        target = card.analysis_button if baseline else card.marker_button
        if not target.isVisible():
            target = self.view_switch
        TeachingTip.create(target, '基线已记录' if baseline else '开始记录了',
                           '每完成一轮操作再打一个点，到分析页查看每轮变化。' if baseline else
                           '让被测程序预热至稳定，再点“打点”记录基线。',
                           icon=FluentIcon.INFO, duration=6000, parent=self)
        update_values([(item, True)])

    # ========== 进程列表 ==========

    def refresh_processes(self) -> None:
        """重新读取进程列表（页头按钮 / F5）：后台读取，进行中再按只合并为一次补读。"""
        self.form.picker.refresh()

    def _on_process_list_loading(self, loading: bool) -> None:
        self.refresh_button.setEnabled(not loading)

    def showEvent(self, event):
        super().showEvent(event)
        self._enable_shortcuts(True)
        self.form.picker.refresh_if_stale()

    def hideEvent(self, event):
        self._enable_shortcuts(False)
        super().hideEvent(event)

    def changeEvent(self, event):
        # 用户从被测程序切回本窗口：被测进程多半刚启动，顺手刷新进程列表
        super().changeEvent(event)
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and self.isVisible()):
            self.form.picker.refresh_if_stale()

    # ========== 快捷键 ==========

    def _add_shortcut(self, key: str, slot) -> QShortcut:
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.WindowShortcut)
        shortcut.activated.connect(slot)
        shortcut.setEnabled(False)
        self._shortcuts.append(shortcut)
        return shortcut

    def _enable_shortcuts(self, enabled: bool) -> None:
        # 本页隐藏时停用：分析页等可能也用 F5，同时启用会互相冲突（谁都不触发）
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)

    def shortcut_keys(self) -> List[str]:
        """本页注册的快捷键（测试与帮助文案用）。"""
        return [s.key().toString() for s in self._shortcuts]

    # ========== 外部设置 ==========

    def set_default_interval(self, seconds: int) -> None:
        """设置页"默认采集间隔"变更时由主窗口调用：同步到新建表单（不影响已创建的任务）。"""
        self.form.set_default_interval(seconds)

    def set_hotkey_text(self, hotkey_text: str) -> None:
        """全局打点快捷键变了（或不可用）：同步到空状态引导。"""
        self.board.set_hotkey_text(hotkey_text)
