"""
设置页面

四个分组：
- 常规：应用主题 · 默认采集间隔 · 关闭行为 · 监控期间阻止睡眠 · 打点快捷键
- 数据：历史数据保留 · 压缩数据库（按保留策略决定是否同时清理过期任务）· 重建分钟汇总 · 数据目录
  · 报告配色（导出的报告图片固定浅色 / 跟随主题）· 诊断信息（复制诊断信息到剪贴板 / 在后台导出 zip 诊断包）
- 导出：文件命名模板（报告、明细 CSV、证据包的默认文件名，带实时预览）
- 关于与更新：版本与项目主页 · 检查更新（原"关于"页并入，见 ui/pages/about_update）
"""
import logging
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import (
    ScrollArea, ExpandLayout, SettingCardGroup, OptionsSettingCard, ComboBoxSettingCard,
    PushSettingCard, SettingCard, StateToolTip, LineEdit, TransparentToolButton, PushButton,
    FluentIcon as FIF, setTheme, qconfig, Theme, Pivot, ComboBox, TransparentPushButton
)

from app_config import (
    DEFAULT_EXPORT_NAME_TEMPLATE, cfg, get_export_name_template, set_export_name_template, update_values,
)
from core.diagnostics import collect_diagnostics, write_diagnostic_bundle
from core.naming_template import (
    available_fields_text, preview_filename, unknown_fields,
)
from core.maintenance_service import (
    BUSY_DELETION_RUNNING, BUSY_EXPORT_RUNNING, BUSY_MAINTENANCE_RUNNING, BUSY_STORAGE_JOB_RUNNING,
    BUSY_TASKS_RUNNING, MaintenanceBusy, MaintenanceService,
)
from core.workers.base import BackgroundTask, CancelToken
from core.workers.cleanup_worker import CleanupWorker
from data.database import Database
from ui.components.spinbox_setting_card import SpinBoxSettingCard
from ui.hotkey import (
    DEFAULT_MARKER_HOTKEY, STATE_INVALID, MarkerHotkey, hotkey_status_text, normalize_hotkey,
)
from ui.pages.about_update import AboutUpdateGroup
from ui.services import UiServices
from ui.storage_notices import INTEGRITY_TITLE, integrity_card_content
from ui.typography import PageTitleLabel
from ui.widgets.qfw_compat import CnSwitchSettingCard
import config

if TYPE_CHECKING:
    from core.monitor_manager import MonitorManager

logger = logging.getLogger(__name__)

# 数据库维护按钮：永久保留时只压缩、不删数据，按钮直说"压缩数据库"
COMPACT_TEXT = "压缩数据库"
CLEANUP_TEXT = "清理并压缩"
REBUILD_TEXT = "重建汇总"
HOTKEY_PLACEHOLDER = "点击后按下组合键"
KEEP_AWAKE_TITLE = "监控期间阻止电脑睡眠"
KEEP_AWAKE_CONTENT = "有运行中任务时阻止系统因空闲进入睡眠；挡不住合上盖子、手动睡眠与电源按钮"
AUTOMATION_TITLE = "允许命令行与脚本控制"
AUTOMATION_CONTENT = "允许 pmctl 与脚本经本机管道开始/停止监控、打点和导出；只对当前用户开放"
NOTIFY_BACKGROUND_TITLE = "窗口不在前台时用系统通知提醒"
NOTIFY_BACKGROUND_CONTENT = "最小化、隐藏到托盘或被其他窗口盖住时，任务异常结束、导出完成等提示改发托盘气泡"
# 报告配色卡片：选项文案与 app_config.REPORT_COLOR_SCHEMES（light / follow）逐一对应
REPORT_COLOR_TITLE = "报告配色"
REPORT_COLOR_CONTENT = "导出的报告图片默认用浅色，便于贴进文档与邮件；也可跟随当前界面主题"
REPORT_COLOR_TEXTS = ("固定浅色", "跟随主题")
# 导出分组：文件命名模板卡片
NAME_TEMPLATE_TITLE = "文件命名模板"
NAME_TEMPLATE_PLACEHOLDER = DEFAULT_EXPORT_NAME_TEMPLATE
NAME_TEMPLATE_PREVIEW = "预览：{name}"
NAME_TEMPLATE_UNKNOWN = "未知字段 {fields} 将原样写进文件名；可用字段：{available}"
NAME_TEMPLATE_RESET_TEXT = "恢复默认"
NAME_TEMPLATE_TIP = (
    "报告、明细 CSV 与证据包的默认文件名；空字段连同旁边的分隔符一起省略\n"
    "{process} 进程名　{build} 文件版本　{scenario} 场景（暂无）　{note} 备注（前 20 字）\n"
    "{date} 开始时间 YYYYMMDD_HHMM　{kind} 文件类别（报告/明细/证据包）　{pid} 进程 PID")
