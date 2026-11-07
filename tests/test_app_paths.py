"""AppPaths：路径解析规则（环境变量 / 打包 / 开发）与无副作用"""
import os
import sys

import pytest

import config
from app.paths import AppPaths

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = r'C:\Users\someone\AppData\Local'


def _fields_under(paths: AppPaths):
    return {
        'db_path': os.path.join(paths.data_dir, 'monitor.db'),
        'config_path': os.path.join(paths.data_dir, 'config.json'),
        'prefs_path': os.path.join(paths.data_dir, 'update_prefs.json'),
    }


def test_env_override_puts_logs_inside_data_dir(tmp_path):
    target = str(tmp_path / "portable")
    paths = AppPaths.resolve(env={config.DATA_DIR_ENV: target}, frozen=False)
    assert paths.data_dir == os.path.abspath(target)
    assert paths.log_dir == os.path.join(os.path.abspath(target), "logs")
    for name, expected in _fields_under(paths).items():
        assert getattr(paths, name) == expected


@pytest.mark.parametrize("frozen", [False, True])
def test_env_override_wins_in_both_modes(tmp_path, frozen):
    target = str(tmp_path / "x")
    env = {config.DATA_DIR_ENV: target, 'LOCALAPPDATA': LOCAL}
    assert AppPaths.resolve(env=env, frozen=frozen).data_dir == os.path.abspath(target)


def test_frozen_uses_localappdata():
    paths = AppPaths.resolve(env={'LOCALAPPDATA': LOCAL}, frozen=True)
    assert paths.data_dir == os.path.join(LOCAL, config.APP_NAME, "data")
    assert paths.log_dir == os.path.join(LOCAL, config.APP_NAME, "logs")
    assert paths.base_dir == os.path.dirname(sys.executable)
    for name, expected in _fields_under(paths).items():
        assert getattr(paths, name) == expected


def test_frozen_without_localappdata_falls_back_to_home():
    paths = AppPaths.resolve(env={}, frozen=True)
    assert paths.data_dir == os.path.join(os.path.expanduser('~'), config.APP_NAME, "data")


def test_dev_uses_project_root():
    paths = AppPaths.resolve(env={}, frozen=False)
    assert paths.base_dir == PROJECT_ROOT == config.BASE_DIR
    assert paths.data_dir == os.path.join(PROJECT_ROOT, "data")
    assert paths.log_dir == os.path.join(PROJECT_ROOT, "logs")


def test_empty_env_var_is_ignored():
    paths = AppPaths.resolve(env={config.DATA_DIR_ENV: ""}, frozen=False)
    assert paths.data_dir == os.path.join(PROJECT_ROOT, "data")


def test_defaults_read_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / "env"))
    assert AppPaths.resolve().data_dir == str(tmp_path / "env")


def test_resolve_has_no_side_effects(tmp_path):
    target = tmp_path / "nothing"
    AppPaths.resolve(env={config.DATA_DIR_ENV: str(target)}, frozen=False)
    assert not target.exists()


def test_ensure_dirs_creates_data_and_log_dirs(tmp_path):
    paths = AppPaths.resolve(env={config.DATA_DIR_ENV: str(tmp_path / "d")}, frozen=False)
    paths.ensure_dirs()
    paths.ensure_dirs()   # 幂等
    assert os.path.isdir(paths.data_dir) and os.path.isdir(paths.log_dir)


def test_under_builds_paths_for_test_dirs(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    assert paths.data_dir == str(tmp_path)
    assert paths.log_dir == str(tmp_path / "logs")
    for name, expected in _fields_under(paths).items():
        assert getattr(paths, name) == expected


def test_is_frozen_dataclass(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    with pytest.raises(Exception):
        paths.data_dir = "elsewhere"


def test_config_compat_values_match(tmp_path, monkeypatch):
    """config 的兼容常量与 AppPaths 一致"""
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    paths = AppPaths.resolve()
    assert config.DATA_DIR == paths.data_dir
    assert config.DB_PATH == paths.db_path
    assert config.UPDATE_PREFS_PATH == paths.prefs_path
    assert config.LOG_DIR == paths.log_dir
    with pytest.raises(AttributeError):
        config.NO_SUCH_CONSTANT  # noqa: B018
