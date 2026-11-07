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
from PyQt5.QtWidgets import QApplication, QWidget

from qfluentwidgets import qconfig

import config
import ui.pages.about_update as about_module
from app_config import cfg
from core.contracts import StopReason
from core.monitor_manager import MonitorManager
from core.update_checker import MSG_CANNOT_VERIFY
from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
from tests.factories import make_about_update_group, make_services, make_setting_page
from tests.helpers import poll_until
from ui.pages.about_update import MSG_NO_DIGEST
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


def test_about_update_group_requires_services(qapp):
    from ui.pages.about_update import AboutUpdateGroup
    with pytest.raises(TypeError):
        AboutUpdateGroup()


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


def test_keep_awake_switch_roundtrips_config(setting, tmp_path):
    """"监控期间阻止电脑睡眠"开关：默认开，点击写配置并落盘，配置变化反映到开关"""
    from ui.pages.setting_page import KEEP_AWAKE_CONTENT, KEEP_AWAKE_TITLE

    config_path = tmp_path / "keep_awake_config.json"
    qconfig.load(str(config_path), cfg)
    item = cfg.keep_awake_while_monitoring
    try:
        card = setting.keep_awake_card
        assert card.titleLabel.text() == KEEP_AWAKE_TITLE
        assert card.contentLabel.text() == KEEP_AWAKE_CONTENT
        assert card.isChecked() is True and card.switchButton.text == "开"

        card.switchButton.toggleChecked()          # 用户点击
        assert cfg.get(item) is False
        assert card.switchButton.text == "关"
        assert '"KeepAwakeWhileMonitoring": false' in config_path.read_text(encoding='utf-8')

        qconfig.set(item, True, save=False)        # 配置变化回显到开关
        assert card.isChecked() is True
    finally:
        item.value = item.defaultValue


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

def test_cleanup_lock_taken_and_released_on_ui_thread(setting, manager, dialogs,
                                                     monkeypatch):
    """两段式维护锁：确认后在界面线程占锁、线程结束后在界面线程释放；后台线程不碰锁"""
    import threading
    callers = []
    real_begin, real_end = manager.begin_maintenance, manager.end_maintenance

    def begin():
        callers.append(("begin", threading.current_thread() is threading.main_thread()))
        return real_begin()

    def end():
        callers.append(("end", threading.current_thread() is threading.main_thread()))
        return real_end()

    monkeypatch.setattr(manager, 'begin_maintenance', begin)
    monkeypatch.setattr(manager, 'end_maintenance', end)
    dialogs.always("confirm", True)

    setting._on_cleanup_clicked()
    # 线程启动前锁已在界面线程占好
    assert callers == [("begin", True)]
    assert setting._cleanup_worker.already_locked is True
    assert poll_until(
        lambda: (QApplication.processEvents() or True) and setting._cleanup_worker is None,
        timeout=20)

    assert callers == [("begin", True), ("end", True)]
    assert manager.is_maintenance_running() is False


@pytest.mark.parametrize("start_during_confirm, text", [
    ("export", "正在导出数据"),
    ("task", "有监控任务正在运行"),
])
def test_busy_started_during_confirm_refused_before_thread(setting, manager, notifier,
                                                           monkeypatch, start_during_confirm,
                                                           text):
    """确认框弹出期间开始了导出/任务：占锁在启动线程之前被拒，按原因提示，不启动线程"""
    def confirm(days):
        if start_during_confirm == "export":
            assert manager.begin_export() is True
        else:
            monkeypatch.setattr(manager, 'has_running_tasks', lambda: True)
        return True

    monkeypatch.setattr(setting, '_confirm_cleanup', confirm)

    setting._on_cleanup_clicked()

    assert setting._cleanup_worker is None
    assert setting.services.threads.entries() == []
    assert manager.is_maintenance_running() is False
    assert notifier.levels() == ["warning"]
    notice = notifier.last("warning")
    assert notice.title == "无法清理" and text in notice.content
    assert notice.duration == 4000
    assert setting._state_tooltip is None
    if start_during_confirm == "export":
        manager.end_export()


def test_export_refused_while_cleanup_running(setting, db, manager, dialogs, monkeypatch):
    """清理线程运行期间（锁已在界面线程占好）开始导出被拒绝"""
    import threading
    gate = threading.Event()
    real_vacuum = db.maintenance.vacuum
    monkeypatch.setattr(db.maintenance, 'vacuum', lambda: (gate.wait(10), real_vacuum())[1])
    dialogs.always("confirm", True)

    setting._on_cleanup_clicked()
    try:
        assert setting._cleanup_worker is not None
        assert manager.begin_export() is False
    finally:
        gate.set()
    assert poll_until(
        lambda: (QApplication.processEvents() or True) and setting._cleanup_worker is None,
        timeout=20)
    assert manager.begin_export() is True
    manager.end_export()


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