NAME_TEMPLATE_CARD_HEIGHT = 70
NAME_TEMPLATE_WARN_HEIGHT = 92
# 诊断信息卡片
DIAGNOSTICS_TITLE = "诊断信息"
DIAGNOSTICS_CONTENT = "反馈问题时附上：版本与环境、数据库、各任务数据质量、本程序资源占用与最近日志"
COPY_DIAGNOSTICS_TEXT = "复制诊断信息"
EXPORT_DIAGNOSTICS_TEXT = "导出诊断包…"
DIAGNOSTICS_COPIED_TITLE = "已复制"
DIAGNOSTICS_COPIED_CONTENT = "诊断信息已复制到剪贴板，可直接粘贴到问题反馈里"
DIAGNOSTICS_COPY_FAILED_TITLE = "复制诊断信息失败"
DIAGNOSTICS_DIALOG_TITLE = "选择诊断包的保存位置"
DIAGNOSTICS_EXPORTING_TEXT = "正在导出…"
DIAGNOSTICS_EXPORTED_TITLE = "诊断包已导出"
DIAGNOSTICS_EXPORT_FAILED_TITLE = "导出诊断包失败"
OPEN_FOLDER_TEXT = "打开文件夹"
OPEN_FOLDER_FAILED_TITLE = "无法打开文件夹"
DIAGNOSTICS_THREAD_NAME = "导出诊断包"
DIAGNOSTICS_EXPORTED_INFOBAR_MS = 8000
# "数据目录"卡片末尾显示的数据目录来源
DATA_DIR_SOURCE_TEXTS = {
    config.DATA_DIR_SOURCE_CLI: "命令行指定",
    config.DATA_DIR_SOURCE_ENV: "环境变量",
    config.DATA_DIR_SOURCE_PORTABLE: "便携模式",
    config.DATA_DIR_SOURCE_DEFAULT: "默认",
}


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
    BUSY_STORAGE_JOB_RUNNING: "数据库正在做例行整理，请几秒后再试",
    BUSY_DELETION_RUNNING: "正在删除任务数据，请等待删除完成后再清理数据库",
}
_BUSY_FALLBACK_TEXT = "数据库正忙，请稍后再清理"
# 重建汇总被拒绝的原因 → 提示文案
_REBUILD_BUSY_TEXTS = {
    BUSY_TASKS_RUNNING: "有监控任务正在运行，请先停止全部任务再重建汇总",
    BUSY_EXPORT_RUNNING: "正在导出数据，请等导出完成后再重建汇总",
    BUSY_MAINTENANCE_RUNNING: "数据库正在维护中，请稍候",
    BUSY_STORAGE_JOB_RUNNING: "数据库正在做例行整理，请几秒后再试",
    BUSY_DELETION_RUNNING: "正在删除任务数据，请等待删除完成后再重建汇总",
}
_REBUILD_BUSY_FALLBACK_TEXT = "数据库正忙，请稍后再重建汇总"


