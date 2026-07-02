"""
全局配置文件
定义应用程序的配置参数
"""
import sys
import os


# 应用信息
APP_NAME = "进程监控助手"
APP_VERSION = "1.1.0"

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
DEFAULT_INTERVAL = 1.0  # 默认采集间隔（秒）

# 数据保存配置
SAVE_BATCH_SIZE = 1     # 批量保存数据的大小（改为1以便实时显示历史数据）

# UI配置
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 600
