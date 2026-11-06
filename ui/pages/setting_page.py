"""
设置页面

三个分组：
- 常规：应用主题 · 默认采集间隔 · 关闭行为 · 打点快捷键
- 数据：历史数据保留 · 压缩数据库（按保留策略决定是否同时清理过期任务）· 数据目录
- 关于与更新：版本与项目主页 · 检查更新（原"关于"页并入，见 ui/pages/about_update）
"""
import logging
import os
from typing import TYPE_CHECKING, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import (
    ScrollArea, ExpandLayout, SettingCardGroup, OptionsSettingCard,
    PushSettingCard, SettingCard, StateToolTip, LineEdit, TransparentToolButton,
    FluentIcon as FIF, setTheme, qconfig, Theme
)

from app_config import cfg
from core.maintenance_service import (
    BUSY_EXPORT_RUNNING, BUSY_MAINTENANCE_RUNNING, BUSY_TASKS_RUNNING,
    MaintenanceBusy, MaintenanceService,
)
from core.workers.cleanup_worker import CleanupWorker
from data.database import Database
from ui.components.spinbox_setting_card import SpinBoxSettingCard
from ui.hotkey import (
    DEFAULT_MARKER_HOTKEY, STATE_INVALID, MarkerHotkey, hotkey_status_text, normalize_hotkey,
)
from ui.pages.about_update import AboutUpdateGroup
from ui.services import UiServices
from ui.typography import PageTitleLabel
from ui.widgets.qfw_compat import CnSwitchSettingCard
import config

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager

logger = logging.getLogger(__name__)

# 数据库维护按钮：永久保留时只压缩、不删数据，按钮直说"压缩数据库"
COMPACT_TEXT = "压缩数据库"
CLEANUP_TEXT = "清理并压缩"
HOTKEY_PLACEHOLDER = "点击后按下组合键"


def _retention_text(days: int) -> str:
    """保留天数选项的显示文案（0 = 永久保留）"""
    return "永久保留" if days <= 0 else f"{days} 天"


def maintenance_button_text(days: int) -> str:
    """数据库维护按钮文案：永久保留 → "压缩数据库"；有保留期限 → "清理并压缩"。"""
    return COMPACT_TEXT if days <= 0 else CLEANUP_TEXT