def test_cleanup_holds_maintenance_until_thread_finished(setting, db, manager, dialogs,
                                                         monkeypatch):
    """压缩期间处于维护状态；线程结束后维护已退出、引用已释放、按钮恢复"""
    dialogs.always("confirm", True)
    during = []
    real_vacuum = db.maintenance.vacuum
    monkeypatch.setattr(db.maintenance, 'vacuum', lambda: (
        during.append(manager.is_maintenance_running()), real_vacuum())[1])

    setting._on_cleanup_clicked()
    assert not setting.cleanup_card.button.isEnabled()
    worker = setting._cleanup_worker
    assert worker is not None
    assert worker.retention_days == 30

    assert poll_until(
        lambda: (QApplication.processEvents() or True) and setting._cleanup_worker is None,
        timeout=20)
    assert during == [True]
    assert manager.is_maintenance_running() is False
    assert setting.cleanup_card.button.isEnabled()


def test_cleanup_worker_registered_and_released(setting, dialogs):
    """清理线程登记到 services.threads（不可取消、5 秒超时），结束后自动注销并释放"""
    from PyQt5 import sip
    from PyQt5.QtCore import QCoreApplication, QEvent
    dialogs.always("confirm", True)

    setting._on_cleanup_clicked()
    worker = setting._cleanup_worker
    entries = setting.services.threads.entries()
    assert [(e.name, e.thread, e.cancel, e.timeout_ms) for e in entries] == [
        ("清理数据库", worker, None, 5000)]

    def released():
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        return setting.services.threads.entries() == [] and sip.isdeleted(worker)

    assert poll_until(released, timeout=20)
    assert setting._cleanup_worker is None


@pytest.mark.parametrize("failing", ["vacuum", "cleanup_older_than"])
def test_cleanup_failure_reports_error_instead_of_done(setting, db, manager, dialogs,
                                                       notifier, monkeypatch, failing):
    """压缩/清理抛 StorageError：发失败信号、提示"清理失败"，不再显示"数据库清理完成"；
    维护状态照常在线程结束时退出"""
    from data.errors import StorageError

    def boom(*a, **k):
        raise StorageError("压缩数据库失败: database is locked")

    monkeypatch.setattr(db.maintenance, failing, boom)
    done = []
    monkeypatch.setattr(setting, '_on_cleanup_finished', lambda deleted: done.append(deleted))
    dialogs.always("confirm", True)

    setting._on_cleanup_clicked()
    assert poll_until(
        lambda: (QApplication.processEvents() or True) and setting._cleanup_worker is None,
        timeout=20)

    assert done == []
    error = notifier.last("error")
    assert error.title == "清理失败" and "database is locked" in error.content
    assert setting._state_tooltip is None
    # 线程失败后（finished）界面线程已释放维护锁
    assert manager.is_maintenance_running() is False
    assert manager.begin_export() is True
    manager.end_export()
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
    page = make_about_update_group(about_services)
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


def test_install_launch_success_emits_install_requested(about, dialogs, monkeypatch):
    """安装程序已启动：发 install_requested（由主窗口接到 quit_for_install），
    页面不再反向调用窗口方法"""
    _accept_install_dialog(monkeypatch)
    requested = []
    about.install_requested.connect(lambda: requested.append(True))
    monkeypatch.setattr(about, 'quit_for_install', lambda: pytest.fail("不应反向调用窗口"),
                        raising=False)
    about._pending_info = _info()

    about._on_download_finished(r"C:\temp\setup.exe")

    assert dialogs.methods_called() == ["open_file"]
    assert requested == [True]


def test_install_launch_failure_does_not_emit_install_requested(about, dialogs,
                                                                monkeypatch):
    _accept_install_dialog(monkeypatch)
    dialogs.push("open_file", OSError("拒绝访问"))
    requested = []
    about.install_requested.connect(lambda: requested.append(True))
    about._pending_info = _info()

    about._on_download_finished(r"C:\temp\setup.exe")

    assert requested == []


def test_install_postponed_keeps_installer_path(about, dialogs, monkeypatch):
    _accept_install_dialog(monkeypatch, accepted=False)
    about._pending_info = _info()

    about._on_download_finished(r"C:\temp\setup.exe")

    assert dialogs.calls == []
    assert about.status_label.text() == r"安装包已保存：C:\temp\setup.exe"


# ---------- 关于页：后台线程登记 ----------

def _stub_thread_class(base_signals: dict):
    """构造一个立即结束的线程桩：带关于页会连接的具名信号"""
    from PyQt5.QtCore import pyqtSignal
    from core.workers.base import BackgroundTask

    attrs = {name: pyqtSignal(*types) for name, types in base_signals.items()}
    attrs['__init__'] = lambda self, *a, **k: BackgroundTask.__init__(self)
    attrs['execute'] = lambda self, token: None
    attrs['handle_result'] = lambda self, value: None
    return type('_StubThread', (BackgroundTask,), attrs)


