"""
存储守卫的界面提示

core.storage_guard.StorageGuard 只发信号、不碰界面；有窗口模式下由主窗口把它交给这里，
把信号翻译成提示：

- 剩余空间低于 1 GB：一条 10 秒的警告（守卫保证每次下探只发一次）；
- 低于 200 MB：写明剩余空间与数据库路径的常驻警告（不自动消失），并告知已暂停几个任务；
- 回升到 300 MB 以上：常驻警告改成"空间已恢复，可继续监控"（任务不自动恢复，需手动继续）；
- 守卫暂停的任务集合变化：交给监控页标出"因磁盘空间不足暂停"；
- quick_check 未通过：主窗口一条常驻警告 + 设置页"数据"分组的警告卡片，各一次。

磁盘空间类提示同一时刻最多一条：新的空间提示出现前先关掉上一条（"不足 1 GB"、常驻的"严重不足"、
"已恢复"），状态以最新一条为准。提示条一律挂在同一个锚点（主窗口）上、由 Notifier 居中靠顶显示，
与其他提示一致。

窗口隐藏在托盘时另补一条托盘气泡（提示条挂在隐藏的窗口上用户看不到）。文案是纯函数，
不依赖 Qt 控件，便于测试。
"""
import logging
from typing import Any, Callable, Optional

from PyQt5.QtCore import QObject

from core.storage_guard import GB, MB, PAUSE_FREE_BYTES, WARN_FREE_BYTES, format_size

logger = logging.getLogger(__name__)


def format_threshold(num_bytes: int) -> str:
    """阈值类字节数 → "200 MB" / "1 GB"（整数，不带小数）；剩余空间这类实测值仍用 format_size。"""
    for unit, size in (("GB", GB), ("MB", MB)):
        if num_bytes >= size and num_bytes % size == 0:
            return f"{num_bytes // size} {unit}"
    return format_size(num_bytes)


LOW_SPACE_TITLE = f"磁盘空间不足 {format_threshold(WARN_FREE_BYTES)}"
CRITICAL_TITLE = "磁盘空间严重不足，已暂停监控"
RECOVERED_TITLE = "空间已恢复，可继续监控"
INTEGRITY_TITLE = "数据库完整性检查未通过"
# 监控页任务卡片上标注的暂停原因
SPACE_PAUSE_REASON = "磁盘空间不足"
SPACE_PAUSE_TOOLTIP = "因磁盘空间不足自动暂停；释放空间后点“恢复”继续"
LOW_SPACE_DURATION_MS = 10_000


def low_space_text(free_bytes: int, db_path: str) -> str:
    return (f"数据库所在磁盘剩余 {format_size(free_bytes)}。长时间监控会持续写入，"
            f"请清理磁盘或把数据目录换到更大的磁盘（当前数据库：{db_path}）。"
            f"低于 {format_threshold(PAUSE_FREE_BYTES)} 时会自动暂停全部监控任务。")


def critical_text(free_bytes: int, db_path: str, paused_count: int) -> str:
    paused = (f"已暂停 {paused_count} 个运行中的任务，" if paused_count
              else "没有需要暂停的运行中任务，")
    return (f"数据库所在磁盘剩余 {format_size(free_bytes)}（不足 "
            f"{format_threshold(PAUSE_FREE_BYTES)}）。{paused}以免写库失败、丢数据；"
            f"释放空间后请在监控页手动点“恢复”继续。数据库：{db_path}")


def recovered_text(free_bytes: int, paused_count: int) -> str:
    tail = (f"因空间不足暂停的 {paused_count} 个任务不会自动恢复，请在监控页手动点“恢复”继续。"
            if paused_count else "")
    return f"磁盘剩余 {format_size(free_bytes)}。{tail}".strip()


def integrity_text(summary: str) -> str:
    return (f"后台检查（quick_check）发现问题：{summary}。建议先备份数据目录；"
            "可在“设置 → 数据”里点“复制诊断信息”并联系作者协助排查。")


def integrity_card_content(summary: str) -> str:
    """设置页"数据"分组警告卡片的说明行"""
    return f"后台检查发现问题：{summary}。请先备份数据目录，点右侧“复制诊断信息”并联系作者。"


