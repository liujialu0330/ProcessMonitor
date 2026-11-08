"""
配置文件版本化：migrate_config 表驱动迁移、load_app_config 的迁移回写、
版本高于当前时不改写文件、损坏 JSON 时保持原有行为
"""
import json
import logging
import os

import pytest

import app_config
from app.paths import AppPaths
from app_config import (
    CONFIG_MIGRATIONS, CONFIG_VERSION_KEY, CURRENT_CONFIG_VERSION, cfg, config_version_of,
    migrate_config,
)


@pytest.fixture(autouse=True)
def _reset_config_version():
    """conftest 不复位版本号与"新版原始内容"，这里自行复位，避免泄漏给其他用例"""
    yield
    cfg.config_version.value = CURRENT_CONFIG_VERSION
    cfg._newer_raw = {}


def _write(paths, data):
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.config_path, 'w', encoding='utf-8') as f:
        f.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))


def _read_bytes(paths):
    with open(paths.config_path, 'rb') as f:
        return f.read()


def _saved(paths):
    with open(paths.config_path, encoding='utf-8') as f:
        return json.load(f)


# ========== migrate_config（纯函数） ==========

def test_current_version_is_3():
    assert CURRENT_CONFIG_VERSION == 3


def test_migration_table_covers_every_version_below_current():
    assert sorted(CONFIG_MIGRATIONS) == list(range(1, CURRENT_CONFIG_VERSION))


@pytest.mark.parametrize("data, expected", [
    ({}, 1),
    ({"Data": {"RetentionDays": 30}}, 1),
    ({CONFIG_VERSION_KEY: 1}, 1),
    ({CONFIG_VERSION_KEY: 2}, 2),
    ({CONFIG_VERSION_KEY: 7}, 7),
    ({CONFIG_VERSION_KEY: "2"}, 1),
    ({CONFIG_VERSION_KEY: 0}, 1),
    ({CONFIG_VERSION_KEY: -3}, 1),
    ({CONFIG_VERSION_KEY: True}, 1),
    ({CONFIG_VERSION_KEY: 2.0}, 1),
    ({CONFIG_VERSION_KEY: None}, 1),
])
def test_config_version_of(data, expected):
    assert config_version_of(data) == expected


V1 = {
    "Data": {"RetentionDays": 30},
    "Monitor": {"LastMetrics": ["memory_vms", "num_ctx_switches_involuntary", "handles"]},
    "QFluentWidgets": {"ThemeMode": "Dark"},
}


@pytest.mark.parametrize("data, expected, steps", [
    # 没有版本键的旧文件：1 → 2 → 3，只写版本号，其余内容原样
    (V1, dict(V1, **{CONFIG_VERSION_KEY: 3}), ["1 → 2", "2 → 3"]),
    ({CONFIG_VERSION_KEY: 1, "X": {"y": 1}}, {CONFIG_VERSION_KEY: 3, "X": {"y": 1}},
     ["1 → 2", "2 → 3"]),
    # 2 → 3（新增窗口状态与通知开关，缺键即缺省）：只写版本号
    ({CONFIG_VERSION_KEY: 2, "X": {"y": 1}}, {CONFIG_VERSION_KEY: 3, "X": {"y": 1}}, ["2 → 3"]),
    # 非法版本号按 1 处理
    ({CONFIG_VERSION_KEY: "abc"}, {CONFIG_VERSION_KEY: 3}, ["1 → 2", "2 → 3"]),
    # 已是当前版本：不迁移
    ({CONFIG_VERSION_KEY: 3, "X": 1}, {CONFIG_VERSION_KEY: 3, "X": 1}, []),
    # 高于当前版本：原样返回，不迁移
    ({CONFIG_VERSION_KEY: 9, "New": {"k": 1}}, {CONFIG_VERSION_KEY: 9, "New": {"k": 1}}, []),
])
def test_migrate_config_table(data, expected, steps):
    before = json.dumps(data, sort_keys=True)
    result, done = migrate_config(data)
    assert result == expected
    assert done == steps
    # 纯函数：不修改入参
    assert json.dumps(data, sort_keys=True) == before


def test_v2_migration_leaves_metric_keys_to_normalize_metrics():
    """v2 不改写指标键：已废弃键与旧别名由 normalize_metrics 在加载时处理"""
    result, _ = migrate_config(V1)
    assert result["Monitor"]["LastMetrics"] == V1["Monitor"]["LastMetrics"]
    normalized = app_config.normalize_metrics(V1["Monitor"]["LastMetrics"])
    assert "num_ctx_switches_involuntary" not in normalized
    assert "memory_vms" not in normalized and "memory_private" in normalized


def test_migrations_run_in_order(monkeypatch):
    """框架按源版本逐步执行，最后统一写当前版本号"""
    calls = []

    def step(n):
        def run(data):
            calls.append(n)
            out = dict(data)
            out.setdefault("trail", []).append(n)
            return out
        return run

    monkeypatch.setattr(app_config, "CURRENT_CONFIG_VERSION", 4)
    monkeypatch.setattr(app_config, "CONFIG_MIGRATIONS", {1: step(1), 2: step(2), 3: step(3)})
    result, steps = migrate_config({CONFIG_VERSION_KEY: 2})
    assert calls == [2, 3]
    assert steps == ["2 → 3", "3 → 4"]
    assert result == {CONFIG_VERSION_KEY: 4, "trail": [2, 3]}


