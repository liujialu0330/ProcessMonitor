"""
模态对话框与系统交互（Dialogs）

确认框、打点标签输入、指标选择、保存路径、选择文件/文件夹、关窗三选一，以及打开文件夹/网址、
复制图片与文本到剪贴板，统一经由注入的 Dialogs：页面测试换成脚本化替身即可预设
"用户的选择"，不会弹出阻塞的模态对话框，也不会真的拉起资源管理器或浏览器。

parent 约定：对话框类方法的 parent 由页面传 self.window()（遮罩覆盖整个主窗口，
与现有写法一致）；ask_save_path 的 parent 传页面自身即可。
"""
import os
from datetime import datetime
from typing import Callable, List, Optional, Protocol, Sequence, runtime_checkable

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices, QPixmap
from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QWidget

from ui.components.confirm import DestructiveConfirmBox
from ui.components.dialogs import CloseActionDialog, MarkerDialog
from ui.components.metric_selector import MetricSelectorDialog

# 关窗三选一的取值
CLOSE_ACTION_QUIT = 'quit'
CLOSE_ACTION_TRAY = 'tray'
CLOSE_ACTION_CANCEL = 'cancel'
CLOSE_ACTIONS = (CLOSE_ACTION_QUIT, CLOSE_ACTION_TRAY, CLOSE_ACTION_CANCEL)

# 运行一个已构造好的对话框并返回"是否被确认"
DialogRunner = Callable[[QDialog], bool]


def exec_dialog(dialog: QDialog) -> bool:
    """生产环境的对话框运行方式：进入模态循环，确认返回 True。"""
    return bool(dialog.exec())


@runtime_checkable
class Dialogs(Protocol):
    """对话框与系统交互服务。"""

    def confirm(self, parent: QWidget, title: str, content: str,
                confirm_text: str, cancel_text: str = "取消") -> bool:
        """确认框：确认返回 True，取消/关闭返回 False。

        按钮文案由调用方给出；显示后焦点落在"取消"上，顺手回车不会执行操作。
        """
        ...

    def ask_marker_label(self, parent: QWidget, default_label: str,
                         timestamp: datetime, *, process_name: str = '',
                         pid: Optional[int] = None) -> Optional[str]:
        """打点名称输入框（显示"进程名 · PID"与点击时刻 timestamp）：保存返回去除首尾
        空白的名称，取消返回 None。"""
        ...

    def choose_metrics(self, parent: QWidget, selected: Sequence[str], *,
                       group: bool = False) -> Optional[List[str]]:
        """指标选择框（预选 selected）：确定返回所选指标列表（按指标注册表顺序），取消返回 None。
        group 为真时是进程组（进程树 / 同名进程）任务的选择：对组不适用的指标置灰，
        返回值末尾带进程总数。"""
        ...

    def ask_save_path(self, parent: QWidget, title: str, default_path: str,
                      name_filter: str) -> Optional[str]:
        """保存文件对话框：返回规范化后的路径（Windows 反斜杠），取消返回 None。"""
        ...

    def ask_open_file(self, parent: QWidget, title: str, start_dir: str,
                      name_filter: str) -> Optional[str]:
        """选择已有文件的对话框：返回规范化后的路径，取消返回 None。"""
        ...

    def ask_directory(self, parent: QWidget, title: str, start_dir: str) -> Optional[str]:
        """选择文件夹的对话框：返回规范化后的路径，取消返回 None。"""
        ...

    def ask_close_action(self, parent: QWidget, running_count: int,
                         can_tray: bool, notice: str = "") -> str:
        """仍有任务在监控时关闭主窗口的三选一：'quit' / 'tray' / 'cancel'。
        notice 非空时作为醒目的一句提示附在对话框里（如退出后会丢失的数据）。"""
        ...

    def open_folder(self, path: str) -> None:
        """用系统资源管理器打开文件夹；失败抛出 OSError，由调用方决定是否提示。"""
        ...

    def open_file(self, path: str) -> None:
        """按系统关联方式打开/运行文件（如启动安装程序）；失败抛出 OSError。"""
        ...

    def open_url(self, url: str) -> bool:
        """用系统默认浏览器打开网址，返回是否成功交给系统。"""
        ...

    def copy_pixmap(self, pixmap: QPixmap) -> None:
        """把图片放入系统剪贴板。"""
        ...

    def copy_text(self, text: str) -> None:
        """把文本放入系统剪贴板。"""
        ...