class StorageNotices(QObject):
    """把 StorageGuard 的信号翻译成界面提示（界面线程）。"""

    def __init__(self, guard, notifier, anchor, *, db_path: Optional[str] = None,
                 set_pause_marks: Optional[Callable[[Any], None]] = None,
                 show_integrity: Optional[Callable[[str], None]] = None,
                 tray_message: Optional[Callable[[str, str], None]] = None,
                 parent: Optional[QObject] = None):
        """
        Args:
            guard: StorageGuard
            notifier: ui.services 的 Notifier
            anchor: 提示条挂靠的控件（主窗口）
            db_path: 数据库路径（写进提示）；缺省取 guard.db_path
            set_pause_marks: 守卫暂停的任务 id 集合变化时调用（监控页标注暂停原因）
            show_integrity: quick_check 未通过时调用（设置页警告卡片）
            tray_message: 窗口隐藏在托盘时补发气泡 (标题, 内容)；None 不补发
        """
        super().__init__(parent)
        self._notifier = notifier
        self._anchor = anchor
        self._guard = guard
        self._db_path = db_path or getattr(guard, 'db_path', '')
        self._set_pause_marks = set_pause_marks
        self._show_integrity = show_integrity
        self._tray_message = tray_message
        self._space_bar: Any = None   # 当前显示的磁盘空间类提示条（至多一条）
        self._integrity_shown = False

        guard.low_space.connect(self._on_low_space)
        guard.space_critical.connect(self._on_critical)
        guard.space_recovered.connect(self._on_recovered)
        guard.space_paused_changed.connect(self._on_paused_changed)
        guard.integrity_problem.connect(self._on_integrity_problem)
        # 守卫在组合根里早于窗口构造：接上之前就已出现的结果补显示一次
        summary = getattr(guard, 'integrity_summary', None)
        if summary:
            self._on_integrity_problem(summary)

    # ---------- 磁盘空间 ----------

    def _show_space_bar(self, level: str, title: str, text: str, duration: int) -> None:
        """显示一条磁盘空间提示；上一条空间提示（不论哪一种）先关掉。"""
        self._close(self._space_bar)
        show = getattr(self._notifier, level)
        self._space_bar = show(self._anchor, title, text, duration=duration)

    def _on_low_space(self, free: int) -> None:
        self._show_space_bar("warning", LOW_SPACE_TITLE,
                             low_space_text(free, self._db_path), LOW_SPACE_DURATION_MS)
        self._tray(LOW_SPACE_TITLE, f"剩余 {format_size(free)}")

    def _on_critical(self, free: int, newly_paused) -> None:
        count = len(self._guard.paused_task_ids())
        self._show_space_bar("warning", CRITICAL_TITLE,
                             critical_text(free, self._db_path, count), -1)
        self._tray(CRITICAL_TITLE, f"剩余 {format_size(free)}，已暂停监控任务")

    def _on_recovered(self, free: int) -> None:
        count = len(self._guard.paused_task_ids())
        self._show_space_bar("success", RECOVERED_TITLE, recovered_text(free, count), -1)
        self._tray(RECOVERED_TITLE, f"剩余 {format_size(free)}")

    def _on_paused_changed(self, ids) -> None:
        if self._set_pause_marks is not None:
            self._set_pause_marks(frozenset(ids))

    # ---------- 完整性 ----------

    def _on_integrity_problem(self, summary: str) -> None:
        if self._integrity_shown:
            return
        self._integrity_shown = True
        self._notifier.warning(self._anchor, INTEGRITY_TITLE, integrity_text(summary),
                               duration=-1)
        self._tray(INTEGRITY_TITLE, "请在设置页复制诊断信息并联系作者")
        if self._show_integrity is not None:
            self._show_integrity(summary)

    # ---------- 工具 ----------

    @staticmethod
    def _close(bar: Any) -> None:
        if bar is None:
            return
        try:
            bar.close()
        except RuntimeError:
            pass   # 提示条已随窗口销毁

    def _tray(self, title: str, content: str) -> None:
        if self._tray_message is None:
            return
        try:
            self._tray_message(title, content)
        except Exception:
            logger.warning("补发托盘气泡失败", exc_info=True)

