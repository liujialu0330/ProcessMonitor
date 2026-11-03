"""测试隔离与数据目录配置：用例不得触碰开发机 data\\ 目录；环境变量可覆盖数据目录"""
import os
import sys

import app_config
import config
from data.database import Database

PROJECT_DATA_DIR = os.path.join(config.BASE_DIR, "data")


def _is_under(path: str, parent: str) -> bool:
    return os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(parent)) + os.sep)


def test_autouse_fixture_redirects_all_data_paths():
    """根 conftest 的 autouse fixture 把所有由 DATA_DIR 派生的路径指向临时目录"""
    for path in (config.DATA_DIR, config.DB_PATH, config.UPDATE_PREFS_PATH,
                 app_config.CONFIG_PATH):
        assert not _is_under(path, PROJECT_DATA_DIR) and \
            os.path.normcase(os.path.abspath(path)) != os.path.normcase(PROJECT_DATA_DIR)
    assert os.path.dirname(config.DB_PATH) == config.DATA_DIR
    assert os.path.dirname(config.UPDATE_PREFS_PATH) == config.DATA_DIR
    assert os.path.dirname(app_config.CONFIG_PATH) == config.DATA_DIR


def test_default_database_uses_isolated_path():
    """不传路径构造 Database（如 MonitorTask 的回退）也落在临时目录"""
    db = Database()
    assert db.db_path == config.DB_PATH
    assert os.path.exists(config.DB_PATH)
    assert not _is_under(db.db_path, PROJECT_DATA_DIR)


def test_env_var_overrides_data_dir(monkeypatch, tmp_path):
    target = str(tmp_path / "custom_data")
    monkeypatch.setenv(config.DATA_DIR_ENV, target)
    assert config.DATA_DIR_ENV == "PROCESS_MONITOR_DATA_DIR"
    assert config.get_data_dir() == os.path.abspath(target)

    # 打包环境下环境变量同样优先
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert config.get_data_dir() == os.path.abspath(target)


def test_default_data_dir_unchanged_without_env(monkeypatch):
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)

    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    assert config.get_data_dir() == os.path.join(config.BASE_DIR, "data")

    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('LOCALAPPDATA', r'C:\Users\someone\AppData\Local')
    assert config.get_data_dir() == os.path.join(
        r'C:\Users\someone\AppData\Local', config.APP_NAME, "data")


def test_empty_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv(config.DATA_DIR_ENV, "")
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    assert config.get_data_dir() == os.path.join(config.BASE_DIR, "data")


def test_monitor_manager_singleton_reset_between_tests_part1(qapp):
    """与下一个用例成对：验证单例由根 conftest 在用例间重置"""
    from core.monitor_manager import MonitorManager
    assert MonitorManager._instance is None
    manager = MonitorManager(db=Database())
    manager.create_task(os.getpid(), "x", ["memory_rss"], 1.0)
    assert manager.get_task_count() == 1


def test_monitor_manager_singleton_reset_between_tests_part2(qapp):
    from core.monitor_manager import MonitorManager
    assert MonitorManager._instance is None
    assert MonitorManager(db=Database()).get_task_count() == 0
