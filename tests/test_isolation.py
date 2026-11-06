"""测试隔离与数据目录配置：用例不得触碰开发机 data\\ 目录；环境变量可覆盖数据目录"""
import os
import sys

import app_config
import config
from data.database import Database
from utils.logger import get_log_dir

PROJECT_DATA_DIR = os.path.join(config.BASE_DIR, "data")


def _is_under(path: str, parent: str) -> bool:
    return os.path.normcase(os.path.abspath(path)).startswith(
        os.path.normcase(os.path.abspath(parent)) + os.sep)


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def test_autouse_fixture_redirects_all_data_paths(app_paths):
    """根 conftest 只设环境变量，所有由数据目录派生的路径在访问时都指向临时目录"""
    assert os.environ[config.DATA_DIR_ENV] == app_paths.data_dir
    for path in (config.DATA_DIR, config.DB_PATH, config.UPDATE_PREFS_PATH,
                 app_config.CONFIG_PATH, get_log_dir()):
        assert not _is_under(path, PROJECT_DATA_DIR) and not _same(path, PROJECT_DATA_DIR)
        assert _same(path, app_paths.data_dir) or _is_under(path, app_paths.data_dir)
    assert os.path.dirname(config.DB_PATH) == config.DATA_DIR
    assert os.path.dirname(config.UPDATE_PREFS_PATH) == config.DATA_DIR
    assert os.path.dirname(app_config.CONFIG_PATH) == config.DATA_DIR
    assert get_log_dir() == app_paths.log_dir


def test_path_constants_are_not_cached_at_import(monkeypatch, tmp_path):
    """兼容路径常量访问时现算：环境变量在导入之后改变也随之生效"""
    target = str(tmp_path / "later")
    monkeypatch.setenv(config.DATA_DIR_ENV, target)
    assert config.DATA_DIR == os.path.abspath(target)
    assert config.DB_PATH == os.path.join(os.path.abspath(target), "monitor.db")
    assert app_config.CONFIG_PATH == os.path.join(os.path.abspath(target), "config.json")
    # 只读路径不创建目录
    assert not os.path.exists(target)


def test_importing_config_has_no_side_effects(tmp_path):
    """在全新解释器里导入 config 与 app_config 不创建数据目录"""
    import subprocess
    target = tmp_path / "fresh"
    env = dict(os.environ, **{config.DATA_DIR_ENV: str(target)})
    code = "import config, app_config, utils.logger; print(config.DB_PATH)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=config.BASE_DIR, env=env,
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    assert not target.exists()


def test_default_database_uses_isolated_path():
    """不传路径构造 Database（兼容旧调用方）也落在临时目录"""
    db = Database()
    assert db.db_path == config.DB_PATH
    assert os.path.exists(config.DB_PATH)
    assert not _is_under(db.db_path, PROJECT_DATA_DIR)
    db.close()


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
