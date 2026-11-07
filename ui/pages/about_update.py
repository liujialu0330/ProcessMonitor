"""
关于与更新（设置页的一个分组）

原"关于"页并入设置页：应用身份、版本与项目主页一张卡片；软件更新一张卡片
（状态行"已是最新版本 · 上次检查 16:40"、普通样式的"检查更新"按钮），下载、校验
与安装流程原样保留。主窗口经 setting_page.about_group 使用本分组。
"""
import os
import sys
from typing import Callable, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from qfluentwidgets import (
    FluentIcon, StrongBodyLabel, BodyLabel,
    MessageBoxBase, SubtitleLabel, PushButton,
    ProgressBar, SettingCard, SettingCardGroup, TextEdit
)

import app_config
import config
from core.update_checker import UpdateChecker, UpdateDownloader, MSG_CANNOT_VERIFY
from ui.services import UiServices

GROUP_TITLE = "关于与更新"
APP_TAGLINE = "面向测试的内存 / 句柄 / GDI 泄漏排查工具"

# 缺少安装包校验信息时展示给用户的说明（core 层的 MSG_CANNOT_VERIFY 偏技术化）
MSG_NO_DIGEST = "该版本未提供校验信息，请前往发布页手动下载"


def up_to_date_text(checked_at: str = '') -> str:
    """"已是最新版本（v1.4.1）· 上次检查 16:40"（没有检查时间时省略后半句）。"""
    text = f"已是最新版本（v{config.APP_VERSION}）"
    return f"{text} · 上次检查 {checked_at}" if checked_at else text


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
    """下载完成后的安装确认对话框。

    "立即安装"会退出本程序（连带停止全部监控任务），不是"安全"的默认操作：
    显示后焦点给"稍后"，顺手回车不会把正在跑的测试停掉。
    """

    def __init__(self, parent, version: str):
        super().__init__(parent)
        title = SubtitleLabel(f"新版本 v{version} 已下载完成")
        self.viewLayout.addWidget(title)
        hint = BodyLabel("是否立即退出程序并启动安装向导？正在监控的任务会被停止。")
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.yesButton.setText("立即安装")
        self.cancelButton.setText("稍后")
        self.widget.setMinimumWidth(380)

    def showEvent(self, event):
        # 必须在父类 showEvent 之后：显示时会恢复到构造期记下的焦点控件（确认按钮）
        super().showEvent(event)
        self.cancelButton.setFocus()


