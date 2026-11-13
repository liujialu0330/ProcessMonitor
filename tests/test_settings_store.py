"""配置的 GUI/后台等价、未来字段保留与按需图片绑定边界。"""
import copy
import builtins
import json
import os
import subprocess
import sys

import pytest
from qfluentwidgets import QConfig, Theme, qconfig

import app_config
import settings_schema as schema
import settings_store as store
from app.paths import AppPaths


def _write(paths, data):
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.config_path, 'w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)


def _read(paths):
    with open(paths.config_path, encoding='utf-8') as stream:
        return json.load(stream)


def test_background_config_in_clean_interpreter_never_imports_ui(tmp_path):
    script = '''
import importlib.abc, json, os, sys
class RejectUi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'qfluentwidgets', 'PyQt5', 'ui', 'app_config'}:
            raise AssertionError('后台配置导入了界面依赖: ' + fullname)
sys.meta_path.insert(0, RejectUi())
from settings_store import load_settings
root = sys.argv[1]
settings = load_settings(os.path.join(root, 'config.json'), os.path.join(root, 'old.json'))
assert settings.get('default_interval') == 1
assert settings.snapshot()['QFluentWidgets']['ThemeMode'] == 'Auto'
settings.set('default_interval', 7)
assert load_settings(os.path.join(root, 'config.json'), os.path.join(root, 'old.json')).get('default_interval') == 7
'''
    env = dict(os.environ, PROCESS_MONITOR_DATA_DIR=str(tmp_path / 'data'))
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'config')],
                            env=env, capture_output=True, text=True, encoding='utf-8',
                            timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('content', [
    {'config_version': 3},
    {'Monitor': {'DefaultInterval': 4, 'LastMetrics': ['memory_vms', 'num_handles'],
                 'MetricPresets': {'内存': ['memory_rss']}, 'KeepAwakeWhileMonitoring': False},
     'Data': {'RetentionDays': 30}, 'Automation': {'Enabled': False},
     'Export': {'ReportColorScheme': 'follow', 'NameTemplate': '{scenario}_{date}'},
     'QFluentWidgets': {'ThemeMode': 'Dark'}},
    {'config_version': 8, 'Future': {'x': [1, 2]},
     'Data': {'RetentionDays': 15, 'FutureKey': {'keep': True}},
     'Monitor': {'DefaultInterval': 9999, 'LastMetrics': ['bad', 'memory_pagefile']},
     'QFluentWidgets': {'ThemeMode': 'Auto', 'FutureTheme': 7}},
    {'config_version': 3, 'Monitor': {'DefaultInterval': 'bad',
                                    'LastMetrics': None, 'MetricPresets': []},
     'Guide': {'Started': 'bad'}, 'Window': {'Geometry': [1, 2, -1, 100]},
     'Automation': {'Enabled': False}, 'Export': {'NameTemplate': ''}},
])
def test_gui_and_background_interpret_same_config(tmp_path, content):
    gui_paths = AppPaths.under(str(tmp_path / 'gui'))
    background_paths = AppPaths.under(str(tmp_path / 'background'))
    _write(gui_paths, content)
    _write(background_paths, content)
    # 模拟新解释器的库默认值；根夹具为界面测试把它预设成了 AUTO。
    app_config.cfg.themeMode.value = app_config.cfg.themeMode.defaultValue
    app_config.load_app_config(gui_paths)
    runtime = store.load_settings(background_paths.config_path, background_paths.prefs_path)
    for name in schema.SETTINGS:
        assert app_config.cfg.get(getattr(app_config.cfg, name)) == runtime.get(name), name
    assert app_config.cfg.themeMode.value.value == runtime.snapshot()['QFluentWidgets']['ThemeMode']


def test_first_launch_json_is_equivalent_and_theme_identity_is_preserved(tmp_path):
    gui_paths = AppPaths.under(str(tmp_path / 'gui'))
    background_paths = AppPaths.under(str(tmp_path / 'background'))
    app_config.cfg.themeMode.value = Theme.AUTO
    app_config.load_app_config(gui_paths)
    store.load_settings(background_paths.config_path, background_paths.prefs_path)
    assert _read(gui_paths) == _read(background_paths)
    assert app_config.AppConfig.themeMode is QConfig.themeMode
    assert app_config.AppConfig.themeColor is QConfig.themeColor


