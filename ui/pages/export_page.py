"""
数据导出页面
将监控任务的数据导出为CSV文件
"""
import os
from datetime import datetime
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QScrollArea,
                             QFileDialog, QSizePolicy)
from qfluentwidgets import (
    ComboBox, CardWidget, PrimaryPushButton, PushButton, FluentIcon,
    StrongBodyLabel, BodyLabel, CaptionLabel, LineEdit,
    InfoBar, InfoBarPosition, HorizontalSeparator
)

from core.export_worker import ExportWorker
from data.database import Database
from ui.typography import DataCaptionLabel, PageTitleLabel
from utils.metrics import get_metric_display_name


class ExportPage(QScrollArea):
    """数据导出页面"""

    def __init__(self, parent=None, db=None):
        """初始化页面

        Args:
            parent: 父窗口
            db: 数据库实例（可选，默认回退新建 Database()；生产路径必须由
                MainWindow 注入，回退仅为兼容兜底）
        """
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("exportPage")

        # 数据库实例
        self.db = db if db is not None else Database()

        # 当前选中的任务ID
        self.current_task_id = None

        # 当前任务信息
        self.current_task = None

        # 导出工作线程（持引用防GC；导出期间非None，closeEvent按shutdown_thread模式接入）
        self._export_worker = None

        # 初始化UI
        self._init_ui()

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
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(20)

        title = PageTitleLabel("导出数据")
        main_layout.addWidget(title)

        # 导出是一个连续任务：选任务 -> 确认摘要 -> 选位置并导出。
        # 收敛到同一张 Fluent 卡片，避免三张等权大卡片打断操作流。
        export_card = CardWidget()
        export_layout = QVBoxLayout(export_card)
        export_layout.setContentsMargins(20, 18, 20, 20)
        export_layout.setSpacing(14)

        export_layout.addWidget(StrongBodyLabel("选择要导出的任务"))

        # 任务选择行
        task_row_layout = QHBoxLayout()
        task_row_layout.setSpacing(15)

        self.task_combo = ComboBox()
        self.task_combo.setPlaceholderText("选择监控任务")
        self.task_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.task_combo.currentIndexChanged.connect(self._on_task_selected)

        # 刷新按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._load_tasks)

        task_row_layout.addWidget(self.task_combo, 1)
        task_row_layout.addWidget(self.refresh_button)

        export_layout.addLayout(task_row_layout)

        # 空状态占位（C3）：任务下拉为空时显示，同时禁用下拉框与浏览/导出按钮，
        # 避免用户点击后触发既有"未选任务"提示与本占位重复打扰（评审修订 N6）
        self.empty_state_label = BodyLabel(
            "暂无可导出的数据，请先创建监控任务")
        self.empty_state_label.setAlignment(Qt.AlignCenter)
        self.empty_state_label.setVisible(False)
        export_layout.addWidget(self.empty_state_label)

        self.summary_top_separator = HorizontalSeparator()
        export_layout.addWidget(self.summary_top_separator)

        # 紧凑任务摘要：保留原有公共 label 属性，便于既有逻辑与测试继续读取。
        self.summary_widget = QWidget()
        summary_layout = QVBoxLayout(self.summary_widget)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(8)

        # 进程信息行
        process_layout = QHBoxLayout()
        process_layout.setSpacing(20)
        self.process_name_label = StrongBodyLabel("未选择任务")
        process_layout.addWidget(self.process_name_label)
        process_layout.addSpacing(8)
        process_layout.addWidget(CaptionLabel("PID"))
        self.pid_label = DataCaptionLabel("—")
        process_layout.addWidget(self.pid_label)
        process_layout.addSpacing(8)
        process_layout.addWidget(CaptionLabel("状态"))
        self.status_label = CaptionLabel("—")
        process_layout.addWidget(self.status_label)
        process_layout.addStretch()
        summary_layout.addLayout(process_layout)

        # 监控指标行
        metric_layout = QHBoxLayout()
        metric_layout.setSpacing(20)
        metric_layout.addWidget(CaptionLabel("指标"))
        self.metric_label = CaptionLabel("—")
        metric_layout.addWidget(self.metric_label)
        metric_layout.addStretch()
        summary_layout.addLayout(metric_layout)

        # 时间范围行
        time_layout = QHBoxLayout()
        time_layout.setSpacing(20)
        time_layout.addWidget(CaptionLabel("时间"))
        self.start_time_label = DataCaptionLabel("—")
        time_layout.addWidget(self.start_time_label)
        time_layout.addWidget(CaptionLabel("—"))
        self.end_time_label = DataCaptionLabel("—")
        time_layout.addWidget(self.end_time_label)
        time_layout.addSpacing(12)
        time_layout.addWidget(CaptionLabel("数据"))
        self.data_count_label = DataCaptionLabel("—")
        time_layout.addWidget(self.data_count_label)
        time_layout.addStretch()
        summary_layout.addLayout(time_layout)
        export_layout.addWidget(self.summary_widget)

        self.summary_bottom_separator = HorizontalSeparator()
        export_layout.addWidget(self.summary_bottom_separator)

        # 保存路径行
        path_layout = QHBoxLayout()
        path_layout.setSpacing(15)

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

        export_layout.addLayout(path_layout)

        format_hint = CaptionLabel("CSV · UTF-8 · 可用 Excel 打开")
        export_layout.addWidget(format_hint)

        main_layout.addWidget(export_card)

        # 导出进度提示（默认隐藏，导出期间显示已处理行数）
        self.export_status_label = CaptionLabel("")
        self.export_status_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.export_status_label)

        # 添加底部伸缩空间
        main_layout.addStretch()

    def _set_empty_state(self, is_empty: bool) -> None:
        """
        切换空状态占位说明与控件禁用（C3，评审修订 N6）

        任务下拉为空时显示占位说明并禁用下拉框、浏览按钮；导出按钮的可用性
        额外与"导出是否正在进行中"做 AND——不能在导出线程运行期间被这里误
        重新启用（例如导出耗时较长、用户中途切走页面又切回来触发 showEvent
        重新加载任务列表的场景），也不能在导出线程结束后被空状态覆盖为禁用。
        按钮禁用后，既有 _browse_save_path/_export_data 内的"未选任务"/
        "未选路径"提示不会被用户点击触发，不与本占位重复打扰。

        Args:
            is_empty: 当前是否处于"无可导出任务"的空状态
        """
        self.empty_state_label.setVisible(is_empty)
        self.summary_widget.setVisible(not is_empty)
        self.summary_top_separator.setVisible(not is_empty)
        self.task_combo.setEnabled(not is_empty)
        self.browse_button.setEnabled(not is_empty)

        is_exporting = self._export_worker is not None and self._export_worker.isRunning()
        if not is_exporting:
            self.export_button.setEnabled(not is_empty)

    def _load_tasks(self):
        """加载任务列表"""
        # 保存当前选中的任务ID
        current_task_id = self.current_task_id

        # 清空下拉框
        self.task_combo.clear()

        # 获取所有任务
        all_tasks = self.db.get_all_tasks()

        # 过滤掉数据点太少的任务
        tasks = []
        for task in all_tasks:
            # 获取数据点数量
            data_count = self.db.get_data_point_count(task.task_id)
            # 至少要有1个数据点才能导出
            if data_count > 0:
                tasks.append(task)

        if not tasks:
            # 清空显示
            self._clear_task_info()
            self.current_task_id = None
            self.current_task = None
            self._set_empty_state(True)
            return

        self._set_empty_state(False)

        # 添加到下拉框
        for task in tasks:
            # 单指标保持原格式（显示指标名），多指标显示"N项指标"
            if len(task.metric_types) == 1:
                metric_text = get_metric_display_name(task.metric_types[0])
            else:
                metric_text = f"{len(task.metric_types)} 项指标"
            display_text = (
                f"{task.process_name} · PID {task.pid} · "
                f"{metric_text} · "
                f"{task.start_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            self.task_combo.addItem(display_text)
            # 设置任务ID作为userData
            index = self.task_combo.count() - 1
            self.task_combo.setItemData(index, task.task_id)

        # 尝试恢复之前选中的任务
        if current_task_id:
            for i in range(self.task_combo.count()):
                if self.task_combo.itemData(i) == current_task_id:
                    self.task_combo.setCurrentIndex(i)
                    self._load_task_info(current_task_id)
                    return

        # 如果没有恢复成功，选择第一个
        if self.task_combo.count() > 0:
            self.task_combo.setCurrentIndex(-1)
            self.task_combo.setCurrentIndex(0)

    def _on_task_selected(self, index: int):
        """任务选择事件"""
        if index < 0:
            self._clear_task_info()
            return

        # 获取任务ID
        task_id = self.task_combo.itemData(index)
        if not task_id:
            return

        self.current_task_id = task_id

        # 加载任务信息
        self._load_task_info(task_id)

    def _load_task_info(self, task_id: str):
        """
        加载并显示任务信息

        Args:
            task_id: 任务ID
        """
        # 获取任务信息
        task = self.db.get_task(task_id)
        if not task:
            InfoBar.error(
                title="任务加载失败",
                content="无法读取所选任务",
                parent=self,
                position=InfoBarPosition.TOP
            )
            self._clear_task_info()
            return

        self.current_task = task

        # 更新进程信息
        self.process_name_label.setText(task.process_name)
        self.pid_label.setText(str(task.pid))

        # 更新监控指标（多指标显示"N 项: 名1、名2…"，超过3个截断并用tooltip显示全部）
        metric_names = [get_metric_display_name(m) for m in task.metric_types]
        if len(metric_names) > 3:
            metric_display = f"{len(metric_names)} 项 · {'、'.join(metric_names[:3])}…"
        else:
            metric_display = f"{len(metric_names)} 项 · {'、'.join(metric_names)}"
        self.metric_label.setText(metric_display)
        self.metric_label.setToolTip('、'.join(metric_names))

        # 更新时间范围
        self.start_time_label.setText(task.start_time.strftime('%Y-%m-%d %H:%M:%S'))
        if task.end_time:
            self.end_time_label.setText(task.end_time.strftime('%Y-%m-%d %H:%M:%S'))
        else:
            self.end_time_label.setText("进行中")

        # 更新数据统计（采集次数与数据点总数）
        data_count = self.db.get_data_point_count(task_id)
        sample_count = self.db.get_sample_count(task_id)
        self.data_count_label.setText(
            f"{sample_count} 次采集 · {data_count} 条数据")

        # 更新任务状态
        status_text = "运行中" if task.status == "running" else "已停止"
        self.status_label.setText(status_text)

    def _clear_task_info(self):
        """清空任务信息显示"""
        self.process_name_label.setText("未选择任务")
        self.pid_label.setText("—")
        self.metric_label.setText("—")
        self.metric_label.setToolTip("")
        self.start_time_label.setText("—")
        self.end_time_label.setText("—")
        self.data_count_label.setText("—")
        self.status_label.setText("—")

    def _browse_save_path(self):
        """浏览保存路径"""
        if not self.current_task:
            InfoBar.warning(
                title="请选择任务",
                content="请先选择要导出的监控任务",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        # 生成默认文件名（单指标保持指标名，多指标显示"N项指标"）
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if len(self.current_task.metric_types) == 1:
            metric_name = get_metric_display_name(self.current_task.metric_types[0])
        else:
            metric_name = f"{len(self.current_task.metric_types)} 项指标"
        # 移除文件名中的非法字符
        process_name = self.current_task.process_name.replace('.exe', '')
        default_filename = f"{process_name}_{metric_name}_{timestamp}.csv"

        # 打开文件保存对话框
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择保存位置",
            default_filename,
            "CSV 文件 (*.csv)"
        )

        if file_path:
            self.path_edit.setText(file_path)

    def _export_data(self):
        """
        导出数据到CSV文件（v1.2.0 批3 线程化：大数据量导出不再阻塞UI）

        实际的游标 fetchmany 分批读取 + pivot_rows 流式写文件在 ExportWorker
        后台线程内完成，本方法只负责校验、禁用按钮、启动线程与持有引用防GC。
        """
        # 已有导出在进行中，忽略重复点击
        if self._export_worker is not None and self._export_worker.isRunning():
            return

        # 检查是否选择了任务
        if not self.current_task_id or not self.current_task:
            InfoBar.warning(
                title="请选择任务",
                content="请先选择要导出的监控任务",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        # 未预先选路径时，直接在主操作中打开保存对话框，
        # 使用户无需先点一次「选择位置」再点「导出」。
        save_path = self.path_edit.text().strip()
        if not save_path:
            self._browse_save_path()
            save_path = self.path_edit.text().strip()
            if not save_path:
                return

        # 轻量存在性检查（COUNT 查询，不拉取全部数据，避免为了校验而重复加载大数据集）
        if self.db.get_data_point_count(self.current_task_id) == 0:
            InfoBar.warning(
                title="暂无可导出数据",
                content="该任务没有可导出的采集数据",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        self.export_button.setEnabled(False)
        self.export_status_label.setText("正在导出…")

        self._export_worker = ExportWorker(
            self.db.db_path, self.current_task, save_path, metric_type=None, parent=self)
        self._export_worker.export_progress.connect(self._on_export_progress)
        self._export_worker.export_finished.connect(self._on_export_finished)
        self._export_worker.error_occurred.connect(self._on_export_error)
        self._export_worker.start()

    def _on_export_progress(self, processed: int):
        """导出进度更新（已处理的数据点行数）"""
        self.export_status_label.setText(f"正在导出… 已处理 {processed} 条数据")

    def _on_export_finished(self, save_path: str, row_count: int, point_count: int):
        """导出完成"""
        self.export_button.setEnabled(True)
        self.export_status_label.setText("")

        info_bar = InfoBar.success(
            title="导出成功",
            content=f"已导出 {row_count} 次采集（{point_count} 条数据）",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=8000
        )
        self._release_export_worker()

        # 导出完成不再自动抢焦点打开资源管理器；保留显式快捷操作。
        self.open_folder_button = PushButton("打开文件夹")
        self.open_folder_button.clicked.connect(
            lambda: self._ask_open_folder(save_path))
        info_bar.addWidget(self.open_folder_button)

    def _on_export_error(self, msg: str):
        """导出失败"""
        self.export_button.setEnabled(True)
        self.export_status_label.setText("")
        InfoBar.error(
            title="导出失败",
            content=msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000
        )

        self._release_export_worker()

    def _release_export_worker(self):
        """
        导出线程已结束（完成/失败）后释放引用：deleteLater() 交还给 Qt 的对象树，
        并把 self._export_worker 置 None，避免每次导出都新建一个 ExportWorker 却
        始终作为 self 的子对象常驻，多次导出后累积占用内存
        """
        if self._export_worker is not None:
            self._export_worker.deleteLater()
            self._export_worker = None

    def _ask_open_folder(self, file_path: str):
        """
        询问是否打开文件所在文件夹

        Args:
            file_path: 文件路径
        """
        # 获取文件所在目录
        folder_path = os.path.dirname(file_path)

        # 用户在导出成功 InfoBar 中显式选择后才打开文件夹。
        try:
            os.startfile(folder_path)
        except Exception:
            pass

    def showEvent(self, event):
        """页面显示事件"""
        super().showEvent(event)
        # 每次显示时刷新任务列表
        self._load_tasks()
