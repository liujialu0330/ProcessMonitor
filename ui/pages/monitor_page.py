"""
实时监控页面
进程选择、新建监控任务，并以任务卡片编排各任务的实时数据与结束后的回看入口
"""
import os
from datetime import datetime
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, pyqtSignal, QStringListModel, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QGridLayout,
                             QScrollArea, QSizePolicy, QCompleter)
from qfluentwidgets import (
    LineEdit, EditableComboBox, PushButton, PrimaryPushButton, SpinBox,
    CardWidget, FluentIcon, InfoBar, InfoBarPosition, MessageBox,
    BodyLabel, CaptionLabel, StrongBodyLabel
)

from core.monitor_manager import MonitorManager
from core.process_collector import ProcessCollector
from ui.components import MetricSelectorDialog, TaskCard
from ui.components.dialogs import MarkerDialog
from ui.components.task_card import USER_STOP_REASON
from ui.typography import PageTitleLabel
from utils.metrics import LEAK_CHECK_PRESET, get_metric_display_name
from app_config import cfg, remember_last_metrics
import config

# 异常结束提示的显示时长（毫秒）：被测进程崩溃是测试里最需要被注意到的事件，
# 不能像普通提示那样两秒就消失
ABNORMAL_STOP_INFOBAR_MS = 15000

# 错误类提示不自动消失（可手动关闭）：InfoBar.error 默认只停 1 秒，用户来不及看清
ERROR_INFOBAR_DURATION = -1

# 表单校验错误文字颜色（浅色 / 深色主题，取 Fluent 的错误色）
FORM_ERROR_LIGHT = QColor(196, 43, 28)
FORM_ERROR_DARK = QColor(255, 153, 164)


