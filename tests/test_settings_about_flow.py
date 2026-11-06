"""
设置页：开关文案、清理确认框、清理数据库的维护状态与线程释放时机、打开数据目录；
关于页：缺少校验摘要时的友好提示与发布页入口、项目主页、安装包启动失败。

页面经 tests/factories.py 构造并注入替身服务：提示由 RecordingNotifier 记录、
对话框选择由 ScriptedDialogs 预设，不替换页面模块里的 InfoBar/对话框类。
"""
import sys
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from qfluentwidgets import qconfig

import config
import ui.pages.about_page as about_module
from app_config import cfg
from core.contracts import StopReason
from core.monitor_manager import MonitorManager
from core.update_checker import MSG_CANNOT_VERIFY
from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
from tests.factories import make_about_page, make_services, make_setting_page
from tests.helpers import poll_until
from ui.pages.about_page import MSG_NO_DIGEST
from ui.services import ManagerTaskStatus, QtDialogs


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def dialogs():
    return ScriptedDialogs()


@pytest.fixture
def prefs():
    return FakePreferences(retention_days=30)


def _close_setting_page(page):
    worker = page._cleanup_worker
    if worker is not None:
        worker.wait(10000)
        QApplication.processEvents()
    page.close()


@pytest.fixture
def setting(qapp, db, manager, notifier, dialogs, prefs):
    services = make_services(notifier=notifier, dialogs=dialogs, preferences=prefs,
                             task_status=ManagerTaskStatus(manager))
    page = make_setting_page(db, manager, services)
    yield page
    _close_setting_page(page)


# ---------- 构造 ----------

def test_setting_page_requires_db_manager_and_services(qapp):
    """依赖必填：不再支持缺省构造（旧的"数据库未就绪"兜底已删除）"""
    from ui.pages.setting_page import SettingPage
    with pytest.raises(TypeError):
        SettingPage()


def test_about_page_requires_services(qapp):
    from ui.pages.about_page import AboutPage
    with pytest.raises(TypeError):
        AboutPage()


# ---------- 开关文案 ----------

def test_close_to_tray_switch_uses_chinese_text(setting, tmp_path):
    # SwitchSettingCard.setValue 会经 qconfig.set 落盘：先把配置文件重定向到 tmp，
    # 避免在项目根目录写出游离的 config\ 目录
    qconfig.load(str(tmp_path / "switch_probe_config.json"), cfg)
    card = setting.close_to_tray_card
    card.setValue(True)
    assert card.switchButton.text == "开"
    card.setValue(False)
    assert card.switchButton.text == "关"
    # 用户点击开关的路径同样保持中文
    card.switchButton.toggleChecked()
    assert card.switchButton.text == "开"


# ---------- 数据目录 ----------

def test_open_data_dir_goes_through_dialogs(setting, dialogs, notifier):
    setting.open_dir_card.clicked.emit()

    assert dialogs.calls_of("open_folder") == [{"path": config.DATA_DIR}]
    assert notifier.notices == []


def test_open_data_dir_failure_shows_error(setting, dialogs, notifier):
    dialogs.push("open_folder", OSError("找不到路径"))

    setting._on_open_data_dir_clicked()

    assert notifier.last().level == "error"
    assert "找不到路径" in notifier.last().content


# ---------- 清理数据库：维护状态与线程释放 ----------

def test_cleanup_refused_while_tasks_are_running(setting, manager, dialogs, notifier,
                                                 monkeypatch):
    monkeypatch.setattr(manager, 'begin_maintenance', lambda: False)

    setting._on_cleanup_clicked()

    assert setting._cleanup_worker is None
    assert notifier.levels() == ["warning"]
    assert "confirm" not in dialogs.methods_called(), "被拒绝时不应再弹确认"


def test_cleanup_refusal_reason_running_tasks(setting, manager, notifier, monkeypatch):
    """有任务在监控时被拒：提示停止任务"""
    monkeypatch.setattr(manager, 'has_running_tasks', lambda: True)

    setting._on_cleanup_clicked()

    assert setting._cleanup_worker is None
    assert manager.is_maintenance_running() is False
    notice = notifier.last("warning")
    assert notice.title == "无法清理" and "监控任务" in notice.content
    assert notice.duration == 4000


def test_cleanup_refused_while_exporting(setting, manager, dialogs, notifier):
    """导出进行中被拒：提示等导出完成，且不弹确认、不进入维护"""
    assert manager.begin_export() is True

    setting._on_cleanup_clicked()

    assert setting._cleanup_worker is None
    assert manager.is_maintenance_running() is False
    assert notifier.levels() == ["warning"]
    assert "正在导出" in notifier.last().content
    assert "confirm" not in dialogs.methods_called()
    manager.end_export()


