"""
数据库升级进度窗口

大库首次启动时，重建式迁移可能要十几秒到几分钟。主窗口出现之前没有任何界面，用户容易
以为程序卡死而强杀进程，所以组合根在满足阈值时（见 app.bootstrap）先显示这个小窗口：
标题、进度条、"已处理 N / M 行 · 备份保存在 …"；生成分钟汇总阶段显示"正在生成汇总 N / M"。
回退 1.4.1 期间写入的数据较多时，启动校正的导回一步也复用本窗口（标题与说明不同，没有备份）。

线程模型：迁移在 MigrationWorker（BackgroundTask 子类）里执行；迁移运行器的进度回调
(阶段名, 已完成, 总数) 在工作线程里同步调用，这里只发信号，经队列连接回到界面线程更新进度。
run_with_progress_window 在界面线程里轮询等待线程结束、其间处理界面事件（此时还没有
主窗口，也没有其他线程），结束后关闭窗口并把结果（或异常）原样交还调用方。

本模块不导入数据层：要执行的迁移由组合根以可调用对象传入。
"""
import logging
from typing import Any, Callable, Optional, Tuple

from PyQt5.QtCore import QCoreApplication, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ProgressBar, SubtitleLabel, isDarkTheme

import config
from core.workers.base import BackgroundTask, CancelToken
from ui.formatting import format_count

logger = logging.getLogger(__name__)

MIGRATION_WINDOW_TITLE = "正在升级数据库…"
# 进度行：{done}/{total} 为已处理与总行数（千分位）
MIGRATION_PROGRESS_TEMPLATE = "已处理 {done} / {total} 行"
# 生成分钟汇总阶段：{done}/{total} 为已完成与总序列数
MIGRATION_ROLLUPS_TEMPLATE = "正在生成汇总 {done} / {total}"
MIGRATION_PREPARING_TEMPLATE = "正在准备…"
# 已知行数估计时的准备文案：{rows} 为估计行数（千分位）
MIGRATION_PREPARING_ROWS_TEMPLATE = "正在准备（约 {rows} 行）…"
MIGRATION_FINISHING_TEMPLATE = "正在收尾…"
# 有备份时接在每条状态文案之后：{backup} 为旧文件的备份路径
MIGRATION_BACKUP_SUFFIX = " · 备份保存在 {backup}"
MIGRATION_HINT_TEXT = "首次启动新版本需要升级数据库，完成后自动打开主窗口，请勿关闭程序。"

# 导回回退 1.4.1 期间写入的数据（启动校正）时的标题与说明
LEGACY_IMPORT_WINDOW_TITLE = "正在合并回退期间的数据…"
LEGACY_IMPORT_HINT_TEXT = "正在把回退旧版本期间记录的数据并入新版本，完成后自动打开主窗口，请勿关闭程序。"

# 进度阶段名（与 data.db.migrations.rebuild 的 STAGE_ROLLUPS 取值一致；本模块不导入数据层）
STAGE_ROLLUPS = 'rollups'

WINDOW_WIDTH = 460
# 等待迁移线程时处理界面事件的间隔（毫秒）
POLL_MS = 50
# 进度条的满刻度（行数可能超过 32 位整数，按比例换算到这个刻度）
PROGRESS_SCALE = 1000

_BACKGROUND_LIGHT = "#f9f9f9"
_BACKGROUND_DARK = "#272727"

# 迁移函数：参数是进度回调 (阶段名, 已完成行数, 总行数)，返回迁移报告
MigrateFn = Callable[[Callable[[str, int, int], None]], Any]


class MigrationWorker(BackgroundTask):
    """在工作线程里执行迁移；进度经 rows_progress 发回界面线程。

    结果与异常保存在 value / error 上，供等待线程结束后的调用方取回（异常要原样
    抛给组合根，不能只剩 failed 信号里的一段文字）。
    """

    # (阶段名, 已完成, 总数)；数量用 object 避免超过 32 位整数时溢出
    progress = pyqtSignal(str, object, object)

    def __init__(self, migrate: MigrateFn):
        super().__init__()
        self._migrate = migrate
        self.value: Any = None
        self.error: Optional[BaseException] = None
        # 最后一次进度 (阶段名, 已完成, 总数)（线程结束后由调用方补一次显示：队列里的
        # 进度事件不保证在局部事件循环退出前都处理完）
        self.last_progress: Optional[Tuple[str, int, int]] = None

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        self.last_progress = (str(stage), int(done), int(total))
        self.progress.emit(str(stage), int(done), int(total))

    def execute(self, token: CancelToken) -> Any:
        return self._migrate(self._on_progress)

    def handle_result(self, value: Any) -> None:
        self.value = value
        super().handle_result(value)

    def handle_failure(self, exc: Exception) -> None:
        self.error = exc
        super().handle_failure(exc)


