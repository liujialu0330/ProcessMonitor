"""
关于页面
显示应用信息，提供检查更新功能
"""
import os
import sys
import webbrowser

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel, QSizePolicy
)
from qfluentwidgets import (
    CardWidget, PrimaryPushButton, FluentIcon,
    StrongBodyLabel, BodyLabel, CaptionLabel,
    MessageBoxBase, SubtitleLabel, PushButton,
    InfoBar, InfoBarPosition, ProgressBar, TextEdit
)

import config
from core.update_checker import (
    UpdateChecker, UpdateDownloader,
    get_skipped_version, set_skipped_version
)
from ui.typography import DataCaptionLabel, PageTitleLabel


class UpdateAvailableDialog(MessageBoxBase):
    """发现新版本的确认对话框：现在更新 / 跳过此版本 / 稍后"""

    def __init__(self, parent, info: dict):
        super().__init__(parent)
        self.skip_requested = False

        title = SubtitleLabel(
            f"发现新版本 v{info['version']}（当前 v{config.APP_VERSION}）")
        self.viewLayout.addWidget(title)

        notes = (info.get('notes') or '').strip()
        if notes:
            self.viewLayout.addWidget(StrongBodyLabel("更新说明"))
            self.notes_edit = TextEdit()
            self.notes_edit.setReadOnly(True)
            self.notes_edit.setPlainText(notes[:4000] + ("…" if len(notes) > 4000 else ""))
            self.notes_edit.setFixedHeight(160)
            self.viewLayout.addWidget(self.notes_edit)

        self.yesButton.setText("现在更新")
        self.cancelButton.setText("稍后")

        # 在按钮区中插入"跳过此版本"按钮
        self.skip_button = PushButton("跳过此版本")
        self.buttonLayout.insertWidget(1, self.skip_button, 1)
        self.skip_button.clicked.connect(self._on_skip_clicked)

        self.widget.setMinimumWidth(420)

    def _on_skip_clicked(self):
        self.skip_requested = True
        self.reject()


class InstallConfirmDialog(MessageBoxBase):
    """下载完成后的安装确认对话框"""

    def __init__(self, parent, version: str):
        super().__init__(parent)
        title = SubtitleLabel(f"新版本 v{version} 已下载完成")
        self.viewLayout.addWidget(title)
        hint = BodyLabel("是否立即退出程序并启动安装向导？")
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.yesButton.setText("立即安装")
        self.cancelButton.setText("稍后")
        self.widget.setMinimumWidth(380)