# ========== load_app_config ==========

def test_load_migrates_v1_file_and_writes_back(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    _write(paths, V1)

    app_config.load_app_config(paths)

    saved = _saved(paths)
    assert saved[CONFIG_VERSION_KEY] == CURRENT_CONFIG_VERSION
    assert {k: v for k, v in saved.items() if k != CONFIG_VERSION_KEY} == V1
    assert cfg.get(cfg.retention_days) == 30
    assert cfg.get(cfg.config_version) == CURRENT_CONFIG_VERSION
    metrics = cfg.get(cfg.last_metrics)
    assert "memory_private" in metrics and "num_ctx_switches_involuntary" not in metrics
    assert not os.path.exists(paths.config_path + ".tmp")


def test_load_current_version_does_not_rewrite(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    _write(paths, '{"config_version": 3, "Data": {"RetentionDays": 7}}')
    before = _read_bytes(paths)

    app_config.load_app_config(paths)

    assert _read_bytes(paths) == before
    assert cfg.get(cfg.retention_days) == 7


def test_load_newer_version_keeps_file_and_warns(tmp_path, caplog):
    paths = AppPaths.under(str(tmp_path))
    newer = {CONFIG_VERSION_KEY: 4, "Data": {"RetentionDays": 30, "Future": 1},
             "FutureGroup": {"A": [1, 2]}}
    _write(paths, newer)
    before = _read_bytes(paths)

    with caplog.at_level(logging.WARNING, logger="app_config"):
        app_config.load_app_config(paths)

    assert _read_bytes(paths) == before
    assert any("高于当前" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    # 能识别的键照常读取
    assert cfg.get(cfg.retention_days) == 30
    assert cfg.get(cfg.config_version) == 4


def test_saving_under_newer_version_keeps_unknown_keys(tmp_path):
    """回退到旧版后改一项设置：新版写下的未知键与版本号不被抹掉"""
    paths = AppPaths.under(str(tmp_path))
    _write(paths, {CONFIG_VERSION_KEY: 4, "Data": {"RetentionDays": 30, "Future": 1},
                   "FutureGroup": {"A": [1, 2]}})
    app_config.load_app_config(paths)

    app_config.set_skipped_version("2.0.1")

    saved = _saved(paths)
    assert saved[CONFIG_VERSION_KEY] == 4
    assert saved["FutureGroup"] == {"A": [1, 2]}
    assert saved["Data"] == {"RetentionDays": 30, "Future": 1}
    assert saved["Update"]["SkippedVersion"] == "2.0.1"


def test_newer_state_does_not_leak_into_next_load(tmp_path):
    newer = AppPaths.under(str(tmp_path / "newer"))
    _write(newer, {CONFIG_VERSION_KEY: 4, "FutureGroup": {"A": 1}})
    app_config.load_app_config(newer)

    fresh = AppPaths.under(str(tmp_path / "fresh"))
    app_config.load_app_config(fresh)
    app_config.set_skipped_version("1.0.0")

    saved = _saved(fresh)
    assert saved[CONFIG_VERSION_KEY] == CURRENT_CONFIG_VERSION
    assert "FutureGroup" not in saved


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", ""])
def test_corrupt_or_non_object_file_behaves_as_before(tmp_path, content):
    """损坏或不是对象的 JSON：不迁移、不改写，按默认配置加载（与原有行为一致）"""
    paths = AppPaths.under(str(tmp_path))
    _write(paths, content)
    before = _read_bytes(paths)

    app_config.load_app_config(paths)

    assert _read_bytes(paths) == before
    assert cfg.get(cfg.retention_days) == cfg.retention_days.defaultValue
    assert cfg.get(cfg.config_version) == CURRENT_CONFIG_VERSION


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", "", "\x00\x00\x00"])
def test_corrupt_file_is_replaced_by_a_valid_one_on_first_save(tmp_path, content):
    """损坏的配置文件：加载按默认值不抛异常，用户改一项设置后写回的是合法的当前版本文件，
    不再留着坏文件让每次启动都读失败"""
    paths = AppPaths.under(str(tmp_path))
    _write(paths, content)

    app_config.load_app_config(paths)
    assert cfg.get(cfg.retention_days) == cfg.retention_days.defaultValue
    app_config.set_skipped_version("1.0.0")

    saved = _saved(paths)
    assert saved[CONFIG_VERSION_KEY] == CURRENT_CONFIG_VERSION
    assert saved["Update"]["SkippedVersion"] == "1.0.0"


def test_first_launch_saves_current_version(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    cfg.config_version.value = 1   # 即使内存里残留旧值，新建的配置文件也写当前版本

    app_config.load_app_config(paths)
    app_config.set_skipped_version("1.0.0")

    assert _saved(paths)[CONFIG_VERSION_KEY] == CURRENT_CONFIG_VERSION


def test_write_back_failure_still_loads(tmp_path, monkeypatch):
    """迁移结果写不回去：本次照常读取旧文件，不留临时文件"""
    paths = AppPaths.under(str(tmp_path))
    _write(paths, V1)
    before = _read_bytes(paths)

    def fail_replace(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(app_config.os, "replace", fail_replace)
    app_config.load_app_config(paths)

    assert _read_bytes(paths) == before
    assert cfg.get(cfg.retention_days) == 30
    assert not os.path.exists(paths.config_path + ".tmp")
