"""
实例名与互斥量/管道名的计算（只用标准库，不依赖 Qt）

单实例守卫（app.single_instance）、IPC 客户端（core.ipc.client）与命令行 pmctl
必须算出同一个名字：
实例名 = ProcessMonitor_<清洗后的用户名>_<数据目录规范化路径 sha1 的前 12 位>，
互斥量 Global\\<实例名> 与本地管道 \\\\.\\pipe\\<实例名> 同名。pmctl 不能导入 PyQt5、
core 不能依赖 app，所以名字的算法放在 utils，三处共用。

互斥量放在全局命名空间：命名管道本身就是全机唯一的（不分会话），互斥量若在会话局部
（Local\\），同一用户在两个会话（如本地控制台 + 远程桌面，或计划任务）里各启动一份时
两边都能拿到互斥量、却只有一边能监听管道，另一边成了收不到命令、关不掉的实例。名字里已含
用户名，不同用户互不干扰。创建 Global\\ 失败（极少见，如受限令牌）时退回 Local\\ 并记日志。
"""
import getpass
import hashlib
import os
import re
from typing import Optional, Tuple

# 实例名前缀与数据目录哈希长度
INSTANCE_NAME_PREFIX = "ProcessMonitor"
DATA_DIR_HASH_LENGTH = 12
# 实例互斥量所在的命名空间：全局（与管道一样跨会话唯一；名字含用户名，不同用户互不干扰）
INSTANCE_MUTEX_NAMESPACE = "Global\\"
# 创建全局互斥量失败时退回的命名空间
INSTANCE_MUTEX_FALLBACK_NAMESPACE = "Local\\"
# Windows 命名管道的路径前缀（QLocalServer 在 Windows 上把服务名放在这里）
PIPE_PATH_PREFIX = "\\\\.\\pipe\\"


def _sanitized_user() -> str:
    """当前用户名，非 [0-9A-Za-z_.-] 的字符替换为下划线（管道名与互斥量名都能安全使用）"""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get('USERNAME', 'user')
    return re.sub(r'[^0-9A-Za-z_.-]', '_', user)


def normalize_data_dir(data_dir: str) -> str:
    """数据目录规范化：绝对路径、折叠 ./..、去尾部分隔符；Windows 上统一小写与反斜杠"""
    return os.path.normcase(os.path.abspath(data_dir))


def data_dir_hash(data_dir: str) -> str:
    """数据目录规范化路径的 sha1 前 12 位（UTF-8 编码后计算）"""
    digest = hashlib.sha1(normalize_data_dir(data_dir).encode('utf-8')).hexdigest()
    return digest[:DATA_DIR_HASH_LENGTH]


def single_instance_name(data_dir: Optional[str] = None) -> str:
    """
    实例名（互斥量与管道共用）：ProcessMonitor_<用户名>_<数据目录哈希>。

    Args:
        data_dir: 数据目录；None 时只含用户名（仅供兼容，正式启动总会传入）
    """
    base = f"{INSTANCE_NAME_PREFIX}_{_sanitized_user()}"
    if data_dir is None:
        return base
    return f"{base}_{data_dir_hash(data_dir)}"


def instance_mutex_name(name: str) -> str:
    """实例互斥量的完整名称（全局命名空间）"""
    return INSTANCE_MUTEX_NAMESPACE + name


def instance_mutex_names(name: str) -> Tuple[str, str]:
    """实例互斥量可能的完整名称：(全局, 退回用的会话局部)。探测"实例是否存在"时两个都要查。"""
    return INSTANCE_MUTEX_NAMESPACE + name, INSTANCE_MUTEX_FALLBACK_NAMESPACE + name


def pipe_path(name: str) -> str:
    """实例管道的完整路径（\\\\.\\pipe\\<实例名>）"""
    return PIPE_PATH_PREFIX + name