class AboutPage(QScrollArea):
    """关于页面"""

    # 静默检查（启动3秒后自动检查）发现新版本时发出（v1.3.0 批4，C5，修复遗留
    # P2-2："静默检查也弹模态对话框会打断用户"）：不再由本页面直接弹窗，改由
    # 主窗口统一处理为非模态 InfoBar 提示；参数为 core.update_checker.
    # parse_release_info 返回的 info 字典
    update_available_silent = pyqtSignal(object)

    def __init__(self, parent=None):
        """初始化页面"""
        super().__init__(parent)
        self.setObjectName("aboutPage")

        # 更新相关状态
        self._checker = None
        self._downloader = None
        self._pending_info = None

        self._init_ui()

    def _init_ui(self):
        """初始化UI"""
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea{background: transparent; border: none}")
        self.viewport().setStyleSheet("background: transparent")

        container = QWidget()
        container.setStyleSheet("background: transparent")
        self.setWidget(container)

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(30, 30, 30, 30)
        main_layout.setSpacing(20)

        title = PageTitleLabel("关于")
        main_layout.addWidget(title)

        # 产品头：把应用身份、版本和项目入口放在同一个视觉单元。
        info_card = CardWidget()
        info_layout = QHBoxLayout(info_card)
        info_layout.setContentsMargins(20, 20, 20, 20)
        info_layout.setSpacing(16)

        icon_label = QLabel()
        icon_label.setFixedSize(52, 52)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_path = config.get_icon_path()
        if os.path.exists(icon_path):
            icon_label.setPixmap(
                QPixmap(icon_path).scaled(
                    48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        info_layout.addWidget(icon_label, 0, Qt.AlignTop)

        product_layout = QVBoxLayout()
        product_layout.setSpacing(3)
        product_layout.addWidget(SubtitleLabel(config.APP_NAME))
        product_layout.addWidget(BodyLabel("轻量的 Windows 进程性能监控工具"))
        product_layout.addWidget(DataCaptionLabel(f"版本 v{config.APP_VERSION}"))
        info_layout.addLayout(product_layout, 1)

        self.repo_url = f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"
        self.repo_button = PushButton("访问项目主页", self, FluentIcon.GITHUB)
        self.repo_button.clicked.connect(self._open_project_homepage)
        info_layout.addWidget(self.repo_button, 0, Qt.AlignVCenter)

        main_layout.addWidget(info_card)

        # ========== 软件更新区域 ==========
        update_card = CardWidget()
        update_layout = QVBoxLayout(update_card)
        update_layout.setContentsMargins(20, 20, 20, 20)
        update_layout.setSpacing(12)

        update_header_layout = QHBoxLayout()
        update_header_layout.setSpacing(15)
        update_header_layout.addWidget(StrongBodyLabel("软件更新"))
        update_header_layout.addStretch()
        self.check_button = PrimaryPushButton("检查更新", self, FluentIcon.SYNC)
        self.check_button.setMaximumWidth(160)
        self.check_button.clicked.connect(self._on_manual_check_clicked)
        update_header_layout.addWidget(self.check_button)
        update_layout.addLayout(update_header_layout)

        self.status_label = BodyLabel("尚未检查更新")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        update_layout.addWidget(self.status_label)

        # 下载进度条（默认隐藏）
        self.progress_bar = ProgressBar()
        self.progress_bar.setVisible(False)
        update_layout.addWidget(self.progress_bar)

        main_layout.addWidget(update_card)
        main_layout.addStretch()

    def _open_project_homepage(self) -> None:
        """打开 GitHub 项目主页。"""
        webbrowser.open(self.repo_url)

    def _on_manual_check_clicked(self) -> None:
        """手动检查更新，避免用 lambda 隐藏按钮的操作语义。"""
        self.check_update(silent=False)

    # ========== 更新流程 ==========

    def check_update(self, silent: bool = False):
        """
        检查更新

        Args:
            silent: True 为启动时静默检查（失败不打扰、尊重跳过版本），
                    False 为用户手动检查
        """
        if self._checker is not None and self._checker.isRunning():
            return
        if self._downloader is not None and self._downloader.isRunning():
            return

        self.check_button.setEnabled(False)
        self.status_label.setText("正在检查更新…")

        self._checker = UpdateChecker(self)
        self._checker.check_finished.connect(
            lambda info: self._on_check_finished(info, silent))
        self._checker.error_occurred.connect(
            lambda msg: self._on_check_error(msg, silent))
        self._checker.start()

    def _on_check_finished(self, info: dict, silent: bool):
        """检查完成"""
        self.check_button.setEnabled(True)

        if not info['has_update']:
            self.status_label.setText(f"已是最新版本（v{config.APP_VERSION}）")
            return

        # 静默检查时尊重"跳过此版本"
        if silent and info['version'] == get_skipped_version():
            self.status_label.setText(f"已跳过版本 v{info['version']}")
            return

        self.status_label.setText(f"发现新版本 v{info['version']}")
        if silent:
            # C5：静默检查不再直接弹模态对话框打断用户，改为发信号，由主窗口
            # 用非模态 InfoBar 提示（手动检查路径不变，仍直接弹下方模态对话框）
            self.update_available_silent.emit(info)
        else:
            self._show_update_dialog(info)

    def show_update_dialog_for(self, info: dict):
        """供主窗口在用户点击 InfoBar「查看」按钮后调用：弹出与手动检查路径
        一致的确认对话框（C5，内部直接复用 _show_update_dialog，不重复实现）"""
        self._show_update_dialog(info)

    def _on_check_error(self, msg: str, silent: bool):
        """检查失败"""
        self.check_button.setEnabled(True)
        if silent:
            # 静默检查失败不打扰用户
            self.status_label.setText("自动检查未完成，可手动重试")
            return
        self.status_label.setText("检查更新失败，可稍后重试")
        InfoBar.warning(
            title="检查更新失败",
            content=msg,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self
        )

    def _show_update_dialog(self, info: dict):
        """弹出发现新版本对话框"""
        dialog = UpdateAvailableDialog(self.window(), info)
        if dialog.exec():
            self._start_update(info)
        elif dialog.skip_requested:
            set_skipped_version(info['version'])
            self.status_label.setText(f"已跳过版本 v{info['version']}")

    def _start_update(self, info: dict):
        """开始更新：开发环境仅提示；有安装包则下载，否则打开发布页"""
        if not getattr(sys, 'frozen', False):
            InfoBar.info(
                title="开发环境",
                content="当前为源码运行环境，请使用 git pull 更新代码。",
                orient=Qt.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=4000,
                parent=self
            )
            self.status_label.setText(
                f"发现新版本 v{info['version']}；源码环境不支持自动安装")
            return

        if not info.get('asset_url'):
            # 该 release 未附带安装包，降级为打开发布页
            webbrowser.open(info['html_url'] or
                            f"https://github.com/{config.GITHUB_OWNER}/"
                            f"{config.GITHUB_REPO}/releases")
            self.status_label.setText("未找到安装包，已打开发布页")
            return

        self._pending_info = info
        self.status_label.setText("正在下载更新…")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.check_button.setEnabled(False)

        self._downloader = UpdateDownloader(
            info['asset_url'], info['asset_name'], info.get('asset_size', 0), self)
        self._downloader.download_progress.connect(self._on_download_progress)
        self._downloader.download_finished.connect(self._on_download_finished)
        self._downloader.error_occurred.connect(self._on_download_error)
        self._downloader.start()

    def _on_download_progress(self, percent: int):
        """下载进度更新"""
        if percent >= 0:
            self.progress_bar.setValue(percent)
            self.status_label.setText(f"正在下载更新… {percent}%")
        else:
            self.status_label.setText("正在下载更新…")

    def _on_download_finished(self, path: str):
        """下载完成，询问是否安装"""
        self.progress_bar.setVisible(False)
        self.check_button.setEnabled(True)
        version = self._pending_info['version'] if self._pending_info else ""
        self.status_label.setText(f"新版本 v{version} 已下载，待安装")

        dialog = InstallConfirmDialog(self.window(), version)
        if dialog.exec():
            try:
                os.startfile(path)
            except Exception as e:
                self.status_label.setText(f"启动安装程序失败：{e}")
                return
            # 退出应用，交给安装向导。评审修订 B3：必须调用 quit_for_install()
            # 而非普通 close()——否则"关闭窗口时最小化到托盘"开启时，安装向导
            # 已经启动，但主程序会被 closeEvent 的隐藏分支拦截、常驻托盘继续
            # 占用文件，导致覆盖安装因文件占用而失败。hasattr 防御：AboutPage
            # 可能被独立构造用于测试（此时 self.window() 是页面自身，没有
            # quit_for_install 方法），仍需能正常关闭。
            window = self.window()
            if hasattr(window, 'quit_for_install'):
                window.quit_for_install()
            else:
                window.close()
        else:
            self.status_label.setText(f"安装包已保存：{path}")

    def _on_download_error(self, msg: str):
        """下载失败"""
        self.progress_bar.setVisible(False)
        self.check_button.setEnabled(True)
        self.status_label.setText("下载失败，可稍后重试")
        InfoBar.warning(
            title="下载失败",
            content=msg,
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=4000,
            parent=self
        )