class MonitorPage(QScrollArea):
    """实时监控页面"""

    # 请求切换到历史页并选中该任务（由 MainWindow 处理，页面之间不互相引用）
    history_requested = pyqtSignal(str)
    # 被测进程非用户停止而结束：(标题, 内容)。主窗口隐藏/不在前台时据此弹托盘气泡
    task_ended_abnormally = pyqtSignal(str, str)

    def __init__(self, parent=None, db=None, manager=None):
        """初始化页面

        Args:
            parent: 父窗口
            db: 数据库实例（未使用，仅为与其他页面保持统一的构造签名；本页面
                所有数据读写都经由 self.manager 完成）
            manager: 监控管理器（生产路径由 MainWindow 注入；为 None 时回退到
                MonitorManager() 单例，仅为兼容独立构造的测试）
        """
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("monitorPage")

        self.manager = manager if manager is not None else MonitorManager()

        # 任务卡片字典 {task_id: TaskCard}（含已结束、尚未关闭的卡片）
        self.task_cards = {}

        # 进程列表缓存 {pid: name}
        self.process_dict = {}
        # 进程列表中各进程的创建时间 {pid: create_time}，开始监控时用于确认身份
        self.process_create_times = {}

        # 当前显示中的错误提示 {(task_id, 内容): InfoBar}：错误提示不自动消失，
        # 同一任务同一错误重复上报时不再叠加
        self._error_bars = {}

        # 已选监控指标：沿用上次选择，默认"泄漏排查"预设
        self.selected_metrics: List[str] = list(cfg.get(cfg.last_metrics))

        # 同步标志，防止循环触发
        self._syncing = False
        # PID 输入框内容是否由下拉框选择自动填入（用户手动编辑后置 False）；
        # 用户改为搜索其他进程时，自动填入的 PID 要清掉，否则会盖过新的选择
        self._pid_autofilled = False

        # 初始化UI
        self._init_ui()

        # 连接信号
        self._connect_signals()

        # 启动时自动加载进程列表（不默认选中任何进程）
        self._refresh_process_list()

    def _init_ui(self):
        """初始化UI"""
        # 设置滚动区域属性
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 移除边框，设置透明背景
        self.setStyleSheet("QScrollArea{background: transparent; border: none}")
        self.viewport().setStyleSheet("background: transparent")

        # 创建主容器
        container = QWidget()
        container.setStyleSheet("background: transparent")
        self.setWidget(container)

        # 主布局
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(16)

        # 页面层级与其他主导航页保持一致，标题与副标题紧密组成一组
        page_header_layout = QVBoxLayout()
        page_header_layout.setSpacing(2)
        page_title = PageTitleLabel("实时监控")
        page_header_layout.addWidget(page_title)
        page_subtitle = CaptionLabel("选择进程和指标，快速创建实时采集任务")
        page_header_layout.addWidget(page_subtitle)
        main_layout.addLayout(page_header_layout)

        # ========== 进程选择区域 ==========
        select_card = CardWidget()
        select_layout = QGridLayout(select_card)
        select_layout.setContentsMargins(18, 16, 18, 16)
        select_layout.setHorizontalSpacing(10)
        select_layout.setVerticalSpacing(12)
        select_layout.setColumnStretch(1, 1)
        select_layout.setColumnStretch(2, 0)

        # 标题
        title_label = StrongBodyLabel("新建监控任务")
        select_layout.addWidget(title_label, 0, 0, 1, 4)

        # 进程搜索是主入口；PID 仍保留为紧凑的快捷/高级入口
        process_label = BodyLabel("进程")
        select_layout.addWidget(process_label, 1, 0)

        # 进程选择下拉框：EditableComboBox + 标准 QCompleter 子串搜索（MatchContains
        # + 大小写不敏感）。completer 模型与 process_combo 自身 items 使用同一份
        # "{name} · PID {pid}"文本，从补全下拉选中后，EditableComboBox 内部据精确
        # 文本匹配定位回 itemData（见 qfluentwidgets combo_box.py::
        # _onComboTextChanged），currentIndexChanged 能正常触发。
        self.process_combo = EditableComboBox()
        self.process_combo.setPlaceholderText("搜索进程名或 PID")
        self.process_combo.setMinimumWidth(0)
        self.process_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)
        self.process_combo.currentTextChanged.connect(self._on_process_text_changed)

        self._process_completer_model = QStringListModel(self)
        process_completer = QCompleter(self._process_completer_model, self)
        process_completer.setFilterMode(Qt.MatchContains)
        process_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.process_combo.setCompleter(process_completer)

        select_layout.addWidget(self.process_combo, 1, 1, 1, 2)

        # 刷新进程列表按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._refresh_process_list)
        self.refresh_button.setFixedWidth(80)
        select_layout.addWidget(self.refresh_button, 1, 3)

        pid_label = CaptionLabel("PID（快捷）")
        select_layout.addWidget(pid_label, 2, 0)

        self.pid_input = LineEdit()
        self.pid_input.setPlaceholderText("直接输入 PID")
        self.pid_input.setMinimumWidth(60)
        self.pid_input.setMaximumWidth(140)
        self.pid_input.textChanged.connect(self._on_pid_changed)
        select_layout.addWidget(self.pid_input, 2, 1)

        # 采集周期与 PID 共用次要参数行，减少卡片纵向占用
        interval_label = CaptionLabel("周期（秒）")
        select_layout.addWidget(interval_label, 2, 2, Qt.AlignRight)

        self.interval_spinbox = SpinBox()
        self.interval_spinbox.setRange(1, 3600)  # 1-3600秒
        self.interval_spinbox.setValue(cfg.get(cfg.default_interval))  # 默认值取自设置页
        # 普通 Fluent SpinBox 横排两个步进按钮，宽度交由其尺寸提示计算，
        # 避免紧凑布局把内部数值编辑区压缩为 0。
        select_layout.addWidget(self.interval_spinbox, 2, 3)

        # 表单校验错误（PID 无效/进程不存在）就近显示在输入框下方，内容一改即清除；
        # 不用 InfoBar，避免一闪即逝或与运行期提示混在一起
        self.form_error_label = CaptionLabel()
        self.form_error_label.setTextColor(FORM_ERROR_LIGHT, FORM_ERROR_DARK)
        self.form_error_label.setWordWrap(True)
        self.form_error_label.hide()
        select_layout.addWidget(self.form_error_label, 3, 1, 1, 3)

        # 测试备注：随任务落库，历史页/导出页用它区分各次测试
        note_label = CaptionLabel("测试备注")
        select_layout.addWidget(note_label, 4, 0)
        self.note_input = LineEdit()
        self.note_input.setPlaceholderText("测试备注（可选，如：建模 200 次）")
        self.note_input.setMaxLength(100)
        self.note_input.setClearButtonEnabled(True)
        select_layout.addWidget(self.note_input, 4, 1, 1, 3)

        # 监控指标选择（弹出多选对话框）
        self.metric_button = PushButton("选择指标", self, FluentIcon.CHECKBOX)
        self.metric_button.clicked.connect(self._on_select_metric_clicked)
        self.metric_button.setFixedWidth(112)
        self.metric_summary_label = CaptionLabel()
        select_layout.addWidget(self.metric_button, 5, 0)
        select_layout.addWidget(self.metric_summary_label, 5, 1, 1, 2)

        # 初始化已选指标摘要
        self._update_metric_summary()

        # 开始监控按钮
        self.start_button = PrimaryPushButton("开始监控", self, FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.start_button.setFixedWidth(116)
        select_layout.addWidget(self.start_button, 5, 3)

        main_layout.addWidget(select_card)

        # ========== 监控任务列表区域 ==========
        # 标题含配额计数：初始文案在 _update_tasks_label 中统一设置
        self.tasks_label = StrongBodyLabel()
        main_layout.addWidget(self.tasks_label)

        # 任务列表容器
        self.tasks_container = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_container)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(10)

        # 空状态占位：没有任何卡片（含已结束卡片）时居中显示
        self.empty_state_label = BodyLabel("暂无监控任务")
        self.empty_state_label.setAlignment(Qt.AlignCenter)
        self.empty_state_label.setMinimumHeight(72)
        self.tasks_layout.addWidget(self.empty_state_label)

        self.tasks_layout.addStretch()

        main_layout.addWidget(self.tasks_container)
        main_layout.addStretch()

        # 初始化配额标题文案与空状态可见性（此时 task_cards 尚为空字典）
        self._update_tasks_label()

    def set_default_interval(self, seconds: int) -> None:
        """更新采集周期输入框的默认显示值（供 MainWindow 联动调用：设置页
        "默认采集周期"变更时同步过来）

        只做 setValue，不判断用户当前是否正聚焦在该 SpinBox 上手动编辑——
        直接覆盖为新默认值即可，不影响已经创建好的监控任务（任务自己的采集
        周期在创建时已经传给 core 层固化，不会被这里的显示值变化影响）。

        Args:
            seconds: 新的默认采集周期（秒）
        """
        self.interval_spinbox.setValue(seconds)

    # ========== 指标选择 ==========

    def _on_select_metric_clicked(self):
        """选择指标按钮点击事件：弹出多选对话框，确定后记住本次选择"""
        dialog = MetricSelectorDialog(self.selected_metrics, self.window())
        if dialog.exec():
            self.selected_metrics = dialog.get_selected()
            remember_last_metrics(self.selected_metrics)
            self._update_metric_summary()

    def _update_metric_summary(self):
        """更新已选指标摘要文案"""
        names = [get_metric_display_name(m) for m in self.selected_metrics]
        summary = f"已选 {len(names)} 项" if names else "未选择指标"
        if set(self.selected_metrics) == set(LEAK_CHECK_PRESET):
            summary += "（泄漏排查预设）"
        details = "、".join(names) if names else "请选择至少一项监控指标"
        self.metric_summary_label.setText(summary)
        self.metric_summary_label.setToolTip(details)

    # ========== 进程选择 ==========

    def _refresh_process_list(self):
        """刷新进程列表；保留之前选中的进程，不再默认选中第一个"""
        # 1. 断开信号，避免中间状态触发不必要的事件
        try:
            self.process_combo.currentIndexChanged.disconnect(self._on_process_selected)
        except TypeError:
            pass  # 如果未连接则忽略

        # 2. 保存当前选中的PID或用户已输入的搜索文本
        current_pid = None
        current_index = self.process_combo.currentIndex()
        if current_index >= 0:
            item_data = self.process_combo.itemData(current_index)
            if item_data:
                current_pid = item_data[0]
        typed_text = self.process_combo.text() if current_pid is None else ""

        # 3. 完全重置ComboBox（EditableComboBox.clear 不清输入框文本）
        self.process_combo.clear()
        self.process_combo.setCurrentIndex(-1)
        self.process_combo.setText("")
        self.process_dict.clear()
        self.process_create_times.clear()

        # 4. 获取并添加进程
        processes = ProcessCollector.get_all_processes()

        for pid, name, create_time in processes:
            # 保存到字典
            self.process_dict[pid] = name
            self.process_create_times[pid] = create_time
            # 添加到ComboBox
            self.process_combo.addItem(f"{name} · PID {pid}")
            # 为最后添加的项设置userData
            index = self.process_combo.count() - 1
            self.process_combo.setItemData(index, (pid, name))

        # 4.1 重建 completer 子串搜索模型，文本需与上面的 items 保持一致，
        # 否则补全选中后回填的文本会在 process_combo.items 里找不到精确匹配
        self._process_completer_model.setStringList(
            [f"{name} · PID {pid}" for pid, name, _create_time in processes])

        # 4.2 addItem 会让 Qt 自动选中第一项，这里撤销并清掉输入框，
        # 保证刷新后不选中任何进程（避免误监控列表第一个进程）
        self.process_combo.setCurrentIndex(-1)
        self.process_combo.setText("")

        # 5. 重新连接信号
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)

        # 6. 恢复之前的选择或搜索文本（不选中任何进程，避免误监控第一个进程）
        if current_pid and current_pid in self.process_dict:
            for i in range(self.process_combo.count()):
                item_data = self.process_combo.itemData(i)
                if item_data and item_data[0] == current_pid:
                    self.process_combo.setCurrentIndex(i)
                    return
        if typed_text:
            self.process_combo.setText(typed_text)

    def _selected_form_pid(self) -> Optional[int]:
        """新建任务表单当前指向的 PID：PID 框有数字时以它为准，否则取下拉框精确选中的项。"""
        pid_text = self.pid_input.text().strip()
        if pid_text:
            try:
                return int(pid_text)
            except ValueError:
                return None
        index = self.process_combo.currentIndex()
        if index >= 0 and (self.process_combo.itemText(index)
                           == self.process_combo.currentText()):
            item_data = self.process_combo.itemData(index)
            if item_data:
                return item_data[0]
        return None

    def _forget_exited_process(self, pid: int) -> None:
        """被测进程已退出时，若表单仍选着它（下拉项或 PID 框），清掉选择并刷新进程列表：
        避免已不存在的 PID 一直留在表单里，用户再点"开始监控"才报"进程不存在"。"""
        if self._selected_form_pid() != pid:
            return
        if ProcessCollector(pid).is_process_running():
            return
        self._syncing = True
        try:
            self.pid_input.clear()
        finally:
            self._syncing = False
        self._pid_autofilled = False
        self._refresh_process_list()

    def _on_pid_changed(self, text: str):
        """PID输入框内容变化"""
        self._clear_form_error()
        if self._syncing:
            return
        self._pid_autofilled = False

        if not text.strip():
            return

        try:
            pid = int(text)
        except ValueError:
            return

        # 查找对应的进程并同步到下拉框
        if pid in self.process_dict:
            self._syncing = True
            try:
                for i in range(self.process_combo.count()):
                    item_data = self.process_combo.itemData(i)
                    if item_data and item_data[0] == pid:
                        self.process_combo.setCurrentIndex(i)
                        break
            finally:
                self._syncing = False

    def _on_process_selected(self, index: int):
        """进程下拉框选择变化：把所选进程的 PID 同步到 PID 输入框"""
        if self._syncing or index < 0:
            return

        item_data = self.process_combo.itemData(index)
        if item_data:
            pid, _name = item_data
            self._syncing = True
            try:
                self.pid_input.setText(str(pid))
            finally:
                self._syncing = False
            self._pid_autofilled = True

    def _on_process_text_changed(self, _text: str):
        """下拉框文本变化：若 PID 框里是此前选择自动填入的，用户改为搜索其他进程
        时清掉它（精确选中某项时 currentIndexChanged 会紧接着重新填入）。"""
        self._clear_form_error()
        if self._syncing or not self._pid_autofilled:
            return
        self._syncing = True
        try:
            self.pid_input.clear()
        finally:
            self._syncing = False
        self._pid_autofilled = False

    def _connect_signals(self):
        """连接信号"""
        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_started.connect(self._on_task_started)
        self.manager.task_stopped.connect(self._on_task_stopped)
        self.manager.data_updated.connect(self._on_data_updated)
        self.manager.error_occurred.connect(self._on_error)
        self.manager.metrics_unavailable.connect(self._on_metrics_unavailable)
        self.manager.task_limit_reached.connect(self._on_limit_reached)

    # ========== 新建任务 ==========

    def _warn(self, content: str, title: str = "提示", duration: int = 3000):
        """页面顶部的警告提示"""
        InfoBar.warning(
            title=title, content=content, parent=self,
            position=InfoBarPosition.TOP, duration=duration)

    def _show_form_error(self, text: str) -> None:
        """在新建任务表单的输入框下方显示校验错误"""
        self.form_error_label.setText(text)
        self.form_error_label.show()

    def _clear_form_error(self) -> None:
        """输入内容变化后，之前的校验错误已不再对应当前内容，清掉"""
        if self.form_error_label.isHidden() and not self.form_error_label.text():
            return
        self.form_error_label.clear()
        self.form_error_label.hide()

    def _resolve_target(self) -> Optional[Tuple[int, str, Optional[float]]]:
        """确定要监控的进程：PID 输入框有值时以 PID 为准，否则取下拉框当前选中的
        有效进程；两者都无效时提示并返回 None。

        返回 (pid, 进程名, 创建时间)。创建时间来自进程列表，供任务启动时确认 PID
        没有被别的进程复用；手动输入的 PID 没有可比对的列表信息，为 None。"""
        pid_text = self.pid_input.text().strip()
        if pid_text:
            try:
                pid = int(pid_text)
            except ValueError:
                self._show_form_error("PID 必须是数字")
                return None
            process_name = ProcessCollector(pid).get_process_name()
            if not process_name:
                self._show_form_error(f"进程 PID {pid} 不存在或无法访问")
                return None
            # PID 是从下拉框选择自动填入的：沿用列表里该进程的创建时间
            create_time = (self.process_create_times.get(pid)
                           if self._pid_autofilled else None)
            return pid, process_name, create_time

        # 下拉框：当前文本必须正好对应某个列表项（手输不匹配的文本不算选中，
        # EditableComboBox 的回车还会把手输文本加成没有 itemData 的新项）
        index = self.process_combo.currentIndex()
        if index >= 0:
            item_data = self.process_combo.itemData(index)
            if item_data and (self.process_combo.itemText(index)
                              == self.process_combo.currentText()):
                pid, process_name = item_data
                return pid, process_name, self.process_create_times.get(pid)

        if self.process_combo.currentText().strip():
            self._warn("输入的内容与进程列表不匹配，请从下拉列表中选择，或直接输入 PID")
        else:
            self._warn("请输入 PID 或从列表中选择进程")
        return None

    def _on_start_clicked(self):
        """开始监控按钮点击事件"""
        target = self._resolve_target()
        if target is None:
            return
        pid, process_name, create_time = target

        if not self.selected_metrics:
            self._warn("请先选择要监控的指标")
            return

        self._start_monitoring(
            pid, process_name, list(self.selected_metrics),
            float(self.interval_spinbox.value()), self.note_input.text().strip(),
            create_time=create_time)

    def _has_running_task_for_pid(self, pid: int) -> bool:
        """该 PID 是否已有运行中（未在停止）的任务"""
        return any(task.pid == pid and task.is_running()
                   for task in self.manager.get_all_tasks())

    def _start_monitoring(self, pid: int, process_name: str, metrics: List[str],
                          interval: float, note: str = '',
                          create_time: Optional[float] = None) -> Optional[str]:
        """创建并启动任务；同一 PID 已有运行中任务时先确认。返回任务ID，
        取消或创建失败返回 None。create_time 为所选进程的创建时间（可为 None）。"""
        if (self._has_running_task_for_pid(pid)
                and not self._confirm_duplicate_pid(pid, process_name)):
            return None
        task_id = self.manager.create_task(
            pid, process_name, metrics, interval, note=note, create_time=create_time)
        if task_id:
            self.manager.start_task(task_id)
        return task_id

    # ========== 需要弹模态对话框的交互（抽成方法，测试中可 monkeypatch） ==========

    def _confirm_duplicate_pid(self, pid: int, process_name: str) -> bool:
        """同一 PID 已有运行中任务：确认是否仍要再创建一个"""
        box = MessageBox(
            "已在监控该进程",
            f"{process_name}（PID {pid}）已有一个运行中的监控任务。\n仍要再创建一个吗？",
            self.window())
        box.yesButton.setText("继续创建")
        box.cancelButton.setText("取消")
        return bool(box.exec())

    def _confirm_stop(self, card: TaskCard) -> bool:
        """停止确认：说明停止后本任务不可续采"""
        box = MessageBox(
            "停止监控",
            f"确定要停止对 {card.process_name}（PID {card.pid}）的监控吗？\n"
            "停止后本任务不可续采，如需继续请重新监控；已采集的数据会保留。",
            self.window())
        box.yesButton.setText("停止")
        box.cancelButton.setText("取消")
        return bool(box.exec())

    def _ask_marker_label(self, default_label: str,
                          timestamp: datetime) -> Optional[str]:
        """弹出打点标签输入框（显示点击时刻）；取消返回 None"""
        dialog = MarkerDialog(default_label, self.window(), timestamp=timestamp)
        if dialog.exec():
            return dialog.get_label()
        return None

    def _find_processes_by_name(
            self, process_name: str) -> List[Tuple[int, Optional[float]]]:
        """按进程名（不区分大小写）查找当前正在运行的进程，返回 (pid, 创建时间) 列表"""
        wanted = process_name.lower()
        # 排除本程序自身：被测的同名进程（如 python.exe）退出后，"重新监控"
        # 不能因为只剩本程序同名而自动监控起自己
        own_pid = os.getpid()
        return [(pid, create_time)
                for pid, name, create_time in ProcessCollector.get_all_processes()
                if name.lower() == wanted and pid != own_pid]

    # ========== 任务列表 ==========

    def _update_tasks_label(self) -> None:
        """
        刷新"监控任务 · 运行中/上限"标题与空状态占位可见性。配额只计仍在运行的
        任务（manager.running_task_count()）；已结束但未关闭的卡片单独计数，
        不占用配额。空状态只在没有任何卡片时显示。
        """
        running = self.manager.running_task_count()
        finished = sum(1 for card in self.task_cards.values() if card.is_finished)
        text = f"监控任务 · {running}/{config.MAX_MONITOR_TASKS} 运行中"
        if finished:
            text += f" · {finished} 个已结束"
        self.tasks_label.setText(text)
        self.empty_state_label.setVisible(not self.task_cards)

    def _remove_card(self, task_id: str) -> None:
        """从列表移除卡片并释放控件"""
        card = self.task_cards.pop(task_id, None)
        if card is not None:
            self.tasks_layout.removeWidget(card)
            card.deleteLater()
        self._update_tasks_label()

    def _on_task_added(self, task_id: str):
        """任务添加事件"""
        task = self.manager.get_task(task_id)
        if not task:
            return
        task_info = task.get_task_info()

        card = TaskCard(
            task_id=task_id,
            process_name=task_info.process_name,
            pid=task_info.pid,
            metric_types=task_info.metric_types,
            parent=self,
            interval=task_info.interval,
            note=task_info.note,
        )

        # 连接卡片按钮信号（TaskCard 只 emit，MonitorPage 调 manager）
        card.stop_clicked.connect(self._on_stop_task)
        card.pause_clicked.connect(self._on_pause_task)
        card.resume_clicked.connect(self._on_resume_task)
        card.marker_clicked.connect(self._on_marker_task)
        card.history_clicked.connect(self._on_history_task)
        card.restart_clicked.connect(self._on_restart_task)
        card.close_clicked.connect(self._remove_card)

        # 添加到布局（在stretch之前）
        self.tasks_layout.insertWidget(self.tasks_layout.count() - 1, card)
        self.task_cards[task_id] = card
        self._update_tasks_label()

    def _on_task_started(self, task_id: str):
        """任务启动事件"""
        self._update_tasks_label()
        InfoBar.success(
            title="成功",
            content="监控任务已启动",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=1000
        )

    def _on_task_stopped(self, task_id: str, reason: str):
        """任务停止事件：卡片保留并切换为"已结束"，同时释放 core 侧的任务对象"""
        card = self.task_cards.get(task_id)
        if card is not None:
            card.set_finished(reason, datetime.now())
            self._forget_exited_process(card.pid)

        # 释放 core 侧任务对象（此时线程只剩收尾，wait 极短）。task_stopped 是 QThread
        # 线程发出的信号、仍在该信号的槽调用栈内：这里是最后一个引用，同步移除会在
        # 发射方栈内销毁 QThread 对象，故延后到事件循环下一轮；卡片转"已结束"保持即时
        QTimer.singleShot(0, lambda tid=task_id: self.manager.remove_task(tid))
        self._update_tasks_label()

        if reason == USER_STOP_REASON:
            InfoBar.info(
                title="任务已停止",
                content=f"原因：{reason}",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        # 被测进程崩溃/退出等非用户停止：提示要显眼且停留更久，
        # 并通知主窗口在后台时补一条托盘气泡
        name = f"{card.process_name}（PID {card.pid}）" if card is not None else task_id
        title = "监控任务异常结束"
        content = f"{name}：{reason}"
        InfoBar.warning(
            title=title,
            content=content,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=ABNORMAL_STOP_INFOBAR_MS
        )
        self.task_ended_abnormally.emit(title, content)

    def _on_data_updated(self, task_id: str, values: dict):
        """数据更新事件：卡片采集次数为内存自增，每周期不查库"""
        card = self.task_cards.get(task_id)
        if card is not None:
            card.update_values(values)
            card.increment_count()

    def _on_metrics_unavailable(self, task_id: str, reasons: dict):
        """部分指标无法采集：卡片标"不可用"，并弹出一次警告"""
        card = self.task_cards.get(task_id)
        if card is None:
            return
        card.mark_unavailable(reasons)

        items = "、".join(
            f"{get_metric_display_name(metric)}（{reason}）"
            for metric, reason in reasons.items())
        content = f"{items}，这些指标不会记录数据。"
        if "权限不足" in reasons.values():
            content += "以管理员身份运行本工具可采集全部指标。"
        InfoBar.warning(
            title="部分指标无法采集",
            content=content,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=10000
        )

    def _on_error(self, task_id: str, error_msg: str):
        """错误事件：提示不自动消失；同一任务的同一错误仍在显示时不再叠加
        （采集异常可能每个周期都上报一次）"""
        key = (task_id, error_msg)
        if key in self._error_bars:
            return
        bar = InfoBar.error(
            title="错误",
            content=error_msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=ERROR_INFOBAR_DURATION
        )
        if bar is not None:
            self._error_bars[key] = bar
            bar.closedSignal.connect(lambda k=key: self._error_bars.pop(k, None))

    def _on_limit_reached(self):
        """任务数量达到上限"""
        InfoBar.warning(
            title="提示",
            content=f"最多只能同时监控 {config.MAX_MONITOR_TASKS} 个任务",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    # ========== 卡片按钮 ==========

    def _on_stop_task(self, task_id: str):
        """停止任务：确认后发出停止请求（非阻塞），卡片等 task_stopped 再转为已结束。
        停止窗口期内 manager 可能返回 False（任务已在停止），界面容忍不报错。"""
        card = self.task_cards.get(task_id)
        if card is None or card.is_finished:
            return
        if not self._confirm_stop(card):
            return
        if self.manager.stop_task(task_id):
            card.set_stopping()

    def _on_pause_task(self, task_id: str):
        """暂停任务：manager.pause_task 成功后回调卡片更新暂停态显示；
        任务正在停止等原因导致返回 False 时不做任何事"""
        if self.manager.pause_task(task_id):
            card = self.task_cards.get(task_id)
            if card:
                card.set_paused(True)

    def _on_resume_task(self, task_id: str):
        """恢复任务：manager.resume_task 成功后回调卡片更新暂停态显示"""
        if self.manager.resume_task(task_id):
            card = self.task_cards.get(task_id)
            if card:
                card.set_paused(False)

    def _on_marker_task(self, task_id: str):
        """打点：弹出标签输入框，经 manager 写库，成功后提示打点时间"""
        card = self.task_cards.get(task_id)
        if card is None or card.is_finished:
            return
        # 打点时间取点击按钮的这一刻：输入标签要花好几秒，按确认时刻记录会让
        # 竖线落在事件之后，短周期下甚至把增长归到下一轮
        clicked_at = datetime.now()
        label = self._ask_marker_label(card.next_marker_label(), clicked_at)
        if label is None:
            return
        if not label.strip():
            self._warn("打点标签不能为空")
            return

        timestamp = self.manager.add_marker(task_id, label, clicked_at)
        if timestamp is None:
            InfoBar.error(
                title="打点失败", content="未能保存打点，请查看日志",
                parent=self, position=InfoBarPosition.TOP,
                duration=ERROR_INFOBAR_DURATION)
            return
        card.note_marker_added()
        InfoBar.success(
            title="已打点",
            content=f"{label.strip()} · {timestamp.strftime('%H:%M:%S')}",
            parent=self, position=InfoBarPosition.TOP, duration=2000)

    def _on_history_task(self, task_id: str):
        """已结束卡片的"查看历史"：交给主窗口切页并选中该任务"""
        self.history_requested.emit(task_id)

    def _on_restart_task(self, task_id: str):
        """已结束卡片的"重新监控"：按进程名找到唯一的运行进程时，以相同指标/周期/
        备注新建任务；找不到或有多个时把进程名填入搜索框，由用户自己选。"""
        card = self.task_cards.get(task_id)
        if card is None:
            return

        matches = self._find_processes_by_name(card.process_name)
        if len(matches) == 1:
            pid, create_time = matches[0]
            interval = card.interval or float(self.interval_spinbox.value())
            self._start_monitoring(
                pid, card.process_name, list(card.metric_types),
                interval, card.note, create_time=create_time)
            return

        self.pid_input.clear()
        self.process_combo.setText(card.process_name)
        self.process_combo.setFocus()
        if matches:
            self._warn(
                f"找到 {len(matches)} 个 {card.process_name} 进程，"
                "请在上方列表中选择要监控的一个", title="请选择进程", duration=5000)
        else:
            self._warn(
                f"当前没有运行中的 {card.process_name}，启动后点击刷新再选择",
                title="未找到进程", duration=5000)
