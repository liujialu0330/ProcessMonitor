"""
应用配置
基于 qfluentwidgets QConfig 体系的用户偏好持久化（JSON 落盘于 AppPaths.config_path，即数据目录下的 config.json）

与 config.py 的关系：config.py 保留作为"缺省值来源"（模块常量，随代码发布固化）；
本模块在其基础上包装为可持久化、可在设置页实时编辑的 ConfigItem。运行时业务
代码统一读 cfg.get(...)，config.py 里被本模块接管的常量只在此处被引用一次
（作为 ConfigItem 的 default 参数），不再是运行时唯一真相来源。

配置文件根上的 config_version 记录文件格式版本（没有该键的旧文件视为 1）。
load_app_config 读文件后、交给 qconfig 之前经 migrate_config（表驱动的纯函数，
CONFIG_MIGRATIONS）迁移到当前版本，迁移发生过才回写；版本高于当前时不改写文件。

【关键契约，务必遵守，勿在本类重定义 themeMode】
主题项直接复用 QConfig 基类内置的 themeMode，本类不重新声明同名 ConfigItem。
原因（已读源码 + 实测核实）：qfluentwidgets.setTheme() 内部固定操作模块级单例
qconfig 的 qconfig.themeMode（common/style_sheet.py: `qconfig.set(qconfig.
themeMode, theme, save)`），而 QConfig.set() 内部又用 `is` 身份比较判断
"这次改的是不是主题项"（common/config.py: `if item is self._cfg.themeMode`）
来决定要不要联动更新已解析的主题值、要不要发 themeChanged 信号。若本类另外
声明一个同名 themeMode 属性，会产生两个不同的 ConfigItem 对象：setTheme() 操作
qfluentwidgets 内置的那个、设置页的 OptionsSettingCard 绑定的是本类重定义的
那个，二者状态互不联动，会出现"设置页选了深色但没生效""重启后主题又变回默认"
之类的错乱。不重定义时 AppConfig.themeMode 与 qconfig.themeMode 是同一对象
（继承自类属性，未被覆盖），已用脚本验证 `AppConfig.themeMode is QConfig.
themeMode` 为 True。
"""
import copy
import json
import logging
import os

from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from qfluentwidgets import (
    QConfig, ConfigItem, ConfigValidator, OptionsConfigItem, RangeConfigItem,
    BoolValidator, OptionsValidator, RangeValidator, qconfig,
    Theme, setTheme
)

import config
from utils.metrics import LEAK_CHECK_PRESET, all_metric_types, canonical_metric_type

if TYPE_CHECKING:
    from app.paths import AppPaths

logger = logging.getLogger(__name__)


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


# 默认打点快捷键（与 ui/hotkey.DEFAULT_MARKER_HOTKEY 一致；本模块不依赖界面层）
DEFAULT_MARKER_HOTKEY = "Ctrl+Alt+M"
MARKER_HOTKEY_MAX_LENGTH = 40


class MarkerHotkeyValidator(ConfigValidator):
    """打点快捷键校验器：非字符串或过长时回落到默认值（写法由界面层再判断）。"""

    def validate(self, value) -> bool:
        return isinstance(value, str) and len(value) <= MARKER_HOTKEY_MAX_LENGTH

    def correct(self, value) -> str:
        return value.strip() if self.validate(value) else DEFAULT_MARKER_HOTKEY


# ========== 配置文件版本 ==========

# config.json 根上的版本号键；没有该键的旧文件视为版本 1
CONFIG_VERSION_KEY = "config_version"
LEGACY_CONFIG_VERSION = 1
CURRENT_CONFIG_VERSION = 2


def _migrate_v1_to_v2(data: dict) -> dict:
    """1 → 2：内容无变更，只写入版本号。

    v2 原计划剔除 Monitor.LastMetrics 里已废弃的 num_ctx_switches_involuntary、
    把 memory_vms / memory_pagefile 映射为 memory_private；这两件事 normalize_metrics
    在每次加载时（MetricListValidator）已经做了，文件里的旧键不影响读取，下次保存时
    自然写成新键，因此这里不重复改写。旧版 update_prefs.json 的导入仍由
    _import_legacy_update_prefs 负责，与版本号无关。
    """
    return dict(data)


# 表驱动迁移：键为源版本，函数把该版本的配置字典变换为"源版本 + 1"（不必写版本号，
# migrate_config 统一写）。新增版本时在这里加一项并调大 CURRENT_CONFIG_VERSION
CONFIG_MIGRATIONS: Dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
}


def config_version_of(data: dict) -> int:
    """配置字典的版本号：缺失或不是正整数时按旧版（1）处理"""
    value = data.get(CONFIG_VERSION_KEY, LEGACY_CONFIG_VERSION)
    if isinstance(value, bool) or not isinstance(value, int) or value < LEGACY_CONFIG_VERSION:
        return LEGACY_CONFIG_VERSION
    return value