def test_cleanup_confirm_text_follows_retention(setting, dialogs, prefs):
    """确认框按钮为"清理"；文案随保留天数区分"删除过期数据"与"仅压缩" """
    dialogs.always("confirm", False)

    assert setting._confirm_cleanup(30) is False
    call = dialogs.last_call("confirm")
    assert call["title"] == "清理并压缩数据库"
    assert call["confirm_text"] == "清理"
    assert "30 天" in call["content"]

    setting._confirm_cleanup(0)
    assert "仅压缩" in dialogs.last_call("confirm")["content"]


def test_cleanup_uses_retention_from_preferences(setting, manager, dialogs, prefs):
    """清理按偏好里的保留天数确认（不直接读全局配置）"""
    prefs.retention = 90
    dialogs.always("confirm", False)

    setting._on_cleanup_clicked()

    assert "90 天" in dialogs.last_call("confirm")["content"]
    assert manager.is_maintenance_running() is False


def test_cleanup_confirm_enter_cancels_with_real_dialog(qapp, db, manager):
    """真实确认框：按钮为"清理/取消"，显示后焦点在"取消"，回车即取消"""
    seen = {}

    def _press_enter(box) -> bool:
        # 不进模态循环：显示后记录状态，再向焦点控件按回车，模拟用户顺手回车
        box.show()
        QTest.qWait(300)  # 等淡入动画结束，避免动画回调落到后续用例里
        seen.update(yes=box.yesButton.text(), cancel=box.cancelButton.text(),
                    focus_is_cancel=box.focusWidget() is box.cancelButton)
        accepted = []
        box.yesSignal.connect(lambda: accepted.append(True))
        QTest.keyClick(box.focusWidget(), Qt.Key_Return)
        for _ in range(50):  # 等 100ms 淡出动画结束、对话框真正关闭
            if not box.isVisible():
                break
            QTest.qWait(20)
        return bool(accepted)

    services = make_services(dialogs=QtDialogs(run_dialog=_press_enter),
                             task_status=ManagerTaskStatus(manager))
    page = make_setting_page(db, manager, services)
    page.show()
    try:
        assert page._confirm_cleanup(30) is False
        assert seen == {"yes": "清理", "cancel": "取消", "focus_is_cancel": True}
    finally:
        _close_setting_page(page)


def test_cancelled_confirmation_leaves_maintenance(setting, manager, dialogs):
    dialogs.always("confirm", False)

    setting._on_cleanup_clicked()

    assert manager.is_maintenance_running() is False
    assert setting._cleanup_worker is None


def test_cleanup_holds_maintenance_until_thread_finished(setting, manager, dialogs,
                                                         monkeypatch):
    dialogs.always("confirm", True)
    states = []
    real_end = manager.end_maintenance
    monkeypatch.setattr(
        manager, 'end_maintenance', lambda: states.append("end") or real_end())

    setting._on_cleanup_clicked()
    assert manager.is_maintenance_running() is True
    assert not setting.cleanup_card.button.isEnabled()
    worker = setting._cleanup_worker
    assert worker is not None
    assert worker.retention_days == 30

    assert poll_until(
        lambda: (QApplication.processEvents() or True) and setting._cleanup_worker is None,
        timeout=20)
    assert states == ["end"]
    assert manager.is_maintenance_running() is False
    assert setting.cleanup_card.button.isEnabled()


def test_result_slot_does_not_release_worker_or_leave_maintenance(setting, manager):
    manager.begin_maintenance()
    sentinel = SimpleNamespace(isRunning=lambda: False, wait=lambda *a: True)
    setting._cleanup_worker = sentinel
    setting._on_cleanup_finished(0)
    assert setting._cleanup_worker is sentinel
    assert manager.is_maintenance_running() is True
    setting._cleanup_worker = None
    manager.end_maintenance()


def test_cleanup_button_follows_manager_signals(setting, manager, monkeypatch):
    assert setting.cleanup_card.button.isEnabled()
    monkeypatch.setattr(manager, 'has_running_tasks', lambda: True)
    manager.task_started.emit("t1")
    assert not setting.cleanup_card.button.isEnabled()
    monkeypatch.setattr(manager, 'has_running_tasks', lambda: False)
    manager.task_stopped.emit("t1", StopReason.USER)
    assert setting.cleanup_card.button.isEnabled()


# ---------- 关于页 ----------

@pytest.fixture
def about_services(notifier, dialogs):
    return make_services(notifier=notifier, dialogs=dialogs)


@pytest.fixture
def about(qapp, about_services):
    page = make_about_page(about_services)
    yield page
    page.close()


def _info(**overrides):
    info = {
        'version': '9.9.9', 'html_url': 'https://example.invalid/releases/v9.9.9',
        'asset_url': 'https://example.invalid/setup.exe', 'asset_name': 'setup.exe',
        'asset_size': 1000, 'asset_digest': None, 'body': ''}
    info.update(overrides)
    return info


def test_release_button_hidden_by_default(about):
    assert about.release_button.isHidden()


