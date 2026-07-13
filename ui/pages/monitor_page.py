"""
实时监控页面
显示进程选择、监控任务列表等
"""
from typing import Dict, List

from PyQt5.QtCore import Qt, pyqtSignal, QStringListModel
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QGridLayout, QScrollArea, QSizePolicy, QCompleter)
from qfluentwidgets import (
    LineEdit, EditableComboBox, PushButton, PrimaryPushButton, SpinBox,
    CardWidget, FluentIcon, InfoBar, InfoBarPosition,
    BodyLabel, CaptionLabel, StrongBodyLabel
)

from core.monitor_manager import MonitorManager
from core.process_collector import ProcessCollector
from ui.components import MetricSelectorDialog, SparklineWidget
from ui.typography import DataCaptionLabel, DataLabel, PageTitleLabel
from utils.metrics import (
    get_metric_display_name, format_metric_value, MetricType
)
from app_config import cfg
import config

# "已暂停"状态提示配色（C4，橙色系浅/深双色）：CaptionLabel.setTextColor 只需
# 设置一次即可自动跟随主题切换（FluentLabelBase 内部已连接 qconfig.themeChanged，
# 会自动用 lightColor/darkColor 重新套用样式），本文件无需再手动连接信号
PAUSED_LABEL_LIGHT_COLOR = QColor("#D83B01")
PAUSED_LABEL_DARK_COLOR = QColor("#FF9D42")


