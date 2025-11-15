"""
全局配置文件
定义应用程序的配置参数
"""
import os

# 应用信息
APP_NAME = "进程监控助手"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Your Name"

# 数据库配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "monitor.db")

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)

# 监控配置
MAX_MONITOR_TASKS = 5  # 最多同时监控5个进程
DEFAULT_INTERVAL = 1.0  # 默认采集间隔（秒）
MIN_INTERVAL = 0.1      # 最小采集间隔
MAX_INTERVAL = 60.0     # 最大采集间隔

# 数据保存配置
SAVE_BATCH_SIZE = 10    # 批量保存数据的大小
MAX_DATA_POINTS = 10000 # 单个任务最多保存的数据点数

# UI配置
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
WINDOW_MIN_WIDTH = 800
WINDOW_MIN_HEIGHT = 600
