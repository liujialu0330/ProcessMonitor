"""
日志基建模块
统一配置应用日志（RotatingFileHandler），main.py 入口最早处调用 setup_logging()。
"""
import logging
import logging.handlers
import os
import sys

import config

# 单文件大小上限与保留份数（固化：1MB x 2，超出即滚动，历史日志累计最多约 3MB）
LOG_MAX_BYTES = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 2

# 幂等标记，避免重复调用 setup_logging 时重复添加 Handler
_configured = False


def get_log_dir() -> str:
    """
    获取日志目录

    Returns:
        str: 日志目录路径
            - 打包环境：%LOCALAPPDATA%\\<APP_NAME>\\logs
            - 开发环境：项目根目录下的 logs\\（已加入 .gitignore）
    """
    if getattr(sys, 'frozen', False):
        local_appdata = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
        return os.path.join(local_appdata, config.APP_NAME, "logs")
    else:
        return os.path.join(config.BASE_DIR, "logs")


def setup_logging() -> str:
    """
    初始化应用日志：RotatingFileHandler 挂到 root logger，各模块用
    `logging.getLogger(__name__)` 即可复用。应在 main.py 入口最早处调用一次。

    Returns:
        str: 日志目录路径（供异常提示等场景显示给用户）
    """
    global _configured

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
