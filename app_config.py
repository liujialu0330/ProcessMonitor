"""
应用配置
基于 qfluentwidgets QConfig 体系的用户偏好持久化（JSON 落盘于 DATA_DIR/config.json）

与 config.py 的关系：config.py 保留作为"缺省值来源"（模块常量，随代码发布固化）；
本模块在其基础上包装为可持久化、可在设置页实时编辑的 ConfigItem。运行时业务
代码统一读 cfg.get(...)，config.py 里被本模块接管的常量只在此处被引用一次
（作为 ConfigItem 的 default 参数），不再是运行时唯一真相来源。

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
import os

from typing import Iterable, List

from qfluentwidgets import (
    QConfig, ConfigItem, ConfigValidator, OptionsConfigItem, RangeConfigItem,
    BoolValidator, OptionsValidator, RangeValidator, qconfig,
    Theme, setTheme
)

import config
from utils.metrics import LEAK_CHECK_PRESET, all_metric_types, canonical_metric_type


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


class AppConfig(QConfig):
    """应用级用户偏好配置"""

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

    # 监控页上次选择的指标：新建任务时沿用，默认"泄漏排查"预设
    last_metrics = ConfigItem(
        "Monitor", "LastMetrics", list(LEAK_CHECK_PRESET), MetricListValidator())


# 全局单例：进程内所有代码统一通过该实例读写配置（不要自行 AppConfig() 构造新实例
# 用于生产代码路径——自定义 ConfigItem 是类属性，天然全进程共享同一份值，
# 但只有这一个实例会被 qconfig.load() 接管、参与自动落盘）
cfg = AppConfig()

# 配置文件路径：与数据库同目录，随 DATA_DIR 的开发/打包环境差异自动切换
CONFIG_PATH = os.path.join(config.DATA_DIR, "config.json")


def load_app_config() -> None:
    """加载应用配置

    进程内只应调用一次（main.py 入口，QApplication 创建之后、MainWindow 创建
    之前）。测试不应调用本函数（会触达真实的 DATA_DIR/config.json），应直接
    调用 qconfig.load(临时路径, cfg) 把配置重定向到 tmp_path。

    首次启动（CONFIG_PATH 尚不存在）时，主题偏好默认"跟随系统"：qconfig.load
    完成后显式调用 setTheme(Theme.AUTO, save=True) 落盘一次；非首启（配置文件
    已存在）不做任何主题相关的额外动作，尊重用户上次保存的选择（哪怕用户主动
    从"跟随系统"改回了固定的浅色/深色）。
    """
    is_first_launch = not os.path.exists(CONFIG_PATH)

    qconfig.load(CONFIG_PATH, cfg)

    if is_first_launch:
        setTheme(Theme.AUTO, save=True)


def remember_last_metrics(metrics: Iterable[str]) -> None:
    """记住监控页的指标选择。

    只在应用配置已由 load_app_config() 接管时才落盘：配置尚未加载（单元测试、
    独立脚本）时只更新内存值，避免 qconfig 把文件写到当前目录下的默认路径。
    """
    loaded = getattr(qconfig, "_cfg", None) is cfg
    qconfig.set(cfg.last_metrics, normalize_metrics(list(metrics)), save=loaded)
