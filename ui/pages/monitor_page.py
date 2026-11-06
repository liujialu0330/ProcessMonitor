"""
实时监控页面

只做组装：页面骨架 + 新建任务表单（NewTaskForm）+ 任务面板（TaskBoard），业务流程
（开始/停止/打点/重新监控、manager 信号响应、提示与确认框）全部在 MonitorPresenter。
"""
from PyQt5.QtCore import pyqtSignal

from core.monitor_manager import MonitorManager
from ui.presenters.monitor_presenter import MonitorPresenter
from ui.services import UiServices
from ui.widgets.monitor.new_task_form import NewTaskForm
from ui.widgets.monitor.task_board import TaskBoard
from ui.widgets.page_scaffold import PageScaffold

PAGE_TITLE = "实时监控"
PAGE_SUBTITLE = "选择进程和指标，快速创建实时采集任务"


class MonitorPage(PageScaffold):
    """实时监控页面"""

    # 请求切换到历史页并选中该任务（由 MainWindow 处理，页面之间不互相引用）
    history_requested = pyqtSignal(str)
    # 被测进程自行结束（不是用户点停止）：(标题, 内容)。主窗口隐藏/不在前台时据此弹托盘气泡
    task_ended_abnormally = pyqtSignal(str, str)

    def __init__(self, db, manager: MonitorManager, services: UiServices, parent=None):
        """初始化页面

        Args:
            db: 数据库实例（本页面所有数据读写都经由 manager 完成，不直接使用；
                与其他页面保持统一的构造签名）
            manager: 监控管理器
            services: 界面服务（提示、对话框、时钟、进程查询、用户偏好）
            parent: 父窗口
        """
        super().__init__(PAGE_TITLE, PAGE_SUBTITLE, parent)
        self.setObjectName("monitorPage")

        self.db = db
        self.manager = manager
        self.services = services

        self.form = NewTaskForm(services.process_query, services.preferences, self.container)
        self.add_section(self.form)
        self.board = TaskBoard(self.container)
        self.add_section(self.board)
        self.content_layout.addStretch(1)

        self.presenter = MonitorPresenter(manager, services, self.form, self.board, self, self)
        self.presenter.history_requested.connect(self.history_requested)
        self.presenter.task_ended_abnormally.connect(self.task_ended_abnormally)

        # 启动时加载一次进程列表（不默认选中任何进程）
        self.form.picker.refresh()

    def set_default_interval(self, seconds: int) -> None:
        """设置页"默认采集周期"变更时由主窗口调用：同步到新建表单（不影响已创建的任务）。"""
        self.form.set_default_interval(seconds)
