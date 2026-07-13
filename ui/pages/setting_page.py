"""
设置页面
将主题、默认采集周期和托盘行为收敛为「常规」，保留「数据」作为独立分组。
"""
import logging
import os

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import (
    ScrollArea, ExpandLayout, SettingCardGroup, OptionsSettingCard,
    PushSettingCard, SwitchSettingCard, MessageBox, StateToolTip,
    FluentIcon as FIF, setTheme, qconfig, Theme
)

from app_config import cfg
from ui.components.spinbox_setting_card import SpinBoxSettingCard
from ui.typography import PageTitleLabel
import config

logger = logging.getLogger(__name__)


def _format_bytes(num_bytes: int) -> str:
    """把字节数格式化为易读文本（自动选择 B/KB/MB/GB/TB，KB 及以上保留 1 位小数）"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class _CleanupWorker(QThread):
    """"清理并压缩数据库"后台线程（v1.3.0 批4）：按保留天数删除过期任务
    （retention_days>0 时）再执行 VACUUM，避免大库操作阻塞 UI 线程。

    异常处理沿用 Database 层"静默失败"约定（cleanup_old_tasks/vacuum 内部已
    自行捕获异常并记日志，不向上抛出）；这里的 try/except/finally 是额外一层
    防御，确保无论如何都会 emit finished_ok，避免 StateToolTip 卡在"进行中"
    不再收尾。
    """

    finished_ok = pyqtSignal(int)  # 参数：被删除的任务数（未执行清理时为0）

    def __init__(self, db, retention_days: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.retention_days = retention_days

    def run(self):
        deleted = 0
        try:
            if self.retention_days > 0:
                deleted = self.db.cleanup_old_tasks(self.retention_days)
            self.db.vacuum()
        except Exception:
            logger.error("清理并压缩数据库失败", exc_info=True)
        finally:
            self.finished_ok.emit(deleted)


class SettingPage(ScrollArea):
    """设置页面"""

    def __init__(self, parent=None, db=None, manager=None):
        """初始化页面

        Args:
            parent: 父窗口
            db: 数据库实例（可选；生产路径由 MainWindow 注入，用于"数据管理"
                卡片查询占用大小与执行清理压缩。为 None 时该卡片显示"数据库
                未就绪"并禁用，不影响其余分组正常使用——保持独立构造的可测性）
            manager: MonitorManager 实例（可选；生产路径由 MainWindow 注入，
                仅用于只读查询是否有运行中任务，决定"清理并压缩数据库"是否
                可点击。本页面不直接操作任务，遵守分层）
        """
        super().__init__(parent)
        self.setObjectName("settingPage")
        self.db = db
        self.manager = manager
        self._cleanup_worker: _CleanupWorker = None
        self._state_tooltip: StateToolTip = None

        # 承载各设置分组的滚动内容容器
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self._init_appearance_group()
        self._init_monitor_group()
        self._init_data_group()
        self._init_behavior_group()

        self._init_layout()
        self._connect_signals()

        # 构造时先刷新一次，避免在首次 showEvent 触发前卡片停留在占位文案
        self._refresh_data_management_state()

    def _init_appearance_group(self):
        """常规分组：外观、监控默认值与窗口行为共享一个容器。"""
        self.general_group = SettingCardGroup("常规", self.scroll_widget)
        # 保留原有属性，兼容已有的 GUI 冒烟与外部读取。
        self.appearance_group = self.general_group
        self.theme_card = OptionsSettingCard(
            cfg.themeMode, FIF.BRUSH, "应用主题", "选择浅色、深色或跟随系统",
            texts=["浅色", "深色", "跟随系统"], parent=self.general_group)
        self.general_group.addSettingCard(self.theme_card)

    def _init_monitor_group(self):
        """常规分组：默认采集周期。"""
        self.monitor_group = self.general_group
        self.default_interval_card = SpinBoxSettingCard(
            cfg.default_interval, FIF.STOP_WATCH, "默认采集周期",
            "用于新建任务，创建时仍可调整",
            parent=self.general_group, suffix=" 秒")
        self.general_group.addSettingCard(self.default_interval_card)

    def _init_data_group(self):
        """数据分组：历史数据保留策略 + 数据管理（v1.3.0 批4：清理压缩、打开
        数据目录）"""
        self.data_group = SettingCardGroup("数据", self.scroll_widget)
        self.retention_card = OptionsSettingCard(
            cfg.retention_days, FIF.DELETE, "历史数据保留",
            "启动时清理超过保留期限的已停止任务",
            texts=["永久保留", "7 天", "30 天", "90 天", "180 天"],
            parent=self.data_group)
        self.data_group.addSettingCard(self.retention_card)

        # 清理并压缩数据库：content 显示当前占用，随 showEvent 刷新
        # （见 _refresh_data_management_state）；运行中有任务时按钮禁用
        self.cleanup_card = PushSettingCard(
            "立即清理", FIF.BROOM, "清理并压缩数据库", "当前占用 —",
            parent=self.data_group)
        self.data_group.addSettingCard(self.cleanup_card)

        # 长路径收敛为末两级，完整值放在 tooltip，避免卡片文字挤压按钮。
        normalized_path = os.path.normpath(config.DATA_DIR)
        parent_name = os.path.basename(os.path.dirname(normalized_path))
        dir_name = os.path.basename(normalized_path)
        compact_path = os.path.join("…", parent_name, dir_name)
        self.open_dir_card = PushSettingCard(
            "打开", FIF.FOLDER, "数据目录", compact_path, parent=self.data_group)
        self.open_dir_card.contentLabel.setToolTip(normalized_path)
        self.open_dir_card.button.setToolTip(normalized_path)
        self.data_group.addSettingCard(self.open_dir_card)

    def _init_behavior_group(self):
        """常规分组：关闭窗口时是否最小化到系统托盘。"""
        self.behavior_group = self.general_group
        self.close_to_tray_card = SwitchSettingCard(
            FIF.MINIMIZE, "关闭到系统托盘",
            "关闭窗口后继续采集，可从托盘菜单退出",
            configItem=cfg.close_to_tray, parent=self.general_group)
        self.general_group.addSettingCard(self.close_to_tray_card)

    def _init_layout(self):
        """整体布局：ScrollArea + ExpandLayout 承载各分组

        对齐 qfluentwidgets gallery 设置页惯例，选用 ExpandLayout 而非普通
        QVBoxLayout：外观分组里的"应用主题"、数据分组里的"历史数据保留"都是
        OptionsSettingCard——它继承自 ExpandSettingCard，点击可展开出一列单选
        按钮。展开时子控件靠手动 resize() 变高，只有 ExpandLayout 会通过
        installEventFilter 捕获这个 resize 事件并逐层级联调整外层容器高度，
        普通 QVBoxLayout 感知不到这种手动 resize，会导致展开内容被裁切或与
        下方分组重叠。
        """
        self.expand_layout.setSpacing(20)
        self.expand_layout.setContentsMargins(30, 24, 30, 24)
        self.page_title = PageTitleLabel("设置", self.scroll_widget)
        self.expand_layout.addWidget(self.page_title)
        self.expand_layout.addWidget(self.appearance_group)
        self.expand_layout.addWidget(self.data_group)

        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 透明背景：与其余页面的视觉风格保持一致，让 FluentWindow 的背景效果透出
        self.enableTransparentBackground()
        self.viewport().setStyleSheet("background: transparent")

    def _connect_signals(self):
        """主题切换即时生效 + 数据管理卡片交互

        qconfig.set()（OptionsSettingCard 点击单选按钮时内部调用）只更新配置
        状态并发信号，不会主动刷新已创建控件的样式表，需要显式调用 setTheme()
        触发 updateStyleSheet() 才能让已存在的窗口部件立刻改观。

        这里连接的是 qconfig.themeChanged 而不是 cfg.themeMode.valueChanged，
        两者看似都能感知到主题变化，但触发时机不同：QConfig.set() 内部顺序是
        "先 emit themeMode.valueChanged，几行之后才解析 AUTO 并更新 self.theme、
        再 emit themeChanged"——若挂在 valueChanged 上，回调里再调用 setTheme()
        时 self.theme 还没被更新到位，updateStyleSheet() 会用旧主题重绘一次；
        挂在 themeChanged 上时 self.theme 已经是最新解析结果，一步到位。已用
        独立脚本实测核实这套连接不会死循环（setTheme 内部再次 qconfig.set 时
        值已相等会短路返回，调用链长度恒为 1）。
        """
        qconfig.themeChanged.connect(self._on_theme_changed_apply)
        self.cleanup_card.clicked.connect(self._on_cleanup_clicked)
        self.open_dir_card.clicked.connect(self._on_open_data_dir_clicked)

    def _on_theme_changed_apply(self, theme: Theme) -> None:
        """主题变化回调：用绑定方法而非 lambda 连接全局 qconfig 信号，连接可随
        页面对象销毁自动断开——lambda 会在全局单例上留下无法回收的连接，多次
        构造页面（如测试场景）时逐渐累积"""
        setTheme(theme)

    def showEvent(self, event):
        """页面显示事件：每次进入设置页刷新数据库占用与清理按钮可用状态
        （运行中任务数量可能在离开设置页期间发生变化）"""
        super().showEvent(event)
        self._refresh_data_management_state()

    # ========== 数据管理（v1.3.0 批4） ==========

    def _refresh_data_management_state(self) -> None:
        """刷新"清理并压缩数据库"卡片的占用文案与可用状态"""
        if self.db is None:
            self.cleanup_card.contentLabel.setText("数据库未就绪")
            self.cleanup_card.button.setEnabled(False)
            return

        size_text = _format_bytes(self.db.get_db_size_bytes())
        self.cleanup_card.contentLabel.setText(f"当前占用 {size_text}")

        has_running = bool(self.manager and self.manager.get_running_tasks())
        self.cleanup_card.button.setEnabled(not has_running)
        self.cleanup_card.button.setToolTip("请先停止全部监控任务" if has_running else "")

    def _on_open_data_dir_clicked(self) -> None:
        """"数据目录"卡片点击：用系统资源管理器打开数据目录"""
        os.startfile(config.DATA_DIR)

    def _on_cleanup_clicked(self) -> None:
        """"清理并压缩数据库"卡片点击：按当前保留策略弹确认文案，确认后在
        后台线程执行清理+压缩，StateToolTip 反馈进行中/完成"""
        if self.db is None:
            return
        if self._cleanup_worker is not None and self._cleanup_worker.isRunning():
            return

        days = cfg.get(cfg.retention_days)
        if days > 0:
            content = f"将删除超过 {days} 天的已停止任务数据并压缩数据库文件，此操作不可撤销。"
        else:
            content = "当前为永久保留，仅压缩数据库文件，不会删除数据。"

        box = MessageBox("清理并压缩数据库", content, self.window())
        if not box.exec():
            return

        self.cleanup_card.button.setEnabled(False)
        self._state_tooltip = StateToolTip(
            "正在清理数据", "正在压缩数据库，请稍候…", self.window())
        self._state_tooltip.move(self._state_tooltip.getSuitablePos())
        self._state_tooltip.show()

        self._cleanup_worker = _CleanupWorker(self.db, days, self)
        self._cleanup_worker.finished_ok.connect(self._on_cleanup_finished)
        self._cleanup_worker.start()

    def _on_cleanup_finished(self, deleted: int) -> None:
        """清理线程完成：StateToolTip 收尾，刷新占用显示与按钮可用状态，并
        释放线程引用"""
        if self._state_tooltip is not None:
            self._state_tooltip.setContent("数据库清理完成")
            self._state_tooltip.setState(True)
            self._state_tooltip = None
        self._refresh_data_management_state()
        self._release_cleanup_worker()

    def _release_cleanup_worker(self) -> None:
        """清理线程已结束后释放引用（deleteLater 交还 Qt 对象树），对齐导出
        页 _release_export_worker 的同一模式，避免每次点击都新建一个线程对象
        却始终作为 self 的子对象常驻"""
        if self._cleanup_worker is not None:
            self._cleanup_worker.deleteLater()
            self._cleanup_worker = None
