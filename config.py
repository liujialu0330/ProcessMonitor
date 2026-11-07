"""
全局配置文件
定义应用程序的配置参数
"""
import sys
import os


# 应用信息
APP_NAME = "进程监控助手"
# 预发布期间带后缀（2.0.0-beta.N / 2.0.0-rc.N），正式发布时改为 2.0.0。读取方：窗口标题、
# 关于页、更新检查（按 SemVer 比较，带后缀的低于同号正式版）、库 meta 的写入版本、
# build\gen_version_info.py（文件版本四元组）与 build.bat（安装包文件名与 /DMyAppVersion）
APP_VERSION = "2.0.0-beta.1"

# GitHub 仓库信息（自动更新用）
GITHUB_OWNER = "liujialu0330"
GITHUB_REPO = "ProcessMonitor"


def get_base_dir():
    """
    获取基础目录

    Returns:
        str: 基础目录路径
            - 开发环境：返回项目根目录
            - 打包环境：返回exe所在目录（即用户选择的安装目录）
    """
    if getattr(sys, 'frozen', False):
        # 打包后的环境：返回exe所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发环境：返回项目根目录
        return os.path.dirname(os.path.abspath(__file__))


# 基础目录
BASE_DIR = get_base_dir()


def get_icon_path():
    """
    获取应用图标（app_green_icon.ico）的运行时查找路径

    图标查找单点（原分散在 main.py 内的三分支逻辑，现收敛于此）：
        - 打包环境（onedir）：图标随 _internal\\ 目录一起收进 datas，
          位于 exe 同级目录下（PyInstaller onedir 不再有 _MEIPASS 临时解压目录，
          onefile 时代的 sys._MEIPASS 分支保留仅作兼容，理论上不会命中）。
        - 开发环境：图标位于项目 build\\ 目录下。

    Returns:
        str: 图标文件的绝对路径（不保证文件一定存在，调用方应自行 os.path.exists 判断）
    """
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            # 兼容 onefile 打包方式（历史遗留，onedir 模式下通常不会走到这里）
            return os.path.join(sys._MEIPASS, 'app_green_icon.ico')
        # onedir 模式：Inno Setup 已将图标复制到 exe 同目录（{app} 根）
        return os.path.join(BASE_DIR, 'app_green_icon.ico')
    else:
        # 开发环境：图标在 build 目录
        return os.path.join(BASE_DIR, 'build', 'app_green_icon.ico')


# 覆盖数据目录的环境变量名
DATA_DIR_ENV = "PROCESS_MONITOR_DATA_DIR"
# 指定数据目录的命令行参数（--data-dir <路径> 或 --data-dir=<路径>），优先于环境变量
DATA_DIR_ARG = "--data-dir"
# 便携标志文件：基础目录（打包为 exe 所在目录，开发为项目根）下存在它时，
# 数据与日志放在基础目录内的 data\ 与 logs\
PORTABLE_FLAG_FILENAME = "portable.flag"

# 数据目录的来源（AppPaths.source 的取值），按解析优先级从高到低
DATA_DIR_SOURCE_CLI = "cli"
DATA_DIR_SOURCE_ENV = "env"
DATA_DIR_SOURCE_PORTABLE = "portable"
DATA_DIR_SOURCE_DEFAULT = "default"


# 由数据目录派生的兼容路径常量：不在导入期计算（导入本模块不得有创建目录等副作用，
# 也不能把导入那一刻的环境变量固化下来），每次访问时按 AppPaths.resolve() 现算。
# 新代码请直接用组合根注入的 AppPaths。
_DERIVED_PATHS = {
    'DATA_DIR': 'data_dir',
    'DB_PATH': 'db_path',
    'LOG_DIR': 'log_dir',
    'UPDATE_PREFS_PATH': 'prefs_path',
    # 数据目录来源（DATA_DIR_SOURCE_* 之一），供设置页显示
    'DATA_DIR_SOURCE': 'source',
}


def __getattr__(name):
    attr = _DERIVED_PATHS.get(name)
    if attr is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.paths import AppPaths
    return getattr(AppPaths.resolve(), attr)


# 监控配置
MAX_MONITOR_TASKS = 5  # 最多同时监控5个进程
DEFAULT_INTERVAL = 1.0  # 默认采集间隔（秒）；缺省值，运行时以设置页（app_config.cfg）为准

# 数据保存配置
# 固化为1，勿调大：每周期立即落库，配合 flush 失败重试与 1000
# 条缓冲上限保证"停止时数据不丢"的语义简单可靠；调大会引入"崩溃时丢一批未落库
# 数据"的新风险，且与当前"实时显示历史数据"的产品预期冲突
SAVE_BATCH_SIZE = 1

# 数据保留天数（启动时自动清理已停止且过期的历史任务）
# 默认 0 = 禁用自动清理：历史数据的删除应由用户在历史页显式点击
# "删除此任务数据"完成，避免用户在不知情的情况下丢失数据；调大为正整数即可启用，
# 启用时只清理 status='stopped' 且 结束时间早于"当前时间-该天数"的任务（含数据点）
# 缺省值，运行时以设置页（app_config.cfg）为准
DATA_RETENTION_DAYS = 0

# UI配置
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 600
