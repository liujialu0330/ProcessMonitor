"""
应用路径解析

数据目录、数据库、日志目录、配置文件、旧版更新偏好文件的位置统一在这里一次算出，
其余模块不再各自判断"打包还是开发环境""有没有设环境变量""是不是便携运行"。

数据目录按以下顺序取第一个命中的来源（AppPaths.source 记录命中的是哪一条）：
1. 命令行 --data-dir <路径> 或 --data-dir=<路径>（cli）；
2. 环境变量 PROCESS_MONITOR_DATA_DIR（非空，env）；
3. 便携标志：基础目录（打包为 exe 所在目录，开发为项目根）下存在 portable.flag 文件时，
   数据目录为 <基础目录>\\data（portable）；
4. 默认：打包环境为 %LOCALAPPDATA%\\<APP_NAME>\\data（避开 Program Files 权限问题），
   开发环境为项目根目录下的 data\\（default）。

日志目录：命令行与环境变量指定时为 <数据目录>\\logs，日志随数据一起隔离；便携模式为
<基础目录>\\logs；默认为与数据目录同级的 logs\\。数据库 monitor.db、配置 config.json、
旧版更新偏好 update_prefs.json 都在数据目录下。

resolve() 只做纯计算（便携模式只检查标志文件是否存在）、不创建目录；需要落盘前由入口
调用 ensure_dirs()。命令行参数缺省取 sys.argv：config 的兼容路径常量等惰性解析的调用方
因此与组合根看到同一个数据目录。

sync_dir_hint() 判断数据目录是否位于网盘同步目录：SQLite（尤其 WAL 模式）在同步过程中
可能损坏，组合根据此在主窗口给出提示。
"""
import os
import sys
from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

import config

DB_FILENAME = "monitor.db"
CONFIG_FILENAME = "config.json"
# 旧版单独保存"跳过的版本"的文件；现已并入 config.json，仅用于一次性导入
PREFS_FILENAME = "update_prefs.json"
LOG_DIRNAME = "logs"
DATA_DIRNAME = "data"

# 项目根目录（开发环境的基础目录）：本文件位于 <根>\app\paths.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 同步盘提示：{name} 为识别出的同步服务名称
SYNC_DIR_HINT_TEMPLATE = (
    "数据目录位于同步盘（{name}），SQLite 数据库在同步过程中可能损坏，"
    "建议用 --data-dir 或便携模式把数据放在本地目录")
# OneDrive 客户端设置的环境变量，值为同步根目录
ONEDRIVE_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
# 路径分段里出现这些名称（不区分大小写，按子串匹配，可覆盖"OneDrive - 公司名"
# "Dropbox (Personal)" 这类目录名）即视为同步盘：(匹配串, 显示名称)
SYNC_DIR_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("OneDrive", "OneDrive"),
    ("Dropbox", "Dropbox"),
    ("Google Drive", "Google Drive"),
    ("坚果云", "坚果云"),
    ("Nutstore", "坚果云"),
    ("iCloudDrive", "iCloud Drive"),
)


class DataDirError(ValueError):
    """命令行给出的数据目录不可用（缺少路径或路径为空）"""


def _base_dir(frozen: bool) -> str:
    """基础目录：打包环境为 exe 所在目录（安装目录），开发环境为项目根目录"""
    if frozen:
        return os.path.dirname(sys.executable)
    return _PROJECT_ROOT


def parse_data_dir_arg(argv: Sequence[str]) -> Optional[str]:
    """
    从命令行参数取出 --data-dir 的值（argv[0] 是程序名，不参与解析）。

    同时接受 "--data-dir <路径>" 与 "--data-dir=<路径>"；出现多次时以最后一次为准。

    Returns:
        路径；没有该参数时为 None

    Raises:
        DataDirError: 参数缺少路径或路径为空（不能悄悄当作当前目录）
    """
    value: Optional[str] = None
    found = False
    args = list(argv[1:])
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == config.DATA_DIR_ARG:
            found = True
            value = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if arg.startswith(config.DATA_DIR_ARG + "="):
            found = True
            value = arg[len(config.DATA_DIR_ARG) + 1:]
        i += 1
    if not found:
        return None
    if value is None or not value.strip():
        raise DataDirError(f"命令行参数 {config.DATA_DIR_ARG} 缺少数据目录路径")
    return value


def strip_data_dir_args(argv: Sequence[str]) -> List[str]:
    """去掉 --data-dir 及其值后的命令行参数（交给 QApplication，保持参数干净）"""
    result: List[str] = []
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if i > 0 and arg == config.DATA_DIR_ARG:
            skip_next = True
            continue
        if i > 0 and arg.startswith(config.DATA_DIR_ARG + "="):
            continue
        result.append(arg)
    return result