def test_repo_button_opens_project_homepage(about, dialogs):
    about.repo_button.click()

    assert dialogs.calls_of("open_url") == [{"url": about.repo_url}]
    assert about.repo_url.startswith("https://github.com/")


def test_missing_digest_shows_friendly_message_and_release_entry(about, notifier,
                                                                 monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(
        about_module, 'UpdateDownloader',
        lambda *a, **k: pytest.fail("缺少校验摘要时不应启动下载"))

    about._start_update(_info())

    assert about.status_label.text() == MSG_NO_DIGEST
    assert not about.release_button.isHidden()
    notice = notifier.last()
    assert notice.level == "warning" and MSG_NO_DIGEST in notice.content
    assert notice.duration == 8000
    # 提示条上追加了"打开发布页"按钮
    assert len(notice.bar.widgets) == 1
    assert not about.progress_bar.isVisible()
    assert about.check_button.isEnabled()


def test_release_button_opens_release_page(about, dialogs, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    about._start_update(_info())

    about.release_button.click()

    assert dialogs.calls_of("open_url") == [{"url": 'https://example.invalid/releases/v9.9.9'}]


def test_infobar_release_button_opens_release_page(about, notifier, dialogs, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    about._start_update(_info())

    notifier.last().bar.widgets[0].click()

    assert dialogs.calls_of("open_url") == [{"url": 'https://example.invalid/releases/v9.9.9'}]


def test_missing_asset_falls_back_to_release_page(about, dialogs, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)

    about._start_update(_info(asset_url=None))

    assert dialogs.calls_of("open_url") == [{"url": 'https://example.invalid/releases/v9.9.9'}]
    assert about.status_label.text() == "未找到安装包，已打开发布页"


def test_source_environment_only_hints(about, notifier, dialogs, monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)

    about._start_update(_info(asset_digest='sha256:00'))

    assert notifier.last().level == "info" and notifier.last().title == "开发环境"
    assert dialogs.calls == []


def test_release_url_falls_back_to_repo_releases(about):
    url = about._release_page_url({})
    assert url.startswith("https://github.com/") and url.endswith("/releases")


def test_download_error_cannot_verify_maps_to_friendly_message(about):
    about._pending_info = _info()

    about._on_download_error(MSG_CANNOT_VERIFY)

    assert about.status_label.text() == MSG_NO_DIGEST
    assert not about.release_button.isHidden()


def test_other_download_errors_keep_generic_message(about, notifier):
    about._on_download_error("网络错误")

    assert about.status_label.text() == "下载失败，可稍后重试"
    assert about.release_button.isHidden()
    assert notifier.last().level == "warning"
    assert notifier.last().content == "网络错误"


def test_manual_check_error_shows_warning_but_silent_does_not(about, notifier):
    about._on_check_error("连接超时", silent=True)
    assert notifier.notices == []
    assert about.status_label.text() == "自动检查未完成，可手动重试"

    about._on_check_error("连接超时", silent=False)
    assert notifier.last().title == "检查更新失败"
    assert notifier.last().content == "连接超时"


def _accept_install_dialog(monkeypatch, accepted=True):
    """安装确认框不进模态循环：直接给出用户的选择"""
    monkeypatch.setattr(about_module.InstallConfirmDialog, 'exec', lambda self: accepted)


def test_install_launch_failure_shows_error_text_and_does_not_quit(about, dialogs,
                                                                   monkeypatch):
    """启动安装程序失败：把异常文案显示在状态栏，且不退出应用"""
    _accept_install_dialog(monkeypatch)
    dialogs.push("open_file", OSError("拒绝访问"))
    closed = []
    monkeypatch.setattr(about, 'close', lambda: closed.append(True))
    about._pending_info = _info()

    about._on_download_finished(r"C:\temp\setup.exe")

    assert dialogs.last_call("open_file") == {"path": r"C:\temp\setup.exe"}
    assert about.status_label.text() == "启动安装程序失败：拒绝访问"
    assert closed == []


def test_install_launch_success_quits_for_install(about, dialogs, monkeypatch):
    """安装程序已启动：调用窗口的 quit_for_install 走真退出路径"""
    _accept_install_dialog(monkeypatch)
    quits = []
    monkeypatch.setattr(about, 'quit_for_install', lambda: quits.append(True),
                        raising=False)
    about._pending_info = _info()

    about._on_download_finished(r"C:\temp\setup.exe")

    assert dialogs.methods_called() == ["open_file"]
    assert quits == [True]


def test_install_postponed_keeps_installer_path(about, dialogs, monkeypatch):
    _accept_install_dialog(monkeypatch, accepted=False)
    about._pending_info = _info()

    about._on_download_finished(r"C:\temp\setup.exe")

    assert dialogs.calls == []
    assert about.status_label.text() == r"安装包已保存：C:\temp\setup.exe"
