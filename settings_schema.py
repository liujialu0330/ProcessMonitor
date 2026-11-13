"""界面与后台共用的配置定义、校验和迁移；不导入 Qt 或界面库。"""
import copy
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from utils.metrics import LEAK_CHECK_PRESET, canonical_metric_type, all_metric_types


class ConfigValidator:
    def validate(self, value):
        return True

    def correct(self, value):
        return value


class OptionsValidator(ConfigValidator):
    def __init__(self, options):
        self.options = list(options)
        if not self.options:
            raise ValueError('配置选项不能为空')

    def validate(self, value):
        return value in self.options

    def correct(self, value):
        return value if self.validate(value) else self.options[0]


class BoolValidator(OptionsValidator):
    def __init__(self):
        # 保留既有配置校验语义，不用 bool('false') 之类的字符串强制转换。
        super().__init__([True, False])


class RangeValidator(ConfigValidator):
    def __init__(self, minimum, maximum):
        self.min, self.max = minimum, maximum
        self.range = (minimum, maximum)

    def validate(self, value):
        return (isinstance(value, (int, float))
                and (not isinstance(value, float) or math.isfinite(value))
                and self.min <= value <= self.max)

    def correct(self, value):
        # 手改 JSON 的字符串/空值不能打断其余配置项的加载。
        if (not isinstance(value, (int, float))
                or isinstance(value, float) and not math.isfinite(value)):
            return self.min
        return min(max(self.min, value), self.max)


@dataclass(frozen=True)
class SettingSpec:
    group: str
    key: str
    default: Any
    validator: Any
    kind: str = 'value'

    def correct(self, value):
        return self.validator.correct(copy.deepcopy(value))


def value_field(group, key, default, validator=None):
    return SettingSpec(group, key, default, validator or ConfigValidator())


def option_field(group, key, default, validator):
    return SettingSpec(group, key, default, validator, 'options')


def range_field(group, key, default, validator):
    return SettingSpec(group, key, default, validator, 'range')


def normalize_metrics(value) -> List[str]:
    """把配置里读出的指标列表归一：非列表/未知键丢弃，旧键经
    canonical_metric_type 归一，去重保序；结果为空则回落到"泄漏排查"预设。"""
    known = set(all_metric_types())
    result: List[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, str):
                continue
            key = canonical_metric_type(item)
            if key in known and key not in result:
                result.append(key)
    return result or list(LEAK_CHECK_PRESET)


class MetricListValidator(ConfigValidator):
    """监控指标列表校验器：加载与赋值时都按 normalize_metrics 纠正，
    配置文件被手改或指标键随版本变化时不会让监控页拿到非法指标。"""

    def validate(self, value) -> bool:
        return value == normalize_metrics(value)

    def correct(self, value) -> List[str]:
        return normalize_metrics(value)


class MetricPresetsValidator(ConfigValidator):
    def correct(self, value):
        if not isinstance(value, dict):
            return {}
        return {name.strip()[:40]: normalize_metrics(metrics)
                for name, metrics in list(value.items())[:30]
                if isinstance(name, str) and name.strip() and isinstance(metrics, list)}

    def validate(self, value):
        return value == self.correct(value)


class DirectoryTextValidator(ConfigValidator):
    def validate(self, value):
        return isinstance(value, str) and len(value) <= 32767 and '\x00' not in value

    def correct(self, value):
        return value.strip() if self.validate(value) else ''


# 默认打点快捷键（与 ui/hotkey.DEFAULT_MARKER_HOTKEY 一致；本模块不依赖界面层）
DEFAULT_MARKER_HOTKEY = "Ctrl+Alt+M"
MARKER_HOTKEY_MAX_LENGTH = 40


class MarkerHotkeyValidator(ConfigValidator):
    """打点快捷键校验器：非字符串或过长时回落到默认值（写法由界面层再判断）。"""

    def validate(self, value) -> bool:
        return isinstance(value, str) and len(value) <= MARKER_HOTKEY_MAX_LENGTH

    def correct(self, value) -> str:
        return value.strip() if self.validate(value) else DEFAULT_MARKER_HOTKEY


