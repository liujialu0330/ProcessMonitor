"""
数据目录解析顺序（命令行 → 环境变量 → 便携标志 → 默认）、AppPaths.source、
同步盘判定，以及它们在主窗口提示与设置页"数据目录"卡片上的落点
"""
import os
import sys

import pytest

import config
from app import paths as paths_module
from app.paths import (
    AppPaths, DataDirError, SYNC_DIR_HINT_TEMPLATE, parse_data_dir_arg, strip_data_dir_args,
    sync_dir_hint,
)

LOCAL = r'C:\Users\someone\AppData\Local'


@pytest.fixture
def portable_root(tmp_path, monkeypatch):
    """开发环境的基础目录换成临时目录（便携标志放在这里）"""
    root = tmp_path / "app_root"
    root.mkdir()
    monkeypatch.setattr(paths_module, "_PROJECT_ROOT", str(root))
    return root


def _flag(root):
    (root / config.PORTABLE_FLAG_FILENAME).write_text("", encoding="utf-8")


# ========== 命令行参数 ==========

@pytest.mark.parametrize("argv, expected", [
    (["prog"], None),
    (["prog", "--data-dir", r"D:\pm"], r"D:\pm"),
    (["prog", r"--data-dir=D:\pm"], r"D:\pm"),
    (["prog", "--data-dir", r"D:\a", r"--data-dir=D:\b"], r"D:\b"),
    (["prog", "--other", "--data-dir", r"D:\a b\数据"], r"D:\a b\数据"),
    # argv[0] 不参与解析
    (["--data-dir", "x"], None),
    # 前缀相同的其他参数不算
    (["prog", "--data-dirx=1"], None),
])
def test_parse_data_dir_arg(argv, expected):
    assert parse_data_dir_arg(argv) == expected


@pytest.mark.parametrize("argv", [
    ["prog", "--data-dir"],
    ["prog", "--data-dir="],
    ["prog", "--data-dir", ""],
    ["prog", "--data-dir", "   "],
])
def test_missing_or_empty_data_dir_is_an_error(argv):
    """缺少路径不能悄悄当作当前目录"""
    with pytest.raises(DataDirError):
        parse_data_dir_arg(argv)
    with pytest.raises(DataDirError):
        AppPaths.resolve(env={}, frozen=False, argv=argv)


@pytest.mark.parametrize("argv, expected", [
    (["prog"], ["prog"]),
    (["prog", "--data-dir", "x", "-style", "fusion"], ["prog", "-style", "fusion"]),
    (["prog", "--data-dir=x", "y"], ["prog", "y"]),
    (["prog", "--data-dir"], ["prog"]),
])
def test_strip_data_dir_args(argv, expected):
    assert strip_data_dir_args(argv) == expected


# ========== 解析顺序与 source ==========

@pytest.mark.parametrize("frozen", [False, True])
def test_cli_wins_over_env_and_portable(tmp_path, portable_root, frozen, monkeypatch):
    if frozen:
        monkeypatch.setattr(sys, "executable", str(portable_root / "app.exe"))
    _flag(portable_root)
    target = str(tmp_path / "cli")
    env = {config.DATA_DIR_ENV: str(tmp_path / "env"), "LOCALAPPDATA": LOCAL}
    paths = AppPaths.resolve(env=env, frozen=frozen, argv=["prog", "--data-dir", target])
    assert paths.source == config.DATA_DIR_SOURCE_CLI
    assert paths.data_dir == os.path.abspath(target)
    assert paths.log_dir == os.path.join(os.path.abspath(target), "logs")
    assert paths.db_path == os.path.join(os.path.abspath(target), "monitor.db")
    assert paths.config_path == os.path.join(os.path.abspath(target), "config.json")


def test_relative_cli_path_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = AppPaths.resolve(env={}, frozen=False, argv=["prog", "--data-dir=rel"])
    assert paths.data_dir == str(tmp_path / "rel")


def test_env_wins_over_portable(tmp_path, portable_root):
    _flag(portable_root)
    paths = AppPaths.resolve(env={config.DATA_DIR_ENV: str(tmp_path / "env")},
                             frozen=False, argv=["prog"])
    assert paths.source == config.DATA_DIR_SOURCE_ENV
    assert paths.data_dir == str(tmp_path / "env")


def test_portable_flag_in_dev_root(portable_root):
    _flag(portable_root)
    paths = AppPaths.resolve(env={}, frozen=False, argv=["prog"])
    assert paths.source == config.DATA_DIR_SOURCE_PORTABLE
    assert paths.base_dir == str(portable_root)
    assert paths.data_dir == str(portable_root / "data")
    assert paths.log_dir == str(portable_root / "logs")
    assert paths.db_path == str(portable_root / "data" / "monitor.db")