class MigrationWindow(QWidget):
    """升级进度小窗口：没有关闭按钮，关闭事件一律忽略，直到 finish()。"""

    def __init__(self, backup_path: Optional[str], estimated_rows: int = 0, parent=None):
        """
        Args:
            backup_path: 旧文件的备份路径（接在每条状态文案后）；None 表示没有备份，不显示
            estimated_rows: 行数估计（收到第一次进度之前显示在准备文案里，0 表示不显示）
        """
        super().__init__(parent, Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self._backup_path = backup_path
        self._finished = False
        self.setWindowTitle(f"{config.APP_NAME} - {MIGRATION_WINDOW_TITLE}")
        self.setFixedWidth(WINDOW_WIDTH)
        self.setObjectName("MigrationWindow")
        background = _BACKGROUND_DARK if isDarkTheme() else _BACKGROUND_LIGHT
        self.setStyleSheet(f"#MigrationWindow {{ background: {background}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        self.title_label = SubtitleLabel(MIGRATION_WINDOW_TITLE, self)
        layout.addWidget(self.title_label)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, PROGRESS_SCALE)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.status_label = BodyLabel(self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.hint_label = CaptionLabel(MIGRATION_HINT_TEXT, self)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        if estimated_rows > 0:
            preparing = MIGRATION_PREPARING_ROWS_TEMPLATE.format(rows=format_count(estimated_rows))
        else:
            preparing = MIGRATION_PREPARING_TEMPLATE
        self._set_status(preparing)

    @property
    def is_finished(self) -> bool:
        return self._finished

    def set_texts(self, title: str, hint: str) -> None:
        """换标题与说明（导回回退期间的数据时复用本窗口）"""
        self.setWindowTitle(f"{config.APP_NAME} - {title}")
        self.title_label.setText(title)
        self.hint_label.setText(hint)

    def _set_status(self, text: str) -> None:
        if self._backup_path:
            text += MIGRATION_BACKUP_SUFFIX.format(backup=self._backup_path)
        self.status_label.setText(text)

    @pyqtSlot(str, object, object)
    def set_stage_progress(self, stage: str, done: int, total: int) -> None:
        """按阶段更新进度：生成汇总阶段按序列数显示，其余（搬样本）按行数（见 set_progress）"""
        if stage != STAGE_ROLLUPS:
            self.set_progress(done, total)
            return
        done, total = int(done), int(total)
        if total <= 0:
            return
        done = min(max(done, 0), total)
        self.progress_bar.setValue(int(done * PROGRESS_SCALE / total))
        self._set_status(MIGRATION_ROLLUPS_TEMPLATE.format(
            done=format_count(done), total=format_count(total)))

    @pyqtSlot(object, object)
    def set_progress(self, done: int, total: int) -> None:
        """更新进度（界面线程调用；声明为槽，跨线程连接时确定在本窗口所在线程执行）"""
        done, total = int(done), int(total)
        if total <= 0:
            return
        done = min(max(done, 0), total)
        self.progress_bar.setValue(int(done * PROGRESS_SCALE / total))
        if done >= total:
            text = MIGRATION_FINISHING_TEMPLATE
        else:
            text = MIGRATION_PROGRESS_TEMPLATE.format(
                done=format_count(done), total=format_count(total))
        self._set_status(text)

    def finish(self) -> None:
        """迁移结束（成功或失败）：允许关闭并关闭窗口"""
        self._finished = True
        self.close()

    def closeEvent(self, event):
        # 迁移进行中不能中断：中途退出会留下 .migrating 临时文件，下次启动还要从头再来
        if not self._finished:
            event.ignore()
            return
        super().closeEvent(event)


def run_with_progress_window(migrate: MigrateFn, *, backup_path: Optional[str],
                             estimated_rows: int = 0,
                             window: Optional[MigrationWindow] = None,
                             title: Optional[str] = None,
                             hint: Optional[str] = None) -> Any:
    """
    显示进度窗口，在工作线程里执行 migrate，等待其结束后关闭窗口并返回结果。

    必须在界面线程、主窗口创建之前调用。migrate 抛出的异常在这里原样重新抛出。

    Args:
        migrate: 要执行的耗时操作，参数为进度回调 (阶段名, 已完成, 总数)
        backup_path: 旧文件的备份路径（显示给用户）；None 表示没有备份
        estimated_rows: 行数估计（收到第一次进度之前显示在准备文案里，0 表示不显示）
        window: 测试注入的窗口；None 时新建
        title / hint: 标题与说明（None 用数据库升级的文案）
    """
    if window is None:
        window = MigrationWindow(backup_path, estimated_rows)
    if title is not None or hint is not None:
        window.set_texts(title or MIGRATION_WINDOW_TITLE, hint or MIGRATION_HINT_TEXT)
    worker = MigrationWorker(migrate)
    worker.progress.connect(window.set_stage_progress)
    window.show()
    worker.start()
    # 不用局部 QEventLoop：此前若有人调用过 QCoreApplication.quit()/exit()（而主事件循环
    # 还没开始），Qt 5 的 QEventLoop.exec_() 会立即返回，界面线程随即卡在 wait() 上、
    # 进度永远显示不出来。这里自己轮询：每次最多等 POLL_MS，其间处理界面事件
    while not worker.wait(POLL_MS):
        QCoreApplication.processEvents()
    QCoreApplication.processEvents()
    if worker.last_progress is not None:
        window.set_stage_progress(*worker.last_progress)
    window.finish()
    if worker.error is not None:
        raise worker.error
    return worker.value