def _format_bytes(num_bytes: int) -> str:
    """把字节数格式化为易读文本（自动选择 B/KB/MB/GB/TB，KB 及以上保留 1 位小数）"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# 清理被拒绝的原因（MaintenanceBusy.reason）→ 提示文案
_BUSY_REASON_TEXTS = {
    BUSY_TASKS_RUNNING: "有监控任务正在运行，请先停止全部任务再清理数据库",
    # 导出线程长时间读库，与压缩数据库互相抢锁
    BUSY_EXPORT_RUNNING: "正在导出数据，请等导出完成后再清理数据库",
    BUSY_MAINTENANCE_RUNNING: "数据库正在清理中，请稍候",
}
_BUSY_FALLBACK_TEXT = "数据库正忙，请稍后再清理"

# 修饰键（单独按下时不算完成输入）
_MODIFIER_KEYS = {Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta,
                  Qt.Key_AltGr, Qt.Key_Super_L, Qt.Key_Super_R}


def key_event_text(key: int, modifiers) -> str:
    """把一次按键（主键 + 修饰键）写成 "Ctrl+Alt+M"；只按了修饰键时返回空串。"""
    if key in _MODIFIER_KEYS or key == Qt.Key_unknown:
        return ''
    parts = []
    if modifiers & Qt.ControlModifier:
        parts.append("Ctrl")
    if modifiers & Qt.AltModifier:
        parts.append("Alt")
    if modifiers & Qt.ShiftModifier:
        parts.append("Shift")
    if modifiers & Qt.MetaModifier:
        parts.append("Win")
    parts.append(QKeySequence(key).toString())
    return "+".join(parts)


class HotkeyEdit(LineEdit):
    """按键录入框：获得焦点后按下组合键即录入（不需要手打 "Ctrl+Alt+M"）。

    Esc 放弃本次录入；不带修饰键的 Backspace / Delete 清空（= 不使用快捷键）。
    """

    captured = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText(HOTKEY_PLACEHOLDER)
        self.setFixedWidth(160)
        self.setAlignment(Qt.AlignCenter)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        bare = not modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and bare:
            # 留给焦点切换：键盘用户要能离开录入框
            super().keyPressEvent(event)
            return
        if key == Qt.Key_Escape and bare:
            self.clearFocus()
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete) and bare:
            self.captured.emit('')
            return
        text = key_event_text(key, modifiers)
        if text:
            self.captured.emit(text)
        event.accept()


class SettingPage(ScrollArea):
    """设置页面"""

    def __init__(self, db: Database, manager: "MonitorManager",
                 services: UiServices, parent=None, *,
                 maintenance: Optional[MaintenanceService] = None,
                 hotkey: Optional[MarkerHotkey] = None):
        """初始化页面

        设置卡片直接绑定 cfg 的 ConfigItem（卡片自身负责读写与落盘）；
        业务上要用到的偏好（如清理时的保留天数、打点快捷键）经由 services.preferences 读写。

        Args:
            db: 数据库实例："压缩数据库"卡片查询占用大小与执行清理压缩
            manager: 监控管理器：只读查询是否有运行中任务/导出（维护按钮可用状态、
                点击时的预检）。维护锁经 MaintenanceService 的 begin()/end() 占用与
                释放，本页面不直接操作任务与维护状态，遵守分层
            services: 界面服务（提示、确认框、打开文件夹、用户偏好、线程登记表、时钟）
            parent: 父窗口
            maintenance: 数据库维护服务；未给出时按 db 与 manager 组装一个（它只是
                二者之上的无状态薄层）
            hotkey: 全局打点快捷键服务；没有时（测试、未接线）快捷键卡片显示"快捷键不可用"，
                改键只记入偏好
        """
        super().__init__(parent)
        self.setObjectName("settingPage")
        self.db = db
        self.manager = manager
        self.services = services
        self.hotkey = hotkey
        if maintenance is None:
            maintenance = MaintenanceService(db, manager)
        self.maintenance = maintenance
        self._cleanup_worker: Optional[CleanupWorker] = None
        self._state_tooltip: StateToolTip = None

        # 承载各设置分组的滚动内容容器
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self._init_general_group()
        self._init_data_group()
        self.about_group = AboutUpdateGroup(services, self.scroll_widget)

        self._init_layout()
        self._connect_signals()

        # 构造时先刷新一次，避免在首次 showEvent 触发前卡片停留在占位文案
        self._refresh_data_management_state()
        self._refresh_hotkey_card()

    # ========== 分组 ==========

    def _init_general_group(self):
        """常规：主题 · 默认采集间隔 · 关闭行为 · 打点快捷键"""
        self.general_group = SettingCardGroup("常规", self.scroll_widget)
        # 旧分组名都指向"常规"：外部读取与冒烟测试仍用这些属性
        self.appearance_group = self.general_group
        self.monitor_group = self.general_group
        self.behavior_group = self.general_group

        self.theme_card = OptionsSettingCard(
            cfg.themeMode, FIF.BRUSH, "应用主题", "选择浅色、深色或跟随系统",
            texts=["浅色", "深色", "跟随系统"], parent=self.general_group)
        self.general_group.addSettingCard(self.theme_card)

        self.default_interval_card = SpinBoxSettingCard(
            cfg.default_interval, FIF.STOP_WATCH, "默认采集间隔",
            "新建任务时的初始值，创建时仍可调整",
            parent=self.general_group, suffix=" 秒")
        self.general_group.addSettingCard(self.default_interval_card)

        self.close_to_tray_card = CnSwitchSettingCard(
            FIF.MINIMIZE, "关闭窗口时最小化到托盘",
            "开启后关闭窗口仍继续采集，从托盘菜单“退出”才真正结束",
            configItem=cfg.close_to_tray, parent=self.general_group)
        self.general_group.addSettingCard(self.close_to_tray_card)

        # 打点快捷键：录入框 + 恢复默认；说明行显示当前状态（可用 / 不可用的原因）
        self.hotkey_card = SettingCard(
            FIF.TAG, "打点快捷键", hotkey_status_text(None), parent=self.general_group)
        self.hotkey_edit = HotkeyEdit(self.hotkey_card)
        self.hotkey_edit.setToolTip("点击后按下新的组合键；Backspace 清空表示不使用；Esc 放弃")
        self.hotkey_reset_button = TransparentToolButton(FIF.RETURN, self.hotkey_card)
        self.hotkey_reset_button.setToolTip(f"恢复默认（{DEFAULT_MARKER_HOTKEY}）")
        self.hotkey_card.hBoxLayout.addWidget(self.hotkey_edit, 0, Qt.AlignRight)
        self.hotkey_card.hBoxLayout.addSpacing(4)
        self.hotkey_card.hBoxLayout.addWidget(self.hotkey_reset_button, 0, Qt.AlignRight)
        self.hotkey_card.hBoxLayout.addSpacing(16)
        self.general_group.addSettingCard(self.hotkey_card)

    def _init_data_group(self):
        """数据：历史数据保留 · 压缩数据库 · 数据目录"""
        self.data_group = SettingCardGroup("数据", self.scroll_widget)
        # 选项文案与 ConfigItem 校验器的可选值逐一对应：可选值只在应用配置里定义一份
        retention_texts = [_retention_text(days)
                           for days in self.services.preferences.retention_options()]
        self.retention_card = OptionsSettingCard(
            cfg.retention_days, FIF.DELETE, "历史数据保留",
            "启动时清理超过保留期限的已结束任务",
            texts=retention_texts, parent=self.data_group)
        self.data_group.addSettingCard(self.retention_card)

        # 数据库维护：content 显示当前占用；运行中有任务时按钮禁用，
        # 真正的把关在确认后、启动线程前的 MaintenanceService.begin()
        self.cleanup_card = PushSettingCard(
            COMPACT_TEXT, FIF.BROOM, "数据库文件", "当前占用 —",
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

    def _init_layout(self):
        """整体布局：ScrollArea + ExpandLayout 承载各分组

        对齐 qfluentwidgets gallery 设置页惯例，选用 ExpandLayout 而非普通
        QVBoxLayout："应用主题"、"历史数据保留"都是 OptionsSettingCard——它继承自
        ExpandSettingCard，点击可展开出一列单选按钮。展开时子控件靠手动 resize()
        变高，只有 ExpandLayout 会通过 installEventFilter 捕获这个 resize 事件并逐层
        级联调整外层容器高度，普通 QVBoxLayout 感知不到这种手动 resize，会导致展开
        内容被裁切或与下方分组重叠。
        """
        self.expand_layout.setSpacing(24)
        self.expand_layout.setContentsMargins(24, 24, 24, 24)
        self.page_title = PageTitleLabel("设置", self.scroll_widget)
        self.expand_layout.addWidget(self.page_title)
        self.expand_layout.addWidget(self.general_group)
        self.expand_layout.addWidget(self.data_group)
        self.expand_layout.addWidget(self.about_group)

        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 透明背景：与其余页面的视觉风格保持一致，让 FluentWindow 的背景效果透出
        self.enableTransparentBackground()
        self.viewport().setStyleSheet("background: transparent")

    def _connect_signals(self):
        """主题切换即时生效 + 数据管理卡片交互 + 快捷键录入

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
        # 保留策略一改，维护按钮文案（压缩 / 清理并压缩）随之变化
        cfg.retention_days.valueChanged.connect(self._on_retention_changed)
        # 任务启停时刷新维护按钮的可用状态（不再只依赖页面显示事件）
        self.manager.task_started.connect(self._on_task_state_changed)
        self.manager.task_stopped.connect(self._on_task_state_changed)
        self.manager.task_removed.connect(self._on_task_state_changed)

        self.hotkey_edit.captured.connect(self.apply_hotkey)
        self.hotkey_reset_button.clicked.connect(self._on_hotkey_reset_clicked)
        if self.hotkey is not None:
            self.hotkey.status_changed.connect(self._refresh_hotkey_card)

    def _on_theme_changed_apply(self, theme: Theme) -> None:
        """主题变化回调：用绑定方法而非 lambda 连接全局 qconfig 信号，连接可随
        页面对象销毁自动断开——lambda 会在全局单例上留下无法回收的连接，多次
        构造页面（如测试场景）时逐渐累积"""
        setTheme(theme)

    def showEvent(self, event):
        """页面显示事件：每次进入设置页刷新数据库占用与维护按钮可用状态"""
        super().showEvent(event)
        self._refresh_data_management_state()

    def _on_task_state_changed(self, *_args) -> None:
        """任务启动/停止/移除：刷新维护按钮可用状态"""
        self._refresh_data_management_state()

    def _on_retention_changed(self, *_args) -> None:
        self._refresh_data_management_state()

    def scroll_to_about(self) -> None:
        """滚动到"关于与更新"分组（托盘/提示条"查看更新"跳转过来时用）。"""
        self.verticalScrollBar().setValue(self.about_group.y())

    # ========== 打点快捷键 ==========

    def apply_hotkey(self, text: str) -> None:
        """录入了新的快捷键：写法合法才记入偏好并重新注册；不合法只在说明行提示原因。"""
        normalized = normalize_hotkey(text)
        if normalized is None:
            self.hotkey_card.setContent(hotkey_status_text(STATE_INVALID, text))
            return
        self.services.preferences.set_marker_hotkey(normalized)
        self.hotkey_edit.setText(normalized)
        if self.hotkey is not None:
            self.hotkey.bind(normalized)
        self._refresh_hotkey_card()

    def _on_hotkey_reset_clicked(self) -> None:
        self.apply_hotkey(DEFAULT_MARKER_HOTKEY)

    def _refresh_hotkey_card(self) -> None:
        """按当前设定与注册结果刷新录入框和说明行。"""
        if self.hotkey is not None:
            text = self.hotkey.hotkey_text()
            status = self.hotkey.status_text()
        else:
            text = normalize_hotkey(self.services.preferences.marker_hotkey()) or ''
            status = hotkey_status_text(None)
        self.hotkey_edit.setText(text)
        self.hotkey_card.setContent(status)

    def hotkey_status(self) -> str:
        """快捷键卡片当前的说明文案（测试用）。"""
        return self.hotkey_card.contentLabel.text()

    # ========== 数据管理 ==========

    def _is_cleanup_running(self) -> bool:
        """清理线程是否仍在运行"""
        return self._cleanup_worker is not None and self._cleanup_worker.isRunning()

    def _refresh_data_management_state(self) -> None:
        """刷新数据库维护卡片的按钮文案、占用说明与可用状态"""
        days = self.services.preferences.retention_days()
        self.cleanup_card.button.setText(maintenance_button_text(days))

        size_text = _format_bytes(self.db.get_db_size_bytes())
        has_running = self.manager.has_running_tasks()
        # 按钮禁用的原因直接写在说明里：禁用按钮上的 tooltip 很难被发现
        suffix = " · 有任务在监控，停止后可清理" if has_running else ""
        self.cleanup_card.contentLabel.setText(f"当前占用 {size_text}{suffix}")

        # 清理进行中保持禁用，不因任务状态信号被误启用
        self.cleanup_card.button.setEnabled(
            not has_running and not self._is_cleanup_running())
        self.cleanup_card.button.setToolTip("请先停止全部监控任务" if has_running else "")

    def _on_open_data_dir_clicked(self) -> None:
        """"数据目录"卡片点击：用系统资源管理器打开数据目录（失败给出提示）"""
        try:
            self.services.dialogs.open_folder(config.DATA_DIR)
        except OSError as e:
            self.services.notifier.error(self.window(), "无法打开数据目录", str(e))

    def _confirm_cleanup(self, days: int) -> bool:
        """维护前的确认框：标题与确认按钮跟随保留策略（是否会删除数据）"""
        if days > 0:
            title = "清理并压缩数据库"
            content = f"将删除超过 {days} 天的已结束任务数据并压缩数据库文件，此操作不可撤销。"
            confirm_text = "清理"
        else:
            title = COMPACT_TEXT
            content = "当前为永久保留，仅压缩数据库文件，不会删除数据。"
            confirm_text = "压缩"
        return self.services.dialogs.confirm(self.window(), title, content, confirm_text)

    def _busy_reason(self) -> Optional[str]:
        """点击时的只读预检：当前是否会被维护锁拒绝（与 MaintenanceBusy.reason 同口径）"""
        if self.manager.has_running_tasks():
            return BUSY_TASKS_RUNNING
        if self.manager.has_running_exports():
            return BUSY_EXPORT_RUNNING
        return None

    def _warn_busy(self, reason: str) -> None:
        """维护被拒绝：按原因提示"""
        self.services.notifier.warning(
            self.window(), "无法清理",
            _BUSY_REASON_TEXTS.get(reason, _BUSY_FALLBACK_TEXT), duration=4000)
        self._refresh_data_management_state()

    def _on_cleanup_clicked(self) -> None:
        """数据库维护按钮点击：先预检（有运行中任务或导出时直接提示，不弹确认），
        再按当前保留策略确认。确认后在界面线程占用维护锁（确认期间若有任务或导出
        开始，这里被拒并按原因提示，不启动线程），占锁成功才在后台线程执行清理+压缩，
        StateToolTip 反馈进行中/完成；线程结束（无论成败）后释放维护锁。

        占锁放在界面线程：开始监控/开始导出的判断也在界面线程，二者串行，不存在
        "确认之后、后台线程真正占锁之前"任务或导出趁机开始的窗口。"""
        if self._is_cleanup_running():
            return

        reason = self._busy_reason()
        if reason is not None:
            self._warn_busy(reason)
            return

        days = self.services.preferences.retention_days()
        if not self._confirm_cleanup(days):
            return

        try:
            self.maintenance.begin()
        except MaintenanceBusy as busy:
            logger.info("清理数据库被拒绝: %s", busy.reason)
            self._warn_busy(busy.reason)
            return

        try:
            worker = CleanupWorker(self.maintenance, days, already_locked=True)
            worker.result.connect(self._on_cleanup_finished)
            worker.busy.connect(self._on_cleanup_busy)
            worker.failed.connect(self._on_cleanup_failed)
            # 释放引用与维护锁挂在 QThread.finished 上（结果信号是 run() 的最后一个
            # 动作，但此时线程尚未真正结束）；线程对象的 deleteLater 由登记表负责。
            # VACUUM 中途打断会让数据库停在中间态，因此不给取消回调，退出时给较长的
            # 等待超时
            worker.finished.connect(lambda w=worker: self._on_cleanup_thread_finished(w))
            self.services.threads.register(worker, name="清理数据库", timeout_ms=5000)
            self._cleanup_worker = worker
            worker.start()
        except Exception:
            # 线程没能启动就不会有 finished，锁必须在这里释放
            self._cleanup_worker = None
            self.maintenance.end()
            raise

        self.cleanup_card.button.setEnabled(False)
        self._state_tooltip = StateToolTip(
            "正在清理数据", "正在压缩数据库，请稍候…", self.window())
        self._state_tooltip.move(self._state_tooltip.getSuitablePos())
        self._state_tooltip.show()

    def _close_state_tooltip(self) -> None:
        """关闭"进行中"提示（不显示"完成"）"""
        if self._state_tooltip is not None:
            self._state_tooltip.close()
            self._state_tooltip.deleteLater()
            self._state_tooltip = None

    def _on_cleanup_finished(self, result) -> None:
        """清理线程完成：StateToolTip 收尾，刷新占用显示"""
        if self._state_tooltip is not None:
            self._state_tooltip.setContent("数据库清理完成")
            self._state_tooltip.setState(True)
            self._state_tooltip = None
        self._refresh_data_management_state()

    def _on_cleanup_busy(self, reason: str) -> None:
        """维护服务拒绝清理（只在线程自行占锁时可能发生；两段式下已在界面线程拦截）：
        关闭进行中提示并说明原因"""
        self._close_state_tooltip()
        self._warn_busy(reason)

    def _on_cleanup_failed(self, reason: str) -> None:
        """清理线程失败：StateToolTip 关闭（不显示"完成"），弹出错误提示"""
        self._close_state_tooltip()
        self.services.notifier.error(
            self.window(), "清理失败", f"清理并压缩数据库失败：{reason}")
        self._refresh_data_management_state()

    def _on_cleanup_thread_finished(self, worker) -> None:
        """清理线程真正结束（无论成功、失败）：释放维护锁与引用，刷新按钮可用状态。"""
        self.maintenance.end()
        if self._cleanup_worker is worker:
            self._cleanup_worker = None
        self._refresh_data_management_state()