def test_portable_flag_next_to_exe(tmp_path, monkeypatch):
    install = tmp_path / "安装目录"
    install.mkdir()
    _flag(install)
    monkeypatch.setattr(sys, "executable", str(install / "进程监控助手.exe"))
    paths = AppPaths.resolve(env={"LOCALAPPDATA": LOCAL}, frozen=True, argv=["x.exe"])
    assert paths.source == config.DATA_DIR_SOURCE_PORTABLE
    assert paths.data_dir == str(install / "data")
    assert paths.log_dir == str(install / "logs")


def test_portable_flag_must_be_a_file(portable_root):
    (portable_root / config.PORTABLE_FLAG_FILENAME).mkdir()
    paths = AppPaths.resolve(env={}, frozen=False, argv=["prog"])
    assert paths.source == config.DATA_DIR_SOURCE_DEFAULT


def test_default_without_any_override(portable_root):
    paths = AppPaths.resolve(env={}, frozen=False, argv=["prog"])
    assert paths.source == config.DATA_DIR_SOURCE_DEFAULT
    assert paths.data_dir == str(portable_root / "data")
    assert paths.log_dir == str(portable_root / "logs")


def test_frozen_default_source():
    paths = AppPaths.resolve(env={"LOCALAPPDATA": LOCAL}, frozen=True, argv=["x.exe"])
    assert paths.source == config.DATA_DIR_SOURCE_DEFAULT
    assert paths.data_dir == os.path.join(LOCAL, config.APP_NAME, "data")


def test_default_ignores_overrides(tmp_path, portable_root):
    _flag(portable_root)
    paths = AppPaths.default(env={config.DATA_DIR_ENV: str(tmp_path)}, frozen=False)
    assert paths.source == config.DATA_DIR_SOURCE_DEFAULT
    assert paths.data_dir == str(portable_root / "data")


def test_resolve_reads_sys_argv_by_default(tmp_path, monkeypatch):
    """惰性解析的调用方（config.DATA_DIR、设置页）与组合根看到同一个数据目录"""
    target = str(tmp_path / "from_argv")
    monkeypatch.setattr(sys, "argv", ["prog", "--data-dir", target])
    assert AppPaths.resolve().data_dir == target
    assert AppPaths.resolve().source == config.DATA_DIR_SOURCE_CLI
    assert config.DATA_DIR == target
    assert config.DATA_DIR_SOURCE == config.DATA_DIR_SOURCE_CLI


def test_env_source_via_config(app_paths):
    assert config.DATA_DIR_SOURCE == config.DATA_DIR_SOURCE_ENV
    assert app_paths.source == config.DATA_DIR_SOURCE_ENV


def test_resolve_with_portable_flag_has_no_side_effects(portable_root):
    _flag(portable_root)
    AppPaths.resolve(env={}, frozen=False, argv=["prog"])
    assert sorted(os.listdir(portable_root)) == [config.PORTABLE_FLAG_FILENAME]


def test_under_defaults_to_default_source(tmp_path):
    assert AppPaths.under(str(tmp_path)).source == config.DATA_DIR_SOURCE_DEFAULT


def test_ensure_dirs_raises_for_unusable_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    paths = AppPaths.resolve(env={}, frozen=False,
                             argv=["prog", "--data-dir", str(blocker / "sub")])
    with pytest.raises(OSError):
        paths.ensure_dirs()


# ========== 同步盘判定 ==========

def _hint(name):
    return SYNC_DIR_HINT_TEMPLATE.format(name=name)