class TaskCard(CardWidget):
    """监控任务卡片组件"""

    # 停止按钮点击信号
    stop_clicked = pyqtSignal(str)  # task_id
    # 暂停/恢复按钮点击信号（C4，信号中转模式，对齐既有 stop_clicked）：
    # TaskCard 不直接持有 manager，只 emit 信号，由 MonitorPage 调用
    # manager.pause_task/resume_task，并在调用成功后回调 set_paused() 更新显示
    pause_clicked = pyqtSignal(str)   # task_id
    resume_clicked = pyqtSignal(str)  # task_id

    # 指标值网格每行列数
    VALUE_COLUMNS = 3

    def __init__(self, task_id: str, process_name: str, pid: int,
                 metric_types: List[str], parent=None):
        """
        初始化任务卡片

        Args:
            task_id: 任务ID
            process_name: 进程名称
            pid: 进程ID
            metric_types: 监控指标类型列表
            parent: 父窗口
        """
        super().__init__(parent)

        self.task_id = task_id
        self.process_name = process_name
        self.pid = pid
        self.metric_types = list(metric_types)

        # 指标值标签字典 {指标类型: CaptionLabel}
        self.value_labels: Dict[str, QLabel] = {}

        # 采集次数：内存计数，卡片创建时直接置0（v1.2.0 架构评审裁决——源码核实当前
        # 无"重启后恢复运行中任务"的路径，建卡片时查库是死逻辑，故不查）；每次
        # _on_data_updated 收到数据+1。未来若支持重启恢复任务，需按 task_id 从数据库
        # 回填初值（get_sample_count 方法仍保留可用）。
        self.count = 0

        # 暂停状态（C4）：卡片创建时任务必为运行态，初值 False；由 MonitorPage
        # 在 manager.pause_task/resume_task 调用成功后回调 set_paused() 更新
        self.is_paused = False

        self._init_ui()

    def _init_ui(self):
        """初始化UI"""
        # 卡片随指标数量自适应增高，水平方向由页面宽度决定
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # 顶部：进程身份、状态和任务操作保持在同一视觉层级
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)

        identity_layout = QVBoxLayout()
        identity_layout.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title_label = StrongBodyLabel(self.process_name)
        self.title_label.setToolTip(self.process_name)
        # 长进程名允许被布局压缩，不与右侧操作按钮共同撑宽卡片
        self.title_label.setMinimumWidth(1)
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title_row.addWidget(self.title_label, 1)

        self.running_label = CaptionLabel("运行中")
        title_row.addWidget(self.running_label)
        self.paused_label = CaptionLabel("已暂停")
        self.paused_label.setTextColor(PAUSED_LABEL_LIGHT_COLOR, PAUSED_LABEL_DARK_COLOR)
        self.paused_label.setVisible(False)
        title_row.addWidget(self.paused_label)
        identity_layout.addLayout(title_row)

        identity_caption = DataCaptionLabel(
            f"PID {self.pid} · {len(self.metric_types)} 项指标")
        identity_caption.setToolTip(
            f"PID {self.pid} · 监控 {len(self.metric_types)} 项指标")
        identity_caption.setMinimumWidth(1)
        identity_caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        identity_layout.addWidget(identity_caption)
        header_layout.addLayout(identity_layout, 1)

        # 暂停/恢复按钮在停止按钮左侧，保留既有操作契约
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        self.pause_button = PushButton("暂停", self, FluentIcon.PAUSE)
        self.pause_button.clicked.connect(self._on_pause_button_clicked)
        self.pause_button.setFixedWidth(88)
        button_layout.addWidget(self.pause_button)

        self.stop_button = PushButton("停止", self, FluentIcon.CANCEL)
        self.stop_button.clicked.connect(lambda: self.stop_clicked.emit(self.task_id))
        self.stop_button.setFixedWidth(88)
        button_layout.addWidget(self.stop_button)

        header_layout.addLayout(button_layout)
        header_layout.setAlignment(button_layout, Qt.AlignRight | Qt.AlignTop)
        layout.addLayout(header_layout)

        # 中部：指标名与值分行，用等宽列增强快速比较能力
        values_layout = QGridLayout()
        values_layout.setHorizontalSpacing(20)
        values_layout.setVerticalSpacing(8)
        for i, metric_type in enumerate(self.metric_types):
            metric_widget = QWidget(self)
            metric_layout = QVBoxLayout(metric_widget)
            metric_layout.setContentsMargins(0, 0, 0, 0)
            metric_layout.setSpacing(2)

            metric_name = CaptionLabel(get_metric_display_name(metric_type))
            metric_name.setToolTip(get_metric_display_name(metric_type))
            metric_name.setMinimumWidth(1)
            metric_name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            value_label = DataLabel("--")
            value_label.setToolTip(get_metric_display_name(metric_type))
            value_label.setMinimumWidth(1)
            value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            metric_layout.addWidget(metric_name)
            metric_layout.addWidget(value_label)

            self.value_labels[metric_type] = value_label
            values_layout.addWidget(
                metric_widget, i // self.VALUE_COLUMNS, i % self.VALUE_COLUMNS)
        for column in range(self.VALUE_COLUMNS):
            values_layout.setColumnStretch(column, 1)
        layout.addLayout(values_layout)

        # 底部：在图表上方明示标注趋势对应的首指标
        trend_header = QHBoxLayout()
        trend_header.setSpacing(8)
        trend_metric_name = (
            get_metric_display_name(self.metric_types[0])
            if self.metric_types else "指标"
        )
        self.sparkline_label = CaptionLabel(
            f"{trend_metric_name} · 最近 60 次趋势")
        self.sparkline_label.setToolTip(
            f"{trend_metric_name}：最近 60 次采集趋势")
        self.sparkline_label.setMinimumWidth(1)
        self.sparkline_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        trend_header.addWidget(self.sparkline_label, 1)

        # 采集次数（内存计数，非落库条数）作为趋势的辅助上下文
        self.count_label = DataCaptionLabel("已记录：0 次采集")
        self.count_label.setToolTip("采集次数（非落库条数）")
        trend_header.addWidget(self.count_label)
        layout.addLayout(trend_header)

        self.sparkline = SparklineWidget(maxlen=60)
        if self.metric_types:
            self.sparkline.setToolTip(
                f"最近 60 次采集趋势（{get_metric_display_name(self.metric_types[0])}）")
        layout.addWidget(self.sparkline)

    def update_values(self, values: dict):
        """
        批量更新指标显示值，并在包含首指标时向趋势图追加一点（E）

        Args:
            values: 指标值字典 {指标类型: 指标值}
        """
        for metric_type, value in values.items():
            value_label = self.value_labels.get(metric_type)
            if value_label is None:
                continue
            formatted_value = format_metric_value(metric_type, value)
            value_label.setText(formatted_value)
            value_label.setToolTip(
                f"{get_metric_display_name(metric_type)}：{formatted_value}")

        # 趋势图只跟踪首指标（E，方案3.5）。暂停期间 core 不采集、不 emit
        # data_updated，本方法根本不会被调用，趋势图自然随之静止，无需在这里
        # 额外判断暂停状态（暂停语义详见 set_paused 方法注释，方案 N3）
        if self.metric_types:
            first_metric = self.metric_types[0]
            if first_metric in values:
                self.sparkline.append(values[first_metric])

    def increment_count(self):
        """采集次数+1并刷新文案（内存计数，每次收到 data_updated 调用一次）"""
        self.count += 1
        self.count_label.setText(f"已记录：{self.count} 次采集")

    def _on_pause_button_clicked(self):
        """
        暂停/恢复按钮点击：只负责按当前状态 emit 对应信号，不直接调用 manager
        （信号中转模式，对齐既有 stop_clicked；TaskCard 不持有 manager 引用）。

        按钮图标/文案与"已暂停"提示不在这里乐观更新——留给 MonitorPage 在确认
        manager.pause_task/resume_task 调用成功后回调 set_paused()，避免任务
        已不在运行等原因导致调用失败时，卡片显示与真实状态不一致。
        """
        if self.is_paused:
            self.resume_clicked.emit(self.task_id)
        else:
            self.pause_clicked.emit(self.task_id)

    def set_paused(self, is_paused: bool) -> None:
        """
        更新暂停/恢复按钮的图标文案与"已暂停"状态提示的可见性。

        由 MonitorPage 在 manager.pause_task/resume_task 调用成功后回调。

        暂停语义（方案 N3，已按 core 源码核实，core 不改）：core 主循环里
        collect_metrics 是唯一的存活探测点，暂停态下不会被调用，因此暂停期间
        被监控进程退出不会被感知——卡片会一直显示"已暂停"，直到恢复后下一次
        采集才发现进程已消失，此时走 task_stopped(reason="进程已终止")（不是
        error_occurred），卡片按既有停止流程被正常移除，属预期行为。

        Args:
            is_paused: 任务是否处于暂停状态
        """
        self.is_paused = is_paused
        if is_paused:
            self.pause_button.setText("恢复")
            self.pause_button.setIcon(FluentIcon.PLAY)
        else:
            self.pause_button.setText("暂停")
            self.pause_button.setIcon(FluentIcon.PAUSE)
        self.running_label.setVisible(not is_paused)
        self.paused_label.setVisible(is_paused)


class MonitorPage(QScrollArea):
    """实时监控页面"""

    def __init__(self, parent=None, db=None):
        """初始化页面

        Args:
            parent: 父窗口
            db: 数据库实例（未使用，仅为与其他页面保持统一的构造签名；本页面
                所有数据读写都经由 self.manager 完成）
        """
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("monitorPage")

        # 监控管理器（单例：仅首次构造时传入的 db 生效）
        self.manager = MonitorManager()

        # 任务卡片字典 {task_id: TaskCard}
        self.task_cards = {}

        # 进程列表缓存 {pid: name}
        self.process_dict = {}

        # 已选监控指标列表（默认预选工作集内存）
        self.selected_metrics: List[str] = [MetricType.MEMORY_RSS]

        # 同步标志，防止循环触发
        self._syncing = False

        # 初始化UI
        self._init_ui()

        # 连接信号
        self._connect_signals()

        # 启动时自动加载进程列表
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

        # 进程选择下拉框（C1 进程搜索）：EditableComboBox + 标准 QCompleter 子串
        # 搜索（MatchContains + 大小写不敏感）。completer 模型与 process_combo
        # 自身 items 使用同一份"{name} · PID {pid}"文本，从补全下拉选中后，
        # EditableComboBox 内部据精确文本匹配定位回 itemData（见 qfluentwidgets
        # combo_box.py::_onComboTextChanged），currentIndexChanged 能正常触发，
        # 不破坏既有 _syncing 双向同步与"手输不匹配走 warning 路径"的契约。
        self.process_combo = EditableComboBox()
        self.process_combo.setPlaceholderText("搜索进程名或 PID")
        self.process_combo.setMinimumWidth(0)
        self.process_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)

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
        self.interval_spinbox.setFixedWidth(84)
        select_layout.addWidget(self.interval_spinbox, 2, 3)

        # 监控指标选择（弹出多选对话框）
        self.metric_button = PushButton("选择指标", self, FluentIcon.CHECKBOX)
        self.metric_button.clicked.connect(self._on_select_metric_clicked)
        self.metric_button.setFixedWidth(112)
        self.metric_summary_label = CaptionLabel()
        select_layout.addWidget(self.metric_button, 3, 0)
        select_layout.addWidget(self.metric_summary_label, 3, 1, 1, 2)

        # 初始化已选指标摘要
        self._update_metric_summary()

        # 开始监控按钮
        self.start_button = PrimaryPushButton("开始监控", self, FluentIcon.PLAY)
        self.start_button.clicked.connect(self._on_start_clicked)
        self.start_button.setFixedWidth(116)
        select_layout.addWidget(self.start_button, 3, 3)

        main_layout.addWidget(select_card)

        # ========== 监控任务列表区域 ==========
        # 标题含配额计数（C2 配额常驻）：初始文案在 _update_tasks_label 中统一设置
        self.tasks_label = StrongBodyLabel()
        main_layout.addWidget(self.tasks_label)

        # 任务列表容器
        self.tasks_container = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_container)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0)
        self.tasks_layout.setSpacing(10)

        # 空状态占位（C3）：无监控任务时居中显示，随卡片增删切换可见性
        self.empty_state_label = BodyLabel("暂无运行中的任务")
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

    def _on_select_metric_clicked(self):
        """选择指标按钮点击事件：弹出多选对话框"""
        dialog = MetricSelectorDialog(self.selected_metrics, self.window())
        if dialog.exec():
            self.selected_metrics = dialog.get_selected()
            self._update_metric_summary()

    def _update_metric_summary(self):
        """更新已选指标摘要文案"""
        names = [get_metric_display_name(m) for m in self.selected_metrics]
        summary = f"已选 {len(names)} 项" if names else "未选择指标"
        details = "、".join(names) if names else "请选择至少一项监控指标"
        self.metric_summary_label.setText(summary)
        self.metric_summary_label.setToolTip(details)

    def _refresh_process_list(self):
        """刷新进程列表"""
        # 1. 断开信号，避免中间状态触发不必要的事件
        try:
            self.process_combo.currentIndexChanged.disconnect(self._on_process_selected)
        except:
            pass  # 如果未连接则忽略

        # 2. 保存当前选中的PID（如果有）
        current_pid = None
        current_index = self.process_combo.currentIndex()
        if current_index >= 0:
            item_data = self.process_combo.itemData(current_index)
            if item_data:
                current_pid = item_data[0]

        # 3. 完全重置ComboBox
        self.process_combo.clear()
        self.process_combo.setCurrentIndex(-1)
        self.process_dict.clear()

        # 4. 获取并添加进程
        processes = ProcessCollector.get_all_processes()

        for pid, name in processes:
            # 保存到字典
            self.process_dict[pid] = name
            # 添加到ComboBox
            self.process_combo.addItem(f"{name} · PID {pid}")
            # 为最后添加的项设置userData
            index = self.process_combo.count() - 1
            self.process_combo.setItemData(index, (pid, name))

        # 4.1 重建 completer 子串搜索模型（C1），文本需与上面的 items 保持一致，
        # 否则补全选中后回填的文本会在 process_combo.items 里找不到精确匹配
        self._process_completer_model.setStringList(
            [f"{name} · PID {pid}" for pid, name in processes])

        # 5. 重新连接信号
        self.process_combo.currentIndexChanged.connect(self._on_process_selected)

        # 6. 恢复之前的选择或选择第一个进程
        if current_pid and current_pid in self.process_dict:
            # 尝试恢复之前选中的进程
            for i in range(self.process_combo.count()):
                item_data = self.process_combo.itemData(i)
                if item_data and item_data[0] == current_pid:
                    self.process_combo.setCurrentIndex(i)
                    return

        # 如果无法恢复或首次加载，默认选择第一个进程
        if self.process_combo.count() > 0:
            self.process_combo.setCurrentIndex(0)

    def _on_pid_changed(self, text: str):
        """PID输入框内容变化"""
        if self._syncing:
            return

        if not text.strip():
            return

        try:
            pid = int(text)
            # 查找对应的进程并同步到下拉框
            if pid in self.process_dict:
                self._syncing = True
                # 在下拉框中查找并选中该进程
                for i in range(self.process_combo.count()):
                    item_data = self.process_combo.itemData(i)
                    if item_data and item_data[0] == pid:
                        self.process_combo.setCurrentIndex(i)
                        break
                self._syncing = False
        except ValueError:
            pass

    def _on_process_selected(self, index: int):
        """进程下拉框选择变化"""
        if self._syncing or index < 0:
            return

        item_data = self.process_combo.itemData(index)
        if item_data:
            pid, name = item_data
            # 同步到PID输入框
            self._syncing = True
            self.pid_input.setText(str(pid))
            self._syncing = False

    def _connect_signals(self):
        """连接信号"""
        self.manager.task_added.connect(self._on_task_added)
        self.manager.task_started.connect(self._on_task_started)
        self.manager.task_stopped.connect(self._on_task_stopped)
        self.manager.data_updated.connect(self._on_data_updated)
        self.manager.error_occurred.connect(self._on_error)
        self.manager.task_limit_reached.connect(self._on_limit_reached)

    def _on_start_clicked(self):
        """开始监控按钮点击事件"""
        # 获取PID
        pid = None
        process_name = None

        # 优先使用PID输入
        pid_text = self.pid_input.text().strip()
        if pid_text:
            try:
                pid = int(pid_text)
                # 获取进程名
                collector = ProcessCollector(pid)
                process_name = collector.get_process_name()
                if not process_name:
                    InfoBar.error(
                        title="错误",
                        content=f"进程 PID {pid} 不存在或无法访问",
                        parent=self,
                        position=InfoBarPosition.TOP
                    )
                    return
            except ValueError:
                InfoBar.error(
                    title="错误",
                    content="PID 必须是数字",
                    parent=self,
                    position=InfoBarPosition.TOP
                )
                return
        else:
            # 使用进程选择
            index = self.process_combo.currentIndex()
            if index >= 0:
                item_data = self.process_combo.itemData(index)
                if item_data:
                    pid, process_name = item_data
                else:
                    InfoBar.warning(
                        title="提示",
                        content="请输入 PID 或从列表中选择进程",
                        parent=self,
                        position=InfoBarPosition.TOP
                    )
                    return
            else:
                InfoBar.warning(
                    title="提示",
                    content="请输入 PID 或从列表中选择进程",
                    parent=self,
                    position=InfoBarPosition.TOP
                )
                return

        # 获取监控指标
        if not self.selected_metrics:
            InfoBar.warning(
                title="提示",
                content="请先选择要监控的指标",
                parent=self,
                position=InfoBarPosition.TOP
            )
            return

        # 获取采集周期（从SpinBox）
        interval = float(self.interval_spinbox.value())

        # 创建并启动任务
        task_id = self.manager.create_task(
            pid, process_name, list(self.selected_metrics), interval)
        if task_id:
            self.manager.start_task(task_id)

    def _update_tasks_label(self) -> None:
        """
        刷新"监控任务列表（n/上限）"标题与空状态占位可见性（C2 配额常驻 + C3
        空状态）。n 取当前 UI 卡片数（len(self.task_cards)），与卡片生命周期
        天然一致；在 _on_task_added 与 _on_task_stopped（卡片移除处）调用。
        """
        count = len(self.task_cards)
        self.tasks_label.setText(
            f"监控任务 · {count}/{config.MAX_MONITOR_TASKS}")
        self.empty_state_label.setVisible(count == 0)

    def _on_task_added(self, task_id: str):
        """任务添加事件"""
        # 获取任务信息
        task = self.manager.get_task(task_id)
        if task:
            task_info = task.get_task_info()

            # 创建任务卡片
            card = TaskCard(
                task_id=task_id,
                process_name=task_info.process_name,
                pid=task_info.pid,
                metric_types=task_info.metric_types,
                parent=self
            )

            # 连接停止/暂停/恢复信号（C4：TaskCard 只 emit，MonitorPage 调 manager）
            card.stop_clicked.connect(self._on_stop_task)
            card.pause_clicked.connect(self._on_pause_task)
            card.resume_clicked.connect(self._on_resume_task)

            # 添加到布局（在stretch之前）
            self.tasks_layout.insertWidget(self.tasks_layout.count() - 1, card)

            # 保存引用
            self.task_cards[task_id] = card

            # 刷新配额标题与空状态可见性（C2/C3）
            self._update_tasks_label()

    def _on_task_started(self, task_id: str):
        """任务启动事件"""
        InfoBar.success(
            title="成功",
            content="监控任务已启动",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=1000
        )

    def _on_task_stopped(self, task_id: str, reason: str):
        """任务停止事件"""
        # 移除卡片
        if task_id in self.task_cards:
            card = self.task_cards[task_id]
            self.tasks_layout.removeWidget(card)
            card.deleteLater()
            del self.task_cards[task_id]

            # 刷新配额标题与空状态可见性（C2/C3）
            self._update_tasks_label()

        # 移除任务
        self.manager.remove_task(task_id)

        InfoBar.info(
            title="任务已停止",
            content=f"原因：{reason}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _on_data_updated(self, task_id: str, values: dict):
        """数据更新事件"""
        if task_id in self.task_cards:
            self.task_cards[task_id].update_values(values)
            # 采集次数改为内存计数+1（v1.2.0 架构评审裁决），彻底删除每周期查库
            self.task_cards[task_id].increment_count()

    def _on_error(self, task_id: str, error_msg: str):
        """错误事件"""
        InfoBar.error(
            title="错误",
            content=error_msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000
        )

    def _on_limit_reached(self):
        """任务数量达到上限"""
        InfoBar.warning(
            title="提示",
            content=f"最多只能同时监控 {config.MAX_MONITOR_TASKS} 个任务",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000
        )

    def _on_stop_task(self, task_id: str):
        """停止任务"""
        self.manager.stop_task(task_id)

    def _on_pause_task(self, task_id: str):
        """暂停任务（C4）：调用 manager.pause_task 成功后回调卡片更新暂停态显示"""
        if self.manager.pause_task(task_id):
            card = self.task_cards.get(task_id)
            if card:
                card.set_paused(True)

    def _on_resume_task(self, task_id: str):
        """恢复任务（C4）：调用 manager.resume_task 成功后回调卡片更新暂停态显示"""
        if self.manager.resume_task(task_id):
            card = self.task_cards.get(task_id)
            if card:
                card.set_paused(False)
