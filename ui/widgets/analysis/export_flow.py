"""
分析页"导出报告 ▾"的交互流程：选位置 → 确认覆盖 → 离屏渲染图片（主线程）→ 交给视图模型
启动导出线程；进度、完成（带"打开文件夹"）与失败提示（失败常驻、需手动关闭）。

- 报告：保存对话框里选 <任务>_报告.png，同目录同基名再写 <任务>.csv 与 <任务>_摘要.csv；
- CSV：只写明细 CSV（含元数据、已运行秒数与空洞空行）；
- 图片：只写报告图片；
- 复制：报告图片复制到剪贴板（与报告同一渲染管线，不经线程）。
"""
import logging
import os
from contextlib import suppress
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QStandardPaths, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from qfluentwidgets import PushButton

from core.report import (DATA_CSV_SUFFIX, REPORT_IMAGE_SUFFIX, paths_from_image_choice,
                         task_basename)
from ui.formatting import format_count
from ui.viewmodels.analysis_vm import EXPORT_BUSY, EXPORT_MAINTENANCE
from ui.widgets.analysis.toolbar import EXPORT_COPY, EXPORT_CSV, EXPORT_IMAGE, EXPORT_REPORT

logger = logging.getLogger(__name__)

NO_DATA = ("暂无可导出数据", "请先选择有数据的任务")
MAINTENANCE = ("暂时无法导出", "正在清理数据库，请稍后再导出")
BUSY = ("正在导出", "上一次导出尚未完成")


class AnalysisExportFlow(QObject):
    """导出菜单的流程控制（页面持有，一页一个）。

    render: 无参函数，返回报告 QImage（离屏渲染，失败抛异常）。
    status_changed(str)：页面显示在工具条下方的导出状态文字（空串清除）。
    """

    status_changed = pyqtSignal(str)

    def __init__(self, vm, services, host, render: Callable[[], QImage], parent=None):
        super().__init__(parent)
        self._vm = vm
        self._services = services
        self._host = host
        self._render = render
        self.last_dir = (QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
                         or os.path.expanduser('~'))
        self.open_folder_button: Optional[PushButton] = None
        vm.export_progress.connect(self._on_progress)
        vm.export_finished.connect(self._on_finished)
        vm.export_failed.connect(self._on_failed)

    def _warn(self, notice, duration: int = 3000) -> None:
        self._services.notifier.warning(self._host, *notice, duration=duration)

    def run(self, kind: str) -> bool:
        """执行一个导出菜单项；返回是否已启动（复制为是否已复制）。"""
        vm = self._vm
        task = vm.current_task
        if task is None or vm.last_analysis is None or not vm.last_analysis.has_data:
            self._warn(NO_DATA, 2000)
            return False
        if kind == EXPORT_COPY:
            return self._copy()
        if vm.export_worker is not None:
            self._warn(BUSY, 2000)
            return False
        if vm.is_maintenance_running():
            self._warn(MAINTENANCE, 4000)
            return False
        base = task_basename(task)
        if kind == EXPORT_REPORT:
            chosen = self._ask("导出报告", base + REPORT_IMAGE_SUFFIX, "PNG 图片 (*.png)")
            if not chosen:
                return False
            paths = paths_from_image_choice(chosen)
            targets = dict(image_path=paths.image, csv_path=paths.csv,
                           summary_path=paths.summary)
        elif kind == EXPORT_CSV:
            chosen = self._ask("导出 CSV", base + DATA_CSV_SUFFIX, "CSV 文件 (*.csv)")
            if not chosen:
                return False
            targets = dict(csv_path=chosen)
        elif kind == EXPORT_IMAGE:
            chosen = self._ask("导出报告图片", base + REPORT_IMAGE_SUFFIX, "PNG 图片 (*.png)")
            if not chosen:
                return False
            targets = dict(image_path=chosen)
        else:
            raise ValueError(f"未知的导出项：{kind!r}")
        existing = [p for p in targets.values() if os.path.exists(p)]
        if existing and not self._services.dialogs.confirm(
                self._host.window(), "文件已存在",
                "以下文件已存在，是否覆盖？\n" + "\n".join(existing), "覆盖"):
            return False
        image = None
        if 'image_path' in targets:
            image = self._safe_render()
            if image is None:
                return False
        job = vm.report_job(image=image, **targets)
        reason = vm.start_export(job)
        if reason == EXPORT_MAINTENANCE:
            self._warn(MAINTENANCE, 4000)
            return False
        if reason == EXPORT_BUSY:
            self._warn(BUSY, 2000)
            return False
        self.last_dir = os.path.dirname(chosen) or self.last_dir
        self.status_changed.emit("正在导出…")
        return True

    def _ask(self, title: str, filename: str, name_filter: str) -> Optional[str]:
        default = os.path.normpath(os.path.join(self.last_dir, filename))
        return self._services.dialogs.ask_save_path(self._host, title, default, name_filter)

    def _safe_render(self) -> Optional[QImage]:
        try:
            return self._render()
        except Exception:
            logger.error("渲染报告图片失败", exc_info=True)
            self._services.notifier.error(self._host, "导出失败", "生成报告图片失败，请查看日志")
            return None

    def _copy(self) -> bool:
        image = self._safe_render()
        if image is None:
            return False
        try:
            self._services.dialogs.copy_pixmap(QPixmap.fromImage(image))
        except Exception:
            logger.error("复制报告图片失败", exc_info=True)
            self._services.notifier.error(self._host, "复制失败", "复制报告图片失败，请查看日志")
            return False
        self._services.notifier.success(self._host, "复制成功", "报告图片已复制到剪贴板",
                                        duration=2000)
        return True

    # ---------- 线程结果 ----------

    def _on_progress(self, processed: int) -> None:
        self.status_changed.emit(f"正在导出… 已处理 {format_count(processed)} 条数据")

    def _on_finished(self, result) -> None:
        self.status_changed.emit("")
        folder = os.path.dirname(result.paths[0]) if result.paths else self.last_dir
        names = "、".join(os.path.basename(p) for p in result.paths)
        detail = f"已导出 {names}"
        if result.row_count:
            detail += f"（{format_count(result.row_count)} 次采集）"
        bar = self._services.notifier.success(self._host, "导出成功", detail, duration=8000)
        self.open_folder_button = PushButton("打开文件夹")
        self.open_folder_button.clicked.connect(lambda: self._open_folder(folder))
        bar.addWidget(self.open_folder_button)

    def _on_failed(self, message: str) -> None:
        self.status_changed.emit("")
        self._services.notifier.error(self._host, "导出失败", message)

    def _open_folder(self, folder: str) -> None:
        with suppress(OSError):
            self._services.dialogs.open_folder(folder)

