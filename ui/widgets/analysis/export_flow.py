"""
分析页"导出报告 ▾"的交互流程：选位置 → 确认覆盖 → 离屏渲染图片（主线程）→ 交给视图模型
启动导出线程；进度、完成（带"打开文件夹"与"复制路径"）与失败提示（失败常驻、需手动关闭）。

默认文件名由设置页的命名模板生成（core.naming_template，默认 {process}_{build}_{note}_{date}）：
- 报告：保存对话框里选 <模板>_报告.png，同目录同基名再写 <模板>.csv 与 <模板>_摘要.csv；
  模板里写了 {kind} 时类别由它给出，三个文件各取 报告 / 明细 / 摘要，不再追加后缀；
- CSV：只写明细 CSV（含元数据、已运行秒数与空洞空行）；
- 图片：只写报告图片；
- 证据包：保存对话框里选 zip，三件套先写到临时工作目录，再连同 task.json、README.txt 与
  日志摘录打成一个 zip（core.evidence_worker）；
- 复制：报告图片复制到剪贴板（与报告同一渲染管线，不经线程）；
- 复制结论：结论条的"复制结论 ▾"（core.conclusion_text），复制的就是结论条与摘要表当前显示的内容。
"""
import logging
import os
from contextlib import suppress
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QStandardPaths, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from qfluentwidgets import PushButton

import app_config
from core.conclusion_text import FORMAT_LABELS, MetricLine, build_conclusion, render
from core.evidence import EVIDENCE_SUFFIX
from core.evidence_worker import EvidenceWorker
from core.naming_template import (KIND_DETAIL, KIND_EVIDENCE, KIND_REPORT, KIND_SUMMARY,
                                  default_filename, uses_field)
from core.report import (DATA_CSV_SUFFIX, REPORT_IMAGE_SUFFIX, SUMMARY_CSV_SUFFIX,
                         paths_from_image_choice, task_basename)
from ui.formatting import format_count
from ui.services.notifier import (EVENT_COPY_DONE, EVENT_COPY_FAILED, EVENT_EXPORT_BLOCKED,
                                  EVENT_EXPORT_DONE, EVENT_EXPORT_FAILED)
from ui.viewmodels.analysis_vm import EXPORT_BUSY, EXPORT_MAINTENANCE
from ui.widgets.analysis.toolbar import (EXPORT_COPY, EXPORT_CSV, EXPORT_EVIDENCE, EXPORT_IMAGE,
                                         EXPORT_REPORT)
from ui.widgets.analysis.verdict_banner import BASIS_PREFIX

logger = logging.getLogger(__name__)

NO_DATA = ("暂无可导出数据", "请先选择有数据的任务")
MAINTENANCE = ("暂时无法导出", "正在清理数据库，请稍后再导出")
BUSY = ("正在导出", "上一次导出尚未完成")
EVIDENCE_DIALOG_TITLE = "导出证据包"
EVIDENCE_FILTER = "ZIP 压缩包 (*.zip)"
COPY_PATH_TEXT = "复制路径"
COPY_PATH_TITLE = "路径已复制"
COPY_PATH_FILE = "导出文件的完整路径已复制到剪贴板"
COPY_PATH_FOLDER = "导出了多个文件，已复制它们所在的文件夹路径"
COPY_PATH_FAILED = ("复制失败", "复制路径失败，请查看日志")
COPY_CONCLUSION_TITLE = "复制成功"
COPY_CONCLUSION_CONTENT = "结论已复制到剪贴板（{kind}）"
COPY_CONCLUSION_FAILED = ("复制失败", "复制结论失败，请查看日志")


