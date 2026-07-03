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
    InfoBar, InfoBarPosition
)

from core.export_worker import ExportWorker
from data.database import Database
from utils.metrics import get_metric_display_name


class ExportPage(QScrollArea):
    """数据导出页面"""

    def __init__(self, parent=None):
        """初始化页面"""
        super().__init__(parent)

        # 设置对象名称
        self.setObjectName("exportPage")

        # 数据库实例
        self.db = Database()

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

        # 页面标题
        title = StrongBodyLabel("数据导出")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")
        main_layout.addWidget(title)

        # ========== 任务选择区域 ==========
        select_card = CardWidget()
        select_layout = QVBoxLayout(select_card)
        select_layout.setContentsMargins(20, 20, 20, 20)
        select_layout.setSpacing(15)

        # 标题
        select_title = StrongBodyLabel("选择监控任务")
        select_layout.addWidget(select_title)

        # 任务选择行
        task_row_layout = QHBoxLayout()
        task_row_layout.setSpacing(15)

        task_label = BodyLabel("监控任务:")
        self.task_combo = ComboBox()
        self.task_combo.setPlaceholderText("请选择要导出的监控任务")
        self.task_combo.setMinimumWidth(400)
        self.task_combo.currentIndexChanged.connect(self._on_task_selected)

        # 刷新按钮
        self.refresh_button = PushButton("刷新", self, FluentIcon.SYNC)
        self.refresh_button.clicked.connect(self._load_tasks)

        task_row_layout.addWidget(task_label)
        task_row_layout.addWidget(self.task_combo, 1)
        task_row_layout.addWidget(self.refresh_button)

        select_layout.addLayout(task_row_layout)

        main_layout.addWidget(select_card)

        # ========== 任务信息区域 ==========
        info_card = CardWidget()
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(20, 20, 20, 20)
        info_layout.setSpacing(12)

        # 标题
        info_title = StrongBodyLabel("任务信息")
        info_layout.addWidget(info_title)

        # 进程信息行
        process_layout = QHBoxLayout()
        process_layout.setSpacing(20)
        process_layout.addWidget(BodyLabel("进程名称:"))
        self.process_name_label = CaptionLabel("未选择任务")
        process_layout.addWidget(self.process_name_label)
        process_layout.addWidget(BodyLabel("PID:"))
        self.pid_label = CaptionLabel("-")
        process_layout.addWidget(self.pid_label)
        process_layout.addStretch()
        info_layout.addLayout(process_layout)

        # 监控指标行
        metric_layout = QHBoxLayout()
        metric_layout.setSpacing(20)
        metric_layout.addWidget(BodyLabel("监控指标:"))
        self.metric_label = CaptionLabel("-")
        metric_layout.addWidget(self.metric_label)
        metric_layout.addStretch()
        info_layout.addLayout(metric_layout)

        # 时间范围行
        time_layout = QHBoxLayout()
        time_layout.setSpacing(20)
        time_layout.addWidget(BodyLabel("开始时间:"))
        self.start_time_label = CaptionLabel("-")
        time_layout.addWidget(self.start_time_label)
        time_layout.addWidget(BodyLabel("结束时间:"))
        self.end_time_label = CaptionLabel("-")
        time_layout.addWidget(self.end_time_label)
        time_layout.addStretch()
        info_layout.addLayout(time_layout)

        # 数据统计行
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(20)
        stats_layout.addWidget(BodyLabel("数据点数量:"))
        self.data_count_label = CaptionLabel("-")
        stats_layout.addWidget(self.data_count_label)
        stats_layout.addWidget(BodyLabel("任务状态:"))
        self.status_label = CaptionLabel("-")
        stats_layout.addWidget(self.status_label)
        stats_layout.addStretch()
        info_layout.addLayout(stats_layout)

        main_layout.addWidget(info_card)

        # ========== 导出设置区域 ==========
        export_card = CardWidget()
        export_layout = QVBoxLayout(export_card)
        export_layout.setContentsMargins(20, 20, 20, 20)
        export_layout.setSpacing(15)

        # 标题
        export_title = StrongBodyLabel("导出设置")
        export_layout.addWidget(export_title)

        # 保存路径行
        path_layout = QHBoxLayout()
        path_layout.setSpacing(15)

        path_label = BodyLabel("保存路径:")
        self.path_edit = LineEdit()
        self.path_edit.setPlaceholderText("点击浏览选择保存位置")
        self.path_edit.setReadOnly(True)

        self.browse_button = PushButton("浏览", self, FluentIcon.FOLDER)
        self.browse_button.clicked.connect(self._browse_save_path)

        path_layout.addWidget(path_label)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.browse_button)

        export_layout.addLayout(path_layout)

        # 格式说明
        format_hint = CaptionLabel("导出格式: CSV文件 (逗号分隔值，支持Excel打开)")
        format_hint.setStyleSheet("color: #666;")
        export_layout.addWidget(format_hint)

        main_layout.addWidget(export_card)

        # ========== 导出按钮区域 ==========
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.export_button = PrimaryPushButton("导出数据", self, FluentIcon.DOWNLOAD)
        self.export_button.setMinimumWidth(150)
        self.export_button.clicked.connect(self._export_data)

        button_layout.addWidget(self.export_button)
        button_layout.addStretch()

        main_layout.addLayout(button_layout)

        # 导出进度提示（默认隐藏，导出期间显示已处理行数）
        self.export_status_label = CaptionLabel("")
        self.export_status_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.export_status_label)

        # 添加底部伸缩空间
        main_layout.addStretch()

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
            InfoBar.info(
                title="提示",
                content="暂无可导出的监控任务数据",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            # 清空显示
            self._clear_task_info()
            self.current_task_id = None
            self.current_task = None
            return

        # 添加到下拉框
        for task in tasks:
            # 单指标保持原格式（显示指标名），多指标显示"N项指标"
            if len(task.metric_types) == 1:
                metric_text = get_metric_display_name(task.metric_types[0])
            else:
                metric_text = f"{len(task.metric_types)}项指标"
            display_text = (
                f"{task.process_name} (PID: {task.pid}) - "
                f"{metric_text} - "
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
                title="错误",
                content="无法加载任务信息",
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
            metric_display = f"{len(metric_names)} 项: {'、'.join(metric_names[:3])}…"
        else:
            metric_display = f"{len(metric_names)} 项: {'、'.join(metric_names)}"
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
        self.data_count_label.setText(f"采集 {sample_count} 次（共 {data_count} 条数据）")

        # 更新任务状态
        status_text = "运行中" if task.status == "running" else "已停止"
        self.status_label.setText(status_text)

    def _clear_task_info(self):
        """清空任务信息显示"""
        self.process_name_label.setText("未选择任务")
        self.pid_label.setText("-")
        self.metric_label.setText("-")
        self.metric_label.setToolTip("")
        self.start_time_label.setText("-")
        self.end_time_label.setText("-")
        self.data_count_label.setText("-")
        self.status_label.setText("-")

    def _browse_save_path(self):
        """浏览保存路径"""
        if not self.current_task:
            InfoBar.warning(
                title="提示",
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
            metric_name = f"{len(self.current_task.metric_types)}项指标"
        # 移除文件名中的非法字符
        process_name = self.current_task.process_name.replace('.exe', '')
        default_filename = f"{process_name}_{metric_name}_{timestamp}.csv"

        # 打开文件保存对话框
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择保存位置",
            default_filename,
            "CSV文件 (*.csv)"
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
                title="提示",
                content="请先选择要导出的监控任务",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        # 检查是否选择了保存路径
        save_path = self.path_edit.text().strip()
        if not save_path:
            InfoBar.warning(
                title="提示",
                content="请先选择保存路径",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000
            )
            return

        # 轻量存在性检查（COUNT 查询，不拉取全部数据，避免为了校验而重复加载大数据集）
        if self.db.get_data_point_count(self.current_task_id) == 0:
            InfoBar.warning(
                title="提示",
                content="该任务暂无数据可导出",
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

        InfoBar.success(
            title="导出成功",
            content=f"已导出 {row_count} 次采集（{point_count} 条数据）",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000
        )

        # 询问是否打开文件所在文件夹
        self._ask_open_folder(save_path)

        self._release_export_worker()

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

        # 打开文件夹
        try:
            os.startfile(folder_path)
        except Exception:
            pass

    def showEvent(self, event):
        """页面显示事件"""
        super().showEvent(event)
        # 每次显示时刷新任务列表
        self._load_tasks()