# 默认导出命名模板（与 core.naming_template.DEFAULT_TEMPLATE 一致，由测试守护；本模块不依赖业务层）
DEFAULT_EXPORT_NAME_TEMPLATE = "{process}_{build}_{note}_{date}"
EXPORT_NAME_TEMPLATE_MAX_LENGTH = 200


class ExportNameTemplateValidator(ConfigValidator):
    """导出命名模板校验器：必须是非空字符串、不超过上限；否则回落到默认模板。"""

    def validate(self, value) -> bool:
        return (isinstance(value, str) and bool(value.strip())
                and len(value) <= EXPORT_NAME_TEMPLATE_MAX_LENGTH)

    def correct(self, value) -> str:
        return value if self.validate(value) else DEFAULT_EXPORT_NAME_TEMPLATE


WINDOW_PAGE_KEYS = ["monitor", "analysis", "setting"]
SHORT_TEXT_MAX_LENGTH = 80


class WindowRectValidator(ConfigValidator):
    """窗口几何（x, y, 宽, 高）校验器：空列表表示没有记录；其余必须是 4 个整数且宽高为正，
    配置文件被手改成别的形状时按"没有记录"处理。"""

    def validate(self, value) -> bool:
        if value == []:
            return True
        return (isinstance(value, list) and len(value) == 4
                and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
                and value[2] > 0 and value[3] > 0)

    def correct(self, value) -> list:
        return list(value) if self.validate(value) else []


class ShortTextValidator(ConfigValidator):
    """短文本校验器（任务 ID、范围键等标识）：非字符串或过长按空串处理。"""

    def validate(self, value) -> bool:
        return isinstance(value, str) and len(value) <= SHORT_TEXT_MAX_LENGTH

    def correct(self, value) -> str:
        return value if self.validate(value) else ""


# ========== 配置文件版本 ==========

# config.json 根上的版本号键；没有该键的旧文件视为版本 1
CONFIG_VERSION_KEY = "config_version"
LEGACY_CONFIG_VERSION = 1
CURRENT_CONFIG_VERSION = 3


def _migrate_v1_to_v2(data: dict) -> dict:
    """1 → 2：内容无变更，只写入版本号。

    v2 原计划剔除 Monitor.LastMetrics 里已废弃的 num_ctx_switches_involuntary、
    把 memory_vms / memory_pagefile 映射为 memory_private；这两件事 normalize_metrics
    在每次加载时（MetricListValidator）已经做了，文件里的旧键不影响读取，下次保存时
    自然写成新键，因此这里不重复改写。旧版 update_prefs.json 的导入仍由
    _import_legacy_update_prefs 负责，与版本号无关。
    """
    return dict(data)


def _migrate_v2_to_v3(data: dict) -> dict:
    """2 → 3：内容无变更，只写入版本号。

    v3 新增窗口与工作状态记忆（Window / Analysis 两节）和通知开关（Notification 节）；
    缺少这些键时读到的就是缺省值（没有窗口记录 = 居中的默认大小与监控页，通知开关默认开），
    旧文件不需要改写。
    """
    return dict(data)


# 表驱动迁移：键为源版本，函数把该版本的配置字典变换为"源版本 + 1"（不必写版本号，
# migrate_config 统一写）。新增版本时在这里加一项并调大 CURRENT_CONFIG_VERSION
CONFIG_MIGRATIONS: Dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
}


def config_version_of(data: dict) -> int:
    """配置字典的版本号：缺失或不是正整数时按旧版（1）处理"""
    value = data.get(CONFIG_VERSION_KEY, LEGACY_CONFIG_VERSION)
    if isinstance(value, bool) or not isinstance(value, int) or value < LEGACY_CONFIG_VERSION:
        return LEGACY_CONFIG_VERSION
    return value