class AnalysisExportFlow(QObject):
    """导出菜单的流程控制（页面持有，一页一个）。

    render: 无参函数，返回报告 QImage（离屏渲染，失败抛异常）。
    status_changed(str)：页面显示在工具条下方的导出状态文字（空串清除）。
    """

    status_changed = pyqtSignal(str)

    def __init__(self, vm, services, host, render: Callable[[], QImage], parent=None, *,
                 name_template: Optional[Callable[[], str]] = None):
        """name_template：返回当前文件命名模板的函数（缺省读应用配置；测试可注入固定值）。"""
        super().__init__(parent)
        self._vm = vm
        self._services = services
        self._host = host
        self._render = render
        self._name_template = name_template or app_config.get_export_name_template
        self.last_dir = (QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
                         or os.path.expanduser('~'))
        self.open_folder_button: Optional[PushButton] = None
        self.copy_path_button: Optional[PushButton] = None
        vm.export_progress.connect(self._on_progress)
        vm.export_finished.connect(self._on_finished)
        vm.export_failed.connect(self._on_failed)

    def _notify(self, event: str, level: str, title: str, content: str,
                duration: Optional[int] = None):
        """所有提示都经 Notifier.route：同一事件按 key 合并，窗口在后台时走托盘气泡。"""
        return self._services.notifier.route(
            event, level, title, content, key=event, duration=duration, anchor=self._host)

    def _warn(self, notice, duration: int = 3000) -> None:
        self._notify(EVENT_EXPORT_BLOCKED, 'warning', *notice, duration=duration)

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
        template = self._name_template()
        has_kind = uses_field(template, 'kind')
        now = self._services.clock.now()

        def default_name(file_kind: str) -> str:
            return default_filename(template, task, file_kind, now=now)

        if kind == EXPORT_REPORT:
            default = default_name(KIND_REPORT)
            chosen = self._ask("导出报告", default, "PNG 图片 (*.png)")
            if not chosen:
                return False
            targets = self._report_targets(chosen, default, default_name, has_kind)
        elif kind == EXPORT_CSV:
            chosen = self._ask("导出 CSV", default_name(KIND_DETAIL), "CSV 文件 (*.csv)")
            if not chosen:
                return False
            targets = dict(csv_path=chosen)
        elif kind == EXPORT_IMAGE:
            chosen = self._ask("导出报告图片", default_name(KIND_REPORT), "PNG 图片 (*.png)")
            if not chosen:
                return False
            targets = dict(image_path=chosen)
        elif kind == EXPORT_EVIDENCE:
            chosen = self._ask(EVIDENCE_DIALOG_TITLE, default_name(KIND_EVIDENCE),
                               EVIDENCE_FILTER)
            if not chosen:
                return False
            if not chosen.lower().endswith(EVIDENCE_SUFFIX):
                chosen += EVIDENCE_SUFFIX
            targets = dict(out_path=chosen)
        else:
            raise ValueError(f"未知的导出项：{kind!r}")
        existing = [p for p in targets.values() if os.path.exists(p)]
        if existing and not self._services.dialogs.confirm(
                self._host.window(), "文件已存在",
                "以下文件已存在，是否覆盖？\n" + "\n".join(existing), "覆盖"):
            return False
        image = None
        if 'image_path' in targets or kind == EXPORT_EVIDENCE:
            image = self._safe_render()
            if image is None:
                return False
        if kind == EXPORT_EVIDENCE:
            reason = self._start_evidence(base, image, chosen)
        else:
            reason = vm.start_export(vm.report_job(image=image, **targets))
        if reason == EXPORT_MAINTENANCE:
            self._warn(MAINTENANCE, 4000)
            return False
        if reason == EXPORT_BUSY:
            self._warn(BUSY, 2000)
            return False
        self.last_dir = os.path.dirname(chosen) or self.last_dir
        self.status_changed.emit("正在导出…")
        return True

    @staticmethod
    def _report_targets(chosen: str, default: str, default_name, has_kind: bool) -> dict:
        """报告三件套的目标路径。模板没有 {kind}：沿用"<基名>_报告.png / .csv / _摘要.csv"；
        有 {kind}：图片就是所选路径，用户没改默认名时明细与摘要各取自己的类别渲染，改过名则
        同基名加 .csv / _摘要.csv。"""
        if not has_kind:
            paths = paths_from_image_choice(chosen)
            return dict(image_path=paths.image, csv_path=paths.csv, summary_path=paths.summary)
        directory, name = os.path.split(os.path.normpath(chosen))
        if name == default:
            csv_name, summary_name = default_name(KIND_DETAIL), default_name(KIND_SUMMARY)
        else:
            stem = os.path.splitext(name)[0]
            csv_name, summary_name = stem + DATA_CSV_SUFFIX, stem + SUMMARY_CSV_SUFFIX
        return dict(image_path=os.path.join(directory, name),
                    csv_path=os.path.join(directory, csv_name),
                    summary_path=os.path.join(directory, summary_name))

    def _start_evidence(self, base: str, image: QImage, out_path: str) -> Optional[str]:
        """证据包：三件套按报告的文件名组装（证据包工作线程把它们写到临时目录再打包）。"""
        vm = self._vm
        names = paths_from_image_choice(base + REPORT_IMAGE_SUFFIX)
        job = vm.report_job(image=image, image_path=names.image, csv_path=names.csv,
                            summary_path=names.summary)
        analysis = vm.last_analysis
        log_path = self._log_path()
        clock = self._services.clock

        def factory(db, report_job):
            return EvidenceWorker(db, report_job, analysis=analysis, out_path=out_path,
                                  log_path=log_path, clock=clock)

        return vm.start_export(job, worker_factory=factory)

    def _log_path(self) -> Optional[str]:
        provider = getattr(self._services, 'log_path', None)
        if provider is None:
            return None
        try:
            return provider() or None
        except Exception:
            logger.warning("取日志路径失败，证据包不收录日志摘录", exc_info=True)
            return None

    def _ask(self, title: str, filename: str, name_filter: str) -> Optional[str]:
        default = os.path.normpath(os.path.join(self.last_dir, filename))
        return self._services.dialogs.ask_save_path(self._host, title, default, name_filter)

    def _safe_render(self) -> Optional[QImage]:
        try:
            return self._render()
        except Exception:
            logger.error("渲染报告图片失败", exc_info=True)
            self._notify(EVENT_EXPORT_FAILED, 'error', "导出失败", "生成报告图片失败，请查看日志")
            return None

    def _copy(self) -> bool:
        image = self._safe_render()
        if image is None:
            return False
        try:
            self._services.dialogs.copy_pixmap(QPixmap.fromImage(image))
        except Exception:
            logger.error("复制报告图片失败", exc_info=True)
            self._notify(EVENT_COPY_FAILED, 'error', "复制失败", "复制报告图片失败，请查看日志")
            return False
        self._notify(EVENT_COPY_DONE, 'success', "复制成功", "报告图片已复制到剪贴板", 2000)
        return True

    # ---------- 复制结论 ----------

    def copy_conclusion(self, fmt: str) -> bool:
        """结论条"复制结论 ▾"：把当前显示的结论（结论条的判定与主句、摘要表各行、元数据、分段与
        成员结论）按 fmt（core.conclusion_text.FORMAT_*）复制到剪贴板；返回是否已复制。"""
        vm, host = self._vm, self._host
        task, analysis = vm.current_task, vm.last_analysis
        banner = getattr(host, 'verdict_banner', None)
        table = getattr(host, 'summary_table', None)
        summary = banner.summary() if banner is not None else None
        if (task is None or analysis is None or not analysis.has_data or summary is None
                or table is None):
            self._warn(NO_DATA, 2000)
            return False
        try:
            options = vm.member_options if vm.is_group_task else []
            conclusion = build_conclusion(
                task, analysis, verdict=summary.verdict, sentence=summary.sentence,
                metrics=[MetricLine.from_texts(row.texts, row.note) for row in table.rows()],
                basis=banner.basis().removeprefix(BASIS_PREFIX),
                member_names={o.key: o.name for o in options},
                member_instances={o.key: o.instances for o in options
                                  if o.instances is not None},
                with_members=vm.is_group_task and vm.current_member is None)
            self._services.dialogs.copy_text(render(conclusion, fmt))
        except Exception:
            logger.error("复制结论失败", exc_info=True)
            self._notify(EVENT_COPY_FAILED, 'error', *COPY_CONCLUSION_FAILED)
            return False
        self._notify(EVENT_COPY_DONE, 'success', COPY_CONCLUSION_TITLE,
                     COPY_CONCLUSION_CONTENT.format(kind=FORMAT_LABELS[fmt]), 2000)
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
        bar = self._notify(EVENT_EXPORT_DONE, 'success', "导出成功", detail, 8000)
        if bar is None:
            # 窗口在后台时只发了托盘气泡：没有地方放按钮
            self.open_folder_button = self.copy_path_button = None
            return
        self.open_folder_button = PushButton("打开文件夹")
        self.open_folder_button.clicked.connect(lambda: self._open_folder(folder))
        bar.addWidget(self.open_folder_button)
        self.copy_path_button = PushButton(COPY_PATH_TEXT)
        self.copy_path_button.clicked.connect(lambda: self._copy_path(result.paths, folder))
        bar.addWidget(self.copy_path_button)

    def _on_failed(self, message: str) -> None:
        self.status_changed.emit("")
        self._notify(EVENT_EXPORT_FAILED, 'error', "导出失败", message)

    def _copy_path(self, paths, folder: str) -> bool:
        """导出完成提示上的"复制路径"：一个文件复制完整路径，多个文件复制所在文件夹。"""
        paths = list(paths or [])
        single = len(paths) == 1
        text = os.path.normpath(paths[0] if single else folder)
        try:
            self._services.dialogs.copy_text(text)
        except Exception:
            logger.error("复制导出路径失败", exc_info=True)
            self._notify(EVENT_COPY_FAILED, 'error', *COPY_PATH_FAILED)
            return False
        self._notify(EVENT_COPY_DONE, 'success', COPY_PATH_TITLE,
                     COPY_PATH_FILE if single else COPY_PATH_FOLDER, 2000)
        return True

    def _open_folder(self, folder: str) -> None:
        with suppress(OSError):
            self._services.dialogs.open_folder(folder)

