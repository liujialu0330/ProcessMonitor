"""
单实例守卫

同一用户、同一数据目录同时只运行一个本程序实例：后启动的进程通知已运行的实例把主窗口
激活到前台，自己随即退出。不同数据目录（--data-dir / PROCESS_MONITOR_DATA_DIR）的实例
可以并存，同一台机器上的多个用户（如远程桌面）互不影响。

判据与通道分离：
- 判据：Windows 命名互斥量 Local\\<实例名>。CreateMutexW 是原子操作，两个进程同时启动
  也只有一个能"新建"成功，它就是主实例；另一个拿到 ERROR_ALREADY_EXISTS 即为从实例。
  旧方案"先连管道、连不上再 listen"在并发启动时两边都连不上、都成为主实例。
- 通道：QLocalServer/QLocalSocket（Windows 上为命名管道 \\\\.\\pipe\\<实例名>），只用来
  传递激活消息。主实例可能刚拿到互斥量还没来得及 listen()，所以从实例按退避间隔重试连接；
  即使始终连不上也照样退出——互斥量已经证明主实例存在。

实例名 = ProcessMonitor_<清洗后的用户名>_<数据目录规范化路径 sha1 的前 12 位>，互斥量与
管道同名。主实例另外持有固定名互斥量 ProcessMonitor.Running（会话局部与 Global\\ 各一个），
供安装器的 AppMutex 探测"程序是否在运行"——安装器不知道数据目录，算不出实例名。

非 Windows 平台没有命名互斥量，退化为旧的管道判据（连上即视为已有实例）。互斥量相关的任何
系统调用失败都降级为"允许启动"并记日志：不能因为单实例机制本身阻止用户使用。

使用顺序（见 app.bootstrap.run）：QApplication 创建之后、其余初始化之前构造守卫，
notify_existing_instance() 为 True 时直接退出；否则 listen() 成为主实例，
主窗口建好后把 activation_requested 连到 activate_main_window。
"""
import getpass
import hashlib
import logging
import os
import re
import sys
import time
from typing import Callable, List, Optional, Tuple

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

# 激活消息与单次连接/写入的等待上限（毫秒）
ACTIVATE_MESSAGE = b'activate'
SINGLE_INSTANCE_TIMEOUT_MS = 500
# 从实例连接管道失败后的退避间隔（毫秒）；用完后按最后一个间隔继续，直到总时长上限
CONNECT_RETRY_DELAYS_MS = (50, 100, 200, 400, 800)
CONNECT_RETRY_TOTAL_MS = 3000

# 实例名前缀与数据目录哈希长度
INSTANCE_NAME_PREFIX = "ProcessMonitor"
DATA_DIR_HASH_LENGTH = 12
# 实例互斥量所在的命名空间：会话局部，同一用户的不同远程会话也互不干扰
INSTANCE_MUTEX_NAMESPACE = "Local\\"
# 供安装器 AppMutex 探测的固定名互斥量（须与 build\setup.iss 的 AppMutex 一致）
INSTALLER_MUTEX_NAME = "ProcessMonitor.Running"
INSTALLER_MUTEX_NAMESPACES = ("Local\\", "Global\\")

# Win32 错误码
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

# 日志文案
MSG_MUTEX_API_UNAVAILABLE = "单实例互斥量不可用，按允许启动处理"
MSG_MUTEX_CREATE_FAILED = "创建单实例互斥量失败（错误码 %s），按允许启动处理: %s"
MSG_MUTEX_ACCESS_DENIED = "单实例互斥量已存在但无权访问（可能由以管理员身份运行的实例创建），按已有实例处理: %s"
MSG_NOTIFY_GAVE_UP = "已有实例持有互斥量，但 %d 次尝试内未连上其管道，未能激活其窗口，本进程仍退出: %s"
MSG_INSTALLER_MUTEX_FAILED = "创建安装器探测互斥量失败（错误码 %s），忽略: %s"


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