def migrate_config(data: dict, *, current_version: Optional[int] = None,
                   migrations: Optional[Dict[int, Callable[[dict], dict]]] = None
                   ) -> Tuple[dict, List[str]]:
    """
    把配置字典逐版本迁移到 CURRENT_CONFIG_VERSION（纯函数，不修改入参）。

    Returns:
        (迁移后的字典, 执行过的步骤说明)。步骤为空表示无需迁移（已是当前版本，或版本
        高于当前——那种情况原样返回，由调用方决定如何处理）。
    """
    current_version = CURRENT_CONFIG_VERSION if current_version is None else current_version
    migrations = CONFIG_MIGRATIONS if migrations is None else migrations
    version = config_version_of(data)
    if version >= current_version:
        return data, []
    result = copy.deepcopy(data)
    steps: List[str] = []
    while version < current_version:
        result = migrations[version](result)
        steps.append(f"{version} → {version + 1}")
        version += 1
    result[CONFIG_VERSION_KEY] = current_version
    return result, steps


# 报告图片配色：固定浅色（默认，与界面主题无关）/ 跟随当前界面主题
REPORT_SCHEME_LIGHT = "light"
REPORT_SCHEME_FOLLOW = "follow"
REPORT_COLOR_SCHEMES = [REPORT_SCHEME_LIGHT, REPORT_SCHEME_FOLLOW]


class SettingsDefinition:
    # 配置文件版本号（根级键 config_version）：由 load_app_config 维护，不是用户偏好；
    # 声明成 ConfigItem 是为了 qconfig 保存时把它一并写回
    config_version = value_field(CONFIG_VERSION_KEY, "", CURRENT_CONFIG_VERSION)
    monitor_view = option_field('Monitor', 'View', 'cards', OptionsValidator(['cards', 'list']))
    metric_presets = value_field('Monitor', 'MetricPresets', {}, MetricPresetsValidator())
    default_metric_preset = value_field('Monitor', 'DefaultMetricPreset', '', ShortTextValidator())
    guide_started = value_field('Guide', 'Started', False, BoolValidator())
    guide_baseline = value_field('Guide', 'Baseline', False, BoolValidator())
    default_export_dir = value_field('Export', 'DefaultDirectory', '', DirectoryTextValidator())

    # 默认采集周期（秒）：新建监控任务时采集周期输入框的初始值；用户仍可在
    # 监控页为单次任务临时改动，不影响本配置项
    default_interval = range_field(
        "Monitor", "DefaultInterval",
        int(config.DEFAULT_INTERVAL), RangeValidator(1, 3600))

    # 历史数据保留天数：0 = 永久保留（不自动清理），>0 时启动清理已停止且超期的
    # 任务数据（语义与 config.DATA_RETENTION_DAYS 一致，见该常量注释）
    retention_days = option_field(
        "Data", "RetentionDays",
        config.DATA_RETENTION_DAYS, OptionsValidator([0, 7, 30, 90, 180]))

    # 关闭窗口时是否最小化到系统托盘：设置页开关控制，默认关闭（直接退出）
    close_to_tray = value_field("Behavior", "CloseToTray", False, BoolValidator())

    # 有运行中任务时阻止系统因空闲进入睡眠（core.power_guard）：设置页开关控制，默认开启
    keep_awake_while_monitoring = value_field(
        "Monitor", "KeepAwakeWhileMonitoring", True, BoolValidator())

    # 允许命令行与脚本控制（pmctl / 本地 IPC）：关闭后除 ping / activate 外的命令一律回
    # disabled；设置页开关控制，默认开启
    automation_enabled = value_field("Automation", "Enabled", True, BoolValidator())

    # 监控页上次选择的指标：新建任务时沿用，默认"泄漏排查"预设
    last_metrics = value_field(
        "Monitor", "LastMetrics", list(LEAK_CHECK_PRESET), MetricListValidator())

    # 用户在更新提示里选择"跳过"的版本号（空串 = 未跳过任何版本）；
    # 旧版单独存于 update_prefs.json，load_app_config 首次运行时导入并删除旧文件
    skipped_version = value_field("Update", "SkippedVersion", "")

    # 全局打点快捷键（任何程序里按下即为运行中的任务打点，不弹窗）；空串 = 不使用。
    # 写法是否合法、能否注册由 ui/hotkey 判断，这里只保证是不太长的字符串
    marker_hotkey = value_field(
        "Behavior", "MarkerHotkey", DEFAULT_MARKER_HOTKEY, MarkerHotkeyValidator())

    # 报告图片配色：报告常被贴进文档与邮件，默认固定浅色；设置页可改为跟随界面主题
    report_color_scheme = option_field(
        "Export", "ReportColorScheme", REPORT_SCHEME_LIGHT,
        OptionsValidator(REPORT_COLOR_SCHEMES))

    # 导出文件命名模板（core.naming_template 的语法）：报告、明细 CSV、证据包的默认文件名；
    # 清空或写成非字符串时回落到默认模板
    export_name_template = value_field(
        "Export", "NameTemplate", DEFAULT_EXPORT_NAME_TEMPLATE, ExportNameTemplateValidator())

    # 窗口不在前台（最小化、隐藏到托盘、被别的窗口盖住）时，是否用托盘气泡（系统通知）提醒
    notify_in_background = value_field(
        "Notification", "BackgroundBubble", True, BoolValidator())

    # 窗口与工作状态记忆（正常退出时与几何变化后防抖写入，见 ui/window_state）：
    # 窗口几何 [x, y, 宽, 高]（最大化时为还原后的位置与大小）、是否最大化、当前页
    window_geometry = value_field("Window", "Geometry", [], WindowRectValidator())
    window_maximized = value_field("Window", "Maximized", False, BoolValidator())
    window_page = option_field(
        "Window", "Page", WINDOW_PAGE_KEYS[0], OptionsValidator(WINDOW_PAGE_KEYS))
    # 分析页上次选中的任务、时间范围、视图与时间模式（空串 = 没有记录）；键是否仍有效由
    # 分析页读取时判断，这里只保证是不太长的字符串
    analysis_task = value_field("Analysis", "LastTask", "", ShortTextValidator())
    analysis_range = value_field("Analysis", "Range", "", ShortTextValidator())
    analysis_view_mode = value_field("Analysis", "ViewMode", "", ShortTextValidator())
    analysis_time_mode = value_field("Analysis", "TimeMode", "", ShortTextValidator())
    # 摘要表用户勾选的列（逗号分隔的列键；空串 = 没有勾选过，列随窗口宽度的档位走）
    analysis_summary_columns = value_field("Analysis", "SummaryColumns", "", ShortTextValidator())