@pytest.mark.parametrize("var", ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"])
def test_onedrive_env_prefix(var):
    env = {var: r"E:\云同步\个人"}
    assert sync_dir_hint(r"E:\云同步\个人\pm\data", env) == _hint("OneDrive")
    assert sync_dir_hint(r"E:\云同步\个人", env) == _hint("OneDrive")


def test_onedrive_env_prefix_is_case_insensitive():
    env = {"OneDrive": r"e:\SYNC\Root"}
    assert sync_dir_hint(r"E:\sync\root\data", env) == _hint("OneDrive")


def test_env_prefix_needs_a_path_boundary():
    env = {"OneDrive": r"D:\Sync\Cloud"}
    assert sync_dir_hint(r"D:\Sync\CloudOther\data", env) is None


def test_env_prefix_on_other_drive_is_ignored():
    env = {"OneDrive": r"D:\Sync"}
    assert sync_dir_hint(r"C:\Sync\data", env) is None


def test_empty_env_values_are_ignored():
    env = {"OneDrive": "", "OneDriveCommercial": ""}
    assert sync_dir_hint(r"C:\local\data", env) is None


@pytest.mark.parametrize("path, name", [
    (r"C:\Users\a\OneDrive\文档\data", "OneDrive"),
    (r"C:\Users\a\OneDrive - Contoso\data", "OneDrive"),
    (r"C:\Users\a\onedrive\data", "OneDrive"),
    (r"C:\Users\a\Dropbox\data", "Dropbox"),
    (r"C:\Users\a\Dropbox (Personal)\pm", "Dropbox"),
    (r"C:\Users\a\Google Drive\pm", "Google Drive"),
    (r"C:\Users\a\GOOGLE DRIVE\pm", "Google Drive"),
    (r"D:\坚果云\工作\pm", "坚果云"),
    (r"C:\Users\a\Nutstore\pm", "坚果云"),
    (r"C:\Users\a\nutstore\pm", "坚果云"),
    (r"C:\Users\a\iCloudDrive\pm", "iCloud Drive"),
    (r"C:\Users\a\icloudDRIVE\pm", "iCloud Drive"),
])
def test_sync_folder_segments(path, name):
    assert sync_dir_hint(path, {}) == _hint(name)


@pytest.mark.parametrize("path", [
    r"C:\Users\a\AppData\Local\进程监控助手\data",
    r"D:\tools\pm\data",
    r"D:\Drive\data",
    r"\\server\share\pm\data",
])
def test_local_dirs_get_no_hint(path):
    assert sync_dir_hint(path, {}) is None


def test_hint_text_names_the_service_and_the_remedies():
    text = _hint("OneDrive")
    assert "同步盘（OneDrive）" in text
    assert "--data-dir" in text and "便携模式" in text


def test_sync_dir_hint_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("OneDriveCommercial", r"Z:\corp")
    assert sync_dir_hint(r"Z:\corp\pm") == _hint("OneDrive")


# ========== 主窗口提示与设置页来源 ==========

@pytest.fixture
def window(tmp_path, qapp):
    from core.monitor_manager import MonitorManager
    from data.database import Database
    from tests.doubles import FakePreferences, RecordingNotifier, ScriptedDialogs
    from tests.factories import make_main_window, make_services
    from tests.helpers import dispose_widget, stop_and_join
    from ui.services import ManagerTaskStatus

    db = Database(str(tmp_path / "sync_hint.db"))
    manager = MonitorManager(db=db)
    services = make_services(
        notifier=RecordingNotifier(), dialogs=ScriptedDialogs(),
        preferences=FakePreferences(), task_status=ManagerTaskStatus(manager))
    win = make_main_window(db, manager, services)
    win.about_group.check_update = lambda *args, **kwargs: None
    yield win
    stop_and_join(win.monitor_manager)
    dispose_widget(win)
    db.close()


def test_main_window_shows_sync_dir_warning_once(window, qapp):
    from PyQt5.QtTest import QTest
    from ui.main_window import SYNC_DIR_TIP_TITLE

    text = _hint("Dropbox")
    window.show_sync_dir_warning(text, delay_ms=0)
    QTest.qWait(50)

    notices = [n for n in window.services.notifier.notices if n.title == SYNC_DIR_TIP_TITLE]
    assert len(notices) == 1
    notice = notices[0]
    assert (notice.level, notice.content, notice.parent) == ("warning", text, window)
    # 与数据库迁移提示一样停留到用户关闭
    assert notice.duration == -1


@pytest.mark.parametrize("make_source, suffix", [
    (lambda tmp, mp: mp.setattr(sys, "argv", ["prog", "--data-dir", str(tmp / "cli")]),
     "（命令行指定）"),
    (lambda tmp, mp: None, "（环境变量）"),
])
def test_setting_page_shows_data_dir_source(qapp, db, tmp_path, monkeypatch, make_source, suffix):
    from core.monitor_manager import MonitorManager
    from tests.factories import make_setting_page
    from tests.helpers import dispose_widget

    make_source(tmp_path, monkeypatch)
    page = make_setting_page(db, MonitorManager(db=db))
    try:
        text = page.open_dir_card.contentLabel.text()
        assert text.endswith(suffix)
        assert page.open_dir_card.contentLabel.toolTip() == os.path.normpath(config.DATA_DIR)
    finally:
        dispose_widget(page)


def test_setting_page_source_texts_cover_every_source():
    from ui.pages.setting_page import DATA_DIR_SOURCE_TEXTS
    assert set(DATA_DIR_SOURCE_TEXTS) == {
        config.DATA_DIR_SOURCE_CLI, config.DATA_DIR_SOURCE_ENV,
        config.DATA_DIR_SOURCE_PORTABLE, config.DATA_DIR_SOURCE_DEFAULT,
    }