class DiagnosticBundleWorker(BackgroundTask):
    """"导出诊断包"后台线程：读日志末尾、读库、压缩成 zip，不放界面线程。
    结果为 zip 路径；失败（目录不可写等）经 failed 发出。包只有几 MB，不提供取消。"""

    def __init__(self, directory: str, parts: Any, clock=None, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.parts = parts
        self.clock = clock

    def execute(self, token: CancelToken) -> str:
        return write_diagnostic_bundle(self.directory, self.parts, clock=self.clock)


class RebuildRollupsWorker(BackgroundTask):
    """
    "重建汇总"后台线程：按样本整体重算分钟汇总与空洞（大库需要数秒，不放界面线程）。
    两段式维护锁：界面线程已 MaintenanceService.begin() 占锁，本线程以 already_locked=True
    调用、不占也不放锁，线程结束（QThread.finished，无论成败）后由界面线程 end()。
    结果经 result(RebuildRollupsResult) / failed(str) 二选一发出；整个重算是一个事务，
    中途失败整体回滚，因此不提供取消。
    """

    def __init__(self, maintenance_service: MaintenanceService, parent=None):
        super().__init__(parent)
        self.maintenance_service = maintenance_service

    def execute(self, token: CancelToken):
        return self.maintenance_service.rebuild_rollups(already_locked=True)


class BackupDeletionWorker(BackgroundTask):
    def __init__(self, maintenance, path, parent=None):
        super().__init__(parent)
        self.maintenance, self.path = maintenance, path

    def execute(self, token):
        if not token.is_cancelled:
            self.maintenance.delete_migration_backup(self.path, already_locked=True)
            return self.path


class BackupListWorker(BackgroundTask):
    def __init__(self, maintenance, parent=None):
        super().__init__(parent)
        self.maintenance = maintenance

    def execute(self, token):
        return [] if token.is_cancelled else self.maintenance.list_migration_backups()


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
                 hotkey: Optional[MarkerHotkey] = None,
                 diagnostics_provider: Optional[Callable[[], Any]] = None,
                 about_group: Optional[AboutUpdateGroup] = None):
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
            diagnostics_provider: 返回诊断部件（带 paths / db / manager / self_monitor /
                watchdog 等属性的对象，组合根给 AppContext）的函数；None 时只用本页的 db 与
                manager
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
        self._rebuild_worker: Optional[RebuildRollupsWorker] = None
        self._rebuild_tooltip: Optional[StateToolTip] = None
        self._state_tooltip: StateToolTip = None
        self._diagnostics_provider = diagnostics_provider
        self._bundle_worker: Optional[DiagnosticBundleWorker] = None
        self._backup_worker = None
        self._backup_list_worker = None
        self._backup_paths = []
        self.open_bundle_folder_button: Optional[PushButton] = None
        # "数据"分组顶部的完整性警告卡片（quick_check 未通过时才显示，见 show_integrity_warning）
        self.integrity_card: Optional[SettingCard] = None

        # 承载各设置分组的滚动内容容器
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self._init_general_group()
        self._init_notify_group()
        self._init_data_group()
        self._init_export_group()
        self.about_group = about_group or AboutUpdateGroup(services, self.scroll_widget)
        self.about_group.setParent(self.scroll_widget)
        self.about_group.show()

        self._init_layout()
        self._connect_signals()

        # 构造时先刷新一次，避免在首次 showEvent 触发前卡片停留在占位文案
        self._refresh_data_management_state()
        self._refresh_hotkey_card()
        self.refresh_migration_backups()

    # ========== 分组 ==========

    def _init_general_group(self):
        """常规：主题 · 默认采集间隔 · 关闭行为 · 监控期间阻止睡眠 · 打点快捷键"""
        self.general_group = SettingCardGroup("外观", self.scroll_widget)
        # 旧分组名都指向"常规"：外部读取与冒烟测试仍用这些属性
        self.appearance_group = self.general_group
        self.monitor_group = SettingCardGroup('监控默认值', self.scroll_widget)
        self.shortcuts_group = SettingCardGroup('打点与快捷键', self.scroll_widget)
        self.behavior_group = self.general_group

        self.theme_card = OptionsSettingCard(
            cfg.themeMode, FIF.BRUSH, "应用主题", "选择浅色、深色或跟随系统",
            texts=["浅色", "深色", "跟随系统"], parent=self.general_group)
        self.general_group.addSettingCard(self.theme_card)

        self.default_interval_card = SpinBoxSettingCard(
            cfg.default_interval, FIF.STOP_WATCH, "默认采集间隔",
            "新建任务时的初始值，创建时仍可调整",
            parent=self.general_group, suffix=" 秒")
        self.monitor_group.addSettingCard(self.default_interval_card)

        self.close_to_tray_card = CnSwitchSettingCard(
            FIF.MINIMIZE, "关闭窗口时最小化到托盘",
            "开启后关闭窗口仍继续采集，从托盘菜单“退出”才真正结束",
            configItem=cfg.close_to_tray, parent=self.general_group)
        self.general_group.addSettingCard(self.close_to_tray_card)

        # 卡片只写配置；PowerGuard 跟随该配置项的 valueChanged，切换即生效
        self.keep_awake_card = CnSwitchSettingCard(
            FIF.POWER_BUTTON, KEEP_AWAKE_TITLE, KEEP_AWAKE_CONTENT,
            configItem=cfg.keep_awake_while_monitoring, parent=self.general_group)
        self.monitor_group.addSettingCard(self.keep_awake_card)

        # 命令处理器每条请求都现读该配置项，切换即生效
        self.automation_card = CnSwitchSettingCard(
            FIF.COMMAND_PROMPT, AUTOMATION_TITLE, AUTOMATION_CONTENT,
            configItem=cfg.automation_enabled, parent=self.general_group)
        self.monitor_group.addSettingCard(self.automation_card)

        # 打点快捷键：录入框 + 恢复默认；说明行显示当前状态（可用 / 不可用的原因）
        self.hotkey_card = SettingCard(
            FIF.TAG, "打点快捷键", hotkey_status_text(None), parent=self.general_group)
        self.hotkey_edit = HotkeyEdit(self.hotkey_card)
        self.hotkey_edit.setToolTip("点击后按下新的组合键；Backspace 清空表示不使用；Esc 放弃")
        self.hotkey_reset_button = TransparentPushButton('恢复默认', self.hotkey_card)
        self.hotkey_reset_button.setToolTip(f"恢复默认（{DEFAULT_MARKER_HOTKEY}）")
        self.hotkey_card.hBoxLayout.addWidget(self.hotkey_edit, 0, Qt.AlignRight)
        self.hotkey_card.hBoxLayout.addSpacing(4)
        self.hotkey_card.hBoxLayout.addWidget(self.hotkey_reset_button, 0, Qt.AlignRight)
        self.hotkey_card.hBoxLayout.addSpacing(16)
        self.shortcuts_group.addSettingCard(self.hotkey_card)
        from ui.metric_presets import metric_presets
        self.preset_card = SettingCard(FIF.FLAG, '默认指标预设',
                                      '新建任务时使用；选择“沿用上次”可记住临时调整', self.monitor_group)
        self.default_preset_combo = ComboBox(self.preset_card)
        self.default_preset_combo.addItems(['沿用上次', *metric_presets()])
        self.default_preset_combo.setCurrentText(cfg.get(cfg.default_metric_preset) or '沿用上次')
        self.default_preset_combo.currentTextChanged.connect(
            lambda text: update_values([(cfg.default_metric_preset, '' if text == '沿用上次' else text)]))
        self.preset_card.hBoxLayout.addWidget(self.default_preset_combo)
        self.preset_card.hBoxLayout.addSpacing(16)
        self.monitor_group.addSettingCard(self.preset_card)
        self.guide_reset_card = PushSettingCard('重新显示', FIF.INFO, '使用引导',
                                               '重新显示首次开始监控与记录基线的提示',
                                               parent=self.shortcuts_group)
        self.guide_reset_card.clicked.connect(self.reset_guides)
        self.shortcuts_group.addSettingCard(self.guide_reset_card)

    def _init_notify_group(self):
        """通知：窗口不在前台时是否用系统通知提醒"""
        self.notify_group = SettingCardGroup("通知", self.scroll_widget)
        # 通知渠道每次发提示时现读该配置项，切换即生效
        self.notify_background_card = CnSwitchSettingCard(
            FIF.RINGER, NOTIFY_BACKGROUND_TITLE, NOTIFY_BACKGROUND_CONTENT,
            configItem=cfg.notify_in_background, parent=self.notify_group)
        self.notify_group.addSettingCard(self.notify_background_card)

    def _init_data_group(self):
        """数据：历史数据保留 · 压缩数据库 · 重建汇总 · 数据目录 · 报告配色 · 诊断信息"""
        self.data_group = SettingCardGroup("数据", self.scroll_widget)
        self._init_integrity_card()
        # 选项文案与 ConfigItem 校验器的可选值逐一对应：可选值只在应用配置里定义一份
        retention_texts = [_retention_text(days)
                           for days in self.services.preferences.retention_options()]
        self.retention_card = OptionsSettingCard(
            cfg.retention_days, FIF.DELETE, "历史数据保留",
            "启动时清理超过保留期限的已结束任务，场景参考始终保留",
            texts=retention_texts, parent=self.data_group)
        self.data_group.addSettingCard(self.retention_card)

        # 数据库维护：content 显示当前占用；运行中有任务时按钮禁用，
        # 真正的把关在确认后、启动线程前的 MaintenanceService.begin()
        self.cleanup_card = PushSettingCard(
            COMPACT_TEXT, FIF.BROOM, "数据库文件", "当前占用 —",
            parent=self.data_group)
        self.data_group.addSettingCard(self.cleanup_card)

        # 分析页大范围查询读分钟汇总；汇总与原始数据由写入同一事务维护，这里是兜底：
        # 按原始数据整体重算（与清理共用维护锁，运行中有任务时禁用）
        self.rebuild_card = PushSettingCard(
            REBUILD_TEXT, FIF.SYNC, "分析汇总",
            "分析结果与原始数据对不上时，按原始数据重新生成分钟汇总",
            parent=self.data_group)
        self.data_group.addSettingCard(self.rebuild_card)

        # 长路径收敛为末两级，完整值放在 tooltip，避免卡片文字挤压按钮。
        normalized_path = os.path.normpath(config.DATA_DIR)
        parent_name = os.path.basename(os.path.dirname(normalized_path))
        dir_name = os.path.basename(normalized_path)
        compact_path = os.path.join("…", parent_name, dir_name)
        source_text = DATA_DIR_SOURCE_TEXTS.get(config.DATA_DIR_SOURCE, "")
        if source_text:
            compact_path = f"{compact_path}（{source_text}）"
        self.open_dir_card = PushSettingCard(
            "打开", FIF.FOLDER, "数据目录", compact_path, parent=self.data_group)
        self.open_dir_card.contentLabel.setToolTip(normalized_path)
        self.open_dir_card.button.setToolTip(normalized_path)
        self.data_group.addSettingCard(self.open_dir_card)
        self.copy_data_path_button = TransparentPushButton('复制路径', self.open_dir_card)
        self.copy_data_path_button.clicked.connect(
            lambda: self.services.dialogs.copy_text(os.path.dirname(os.path.abspath(self.db.db_path))))
        self.open_dir_card.hBoxLayout.addWidget(self.copy_data_path_button)
        self.open_dir_card.hBoxLayout.addSpacing(16)
        self.backup_card = PushSettingCard('删除备份', FIF.DELETE, '迁移备份',
                                          getattr(self.db, 'backup_path', None) or '本次启动没有迁移备份',
                                          parent=self.data_group)
        self.backup_card.setVisible(bool(getattr(self.db, 'backup_path', None)))
        self.backup_card.clicked.connect(self.delete_migration_backup)
        self.backup_combo = ComboBox(self.backup_card)
        self.backup_combo.setMaximumWidth(240)
        self.backup_combo.currentIndexChanged.connect(self._backup_selected)
        self.backup_card.hBoxLayout.insertWidget(self.backup_card.hBoxLayout.count() - 2,
                                                self.backup_combo)
        self.data_group.addSettingCard(self.backup_card)

        # 报告配色：卡片只写配置，报告渲染时现读（ui/widgets/analysis/report_sheet）
        self.report_color_card = ComboBoxSettingCard(
            cfg.report_color_scheme, FIF.PALETTE, REPORT_COLOR_TITLE, REPORT_COLOR_CONTENT,
            texts=list(REPORT_COLOR_TEXTS), parent=self.data_group)
        self.data_group.addSettingCard(self.report_color_card)

        # 诊断信息：复制到剪贴板（同步，几十毫秒）/ 导出 zip 诊断包（后台线程）
        self.diagnostics_card = SettingCard(
            FIF.DEVELOPER_TOOLS, DIAGNOSTICS_TITLE, DIAGNOSTICS_CONTENT, parent=self.data_group)
        self.copy_diagnostics_button = PushButton(COPY_DIAGNOSTICS_TEXT, self.diagnostics_card)
        self.export_diagnostics_button = PushButton(
            EXPORT_DIAGNOSTICS_TEXT, self.diagnostics_card)
        self.diagnostics_card.hBoxLayout.addWidget(self.copy_diagnostics_button, 0, Qt.AlignRight)
        self.diagnostics_card.hBoxLayout.addSpacing(8)
        self.diagnostics_card.hBoxLayout.addWidget(
            self.export_diagnostics_button, 0, Qt.AlignRight)
        self.diagnostics_card.hBoxLayout.addSpacing(16)
        self.data_group.addSettingCard(self.diagnostics_card)

    def _init_export_group(self) -> None:
        """导出：文件命名模板（输入框 + 实时预览 + 恢复默认）。预览随输入实时更新，模板在输入
        完成（回车或失去焦点）时才写入配置，免得键入一半的 {pro 被存下来。"""
        self.export_group = SettingCardGroup("导出", self.scroll_widget)
        self.data_group.cardLayout.removeWidget(self.report_color_card)
        self.export_group.addSettingCard(self.report_color_card)
        self.export_dir_card = PushSettingCard('选择目录', FIF.FOLDER, '默认导出目录',
                                              cfg.get(cfg.default_export_dir) or '每次导出时选择',
                                              parent=self.export_group)
        self.export_dir_card.clicked.connect(self.choose_default_export_dir)
        self.export_group.addSettingCard(self.export_dir_card)
        self.name_template_card = SettingCard(
            FIF.EDIT, NAME_TEMPLATE_TITLE, "", parent=self.export_group)
        self.name_template_edit = LineEdit(self.name_template_card)
        self.name_template_edit.setFixedWidth(340)
        self.name_template_edit.setPlaceholderText(NAME_TEMPLATE_PLACEHOLDER)
        self.name_template_edit.setClearButtonEnabled(True)
        self.name_template_edit.setToolTip(NAME_TEMPLATE_TIP)
        self.name_template_edit.setText(get_export_name_template())
        self.name_template_reset_button = TransparentPushButton(
            NAME_TEMPLATE_RESET_TEXT, self.name_template_card)
        self.name_template_card.hBoxLayout.addWidget(self.name_template_edit, 0, Qt.AlignRight)
        self.name_template_card.hBoxLayout.addSpacing(8)
        self.name_template_card.hBoxLayout.addWidget(
            self.name_template_reset_button, 0, Qt.AlignRight)
        self.name_template_card.hBoxLayout.addSpacing(16)
        self.export_group.addSettingCard(self.name_template_card)
        self.name_template_edit.textChanged.connect(self._refresh_name_template_preview)
        self.name_template_edit.editingFinished.connect(self._on_name_template_edited)
        self.name_template_reset_button.clicked.connect(self.reset_name_template)
        self._refresh_name_template_preview()

    def _refresh_name_template_preview(self, *_args) -> None:
        """卡片说明行：用示例任务渲染的文件名；模板里有未知字段时再提示一行。"""
        text = self.name_template_edit.text().strip() or DEFAULT_EXPORT_NAME_TEMPLATE
        lines = [NAME_TEMPLATE_PREVIEW.format(name=preview_filename(text))]
        unknown = unknown_fields(text)
        if unknown:
            lines.append(NAME_TEMPLATE_UNKNOWN.format(
                fields="、".join(unknown), available=available_fields_text()))
        self.name_template_card.setContent("\n".join(lines))
        self.name_template_card.setFixedHeight(
            NAME_TEMPLATE_WARN_HEIGHT if unknown else NAME_TEMPLATE_CARD_HEIGHT)
        # 卡片高度变了：分组要重新量高，否则下一张卡片会压住它或把它截掉
        self.export_group.adjustSize()

    def _on_name_template_edited(self) -> None:
        self.apply_name_template(self.name_template_edit.text())

    def apply_name_template(self, text: str) -> str:
        """把输入的模板写入配置（空白视为恢复默认）并同步输入框；返回实际保存的模板。"""
        set_export_name_template(text)
        saved = get_export_name_template()
        if self.name_template_edit.text() != saved:
            self.name_template_edit.setText(saved)
        return saved

    def reset_name_template(self) -> None:
        """"恢复默认"：输入框与配置都回到默认模板。"""
        self.name_template_edit.setText(DEFAULT_EXPORT_NAME_TEMPLATE)
        self.apply_name_template(DEFAULT_EXPORT_NAME_TEMPLATE)

    def _init_integrity_card(self) -> None:
        """"数据"分组的第一张卡片：数据库完整性检查未通过时的警告，平时隐藏。
        卡片先加入再隐藏：ExpandLayout 不处理中途插入，显示时只需让分组重新量高。"""
        self.integrity_card = SettingCard(
            FIF.INFO, INTEGRITY_TITLE, "", parent=self.data_group)
        self.integrity_copy_button = PushButton(COPY_DIAGNOSTICS_TEXT, self.integrity_card)
        self.integrity_copy_button.clicked.connect(self.copy_diagnostics)
        self.integrity_card.hBoxLayout.addWidget(self.integrity_copy_button, 0, Qt.AlignRight)
        self.integrity_card.hBoxLayout.addSpacing(16)
        self.data_group.addSettingCard(self.integrity_card)
        self.integrity_card.hide()

    def show_integrity_warning(self, summary: str) -> None:
        """quick_check 未通过：显示"数据"分组顶部的警告卡片（由主窗口在存储守卫发信号时调用）"""
        card = self.integrity_card
        if card is None:
            return
        card.setContent(integrity_card_content(summary))
        card.show()
        self.data_group.adjustSize()

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
        self.pivot = Pivot(self.scroll_widget)
        self.expand_layout.addWidget(self.pivot)
        self.expand_layout.addWidget(self.general_group)
        self.expand_layout.addWidget(self.monitor_group)
        self.expand_layout.addWidget(self.shortcuts_group)
        self.expand_layout.addWidget(self.notify_group)
        self.expand_layout.addWidget(self.export_group)
        self.expand_layout.addWidget(self.data_group)
        self.expand_layout.addWidget(self.about_group)
        for key, group in (('appearance', self.general_group), ('monitor', self.monitor_group),
                           ('shortcuts', self.shortcuts_group), ('notify', self.notify_group),
                           ('export', self.export_group), ('data', self.data_group),
                           ('about', self.about_group)):
            group.setMaximumWidth(1000)
            self.pivot.addItem(key, group.titleLabel.text(),
                               onClick=lambda g=group: self.verticalScrollBar().setValue(g.y()))
        self.pivot.setCurrentItem('appearance')
        self.pivot.setMaximumWidth(1000)

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
        self.rebuild_card.clicked.connect(self._on_rebuild_clicked)
        self.open_dir_card.clicked.connect(self._on_open_data_dir_clicked)
        self.copy_diagnostics_button.clicked.connect(self.copy_diagnostics)
        self.export_diagnostics_button.clicked.connect(self.export_diagnostics)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'name_template_edit'):
            self.name_template_edit.setFixedWidth(max(150, min(340, self.viewport().width() - 430)))

    def reset_guides(self):
        update_values([(cfg.guide_started, False), (cfg.guide_baseline, False)])
        self.services.notifier.success(self, '引导已重置', '下次开始监控和记录基线时会再次显示提示')

    def choose_default_export_dir(self):
        directory = self.services.dialogs.ask_directory(
            self, '选择默认导出目录', cfg.get(cfg.default_export_dir))
        if directory:
            update_values([(cfg.default_export_dir, os.path.normpath(directory))])
            self.export_dir_card.setContent(os.path.normpath(directory))

    def delete_migration_backup(self):
        index = self.backup_combo.currentIndex()
        path = self._backup_paths[index] if 0 <= index < len(self._backup_paths) else None
        if not path or not self.services.dialogs.confirm(
                self, '删除迁移备份', f'删除后无法通过这份备份恢复升级前的数据：\n{path}', '删除备份'):
            return
        try:
            self.maintenance.begin()
        except Exception as exc:
            self.services.notifier.error(self, '删除备份失败', str(exc))
            return
        worker = BackupDeletionWorker(self.maintenance, path)
        self._backup_worker = worker
        self.backup_card.button.setEnabled(False)
        worker.result.connect(self._backup_deleted)
        worker.failed.connect(lambda message: self.services.notifier.error(self, '删除备份失败', message))
        worker.finished.connect(self._backup_finished)
        try:
            self.services.threads.register(worker, name='删除迁移备份')
            worker.start()
        except Exception:
            self._backup_finished()
            raise

    def _backup_deleted(self, path):
        if path:
            self._update_backups([p for p in self._backup_paths if p != path])
            self.services.notifier.success(self, '备份已删除', '当前数据库和监控记录不受影响')

    def _backup_finished(self):
        self.maintenance.end()
        self._backup_worker = None
        self.backup_card.button.setEnabled(bool(self._backup_paths))

    def refresh_migration_backups(self):
        if self._backup_list_worker is not None:
            return
        worker = BackupListWorker(self.maintenance)
        self._backup_list_worker = worker
        worker.result.connect(self._update_backups)
        worker.finished.connect(self._backup_list_finished)
        self.services.threads.register(worker, name='查找迁移备份')
        worker.start()

    def _backup_list_finished(self):
        self._backup_list_worker = None

    def _update_backups(self, paths):
        self._backup_paths = list(paths or [])
        self.backup_combo.blockSignals(True)
        self.backup_combo.clear()
        self.backup_combo.addItems([os.path.basename(path) for path in self._backup_paths])
        preferred = getattr(self.db, 'backup_path', None)
        if preferred in self._backup_paths:
            self.backup_combo.setCurrentIndex(self._backup_paths.index(preferred))
        self.backup_combo.blockSignals(False)
        self.backup_card.setVisible(bool(self._backup_paths))
        self.backup_card.button.setEnabled(bool(self._backup_paths) and self._backup_worker is None)
        self._backup_selected(self.backup_combo.currentIndex())

    def _backup_selected(self, index):
        if 0 <= index < len(self._backup_paths):
            path = self._backup_paths[index]
            self.backup_card.setContent('升级前的数据库副本；确认不再需要回退后可删除')
            self.backup_card.setToolTip(path)
            self.backup_combo.setToolTip(path)

    # ========== 诊断信息 ==========

    def _diagnostic_parts(self) -> Any:
        if self._diagnostics_provider is not None:
            parts = self._diagnostics_provider()
            if parts is not None:
                return parts
        return SimpleNamespace(db=self.db, manager=self.manager)

    def copy_diagnostics(self) -> Optional[str]:
        """"复制诊断信息"：收集诊断文本放进剪贴板并提示；返回复制的文本（失败为 None）。"""
        try:
            text = collect_diagnostics(self._diagnostic_parts(), clock=self.services.clock)
            self.services.dialogs.copy_text(text)
        except Exception as e:
            logger.error("复制诊断信息失败", exc_info=True)
            self.services.notifier.error(self.window(), DIAGNOSTICS_COPY_FAILED_TITLE, str(e))
            return None
        self.services.notifier.success(
            self.window(), DIAGNOSTICS_COPIED_TITLE, DIAGNOSTICS_COPIED_CONTENT, duration=3000)
        return text

    def export_diagnostics(self) -> Optional[DiagnosticBundleWorker]:
        """"导出诊断包…"：选目录 → 后台线程打包 → 完成提示带"打开文件夹"（不自动打开）。
        返回启动的线程（取消选择或上一次还在导出时为 None）。"""
        if self._bundle_worker is not None:
            return None
        directory = self.services.dialogs.ask_directory(
            self.window(), DIAGNOSTICS_DIALOG_TITLE, os.path.expanduser('~'))
        if not directory:
            return None
        worker = DiagnosticBundleWorker(directory, self._diagnostic_parts(), self.services.clock)
        worker.result.connect(self._on_bundle_written)
        worker.failed.connect(self._on_bundle_failed)
        worker.finished.connect(lambda w=worker: self._on_bundle_thread_finished(w))
        # 打包只读文件、写一个 zip，退出时等它一小会儿即可
        self.services.threads.register(worker, name=DIAGNOSTICS_THREAD_NAME, timeout_ms=3000)
        self._bundle_worker = worker
        self.export_diagnostics_button.setEnabled(False)
        self.export_diagnostics_button.setText(DIAGNOSTICS_EXPORTING_TEXT)
        worker.start()
        return worker

    def _on_bundle_written(self, path: str) -> None:
        folder = os.path.dirname(path)
        bar = self.services.notifier.success(
            self.window(), DIAGNOSTICS_EXPORTED_TITLE, path,
            duration=DIAGNOSTICS_EXPORTED_INFOBAR_MS)
        self.open_bundle_folder_button = PushButton(OPEN_FOLDER_TEXT)
        self.open_bundle_folder_button.clicked.connect(lambda: self._open_folder(folder))
        if bar is not None:
            bar.addWidget(self.open_bundle_folder_button)

    def _open_folder(self, folder: str) -> None:
        try:
            self.services.dialogs.open_folder(folder)
        except OSError as e:
            self.services.notifier.error(self.window(), OPEN_FOLDER_FAILED_TITLE, str(e))

    def _on_bundle_failed(self, reason: str) -> None:
        self.services.notifier.error(self.window(), DIAGNOSTICS_EXPORT_FAILED_TITLE, reason)

    def _on_bundle_thread_finished(self, worker) -> None:
        if self._bundle_worker is worker:
            self._bundle_worker = None
        self.export_diagnostics_button.setEnabled(True)
        self.export_diagnostics_button.setText(EXPORT_DIAGNOSTICS_TEXT)

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

    def _is_rebuild_running(self) -> bool:
        """重建汇总线程是否仍在运行（引用在线程真正结束后才清空）"""
        return self._rebuild_worker is not None

    def _refresh_data_management_state(self) -> None:
        """刷新数据库维护卡片的按钮文案、占用说明与可用状态"""
        days = self.services.preferences.retention_days()
        self.cleanup_card.button.setText(maintenance_button_text(days))

        size_text = _format_bytes(self.db.get_db_size_bytes())
        has_running = self.manager.has_running_tasks()
        # 按钮禁用的原因直接写在说明里：禁用按钮上的 tooltip 很难被发现
        suffix = " · 有任务在监控，停止后可清理" if has_running else ""
        self.cleanup_card.contentLabel.setText(f"当前占用 {size_text}{suffix}")

        # 维护（清理或重建汇总）进行中保持禁用，不因任务状态信号被误启用
        idle = not self._is_cleanup_running() and not self._is_rebuild_running()
        self.cleanup_card.button.setEnabled(not has_running and idle)
        self.cleanup_card.button.setToolTip("请先停止全部监控任务" if has_running else "")
        self.rebuild_card.button.setEnabled(not has_running and idle)
        self.rebuild_card.button.setToolTip("请先停止全部监控任务" if has_running else "")

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

    # ========== 重建汇总 ==========

    def _warn_rebuild_busy(self, reason: str) -> None:
        self.services.notifier.warning(
            self.window(), "无法重建汇总",
            _REBUILD_BUSY_TEXTS.get(reason, _REBUILD_BUSY_FALLBACK_TEXT), duration=4000)
        self._refresh_data_management_state()

    def _on_rebuild_clicked(self) -> None:
        """"重建汇总"点击：与清理同样的两段式——只读预检（有运行中任务或导出时直接提示）→
        界面线程 maintenance.begin() 占锁（被拒按原因提示、不启动线程）→ 后台线程重算 →
        线程 finished（无论成败）后在界面线程 end()。重算不删除任何原始数据，不弹确认框。"""
        if self._is_rebuild_running() or self._is_cleanup_running():
            return
        reason = self._busy_reason()
        if reason is not None:
            self._warn_rebuild_busy(reason)
            return
        try:
            self.maintenance.begin()
        except MaintenanceBusy as busy:
            logger.info("重建汇总被拒绝: %s", busy.reason)
            self._warn_rebuild_busy(busy.reason)
            return

        try:
            worker = RebuildRollupsWorker(self.maintenance)
            worker.result.connect(self._on_rebuild_finished)
            worker.failed.connect(self._on_rebuild_failed)
            worker.finished.connect(lambda w=worker: self._on_rebuild_thread_finished(w))
            # 重算是一个事务，中途打断只会回滚；退出时给较长的等待超时
            self.services.threads.register(worker, name="重建汇总", timeout_ms=5000)
            self._rebuild_worker = worker
            worker.start()
        except Exception:
            self._rebuild_worker = None
            self.maintenance.end()
            raise

        self._refresh_data_management_state()
        self._rebuild_tooltip = StateToolTip(
            "正在重建汇总", "正在按原始数据重新生成分钟汇总，请稍候…", self.window())
        self._rebuild_tooltip.move(self._rebuild_tooltip.getSuitablePos())
        self._rebuild_tooltip.show()

    def _on_rebuild_finished(self, result) -> None:
        if self._rebuild_tooltip is not None:
            self._rebuild_tooltip.setContent(f"汇总重建完成（{result.series} 条序列）")
            self._rebuild_tooltip.setState(True)
            self._rebuild_tooltip = None

    def _on_rebuild_failed(self, reason: str) -> None:
        if self._rebuild_tooltip is not None:
            self._rebuild_tooltip.close()
            self._rebuild_tooltip.deleteLater()
            self._rebuild_tooltip = None
        self.services.notifier.error(
            self.window(), "重建汇总失败", f"重建分钟汇总失败（原汇总保持不变）：{reason}")

    def _on_rebuild_thread_finished(self, worker) -> None:
        """重建线程真正结束（无论成败）：释放维护锁与引用，刷新按钮可用状态。"""
        self.maintenance.end()
        if self._rebuild_worker is worker:
            self._rebuild_worker = None
        self._refresh_data_management_state()
