"""
app_config 用例
覆盖默认值、持久化 roundtrip、非法值回落、首启主题默认 AUTO、
跳过版本（含旧 update_prefs.json 一次性导入）、取值范围单一来源
"""
import json
import os

from qfluentwidgets import Theme, qconfig

import app_config
import config
from app.paths import AppPaths
from app_config import AppConfig, cfg


def test_defaults():
    """未加载任何配置文件时，cfg 三项应为 config.py 常量对应的默认值"""
    assert cfg.get(cfg.default_interval) == int(config.DEFAULT_INTERVAL)
    assert cfg.get(cfg.retention_days) == config.DATA_RETENTION_DAYS
    assert cfg.get(cfg.close_to_tray) is False


def test_persist_roundtrip(tmp_path):
    """qconfig.load 到 tmp_path -> set 改值 -> 用新实例重新 load 同路径 -> 值保持"""
    config_path = str(tmp_path / "config.json")
    qconfig.load(config_path, cfg)

    qconfig.set(cfg.default_interval, 5)
    qconfig.set(cfg.retention_days, 30)
    qconfig.set(cfg.close_to_tray, True)

    # 三个自定义配置项是类级属性、进程内所有 AppConfig 实例共享同一对象，
    # 用新实例重新 load 同一份文件，验证的是"反序列化确实从磁盘读回了正确的值"，
    # 而不是"新实例凭空拥有旧实例的内存状态"
    cfg2 = AppConfig()
    qconfig.load(config_path, cfg2)

    assert cfg2.get(cfg2.default_interval) == 5
    assert cfg2.get(cfg2.retention_days) == 30
    assert cfg2.get(cfg2.close_to_tray) is True


def test_invalid_value_falls_back(tmp_path):
    """配置文件中的值不在合法选项范围内（如保留天数写成 15）时，加载后回落默认值"""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"Data": {"RetentionDays": 15}}), encoding="utf-8")

    cfg3 = AppConfig()
    qconfig.load(str(config_path), cfg3)

    assert cfg3.get(cfg3.retention_days) == config.DATA_RETENTION_DAYS


def test_theme_default_auto(tmp_path):
    """首启（配置文件尚不存在）时，load_app_config() 应将
    主题偏好落盘为"跟随系统"（Theme.AUTO）"""
    paths = AppPaths.under(str(tmp_path))
    config_path = paths.config_path
    assert not os.path.exists(config_path)

    # qconfig.set 在新值与当前值相同时会短路返回、不触发 save()（见 app_config.py
    # 顶部注释引用的源码机制）。本用例验证的正是"首启时主动切到 AUTO 并落盘"
    # 这个动作本身，因此需要显式把起始值安排成非 AUTO（而不是依赖 cfg 当前恰好
    # 是什么值——同进程内其它用例的 _reset_app_config 夹具会把 themeMode 复位为
    # AUTO，若不在这里显式改成别的值，本用例会因为"值没变"而误判落盘逻辑正常）
    cfg.themeMode.value = Theme.LIGHT

    app_config.load_app_config(paths)

    assert cfg.get(cfg.themeMode) == Theme.AUTO
    assert os.path.exists(config_path)
    with open(config_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["QFluentWidgets"]["ThemeMode"] == "Auto"


def test_theme_not_forced_when_config_exists(tmp_path):
    """非首启（配置文件已存在）时，load_app_config() 不应覆盖用户已保存的主题选择"""
    paths = AppPaths.under(str(tmp_path))

    app_config.load_app_config(paths)  # 第一次启动：落盘 AUTO
    qconfig.set(cfg.themeMode, Theme.LIGHT)  # 用户手动切换为固定浅色并保存

    app_config.load_app_config(paths)  # 模拟第二次启动

    assert cfg.get(cfg.themeMode) == Theme.LIGHT


def test_load_without_paths_uses_resolved_data_dir(app_paths):
    """不传路径时按 AppPaths.resolve()（测试中即隔离的临时目录）落盘"""
    app_config.load_app_config()
    assert os.path.normcase(str(cfg.file)) == os.path.normcase(app_paths.config_path)
    app_config.set_skipped_version("9.9.9")   # 已接管：落盘到隔离目录
    assert os.path.exists(app_paths.config_path)


# ========== 跳过的版本 ==========

def _write_legacy_prefs(paths, data):
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.prefs_path, 'w', encoding='utf-8') as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def _saved_config(paths):
    with open(paths.config_path, encoding='utf-8') as f:
        return json.load(f)


def test_skipped_version_default_empty():
    assert app_config.get_skipped_version() == ""


def test_set_skipped_version_persists_after_load(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    app_config.load_app_config(paths)
    app_config.set_skipped_version("1.5.0")
    assert app_config.get_skipped_version() == "1.5.0"
    assert _saved_config(paths)["Update"]["SkippedVersion"] == "1.5.0"


def test_set_skipped_version_without_load_is_memory_only(tmp_path, monkeypatch):
    """配置未加载时只改内存值，不在当前目录写默认配置文件"""
    monkeypatch.chdir(tmp_path)
    app_config.set_skipped_version("2.0.0")
    assert app_config.get_skipped_version() == "2.0.0"
    assert os.listdir(tmp_path) == []


def test_legacy_prefs_imported_and_removed(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    _write_legacy_prefs(paths, {"skipped_version": "1.4.9"})

    app_config.load_app_config(paths)

    assert app_config.get_skipped_version() == "1.4.9"
    assert _saved_config(paths)["Update"]["SkippedVersion"] == "1.4.9"
    assert not os.path.exists(paths.prefs_path)


def test_legacy_prefs_do_not_override_existing_value(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    app_config.load_app_config(paths)
    app_config.set_skipped_version("2.0.0")
    _write_legacy_prefs(paths, {"skipped_version": "1.4.9"})

    app_config.load_app_config(paths)

    assert app_config.get_skipped_version() == "2.0.0"
    assert not os.path.exists(paths.prefs_path)


def test_corrupt_legacy_prefs_is_ignored_and_removed(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    _write_legacy_prefs(paths, "{not json")

    app_config.load_app_config(paths)

    assert app_config.get_skipped_version() == ""
    assert not os.path.exists(paths.prefs_path)


def test_no_legacy_prefs_is_noop(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    app_config.load_app_config(paths)
    assert app_config.get_skipped_version() == ""
    assert not os.path.exists(paths.prefs_path)


# ========== 取值范围单一来源 ==========

def test_interval_range_comes_from_validator():
    assert app_config.interval_range() == (1, 3600)
    validator = cfg.default_interval.validator
    assert app_config.interval_range() == (validator.min, validator.max)


def test_retention_options_come_from_validator():
    assert app_config.retention_options() == [0, 7, 30, 90, 180]
    assert config.DATA_RETENTION_DAYS in app_config.retention_options()
    # 返回副本，调用方改动不影响校验器
    app_config.retention_options().append(999)
    assert 999 not in cfg.retention_days.validator.options
