"""
应用路径解析

数据目录、数据库、日志目录、配置文件、旧版更新偏好文件的位置统一在这里一次算出，
其余模块不再各自判断"打包还是开发环境""有没有设环境变量"。

解析规则：
- 数据目录：设置了环境变量 PROCESS_MONITOR_DATA_DIR（非空）时用它（便于测试/便携运行）；
  否则打包环境为 %LOCALAPPDATA%\\<APP_NAME>\\data（避开 Program Files 权限问题），
  开发环境为项目根目录下的 data\\
- 日志目录：设置了环境变量时为 <数据目录>\\logs，日志随数据一起隔离；
  否则与数据目录同级的 logs\\
- 数据库 monitor.db、配置 config.json、旧版更新偏好 update_prefs.json 都在数据目录下

resolve() 只做纯计算、不创建目录；需要落盘前由入口调用 ensure_dirs()。
"""
import os
import sys
from dataclasses import dataclass
from typing import Mapping, Optional

import config

DB_FILENAME = "monitor.db"
CONFIG_FILENAME = "config.json"
# 旧版单独保存"跳过的版本"的文件；现已并入 config.json，仅用于一次性导入
PREFS_FILENAME = "update_prefs.json"
LOG_DIRNAME = "logs"

# 项目根目录（开发环境的基础目录）：本文件位于 <根>\app\paths.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _base_dir(frozen: bool) -> str:
    """基础目录：打包环境为 exe 所在目录（安装目录），开发环境为项目根目录"""
    if frozen:
        return os.path.dirname(sys.executable)
    return _PROJECT_ROOT


@dataclass(frozen=True)
class AppPaths:
    """一次解析得到的全部应用路径（不可变；测试可直接构造指向临时目录的实例）"""

    base_dir: str
    data_dir: str
    db_path: str
    log_dir: str
    config_path: str
    prefs_path: str

    @classmethod
    def resolve(cls, env: Optional[Mapping[str, str]] = None,
                frozen: Optional[bool] = None) -> "AppPaths":
        """
        按当前环境解析路径（无副作用）。

        Args:
            env: 环境变量映射，默认 os.environ
            frozen: 是否打包环境，默认取 sys.frozen
        """
        if env is None:
            env = os.environ
        if frozen is None:
            frozen = bool(getattr(sys, 'frozen', False))

        base_dir = _base_dir(frozen)
        override = env.get(config.DATA_DIR_ENV)
        if override:
            data_dir = os.path.abspath(override)
            # 显式指定数据目录时日志放在其内部，测试/便携运行不会写到默认位置
            log_dir = os.path.join(data_dir, LOG_DIRNAME)
        else:
            if frozen:
                local_appdata = env.get('LOCALAPPDATA') or os.path.expanduser('~')
                data_dir = os.path.join(local_appdata, config.APP_NAME, "data")
            else:
                data_dir = os.path.join(base_dir, "data")
            log_dir = os.path.join(os.path.dirname(data_dir), LOG_DIRNAME)
        return cls.under(data_dir, log_dir=log_dir, base_dir=base_dir)

    @classmethod
    def under(cls, data_dir: str, log_dir: Optional[str] = None,
              base_dir: Optional[str] = None) -> "AppPaths":
        """以 data_dir 为数据目录构造（测试用临时目录时最方便）；日志目录默认 <data_dir>\\logs"""
        data_dir = os.path.abspath(data_dir)
        return cls(
            base_dir=base_dir or _base_dir(bool(getattr(sys, 'frozen', False))),
            data_dir=data_dir,
            db_path=os.path.join(data_dir, DB_FILENAME),
            log_dir=log_dir or os.path.join(data_dir, LOG_DIRNAME),
            config_path=os.path.join(data_dir, CONFIG_FILENAME),
            prefs_path=os.path.join(data_dir, PREFS_FILENAME),
        )

    def ensure_dirs(self) -> None:
        """创建数据目录与日志目录（已存在则跳过）"""
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
