"""
日志基建模块
统一配置应用日志（RotatingFileHandler），main.py 入口最早处调用 setup_logging()。
"""
import logging
import logging.handlers
import os
from typing import Optional

import config

# 单文件大小上限与保留份数（固化：5MB x 5，超出即滚动，历史日志累计最多约 30MB）
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# 幂等标记，避免重复调用 setup_logging 时重复添加 Handler
_configured = False


def get_log_dir() -> str:
    """
    获取日志目录（由 config.get_data_dir() 推导，不再自行判断打包/开发环境）

    Returns:
        str: 日志目录路径
            - 设置了环境变量 PROCESS_MONITOR_DATA_DIR：<该目录>\\logs，日志随数据一起
              隔离在指定目录内（测试/便携运行不会写到默认位置）
            - 默认：与数据目录同级的 logs\\（打包为 %LOCALAPPDATA%\\<APP_NAME>\\logs，
              开发环境为项目根目录下的 logs\\，已加入 .gitignore）
    """
    data_dir = config.get_data_dir()
    if os.environ.get(config.DATA_DIR_ENV):
        return os.path.join(data_dir, "logs")
    return os.path.join(os.path.dirname(data_dir), "logs")


def setup_logging(log_dir: Optional[str] = None) -> str:
    """
    初始化应用日志：RotatingFileHandler 挂到 root logger，各模块用
    `logging.getLogger(__name__)` 即可复用。应在 main.py 入口最早处调用一次。

    Args:
        log_dir: 日志目录；为 None 时按 get_log_dir() 推导

    Returns:
        str: 日志目录路径（供异常提示等场景显示给用户）
    """
    global _configured

    if log_dir is None:
        log_dir = get_log_dir()
    os.makedirs(log_dir, exist_ok=True)

    if not _configured:
        formatter = logging.Formatter(
            fmt='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8',
        )
        file_handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)

        _configured = True
        logging.getLogger(__name__).info(
            "日志初始化完成: 目录=%s, 版本=v%s", log_dir, config.APP_VERSION)

    return log_dir