def test_update_checker_registered_and_reference_released(about, monkeypatch):
    stub = _stub_thread_class({'check_finished': (dict,), 'error_occurred': (str,)})
    monkeypatch.setattr(about_module, 'UpdateChecker', stub)

    about.check_update(silent=True)
    checker = about._checker
    entries = about.services.threads.entries()
    assert [(e.name, e.thread, e.cancel, e.timeout_ms) for e in entries] == [
        ("检查更新", checker, None, 1000)]

    # 引用在结果槽里清空，登记表在 QThread.finished 时注销：两者先后不定，都要等到
    assert poll_until(lambda: (QApplication.processEvents() or True)
                      and about._checker is None
                      and about.services.threads.entries() == [], timeout=10)


def test_update_downloader_registered_with_cancel(about, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    stub = _stub_thread_class({'download_progress': (int,), 'download_finished': (str,),
                               'error_occurred': (str,)})
    monkeypatch.setattr(about_module, 'UpdateDownloader', stub)

    about._start_update(_info(asset_digest='sha256:' + '0' * 64))
    downloader = about._downloader
    entries = about.services.threads.entries()
    assert [(e.name, e.thread, e.cancel, e.timeout_ms) for e in entries] == [
        ("更新下载", downloader, downloader.cancel, 2000)]

    # 引用在结果槽里清空，登记表在 QThread.finished 时注销：两者先后不定，都要等到
    assert poll_until(lambda: (QApplication.processEvents() or True)
                      and about._downloader is None
                      and about.services.threads.entries() == [], timeout=10)


# ---------- 分组与文案（常规 / 数据 / 关于与更新） ----------

def test_three_groups_in_order(setting):
    from qfluentwidgets import SettingCardGroup
    groups = [w for w in setting.scroll_widget.children() if isinstance(w, SettingCardGroup)]
    assert [g.titleLabel.text() for g in groups] == ["常规", "数据", "关于与更新"]
    assert setting.about_group is groups[2]
    general = [setting.theme_card, setting.default_interval_card,
               setting.close_to_tray_card, setting.hotkey_card]
    assert all(card.parent() is not None for card in general)
    assert setting.default_interval_card.titleLabel.text() == "默认采集间隔"
    assert setting.hotkey_card.titleLabel.text() == "打点快捷键"


def test_maintenance_button_text_follows_retention(setting, prefs):
    from ui.pages.setting_page import maintenance_button_text
    assert maintenance_button_text(0) == "压缩数据库"
    assert maintenance_button_text(30) == "清理并压缩"
    assert setting.cleanup_card.button.text() == "清理并压缩"
    prefs.retention = 0
    setting._refresh_data_management_state()
    assert setting.cleanup_card.button.text() == "压缩数据库"


def test_compact_only_confirm_uses_compact_wording(setting, dialogs):
    dialogs.always("confirm", False)
    setting._confirm_cleanup(0)
    call = dialogs.last_call("confirm")
    assert call["title"] == "压缩数据库" and call["confirm_text"] == "压缩"


def test_scroll_to_about_does_not_raise(setting):
    setting.resize(800, 400)
    setting.show()
    setting.scroll_to_about()
    assert setting.verticalScrollBar().value() <= setting.about_group.y()


def test_about_group_status_shows_last_check_time(about):
    from ui.pages.about_update import APP_TAGLINE, up_to_date_text
    from qfluentwidgets import PrimaryPushButton
    assert about.about_card.contentLabel.text() == APP_TAGLINE \
        == "面向测试的内存 / 句柄 / GDI 泄漏排查工具"
    # 低频操作用普通按钮
    assert about.check_button.text() == "检查更新"
    assert not isinstance(about.check_button, PrimaryPushButton)

    about._on_check_finished({'has_update': False}, silent=False)
    checked_at = about.services.clock.now().strftime('%H:%M')
    assert about.status_label.text() == up_to_date_text(checked_at)
    assert about.status_label.text().startswith("已是最新版本")
    assert about.status_label.text().endswith(f" · 上次检查 {checked_at}")


def test_install_confirm_defaults_to_later(qapp):
    """回车 = 安全操作：安装确认框显示后焦点在"稍后" """
    from ui.pages.about_update import InstallConfirmDialog
    host = QWidget()
    host.resize(600, 400)
    host.show()
    try:
        dialog = InstallConfirmDialog(host, "9.9.9")
        dialog.show()
        QApplication.processEvents()
        assert dialog.cancelButton.hasFocus() or host.window().focusWidget() is dialog.cancelButton \
            or dialog.focusWidget() is dialog.cancelButton
        dialog.close()
    finally:
        host.close()
