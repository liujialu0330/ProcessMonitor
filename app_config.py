"""GUI 配置适配层：保留 Fluent 设置卡片的 ConfigItem 和主题信号契约。

字段、校验与迁移定义来自 settings_schema；无窗口入口使用 settings_store，
不导入本模块。themeMode 沿用 QConfig 基类的同一对象，不得重新声明。
"""
import copy
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

from PyQt5.QtCore import Qt
from qfluentwidgets import (
    QConfig, ConfigItem, OptionsConfigItem, RangeConfigItem, qconfig, Theme, setTheme,
)

import config
import settings_schema as _schema
from settings_schema import (
    CONFIG_MIGRATIONS, CONFIG_VERSION_KEY, CURRENT_CONFIG_VERSION, LEGACY_CONFIG_VERSION,
    DEFAULT_MARKER_HOTKEY, MARKER_HOTKEY_MAX_LENGTH, DEFAULT_EXPORT_NAME_TEMPLATE,
    EXPORT_NAME_TEMPLATE_MAX_LENGTH, WINDOW_PAGE_KEYS, SHORT_TEXT_MAX_LENGTH,
    REPORT_SCHEME_LIGHT, REPORT_SCHEME_FOLLOW, REPORT_COLOR_SCHEMES,
    ConfigValidator, OptionsValidator, RangeValidator, BoolValidator,
    MetricListValidator, MetricPresetsValidator, DirectoryTextValidator,
    MarkerHotkeyValidator, ExportNameTemplateValidator, WindowRectValidator,
    ShortTextValidator, normalize_metrics, config_version_of,
)
from settings_store import prepare_config_file, write_json_atomic, import_legacy_preferences

if TYPE_CHECKING:
    from app.paths import AppPaths

logger = logging.getLogger(__name__)


def migrate_config(data: dict):
    """兼容既有入口；纯迁移算法与后台共用，迁移表仍可独立注入测试。"""
    return _schema.migrate_config(data, current_version=CURRENT_CONFIG_VERSION,
                                  migrations=CONFIG_MIGRATIONS)


class AppConfig(QConfig):
    """GUI 对象身份不变；共享定义只负责构造普通配置项。"""

    def __init__(self):
        super().__init__()
        self._newer_raw = {}

    def toDict(self, serialize=True):
        items = super().toDict(serialize)
        if serialize and self._newer_raw:
            return _schema.merge_serialized(self._newer_raw, items)
        return items


def _config_item(spec):
    item_class = {'value': ConfigItem, 'options': OptionsConfigItem,
                  'range': RangeConfigItem}[spec.kind]
    return item_class(spec.group, spec.key, copy.deepcopy(spec.default), spec.validator)


# QConfig 按类属性扫描 ConfigItem；不能换成仅存在于实例上的动态代理。
for _name, _spec in _schema.SETTINGS.items():
    setattr(AppConfig, _name, _config_item(_spec))

cfg = AppConfig()


def __getattr__(name):
    if name == 'CONFIG_PATH':
        from app.paths import AppPaths
        return AppPaths.resolve().config_path
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_loaded() -> bool:
    return getattr(qconfig, '_cfg', None) is cfg


def load_app_config(paths: Optional["AppPaths"] = None) -> None:
    """GUI 入口：共用文件迁移，再保留原 QConfig 绑定与信号语义。"""
    if paths is None:
        from app.paths import AppPaths
        paths = AppPaths.resolve()
    cfg.config_version.value = CURRENT_CONFIG_VERSION
    raw, first_launch = prepare_config_file(
        paths.config_path, log=logger, migrate=migrate_config,
        current_version=CURRENT_CONFIG_VERSION)
    cfg._newer_raw = raw if config_version_of(raw) > CURRENT_CONFIG_VERSION else {}
    qconfig.load(paths.config_path, cfg)
    if first_launch:
        # 即使当前内存值已为 AUTO，首启也必须创建配置；不能依赖 set() 的值变化分支。
        setTheme(Theme.AUTO, save=False)
        write_json_atomic(paths.config_path, cfg.toDict())
    _import_legacy_update_prefs(paths.prefs_path)


def _migrate_config_file(config_path: str) -> None:
    """兼容旧入口；文件准备只有一份实现。"""
    raw, _ = prepare_config_file(config_path, log=logger, migrate=migrate_config,
                                 current_version=CURRENT_CONFIG_VERSION)
    cfg._newer_raw = raw if config_version_of(raw) > CURRENT_CONFIG_VERSION else {}