class _WinMutexApi:
    """kernel32 命名互斥量的最小封装；构造时 kernel32 不可用（如非 Windows）抛 OSError/AttributeError"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        # 独立的 WinDLL 实例：自己设 argtypes，不影响其他模块对 kernel32 的用法
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._create = kernel32.CreateMutexW
        self._create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._create.restype = wintypes.HANDLE
        self._close = kernel32.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL
        self._get_last_error = ctypes.get_last_error

    def create(self, name: str) -> Tuple[Optional[int], int]:
        """
        CreateMutexW(NULL, FALSE, name)。

        Returns:
            (句柄或 None, GetLastError())；已存在时句柄指向既有互斥量，错误码为 183
        """
        handle = self._create(None, False, name)
        return (handle or None), self._get_last_error()

    def close(self, handle: int) -> None:
        self._close(handle)


def _default_mutex_api() -> Optional[_WinMutexApi]:
    """当前平台可用的互斥量接口；非 Windows 返回 None（退化为管道判据）"""
    if sys.platform != 'win32':
        return None
    return _WinMutexApi()


class SingleInstanceGuard(QObject):
    """
    单实例守卫：命名互斥量判定主从，本地管道传递激活消息。

    主实例持有互斥量直到进程结束（不主动关闭句柄，由系统随进程回收），并监听管道，
    收到其他实例的激活消息时发出 activation_requested。
    """

    # 其他实例请求激活本实例主窗口
    activation_requested = pyqtSignal()

    def __init__(self, name: Optional[str] = None, parent=None, *,
                 data_dir: Optional[str] = None,
                 installer_mutex_name: Optional[str] = INSTALLER_MUTEX_NAME,
                 mutex_api_factory: Optional[Callable[[], Optional[_WinMutexApi]]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None):
        """
        Args:
            name: 实例名；None 时由 data_dir 按 single_instance_name 生成
            parent: 父对象
            data_dir: 数据目录（app.bootstrap 传 AppPaths.data_dir）
            installer_mutex_name: 成为主实例后额外持有的固定名互斥量；None 不创建（测试用）
            mutex_api_factory: 互斥量接口工厂，默认按平台选择；返回 None 表示平台不支持
            sleep: 退避等待函数（参数为秒），测试可注入
            clock: 单调时钟（秒），测试可注入
        """
        super().__init__(parent)
        self._name = name or single_instance_name(data_dir)
        self._installer_mutex_name = installer_mutex_name
        self._mutex_api_factory = mutex_api_factory or _default_mutex_api
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._server: Optional[QLocalServer] = None
        # 互斥量句柄只保存不关闭：随进程结束由系统释放，安装器据此判断进程是否真的退出
        self._instance_mutex: Optional[int] = None
        self._installer_mutexes: List[int] = []

    @property
    def name(self) -> str:
        """实例名（互斥量名去掉命名空间前缀后与管道名相同）"""
        return self._name

    def notify_existing_instance(self) -> bool:
        """
        判定是否已有实例；已有则通知其激活窗口。

        Returns:
            bool: True 表示已有实例（无论激活消息是否送达，本进程都应退出）；
                  False 表示本进程是主实例（已持有互斥量，或互斥量不可用而降级允许启动），
                  调用方随后应 listen()
        """
        try:
            api = self._mutex_api_factory()
        except Exception:
            logger.error(MSG_MUTEX_API_UNAVAILABLE, exc_info=True)
            return False
        if api is None:
            # 非 Windows：没有命名互斥量，退化为管道判据
            return self._send_activation()

        try:
            existing = self._acquire_instance_mutex(api)
        except Exception:
            logger.error(MSG_MUTEX_API_UNAVAILABLE, exc_info=True)
            return False
        if not existing:
            self._acquire_installer_mutexes(api)
            return False

        # 送达与否都退出：互斥量已证明主实例存在，再启动一份会争用同一个数据目录
        self._notify_with_retry()
        return True

    def _acquire_instance_mutex(self, api) -> bool:
        """创建实例互斥量；返回 True 表示已有实例。创建失败（非"已存在"类错误）按无实例处理"""
        handle, error = api.create(INSTANCE_MUTEX_NAMESPACE + self._name)
        if handle is None:
            if error == ERROR_ACCESS_DENIED:
                # 同名对象存在但当前令牌打不开（典型：已有实例以管理员身份运行）
                logger.warning(MSG_MUTEX_ACCESS_DENIED, self._name)
                return True
            logger.warning(MSG_MUTEX_CREATE_FAILED, error, self._name)
            return False
        if error == ERROR_ALREADY_EXISTS:
            try:
                api.close(handle)
            except Exception:
                logger.debug("关闭既有互斥量句柄失败", exc_info=True)
            return True
        self._instance_mutex = handle
        return False

    def _acquire_installer_mutexes(self, api) -> None:
        """
        持有供安装器探测的固定名互斥量（会话局部与 Global\\ 各一个）。

        已存在时（另一个数据目录的实例也在运行）同样保留句柄：先启动的实例退出后，
        互斥量仍因本进程持有而存在。任何失败都忽略（如 Global\\ 名已被其他用户的实例
        创建而无权打开），不影响程序本身。
        """
        if not self._installer_mutex_name:
            return
        for namespace in INSTALLER_MUTEX_NAMESPACES:
            full_name = namespace + self._installer_mutex_name
            try:
                handle, error = api.create(full_name)
            except Exception:
                logger.debug("创建安装器探测互斥量异常: %s", full_name, exc_info=True)
                continue
            if handle is None:
                logger.info(MSG_INSTALLER_MUTEX_FAILED, error, full_name)
                continue
            self._installer_mutexes.append(handle)

    def _notify_with_retry(self) -> bool:
        """
        连接主实例管道并发送激活消息；失败按 CONNECT_RETRY_DELAYS_MS 退避重试，
        总时长不超过 CONNECT_RETRY_TOTAL_MS。

        Returns:
            bool: 激活消息是否已送出
        """
        deadline = self._clock() + CONNECT_RETRY_TOTAL_MS / 1000.0
        attempt = 0
        while True:
            attempt += 1
            if self._send_activation():
                if attempt > 1:
                    logger.info("第 %d 次尝试连上已运行实例: %s", attempt, self._name)
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                logger.warning(MSG_NOTIFY_GAVE_UP, attempt, self._name)
                return False
            index = min(attempt - 1, len(CONNECT_RETRY_DELAYS_MS) - 1)
            self._sleep(min(CONNECT_RETRY_DELAYS_MS[index] / 1000.0, remaining))

    def _send_activation(self) -> bool:
        """
        连接一次管道并发送激活消息。

        Returns:
            bool: True 表示已连上并发送；False 表示没连上（管道不存在或超时）
        """
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(SINGLE_INSTANCE_TIMEOUT_MS):
            return False
        try:
            socket.write(ACTIVATE_MESSAGE)
            socket.flush()
            if not socket.waitForBytesWritten(SINGLE_INSTANCE_TIMEOUT_MS):
                logger.warning("向已运行实例发送激活消息超时，仍按已有实例处理")
            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.UnconnectedState:
                socket.waitForDisconnected(SINGLE_INSTANCE_TIMEOUT_MS)
        except Exception:
            logger.error("通知已运行实例失败", exc_info=True)
        return True

    def listen(self) -> bool:
        """
        开始监听激活消息。失败（如管道被占用）只记日志并返回 False：本进程已凭互斥量
        确定为主实例，照常启动，只是收不到后续启动的激活请求。
        """
        # 清理异常退出可能遗留的同名服务（Windows 命名管道随进程消失，此处为跨平台兜底）；
        # 已持有互斥量，不会误删另一个活着的主实例的管道
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        if not server.listen(self._name):
            logger.warning("单实例监听失败，将收不到其他启动的激活请求: %s", server.errorString())
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def _on_new_connection(self):
        """接收所有排队的连接；数据可能在连接建立前就已到达，立即读一次"""
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._on_ready_read(socket)

    def _on_ready_read(self, socket: QLocalSocket):
        """只认激活消息，其他内容一律忽略"""
        data = bytes(socket.readAll())
        if data.startswith(ACTIVATE_MESSAGE):
            self.activation_requested.emit()


def activate_main_window(window):
    """显示并激活主窗口：窗口隐藏在托盘或被最小化时都要恢复"""
    window.setWindowState((window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    window.show()
    window.raise_()
    window.activateWindow()