class AboutUpdateGroup(SettingCardGroup):
    """设置页"关于与更新"分组：应用信息卡 + 软件更新卡。"""

    # 静默检查（启动3秒后自动检查）发现新版本时发出：不直接弹模态对话框打断用户，
    # 改由主窗口统一处理为非模态提示；参数为
    # core.update_checker.parse_release_info 返回的 info 字典
    update_available_silent = pyqtSignal(object)
    # 安装程序已成功启动、用户确认"立即安装"：主窗口据此走真退出路径
    # （quit_for_install），本分组不反向调用窗口方法
    install_requested = pyqtSignal()

    def __init__(self, services: UiServices, parent=None):
        """
        Args:
            services: 界面服务（提示、打开网址、启动安装包、时钟、线程登记表）
            parent: 父控件
        """
        super().__init__(GROUP_TITLE, parent)
        self.setObjectName("aboutUpdateGroup")
        self.services = services

        # 更新相关状态：线程运行期间非 None，线程结束时清空（对象的释放与退出收尾由
        # services.threads 登记表负责）
        self._checker: Optional[UpdateChecker] = None
        self._downloader: Optional[UpdateDownloader] = None
        self._pending_info = None
        # 主窗口注入的退出前检查：启动安装程序之前调用，返回 False（如用户不同意丢弃
        # 因磁盘已满未写入的数据）则不启动安装程序、应用继续运行；None 表示不检查
        self.exit_guard: Optional[Callable[[], bool]] = None
        # 发布页地址（html_url）：缺少校验信息时"打开发布页"按钮使用
        self._release_url_hint = None

        self._init_cards()

    def _init_cards(self) -> None:
        # 应用信息：名称与版本 · 一句话定位 · [访问项目主页]
        icon_path = config.get_icon_path()
        icon = QIcon(icon_path) if os.path.exists(icon_path) else FluentIcon.INFO
        self.about_card = SettingCard(
            icon, f"{config.APP_NAME} v{config.APP_VERSION}", APP_TAGLINE, parent=self)
        self.repo_url = f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}"
        self.repo_button = PushButton("访问项目主页", self.about_card, FluentIcon.GITHUB)
        self.repo_button.setToolTip(self.repo_url)
        self.repo_button.clicked.connect(self._open_project_homepage)
        self.about_card.hBoxLayout.addWidget(self.repo_button, 0, Qt.AlignRight)
        self.about_card.hBoxLayout.addSpacing(16)
        self.addSettingCard(self.about_card)

        # 软件更新：状态行在说明位置；进度条、发布页按钮、检查更新按钮依次排在右侧
        self.update_card = SettingCard(
            FluentIcon.UPDATE, "软件更新", "尚未检查更新", parent=self)
        self.status_label = self.update_card.contentLabel
        self.status_label.setWordWrap(False)

        # 下载进度条（默认隐藏）
        self.progress_bar = ProgressBar(self.update_card)
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setVisible(False)
        self.update_card.hBoxLayout.addWidget(self.progress_bar, 0, Qt.AlignRight)
        self.update_card.hBoxLayout.addSpacing(12)

        # 新版本缺少安装包校验信息时显示：无法自动下载，引导用户去发布页手动下载
        self.release_button = PushButton("打开发布页", self.update_card, FluentIcon.LINK)
        self.release_button.clicked.connect(self._open_release_page)
        self.release_button.setVisible(False)
        self.update_card.hBoxLayout.addWidget(self.release_button, 0, Qt.AlignRight)
        self.update_card.hBoxLayout.addSpacing(8)

        # 低频操作：普通样式按钮（每个区域最多一个主按钮，这里不需要主按钮）
        self.check_button = PushButton("检查更新", self.update_card, FluentIcon.SYNC)
        self.check_button.clicked.connect(self._on_manual_check_clicked)
        self.update_card.hBoxLayout.addWidget(self.check_button, 0, Qt.AlignRight)
        self.update_card.hBoxLayout.addSpacing(16)
        self.addSettingCard(self.update_card)

    def _notice_parent(self):
        return self.window()

    def _open_project_homepage(self) -> None:
        """打开 GitHub 项目主页。"""
        self.services.dialogs.open_url(self.repo_url)

    def _on_manual_check_clicked(self) -> None:
        """手动检查更新，避免用 lambda 隐藏按钮的操作语义。"""
        self.check_update(silent=False)

    def _checked_at(self) -> str:
        return self.services.clock.now().strftime('%H:%M')

    # ========== 更新流程 ==========

    def _on_checker_finished(self, worker) -> None:
        """检查线程真正结束：释放引用（对象由登记表 deleteLater）"""
        if self._checker is worker:
            self._checker = None

    def _on_downloader_finished(self, worker) -> None:
        """下载线程真正结束：释放引用（对象由登记表 deleteLater）"""
        if self._downloader is worker:
            self._downloader = None

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
        self.release_button.setVisible(False)
        self._release_url_hint = None
        self.status_label.setText("正在检查更新…")

        checker = UpdateChecker()
        self._checker = checker
        checker.check_finished.connect(
            lambda info: self._on_check_finished(info, silent))
        checker.error_occurred.connect(
            lambda msg: self._on_check_error(msg, silent))
        checker.finished.connect(lambda w=checker: self._on_checker_finished(w))
        # 网络请求自带超时、无取消检查点：退出时只短暂等待
        self.services.threads.register(checker, name="检查更新", timeout_ms=1000)
        checker.start()

    def _on_check_finished(self, info: dict, silent: bool):
        """检查完成"""
        self.check_button.setEnabled(True)

        if not info['has_update']:
            self.status_label.setText(up_to_date_text(self._checked_at()))
            return

        # 静默检查时尊重"跳过此版本"
        if silent and info['version'] == app_config.get_skipped_version():
            self.status_label.setText(f"已跳过版本 v{info['version']}")
            return

        self.status_label.setText(f"发现新版本 v{info['version']}")
        if silent:
            # 静默检查不直接弹模态对话框打断用户，改为发信号，由主窗口用非模态
            # 提示（手动检查路径不变，仍直接弹下方模态对话框）
            self.update_available_silent.emit(info)
        else:
            self._show_update_dialog(info)

    def show_update_dialog_for(self, info: dict):
        """供主窗口在用户点击提示条「查看」按钮后调用：弹出与手动检查路径
        一致的确认对话框（内部直接复用 _show_update_dialog，不重复实现）"""
        self._show_update_dialog(info)

    def _on_check_error(self, msg: str, silent: bool):
        """检查失败"""
        self.check_button.setEnabled(True)
        if silent:
            # 静默检查失败不打扰用户
            self.status_label.setText("自动检查未完成，可手动重试")
            return
        self.status_label.setText("检查更新失败，可稍后重试")
        self.services.notifier.warning(
            self._notice_parent(), "检查更新失败", msg, duration=4000)

    def _show_update_dialog(self, info: dict):
        """弹出发现新版本对话框"""
        dialog = UpdateAvailableDialog(self.window(), info)
        if dialog.exec():
            self._start_update(info)
        elif dialog.skip_requested:
            app_config.set_skipped_version(info['version'])
            self.status_label.setText(f"已跳过版本 v{info['version']}")

    def _start_update(self, info: dict):
        """开始更新：开发环境仅提示；有安装包则下载，否则打开发布页"""
        if not getattr(sys, 'frozen', False):
            self.services.notifier.info(
                self._notice_parent(), "开发环境",
                "当前为源码运行环境，请使用 git pull 更新代码。", duration=4000)
            self.status_label.setText(
                f"发现新版本 v{info['version']}；源码环境不支持自动安装")
            return

        if not info.get('asset_url'):
            # 该 release 未附带安装包，降级为打开发布页
            self.services.dialogs.open_url(
                info['html_url'] or
                f"https://github.com/{config.GITHUB_OWNER}/{config.GITHUB_REPO}/releases")
            self.status_label.setText("未找到安装包，已打开发布页")
            return

        if not info.get('asset_digest'):
            # 缺少 SHA-256 摘要：下载线程会拒绝下载，这里直接给出可操作的说明
            self._show_no_digest(info)
            return

        self._pending_info = info
        self.status_label.setText("正在下载更新…")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.check_button.setEnabled(False)

        downloader = UpdateDownloader(
            info['asset_url'], info['asset_name'], info.get('asset_size', 0),
            digest=info.get('asset_digest'))
        self._downloader = downloader
        downloader.download_progress.connect(self._on_download_progress)
        downloader.download_finished.connect(self._on_download_finished)
        downloader.error_occurred.connect(self._on_download_error)
        downloader.finished.connect(lambda w=downloader: self._on_downloader_finished(w))
        # 退出时先取消（下载线程自行删除残留）再等待
        self.services.threads.register(
            downloader, name="更新下载", cancel=downloader.cancel, timeout_ms=2000)
        downloader.start()

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
            if self.exit_guard is not None and not self.exit_guard():
                self.status_label.setText(f"已取消安装，安装包已保存：{path}")
                return
            try:
                self.services.dialogs.open_file(path)
            except OSError as e:
                self.status_label.setText(f"启动安装程序失败：{e}")
                return
            # 退出应用，交给安装向导：主窗口连接本信号后走真退出路径
            # （quit_for_install），而非普通 close()——否则"关闭窗口时最小化到托盘"
            # 开启时，主程序会被 closeEvent 的隐藏分支拦截、常驻托盘继续占用文件，
            # 导致覆盖安装因文件占用而失败。
            self.install_requested.emit()
        else:
            self.status_label.setText(f"安装包已保存：{path}")

    def _release_page_url(self, info: dict = None) -> str:
        """新版本发布页地址：优先用 release 自带的 html_url，缺失时用仓库发布列表。"""
        info = info or {}
        return (info.get('html_url') or self._release_url_hint
                or f"https://github.com/{config.GITHUB_OWNER}/"
                   f"{config.GITHUB_REPO}/releases")

    def _open_release_page(self, info: dict = None):
        """打开发布页（按钮点击时 info 为 False/None，使用已记录的地址）"""
        self.services.dialogs.open_url(
            self._release_page_url(info if isinstance(info, dict) else None))

    def _show_no_digest(self, info: dict = None):
        """该版本未提供安装包校验信息：不自动下载（无法确认安装包完整性），
        说明原因并提供打开发布页的入口。"""
        self.progress_bar.setVisible(False)
        self.check_button.setEnabled(True)
        self._release_url_hint = (info or {}).get('html_url') or self._release_url_hint
        self.status_label.setText(MSG_NO_DIGEST)
        self.release_button.setVisible(True)

        info_bar = self.services.notifier.warning(
            self._notice_parent(), "无法自动更新", MSG_NO_DIGEST, duration=8000)
        open_button = PushButton("打开发布页")
        open_button.clicked.connect(self._open_release_page)
        info_bar.addWidget(open_button)

    def _on_download_error(self, msg: str):
        """下载失败"""
        if msg == MSG_CANNOT_VERIFY:
            self._show_no_digest(self._pending_info)
            return
        self.progress_bar.setVisible(False)
        self.check_button.setEnabled(True)
        self.status_label.setText("下载失败，可稍后重试")
        self.services.notifier.warning(
            self._notice_parent(), "下载失败", msg, duration=4000)