def migrate_config(data: dict) -> Tuple[dict, List[str]]:
    """
    把配置字典逐版本迁移到 CURRENT_CONFIG_VERSION（纯函数，不修改入参）。

    Returns:
        (迁移后的字典, 执行过的步骤说明)。步骤为空表示无需迁移（已是当前版本，或版本
        高于当前——那种情况原样返回，由调用方决定如何处理）。
    """
    version = config_version_of(data)
    if version >= CURRENT_CONFIG_VERSION:
        return data, []
    result = copy.deepcopy(data)
    steps: List[str] = []
    while version < CURRENT_CONFIG_VERSION:
        result = CONFIG_MIGRATIONS[version](result)
        steps.append(f"{version} → {version + 1}")
        version += 1
    result[CONFIG_VERSION_KEY] = CURRENT_CONFIG_VERSION
    return result, steps


# 报告图片配色：固定浅色（默认，与界面主题无关）/ 跟随当前界面主题
REPORT_SCHEME_LIGHT = "light"
REPORT_SCHEME_FOLLOW = "follow"
REPORT_COLOR_SCHEMES = [REPORT_SCHEME_LIGHT, REPORT_SCHEME_FOLLOW]


class AppConfig(QConfig):
    """应用级用户偏好配置"""

    # 配置文件版本号（根级键 config_version）：由 load_app_config 维护，不是用户偏好；
    # 声明成 ConfigItem 是为了 qconfig 保存时把它一并写回
    config_version = ConfigItem(CONFIG_VERSION_KEY, "", CURRENT_CONFIG_VERSION)

    # 默认采集周期（秒）：新建监控任务时采集周期输入框的初始值；用户仍可在
    # 监控页为单次任务临时改动，不影响本配置项
    default_interval = RangeConfigItem(
        "Monitor", "DefaultInterval",
        int(config.DEFAULT_INTERVAL), RangeValidator(1, 3600))

    # 历史数据保留天数：0 = 永久保留（不自动清理），>0 时启动清理已停止且超期的
    # 任务数据（语义与 config.DATA_RETENTION_DAYS 一致，见该常量注释）
    retention_days = OptionsConfigItem(
        "Data", "RetentionDays",
        config.DATA_RETENTION_DAYS, OptionsValidator([0, 7, 30, 90, 180]))

    # 关闭窗口时是否最小化到系统托盘：设置页开关控制，默认关闭（直接退出）
    close_to_tray = ConfigItem("Behavior", "CloseToTray", False, BoolValidator())

    # 有运行中任务时阻止系统因空闲进入睡眠（core.power_guard）：设置页开关控制，默认开启
    keep_awake_while_monitoring = ConfigItem(
        "Monitor", "KeepAwakeWhileMonitoring", True, BoolValidator())

    # 允许命令行与脚本控制（pmctl / 本地 IPC）：关闭后除 ping / activate 外的命令一律回
    # disabled；设置页开关控制，默认开启
    automation_enabled = ConfigItem("Automation", "Enabled", True, BoolValidator())

    # 监控页上次选择的指标：新建任务时沿用，默认"泄漏排查"预设
    last_metrics = ConfigItem(
        "Monitor", "LastMetrics", list(LEAK_CHECK_PRESET), MetricListValidator())

    # 用户在更新提示里选择"跳过"的版本号（空串 = 未跳过任何版本）；
    # 旧版单独存于 update_prefs.json，load_app_config 首次运行时导入并删除旧文件
    skipped_version = ConfigItem("Update", "SkippedVersion", "")

    # 全局打点快捷键（任何程序里按下即为运行中的任务打点，不弹窗）；空串 = 不使用。
    # 写法是否合法、能否注册由 ui/hotkey 判断，这里只保证是不太长的字符串
    marker_hotkey = ConfigItem(
        "Behavior", "MarkerHotkey", DEFAULT_MARKER_HOTKEY, MarkerHotkeyValidator())

    # 报告图片配色：报告常被贴进文档与邮件，默认固定浅色；设置页可改为跟随界面主题
    report_color_scheme = OptionsConfigItem(
        "Export", "ReportColorScheme", REPORT_SCHEME_LIGHT,
        OptionsValidator(REPORT_COLOR_SCHEMES))

    def __init__(self):
        super().__init__()
        # 配置文件版本高于当前时读到的原始内容：保存时保留本版本不认识的键，
        # 避免用户回退到旧版后改一项设置就把新版的配置抹掉
        self._newer_raw: dict = {}

    def toDict(self, serialize=True):
        items = super().toDict(serialize)
        if not serialize or not self._newer_raw:
            return items
        merged = copy.deepcopy(self._newer_raw)
        for key, value in items.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged


