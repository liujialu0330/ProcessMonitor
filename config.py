"""
全局配置文件
定义应用程序的配置参数
"""
import sys
import os


# 应用信息
APP_NAME = "进程监控助手"
APP_VERSION = "1.4.1"

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

    图标查找单点（v1.2.0 架构重构批4收敛，原分散在 main.py 内的三分支逻辑）：
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


def get_data_dir():
    """
    获取数据目录

    Returns:
        str: 数据目录路径
            - 开发环境：返回项目根目录下的data文件夹
            - 打包环境：返回用户本地应用数据目录，避免Program Files权限问题
    """
    if getattr(sys, 'frozen', False):
        local_appdata = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
        return os.path.join(local_appdata, APP_NAME, "data")
    else:
        return os.path.join(BASE_DIR, "data")


# 数据库配置
DATA_DIR = get_data_dir()
DB_PATH = os.path.join(DATA_DIR, "monitor.db")

# 更新偏好文件（记录跳过的版本等）
UPDATE_PREFS_PATH = os.path.join(DATA_DIR, "update_prefs.json")

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)

# 监控配置
MAX_MONITOR_TASKS = 5  # 最多同时监控5个进程
DEFAULT_INTERVAL = 1.0  # 默认采集间隔（秒）；缺省值，运行时以设置页（app_config.cfg）为准

# 数据保存配置
# 固化为1，勿调大：v1.2.0 架构评审裁决——每周期立即落库，配合 flush 失败重试与 1000
# 条缓冲上限保证"停止时数据不丢"的语义简单可靠；调大会引入"崩溃时丢一批未落库
# 数据"的新风险，且与当前"实时显示历史数据"的产品预期冲突
SAVE_BATCH_SIZE = 1

# 数据保留天数（启动时自动清理已停止且过期的历史任务）
# 默认 0 = 禁用自动清理（v1.2.0 架构评审裁决）：历史数据的删除应由用户在历史页显式点击
# "删除此任务数据"完成，避免用户在不知情的情况下丢失数据；调大为正整数即可启用，
# 启用时只清理 status='stopped' 且 结束时间早于"当前时间-该天数"的任务（含数据点）
# 缺省值，运行时以设置页（app_config.cfg，v1.3.0 起用户可在设置页调整此项）为准
DATA_RETENTION_DAYS = 0

# UI配置
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 600