SETTINGS = {name: item for name, item in vars(SettingsDefinition).items()
            if isinstance(item, SettingSpec)}

# 这些值保持 JSON 形式，后台不需要创建 Theme/QColor 对象。
GUI_DEFAULTS = {
    'ThemeMode': 'Light',
    'ThemeColor': '#ff009faa',
    'FontFamilies': ['Segoe UI', 'Microsoft YaHei', 'PingFang SC'],
}


def interval_range() -> Tuple[int, int]:
    validator = SETTINGS['default_interval'].validator
    return validator.min, validator.max


def retention_options() -> List[int]:
    return list(SETTINGS['retention_days'].validator.options)


def read_values(data: dict) -> dict:
    result = {}
    for name, item in SETTINGS.items():
        if item.key:
            section = data.get(item.group, {})
            raw = (section.get(item.key, item.default)
                   if isinstance(section, dict) else item.default)
        else:
            raw = data.get(item.group, item.default)
        result[name] = item.correct(raw)
    return result


def merge_serialized(raw: dict, updates: dict) -> dict:
    """只合并受当前版本管理的键，保留未知组与已知组内的未来字段。"""
    result = copy.deepcopy(raw)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(copy.deepcopy(value))
        else:
            result[key] = copy.deepcopy(value)
    return result


def serialize_values(values: dict, raw: Optional[dict] = None) -> dict:
    result = copy.deepcopy(raw or {})
    for name, item in SETTINGS.items():
        value = copy.deepcopy(values[name])
        if not item.key:
            result[item.group] = value
        else:
            if not isinstance(result.get(item.group), dict):
                result[item.group] = {}
            result[item.group][item.key] = value
    theme = result.get('QFluentWidgets')
    result['QFluentWidgets'] = merge_serialized(GUI_DEFAULTS, theme if isinstance(theme, dict) else {})
    return result