def test_gui_save_background_read_then_background_save_gui_read(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    app_config.load_app_config(paths)
    qconfig.set(app_config.cfg.default_interval, 15)
    qconfig.set(app_config.cfg.themeMode, Theme.DARK)
    runtime = store.load_settings(paths.config_path, paths.prefs_path)
    assert runtime.get('default_interval') == 15
    runtime.set('retention_days', 90)
    app_config.load_app_config(paths)
    assert app_config.cfg.retention_days.value == 90
    assert app_config.cfg.themeMode.value is Theme.DARK


def test_future_values_and_gui_fields_survive_background_save(tmp_path):
    paths = AppPaths.under(str(tmp_path))
    raw = {'config_version': 10, 'Future': {'nested': {'a': [1, 2]}},
           'Data': {'RetentionDays': 7, 'NewOption': 900},
           'QFluentWidgets': {'ThemeMode': 'Auto', 'ThemeColor': '#ffabcdef',
                             'FontFamilies': ['自定义字体'], 'FutureTheme': 'untouched'}}
    _write(paths, raw)
    before = open(paths.config_path, 'rb').read()
    settings = store.load_settings(paths.config_path, paths.prefs_path)
    assert open(paths.config_path, 'rb').read() == before
    settings.set('default_interval', 3)
    saved = _read(paths)
    assert saved['config_version'] == 10
    assert saved['Future'] == raw['Future']
    assert saved['QFluentWidgets'] == raw['QFluentWidgets']
    assert saved['Data']['NewOption'] == 900


def test_runtime_values_and_snapshots_are_independent():
    original = {'Monitor': {'LastMetrics': ['memory_private'],
                            'MetricPresets': {'a': ['num_handles']}}}
    before = copy.deepcopy(original)
    first = store.RuntimeSettings(original)
    second = store.RuntimeSettings(original)
    first.get('last_metrics').append('num_handles')
    first.snapshot()['Monitor']['MetricPresets']['a'].append('memory_rss')
    first.set('last_metrics', ['num_threads'], save=False)
    assert second.get('last_metrics') == ['memory_private']
    assert original == before


@pytest.mark.parametrize('content', ['{not json', '[]', '', '\x00\x00'])
def test_corrupt_background_config_is_only_replaced_on_save(tmp_path, content):
    paths = AppPaths.under(str(tmp_path))
    os.makedirs(paths.data_dir, exist_ok=True)
    with open(paths.config_path, 'w', encoding='utf-8') as stream:
        stream.write(content)
    settings = store.load_settings(paths.config_path, paths.prefs_path)
    assert open(paths.config_path, encoding='utf-8').read() == content
    settings.set('default_interval', 8)
    assert _read(paths)['Monitor']['DefaultInterval'] == 8


def test_atomic_save_failure_keeps_file_and_memory_and_cleans_temp(tmp_path, monkeypatch):
    paths = AppPaths.under(str(tmp_path))
    _write(paths, {'config_version': 3, 'Monitor': {'DefaultInterval': 5}})
    before = open(paths.config_path, 'rb').read()
    settings = store.load_settings(paths.config_path, paths.prefs_path)

    def denied(*_):
        raise PermissionError('locked')

    monkeypatch.setattr(store.os, 'replace', denied)
    with pytest.raises(PermissionError):
        settings.set('default_interval', 9)
    assert settings.get('default_interval') == 5
    assert open(paths.config_path, 'rb').read() == before
    assert not list(tmp_path.rglob('*.tmp'))


@pytest.mark.parametrize('gui', [False, True])
def test_legacy_preferences_survive_repeated_save_failure_then_import(tmp_path, monkeypatch, gui):
    paths = AppPaths.under(str(tmp_path))
    _write(paths, {'config_version': 3})
    with open(paths.prefs_path, 'w', encoding='utf-8') as stream:
        json.dump({'skipped_version': '2.0.1'}, stream)
    owner = app_config if gui else store
    original_write = owner.write_json_atomic
    failures = []

    def denied(*_):
        failures.append(1)
        raise PermissionError('locked')

    monkeypatch.setattr(owner, 'write_json_atomic', denied)
    if gui:
        app_config.load_app_config(paths)
        retry = lambda: app_config._import_legacy_update_prefs(paths.prefs_path)
        value = app_config.get_skipped_version
    else:
        runtime = store.load_settings(paths.config_path, paths.prefs_path)
        value = lambda: runtime.get('skipped_version')
        retry = lambda: store.import_legacy_preferences(
            paths.prefs_path, current=value,
            save=lambda version: runtime.set('skipped_version', version))
    assert value() == ''
    retry()
    assert len(failures) == 2
    assert value() == ''
    assert os.path.exists(paths.prefs_path)
    monkeypatch.setattr(owner, 'write_json_atomic', original_write)
    retry()
    assert value() == '2.0.1'
    assert _read(paths)['Update']['SkippedVersion'] == '2.0.1'
    assert not os.path.exists(paths.prefs_path)


def test_report_snapshot_binding_has_no_file_io_and_is_idempotent(tmp_path, monkeypatch):
    snapshot = store.RuntimeSettings({
        'config_version': 7, 'Monitor': {'DefaultInterval': 3},
        'Export': {'ReportColorScheme': 'follow'},
        'QFluentWidgets': {'ThemeMode': 'Auto'}, 'Future': {'keep': 1}}).snapshot()

    def forbidden(*_args, **_kwargs):
        pytest.fail('仅内存报告绑定不能读写配置文件')

    monkeypatch.setattr(app_config, 'prepare_config_file', forbidden)
    monkeypatch.setattr(app_config, 'write_json_atomic', forbidden)
    monkeypatch.setattr(qconfig, 'load', forbidden)
    monkeypatch.setattr(qconfig, 'save', forbidden)
    events = []
    qconfig.themeChanged.connect(events.append)
    path = str(tmp_path / 'not-created' / 'config.json')
    try:
        app_config.apply_runtime_snapshot(snapshot, path)
        assert app_config.cfg.themeMode.value is Theme.AUTO
        assert app_config.cfg.report_color_scheme.value == 'follow'
        assert app_config.cfg.default_interval.value == 3
        assert app_config.cfg.toDict()['Future'] == {'keep': 1}
        app_config.cfg.themeChanged.emit(Theme.DARK)
        count = len(events)
        assert count >= 1
        events.clear()
        app_config.apply_runtime_snapshot(snapshot, path)
        app_config.apply_runtime_snapshot(snapshot, path)
        app_config.cfg.themeChanged.emit(Theme.DARK)
        assert len(events) == count
        assert not os.path.exists(path)
    finally:
        qconfig.themeChanged.disconnect(events.append)


@pytest.mark.parametrize('gui', [False, True])
def test_legacy_preferences_read_error_is_retryable_without_deleting_source(tmp_path, monkeypatch, gui):
    paths = AppPaths.under(str(tmp_path))
    _write(paths, {'config_version': 3})
    with open(paths.prefs_path, 'w', encoding='utf-8') as stream:
        json.dump({'skipped_version': '2.0.1'}, stream)
    before = open(paths.prefs_path, 'rb').read()
    real_open = builtins.open
    def denied(path, *args, **kwargs):
        if os.fspath(path) == paths.prefs_path:
            raise PermissionError('旧偏好临时无法读取')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', denied)
    for _ in range(2):
        if gui:
            app_config.load_app_config(paths)
            assert app_config.get_skipped_version() == ''
        else:
            runtime = store.load_settings(paths.config_path, paths.prefs_path)
            assert runtime.get('skipped_version') == ''
        assert os.path.exists(paths.prefs_path)
    monkeypatch.setattr(builtins, 'open', real_open)
    assert open(paths.prefs_path, 'rb').read() == before
    if gui:
        app_config.load_app_config(paths)
        assert app_config.get_skipped_version() == '2.0.1'
    else:
        runtime = store.load_settings(paths.config_path, paths.prefs_path)
        assert runtime.get('skipped_version') == '2.0.1'
    assert _read(paths)['Update']['SkippedVersion'] == '2.0.1'
    assert not os.path.exists(paths.prefs_path)