class QtDialogs:
    """生产实现：真实的 Fluent 对话框与系统调用。

    对话框的"构造"与"运行"分开：各方法负责构造对话框、解读结果，运行交给
    run_dialog（默认 exec_dialog 进模态循环）。测试可注入一个不进 exec() 的运行器
    （离屏 show() 后直接点按钮），从而检验真实对话框的文案与返回约定而不阻塞。
    """

    def __init__(self, run_dialog: Optional[DialogRunner] = None):
        self._run = run_dialog if run_dialog is not None else exec_dialog

    def _run_and_dispose(self, dialog: QDialog, on_accept: Callable[[], object],
                         on_reject: object) -> object:
        """运行对话框：确认时取 on_accept()，否则返回 on_reject；结束后释放对话框。"""
        try:
            return on_accept() if self._run(dialog) else on_reject
        finally:
            dialog.deleteLater()

    def confirm(self, parent: QWidget, title: str, content: str,
                confirm_text: str, cancel_text: str = "取消") -> bool:
        """确认框（DestructiveConfirmBox）。"""
        box = DestructiveConfirmBox(
            title, content, confirm_text, parent, cancel_text=cancel_text)
        return bool(self._run_and_dispose(box, lambda: True, False))

    def ask_marker_label(self, parent: QWidget, default_label: str,
                         timestamp: datetime, *, process_name: str = '',
                         pid: Optional[int] = None) -> Optional[str]:
        """打点名称输入框（MarkerDialog）。"""
        dialog = MarkerDialog(default_label, parent, timestamp=timestamp,
                              process_name=process_name, pid=pid)
        return self._run_and_dispose(dialog, dialog.get_label, None)

    def choose_metrics(self, parent: QWidget, selected: Sequence[str], *,
                       group: bool = False) -> Optional[List[str]]:
        """指标选择框（MetricSelectorDialog）。"""
        dialog = MetricSelectorDialog(list(selected), parent, group_mode=group)
        return self._run_and_dispose(dialog, dialog.get_selected, None)

    def ask_save_path(self, parent: QWidget, title: str, default_path: str,
                      name_filter: str) -> Optional[str]:
        """保存文件对话框（QFileDialog.getSaveFileName）。"""
        file_path, _ = QFileDialog.getSaveFileName(
            parent, title, default_path, name_filter)
        # Qt 返回正斜杠路径：规范化后再显示/写入，与资源管理器里看到的一致
        return os.path.normpath(file_path) if file_path else None

    def ask_open_file(self, parent: QWidget, title: str, start_dir: str,
                      name_filter: str) -> Optional[str]:
        """选择已有文件（QFileDialog.getOpenFileName）。"""
        file_path, _ = QFileDialog.getOpenFileName(parent, title, start_dir, name_filter)
        return os.path.normpath(file_path) if file_path else None

    def ask_directory(self, parent: QWidget, title: str, start_dir: str) -> Optional[str]:
        """选择文件夹（QFileDialog.getExistingDirectory）。"""
        directory = QFileDialog.getExistingDirectory(parent, title, start_dir)
        return os.path.normpath(directory) if directory else None

    def ask_close_action(self, parent: QWidget, running_count: int,
                         can_tray: bool, notice: str = "") -> str:
        """关窗三选一（CloseActionDialog）。"""
        dialog = CloseActionDialog(running_count, can_tray=can_tray, parent=parent,
                                   notice=notice)
        return self._run_and_dispose(dialog, lambda: dialog.choice, CLOSE_ACTION_CANCEL)

    def open_folder(self, path: str) -> None:
        """打开文件夹（os.startfile）。"""
        os.startfile(path)

    def open_file(self, path: str) -> None:
        """按系统关联打开/运行文件（os.startfile）。"""
        os.startfile(path)

    def open_url(self, url: str) -> bool:
        """打开网址（QDesktopServices.openUrl）。"""
        return bool(QDesktopServices.openUrl(QUrl(url)))

    def copy_pixmap(self, pixmap: QPixmap) -> None:
        """图片放入剪贴板。"""
        QApplication.clipboard().setPixmap(pixmap)

    def copy_text(self, text: str) -> None:
        """文本放入剪贴板。"""
        QApplication.clipboard().setText(text)