# 全局单例：进程内所有代码统一通过该实例读写配置（不要自行 AppConfig() 构造新实例
# 用于生产代码路径——自定义 ConfigItem 是类属性，天然全进程共享同一份值，
# 但只有这一个实例会被 qconfig.load() 接管、参与自动落盘）
cfg = AppConfig()

def __getattr__(name):
    """兼容旧的模块常量 CONFIG_PATH：访问时按 AppPaths 现算，不在导入期固化路径"""
    if name == 'CONFIG_PATH':
        from app.paths import AppPaths
        return AppPaths.resolve().config_path
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_loaded() -> bool:
    """应用配置是否已由 load_app_config() 接管（接管后才允许落盘）"""
    return getattr(qconfig, "_cfg", None) is cfg


def load_app_config(paths: Optional["AppPaths"] = None) -> None:
    """加载应用配置

    进程内只应调用一次（app.bootstrap.run，QApplication 创建之后、MainWindow 创建
    之前）。测试调用时应传入指向临时目录的 AppPaths，或直接调用
    qconfig.load(临时路径, cfg)。

    首次启动（配置文件尚不存在）时，主题偏好默认"跟随系统"：qconfig.load
    完成后显式调用 setTheme(Theme.AUTO, save=True) 落盘一次；非首启（配置文件
    已存在）不做任何主题相关的额外动作，尊重用户上次保存的选择（哪怕用户主动
    从"跟随系统"改回了固定的浅色/深色）。

    加载前按 migrate_config 把配置文件迁移到当前版本（迁移发生过才回写）；版本高于
    当前时不改写文件、记 WARNING，按能识别的键读取。

    加载后把旧版 update_prefs.json 里"跳过的版本"一次性导入本配置并删除旧文件。

    Args:
        paths: 应用路径；为 None 时按 AppPaths.resolve() 解析
    """
    if paths is None:
        from app.paths import AppPaths
        paths = AppPaths.resolve()
    config_path = paths.config_path
    is_first_launch = not os.path.exists(config_path)

    # 新建或损坏的配置文件按当前版本保存；文件里有版本号时由 qconfig.load 读回
    cfg.config_version.value = CURRENT_CONFIG_VERSION
    cfg._newer_raw = {}
    if not is_first_launch:
        _migrate_config_file(config_path)

    qconfig.load(config_path, cfg)

    if is_first_launch:
        setTheme(Theme.AUTO, save=True)

    _import_legacy_update_prefs(paths.prefs_path)


def _migrate_config_file(config_path: str) -> None:
    """
    按版本号迁移配置文件，迁移发生过才回写（先写临时文件再替换，避免写一半留下坏文件）。

    读不出或不是 JSON 对象时不动它，保持原有行为（qconfig.load 按空配置处理）；
    版本高于当前时不改写，只记 WARNING，并记下原始内容供保存时保留未知的键。
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        logger.warning("读取配置文件失败，按默认配置加载: %s", config_path, exc_info=True)
        return
    if not isinstance(data, dict):
        return

    version = config_version_of(data)
    if version > CURRENT_CONFIG_VERSION:
        logger.warning("配置文件版本 %d 高于当前支持的 %d，不改写文件，按能识别的键读取: %s",
                       version, CURRENT_CONFIG_VERSION, config_path)
        cfg._newer_raw = data
        return

    migrated, steps = migrate_config(data)
    if not steps:
        return
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(migrated, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, config_path)
    except OSError:
        # 写不回去也不影响本次读取：qconfig.load 读旧文件，下次启动再迁移
        logger.warning("回写迁移后的配置文件失败: %s", config_path, exc_info=True)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return
    logger.info("配置文件已迁移到版本 %d（%s）: %s",
                CURRENT_CONFIG_VERSION, "，".join(steps), config_path)


def _import_legacy_update_prefs(prefs_path: str) -> None:
    """
    把旧版 update_prefs.json 的 skipped_version 并入 cfg 后删除旧文件。

    cfg 里已有值时以 cfg 为准（旧文件只可能是更早版本留下的），旧文件同样删除；
    旧文件损坏或读不出时按"没有跳过的版本"处理。删除失败只记日志，下次启动再试。
    """
    if not os.path.exists(prefs_path):
        return
    legacy = ''
    try:
        with open(prefs_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            legacy = str(data.get('skipped_version') or '')
    except Exception:
        logger.warning("读取旧版更新偏好失败，按未跳过版本处理: %s", prefs_path, exc_info=True)
    if legacy and not get_skipped_version():
        set_skipped_version(legacy)
        logger.info("已从旧版更新偏好导入跳过的版本: %s", legacy)
    try:
        os.remove(prefs_path)
    except OSError:
        logger.warning("删除旧版更新偏好文件失败: %s", prefs_path, exc_info=True)


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
