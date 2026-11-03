"""
设置页：开关文案、清理数据库的维护状态与线程释放时机；
关于页：缺少校验摘要时的友好提示与发布页入口。
"""
import sys
from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QApplication

from qfluentwidgets import qconfig

import ui.pages.about_page as about_module
from app_config import cfg
import ui.pages.setting_page as setting_module
from core.monitor_manager import MonitorManager
from core.update_checker import MSG_CANNOT_VERIFY
from tests.helpers import poll_until
from ui.pages.about_page import MSG_NO_DIGEST, AboutPage
from ui.pages.setting_page import SettingPage


def _silent_infobar(calls):
    def _make(level):
        def _call(**kwargs):
            calls.append((level, kwargs))
            return SimpleNamespace(addWidget=lambda *a, **k: None)
        return _call
    return SimpleNamespace(**{lv: _make(lv) for lv in ("info", "success", "warning", "error")})


@pytest.fixture
def manager(db):
    return MonitorManager(db=db)


@pytest.fixture
def setting(qapp, db, manager):
    page = SettingPage(db=db, manager=manager)
    yield page
    worker = page._cleanup_worker
    if worker is not None:
        worker.wait(10000)
        QApplication.processEvents()
    page.close()


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


# ---------- 清理数据库：维护状态与线程释放 ----------

def test_cleanup_refused_while_tasks_are_running(setting, manager, monkeypatch):
    calls = []
    monkeypatch.setattr(setting_module, 'InfoBar', _silent_infobar(calls))
    monkeypatch.setattr(manager, 'begin_maintenance', lambda: False)
    monkeypatch.setattr(
        setting, '_confirm_cleanup', lambda days: pytest.fail("被拒绝时不应再弹确认"))

    setting._on_cleanup_clicked()

    assert setting._cleanup_worker is None
    assert calls and calls[-1][0] == "warning"


def test_cancelled_confirmation_leaves_maintenance(setting, manager, monkeypatch):
    monkeypatch.setattr(setting, '_confirm_cleanup', lambda days: False)

    setting._on_cleanup_clicked()

    assert manager.is_maintenance_running() is False
    assert setting._cleanup_worker is None


def test_cleanup_holds_maintenance_until_thread_finished(setting, manager, db, monkeypatch):
    monkeypatch.setattr(setting, '_confirm_cleanup', lambda days: True)
    states = []
    real_end = manager.end_maintenance
    monkeypatch.setattr(
        manager, 'end_maintenance', lambda: states.append("end") or real_end())

    setting._on_cleanup_clicked()
    assert manager.is_maintenance_running() is True
    assert not setting.cleanup_card.button.isEnabled()
    worker = setting._cleanup_worker
    assert worker is not None

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


def test_cleanup_button_follows_manager_signals(setting, manager, db, monkeypatch):
    assert setting.cleanup_card.button.isEnabled()
    monkeypatch.setattr(manager, 'has_running_tasks', lambda: True)
    manager.task_started.emit("t1")
    assert not setting.cleanup_card.button.isEnabled()
    monkeypatch.setattr(manager, 'has_running_tasks', lambda: False)
    manager.task_stopped.emit("t1", "用户停止")
    assert setting.cleanup_card.button.isEnabled()


# ---------- 关于页 ----------

@pytest.fixture
def about(qapp):
    page = AboutPage()
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


def test_missing_digest_shows_friendly_message_and_release_entry(about, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    calls = []
    monkeypatch.setattr(about_module, 'InfoBar', _silent_infobar(calls))
    monkeypatch.setattr(
        about_module, 'UpdateDownloader',
        lambda *a, **k: pytest.fail("缺少校验摘要时不应启动下载"))

    about._start_update(_info())

    assert about.status_label.text() == MSG_NO_DIGEST
    assert not about.release_button.isHidden()
    assert calls[-1][0] == "warning" and MSG_NO_DIGEST in calls[-1][1]["content"]
    assert not about.progress_bar.isVisible()
    assert about.check_button.isEnabled()


def test_release_button_opens_release_page(about, monkeypatch):
    opened = []
    monkeypatch.setattr(about_module.webbrowser, 'open', lambda url: opened.append(url))
    monkeypatch.setattr(about_module, 'InfoBar', _silent_infobar([]))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    about._start_update(_info())

    about.release_button.click()

    assert opened == ['https://example.invalid/releases/v9.9.9']


def test_release_url_falls_back_to_repo_releases(about):
    url = about._release_page_url({})
    assert url.startswith("https://github.com/") and url.endswith("/releases")


def test_download_error_cannot_verify_maps_to_friendly_message(about, monkeypatch):
    monkeypatch.setattr(about_module, 'InfoBar', _silent_infobar([]))
    about._pending_info = _info()

    about._on_download_error(MSG_CANNOT_VERIFY)

    assert about.status_label.text() == MSG_NO_DIGEST
    assert not about.release_button.isHidden()


def test_other_download_errors_keep_generic_message(about, monkeypatch):
    calls = []
    monkeypatch.setattr(about_module, 'InfoBar', _silent_infobar(calls))

    about._on_download_error("网络错误")

    assert about.status_label.text() == "下载失败，可稍后重试"
    assert about.release_button.isHidden()
    assert calls[-1][1]["content"] == "网络错误"