def _import_legacy_update_prefs(prefs_path: str) -> None:
    def save_imported(value):
        if not _is_loaded():
            raise ValueError('应用配置尚未加载，不能导入旧偏好')
        old = copy.deepcopy(cfg.get(cfg.skipped_version))
        qconfig.set(cfg.skipped_version, value, save=False)
        try:
            write_json_atomic(str(cfg.file), cfg.toDict())
        except (OSError, TypeError, ValueError):
            cfg.skipped_version.value = old
            raise

    import_legacy_preferences(prefs_path, current=get_skipped_version,
                              save=save_imported, log=logger)


def apply_runtime_snapshot(snapshot: dict, config_path: str) -> None:
    """首次后台图片渲染时内存绑定配置；不重新读盘、迁移、保存或改变用户主题偏好。

    可重复调用：ConfigItem 仅在值变化时发信号，主题转发使用唯一连接，避免多次导出
    累积回调。普通监控/CSV 路径不需要导入本模块。
    """
    if not isinstance(snapshot, dict):
        raise TypeError('配置快照必须是字典')
    qconfig._cfg = cfg
    cfg.file = Path(config_path)
    cfg._newer_raw = (copy.deepcopy(snapshot)
                      if config_version_of(snapshot) > CURRENT_CONFIG_VERSION else {})
    for name, value in _schema.read_values(snapshot).items():
        getattr(cfg, name).value = value
    theme = snapshot.get('QFluentWidgets')
    theme = theme if isinstance(theme, dict) else {}
    for name, key in (('themeMode', 'ThemeMode'), ('themeColor', 'ThemeColor'),
                      ('fontFamilies', 'FontFamilies')):
        item = getattr(cfg, name)
        try:
            item.deserializeFrom(copy.deepcopy(theme.get(key, _schema.GUI_DEFAULTS[key])))
        except (TypeError, ValueError):
            item.value = copy.deepcopy(item.defaultValue)
    try:
        cfg.themeChanged.connect(qconfig.themeChanged, Qt.UniqueConnection)
    except TypeError:
        pass  # 已由 GUI 配置加载或前一次报告绑定。
    qconfig.theme = cfg.get(cfg.themeMode)


def get_skipped_version() -> str:
    """用户选择跳过的版本号（空串表示未跳过）"""
    return str(cfg.get(cfg.skipped_version) or '')


def set_skipped_version(version: str) -> None:
    """记录用户选择跳过的版本号。

    与 remember_last_metrics 相同：配置未被 load_app_config() 接管时只改内存值。
    """
    qconfig.set(cfg.skipped_version, str(version or ''), save=_is_loaded())


def get_marker_hotkey() -> str:
    """全局打点快捷键（空串表示不使用）"""
    return str(cfg.get(cfg.marker_hotkey) or '')


def set_marker_hotkey(text: str) -> None:
    """记录全局打点快捷键；与 set_skipped_version 相同，配置未接管时只改内存值。"""
    qconfig.set(cfg.marker_hotkey, str(text or '').strip(), save=_is_loaded())


def get_export_name_template() -> str:
    """导出文件命名模板（始终非空）"""
    value = cfg.get(cfg.export_name_template)
    return value if isinstance(value, str) and value.strip() else DEFAULT_EXPORT_NAME_TEMPLATE


def set_export_name_template(text: str) -> None:
    """记录导出命名模板；与 set_marker_hotkey 相同，配置未接管时只改内存值。"""
    qconfig.set(cfg.export_name_template, str(text or '').strip(), save=_is_loaded())


def update_values(pairs: Iterable[Tuple[ConfigItem, object]]) -> bool:
    """批量写配置项：只写真的变了的项，且整批只落盘一次（窗口状态一次要写好几项）。

    与 set_skipped_version 相同，配置未被 load_app_config() 接管时只改内存值。
    返回是否有项目发生变化。
    """
    changed = False
    for item, value in pairs:
        if cfg.get(item) != value:
            qconfig.set(item, value, save=False)
            changed = True
    if changed and _is_loaded():
        qconfig.save()
    return changed


def interval_range() -> Tuple[int, int]:
    """采集周期（秒）的合法范围 (最小, 最大)：页面输入框的范围从这里取，不另写一份"""
    validator = AppConfig.default_interval.validator
    return validator.min, validator.max


def retention_options() -> List[int]:
    """历史数据保留天数的可选值（0 = 永久保留）：设置页的选项从这里取，不另写一份"""
    return list(AppConfig.retention_days.validator.options)


def remember_last_metrics(metrics: Iterable[str]) -> None:
    """记住监控页的指标选择。

    只在应用配置已由 load_app_config() 接管时才落盘：配置尚未加载（单元测试、
    独立脚本）时只更新内存值，避免 qconfig 把文件写到当前目录下的默认路径。
    """
    qconfig.set(cfg.last_metrics, normalize_metrics(list(metrics)), save=_is_loaded())