@dataclass(frozen=True)
class AppPaths:
    """一次解析得到的全部应用路径（不可变；测试可直接构造指向临时目录的实例）"""

    base_dir: str
    data_dir: str
    db_path: str
    log_dir: str
    config_path: str
    prefs_path: str
    # 数据目录来源：config.DATA_DIR_SOURCE_CLI / ENV / PORTABLE / DEFAULT
    source: str = config.DATA_DIR_SOURCE_DEFAULT

    @classmethod
    def resolve(cls, env: Optional[Mapping[str, str]] = None,
                frozen: Optional[bool] = None,
                argv: Optional[Sequence[str]] = None) -> "AppPaths":
        """
        按 命令行 → 环境变量 → 便携标志 → 默认 的顺序解析路径（不创建任何目录）。

        Args:
            env: 环境变量映射，默认 os.environ
            frozen: 是否打包环境，默认取 sys.frozen
            argv: 命令行参数（含 argv[0]），默认 sys.argv

        Raises:
            DataDirError: 命令行给了 --data-dir 却缺少路径
        """
        if env is None:
            env = os.environ
        if frozen is None:
            frozen = bool(getattr(sys, 'frozen', False))
        if argv is None:
            argv = sys.argv

        base_dir = _base_dir(frozen)
        cli_dir = parse_data_dir_arg(argv)
        env_dir = env.get(config.DATA_DIR_ENV)
        if cli_dir is not None or env_dir:
            source = (config.DATA_DIR_SOURCE_CLI if cli_dir is not None
                      else config.DATA_DIR_SOURCE_ENV)
            data_dir = os.path.abspath(cli_dir if cli_dir is not None else env_dir)
            # 显式指定数据目录时日志放在其内部，测试/便携运行不会写到默认位置
            return cls.under(data_dir, log_dir=os.path.join(data_dir, LOG_DIRNAME),
                             base_dir=base_dir, source=source)
        if os.path.isfile(os.path.join(base_dir, config.PORTABLE_FLAG_FILENAME)):
            return cls.under(os.path.join(base_dir, DATA_DIRNAME),
                             log_dir=os.path.join(base_dir, LOG_DIRNAME),
                             base_dir=base_dir, source=config.DATA_DIR_SOURCE_PORTABLE)
        return cls.default(env=env, frozen=frozen)

    @classmethod
    def default(cls, env: Optional[Mapping[str, str]] = None,
                frozen: Optional[bool] = None) -> "AppPaths":
        """只按默认规则（第 4 条）解析，忽略命令行、环境变量与便携标志。

        也用于数据目录不可用时找一个写启动失败日志的位置。
        """
        if env is None:
            env = os.environ
        if frozen is None:
            frozen = bool(getattr(sys, 'frozen', False))
        base_dir = _base_dir(frozen)
        if frozen:
            local_appdata = env.get('LOCALAPPDATA') or os.path.expanduser('~')
            data_dir = os.path.join(local_appdata, config.APP_NAME, DATA_DIRNAME)
        else:
            data_dir = os.path.join(base_dir, DATA_DIRNAME)
        log_dir = os.path.join(os.path.dirname(data_dir), LOG_DIRNAME)
        return cls.under(data_dir, log_dir=log_dir, base_dir=base_dir,
                         source=config.DATA_DIR_SOURCE_DEFAULT)

    @classmethod
    def under(cls, data_dir: str, log_dir: Optional[str] = None,
              base_dir: Optional[str] = None,
              source: str = config.DATA_DIR_SOURCE_DEFAULT) -> "AppPaths":
        """以 data_dir 为数据目录构造（测试用临时目录时最方便）；日志目录默认 <data_dir>\\logs"""
        data_dir = os.path.abspath(data_dir)
        return cls(
            base_dir=base_dir or _base_dir(bool(getattr(sys, 'frozen', False))),
            data_dir=data_dir,
            db_path=os.path.join(data_dir, DB_FILENAME),
            log_dir=log_dir or os.path.join(data_dir, LOG_DIRNAME),
            config_path=os.path.join(data_dir, CONFIG_FILENAME),
            prefs_path=os.path.join(data_dir, PREFS_FILENAME),
            source=source,
        )

    def ensure_dirs(self) -> None:
        """创建数据目录与日志目录（已存在则跳过）；路径非法或无权限时抛 OSError"""
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_within(path: str, root: str) -> bool:
    """path 是否为 root 本身或位于其下（两者都已 _normalize）"""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # 不同盘符，或一个绝对一个相对
        return False


def sync_dir_hint(data_dir: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """
    数据目录位于网盘同步目录时返回提示文案，否则返回 None（纯函数，不访问磁盘）。

    判定（不区分大小写）：
    - 位于 OneDrive 客户端环境变量 OneDrive / OneDriveConsumer / OneDriveCommercial
      任一所指目录之下；
    - 路径的某一级目录名含 SYNC_DIR_MARKERS 中的名称（OneDrive、Dropbox、Google Drive、
      坚果云 / Nutstore、iCloudDrive）。

    Args:
        data_dir: 数据目录
        env: 环境变量映射，默认 os.environ
    """
    if env is None:
        env = os.environ
    target = _normalize(data_dir)
    for var in ONEDRIVE_ENV_VARS:
        root = env.get(var)
        if root and _is_within(target, _normalize(root)):
            return SYNC_DIR_HINT_TEMPLATE.format(name="OneDrive")

    _drive, rest = os.path.splitdrive(target)
    segments = [s for s in rest.replace('/', '\\').split('\\') if s]
    for marker, name in SYNC_DIR_MARKERS:
        needle = marker.lower()
        if any(needle in segment.lower() for segment in segments):
            return SYNC_DIR_HINT_TEMPLATE.format(name=name)
    return None
