"""
单实例守卫

同一用户、同一数据目录同时只运行一个本程序实例：后启动的进程通知已运行的实例把主窗口
激活到前台，自己随即退出。不同数据目录（命令行 --data-dir、环境变量 PROCESS_MONITOR_DATA_DIR、
便携模式，解析见 app.paths）的实例可以并存，同一台机器上的多个用户（如远程桌面）互不影响。

判据与通道分离：
- 判据：Windows 命名互斥量 Global\\<实例名>。CreateMutexW 是原子操作，两个进程同时启动
  也只有一个能"新建"成功，它就是主实例；另一个拿到 ERROR_ALREADY_EXISTS 即为从实例。
  旧方案"先连管道、连不上再 listen"在并发启动时两边都连不上、都成为主实例。互斥量与管道
  一样跨会话唯一（名字含用户名）：会话局部的互斥量会让同一用户在两个会话里各起一份、
  只有一份能监听管道。全局名创建失败时退回 Local\\<实例名> 并记日志。
- 通道：本地 IPC 服务（core.ipc.server.IpcServer，Windows 上为命名管道
  \\\\.\\pipe\\<实例名>，DACL 只给当前用户）。激活消息与 pmctl 的命令走同一条管道、
  同一套 JSON 行协议：从实例发 {"cmd":"activate"}；旧版的裸 b'activate' 仍然认（保留一个
  版本）。主实例可能刚拿到互斥量还没来得及 listen()，所以从实例按退避间隔重试连接；
  即使始终连不上也照样退出——互斥量已经证明主实例存在。
- 以 --background 启动的从实例不发激活消息（notify_existing_instance(activate=False)）。

实例名 = ProcessMonitor_<清洗后的用户名>_<数据目录规范化路径 sha1 的前 12 位>，互斥量与
管道同名，算法在 utils.instance_name（pmctl 也用它，不能依赖 Qt）。主实例另外持有固定名
互斥量 ProcessMonitor.Running（会话局部与 Global\\ 各一个），供安装器的 AppMutex 探测
"程序是否在运行"——安装器不知道数据目录，算不出实例名。

非 Windows 平台没有命名互斥量，退化为旧的管道判据（连上即视为已有实例）。互斥量相关的任何
系统调用失败都降级为"允许启动"并记日志：不能因为单实例机制本身阻止用户使用。

使用顺序（见 app.bootstrap.run）：QApplication 创建之后、其余初始化之前构造守卫，
notify_existing_instance() 为 True 时直接退出；否则 listen() 成为主实例（IPC 服务随即
开始监听，就绪前除 ping / activate 外的命令回 not_ready），组装完成后组合根把命令处理器
的上下文交给 guard.dispatcher，主窗口建好后把 activation_requested 连到 activate_main_window。
"""
import json
import logging
import sys
import time
from typing import Callable, List, Optional, Tuple

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtNetwork import QLocalSocket

# 实例名算法在 utils（pmctl 与 core 也要用）；这里再导出，保持原有导入路径可用
from utils.instance_name import (  # noqa: F401
    DATA_DIR_HASH_LENGTH, INSTANCE_MUTEX_FALLBACK_NAMESPACE, INSTANCE_MUTEX_NAMESPACE,
    INSTANCE_NAME_PREFIX, _sanitized_user, data_dir_hash, getpass, normalize_data_dir,
    single_instance_name,
)

logger = logging.getLogger(__name__)

# 旧版激活消息（裸字节）；新版从实例发协议请求 ACTIVATE_REQUEST
ACTIVATE_MESSAGE = b'activate'
ACTIVATE_REQUEST = (json.dumps({'v': 1, 'id': 1, 'cmd': 'activate', 'args': {}},
                               separators=(',', ':')) + '\n').encode('ascii')
# 单次连接/写入/等回复的上限（毫秒）
SINGLE_INSTANCE_TIMEOUT_MS = 500
# 从实例连接管道失败后的退避间隔（毫秒）；用完后按最后一个间隔继续，直到总时长上限
CONNECT_RETRY_DELAYS_MS = (50, 100, 200, 400, 800)
CONNECT_RETRY_TOTAL_MS = 3000

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
MSG_MUTEX_GLOBAL_FALLBACK = "创建全局单实例互斥量失败（错误码 %s），退回会话局部互斥量: %s"
MSG_NOTIFY_GAVE_UP = "已有实例持有互斥量，但 %d 次尝试内未连上其管道，未能激活其窗口，本进程仍退出: %s"
MSG_INSTALLER_MUTEX_FAILED = "创建安装器探测互斥量失败（错误码 %s），忽略: %s"


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
    单实例守卫：命名互斥量判定主从，本地 IPC 管道传递激活消息与命令。

    主实例持有互斥量直到进程结束（不主动关闭句柄，由系统随进程回收），listen() 后由
    IpcServer + CommandDispatcher 处理连接；收到 activate 时发出 activation_requested。
    从实例送出激活请求后读一次回复：主实例是无窗口模式时回 invalid_state，记在
    activation_error 里，组合根据此提示用户。
    """

    # 其他实例请求激活本实例主窗口
    activation_requested = pyqtSignal()

    def __init__(self, name: Optional[str] = None, parent=None, *,
                 data_dir: Optional[str] = None,
                 installer_mutex_name: Optional[str] = INSTALLER_MUTEX_NAME,
                 mutex_api_factory: Optional[Callable[[], Optional[_WinMutexApi]]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 background: bool = False, app_version: Optional[str] = None):
        """
        Args:
            name: 实例名；None 时由 data_dir 按 single_instance_name 生成
            parent: 父对象
            data_dir: 数据目录（app.bootstrap 传 AppPaths.data_dir）
            installer_mutex_name: 成为主实例后额外持有的固定名互斥量；None 不创建（测试用）
            mutex_api_factory: 互斥量接口工厂，默认按平台选择；返回 None 表示平台不支持
            sleep: 退避等待函数（参数为秒），测试可注入
            clock: 单调时钟（秒），测试可注入
            background: 本进程是无窗口模式（ping 报告 headless，activate 回 invalid_state）
            app_version: ping 报告的程序版本；None 取 config.APP_VERSION
        """
        super().__init__(parent)
        self._name = name or single_instance_name(data_dir)
        self._installer_mutex_name = installer_mutex_name
        self._mutex_api_factory = mutex_api_factory or _default_mutex_api
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._background = background
        self._app_version = app_version
        # listen() 成功后可用：IPC 服务与命令处理器（组合根就绪后给处理器上下文）
        self.server = None
        self.dispatcher = None
        # 从实例送出激活请求后主实例回复的错误码（无回复或成功为 None）
        self.activation_error: Optional[str] = None
        # 互斥量句柄只保存不关闭：随进程结束由系统释放，安装器据此判断进程是否真的退出
        self._instance_mutex: Optional[int] = None
        self._installer_mutexes: List[int] = []

    @property
    def name(self) -> str:
        """实例名（互斥量名去掉命名空间前缀后与管道名相同）"""
        return self._name

    def notify_existing_instance(self, activate: bool = True) -> bool:
        """
        判定是否已有实例；已有则通知其激活窗口（activate=False 时只判定、不通知：
        以 --background 启动的从实例不应把已运行的窗口拉到前台）。

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
        if activate:
            self._notify_with_retry()
        return True

    def _acquire_instance_mutex(self, api) -> bool:
        """
        创建实例互斥量；返回 True 表示已有实例。

        先建全局名（Global\\，与管道一样跨会话唯一）；全局名创建失败（非"已存在"、非"拒绝
        访问"）时退回会话局部名（Local\\）并记日志；两者都失败按无实例处理（允许启动）。
        """
        full_name = INSTANCE_MUTEX_NAMESPACE + self._name
        handle, error = api.create(full_name)
        if handle is None and error != ERROR_ACCESS_DENIED:
            logger.warning(MSG_MUTEX_GLOBAL_FALLBACK, error, full_name)
            full_name = INSTANCE_MUTEX_FALLBACK_NAMESPACE + self._name
            handle, error = api.create(full_name)
        if handle is None:
            if error == ERROR_ACCESS_DENIED:
                # 同名对象存在但当前令牌打不开（典型：已有实例以管理员身份运行）
                logger.warning(MSG_MUTEX_ACCESS_DENIED, full_name)
                return True
            logger.warning(MSG_MUTEX_CREATE_FAILED, error, full_name)
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
        连接一次管道并发送激活请求，再等一下主实例的回复（记下错误码）。

        Returns:
            bool: True 表示已连上并发送；False 表示没连上（管道不存在或超时）
        """
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(SINGLE_INSTANCE_TIMEOUT_MS):
            return False
        try:
            socket.write(ACTIVATE_REQUEST)
            socket.flush()
            if not socket.waitForBytesWritten(SINGLE_INSTANCE_TIMEOUT_MS):
                logger.warning("向已运行实例发送激活消息超时，仍按已有实例处理")
            self.activation_error = self._read_reply(socket)
            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.UnconnectedState:
                socket.waitForDisconnected(SINGLE_INSTANCE_TIMEOUT_MS)
        except Exception:
            logger.error("通知已运行实例失败", exc_info=True)
        return True

    @staticmethod
    def _read_reply(socket: QLocalSocket) -> Optional[str]:
        """读主实例对激活请求的回复，返回错误码；没有回复（旧版主实例）或成功为 None。"""
        data = b''
        while b'\n' not in data:
            if not socket.bytesAvailable() and not socket.waitForReadyRead(
                    SINGLE_INSTANCE_TIMEOUT_MS):
                return None
            data += bytes(socket.readAll())
        try:
            reply = json.loads(data.split(b'\n', 1)[0].decode('utf-8'))
        except ValueError:
            return None
        if isinstance(reply, dict) and reply.get('ok') is False:
            error = reply.get('error')
            return error.get('code') if isinstance(error, dict) else None
        return None

    def listen(self) -> bool:
        """
        开始监听（IPC 服务：激活消息与命令行控制）。失败（如管道被占用）只记日志并返回
        False：本进程已凭互斥量确定为主实例，照常启动，只是收不到后续的激活请求与命令。
        """
        import os

        import config
        from core.ipc.commands import CommandDispatcher
        from core.ipc.server import IpcServer

        dispatcher = CommandDispatcher(
            app_version=self._app_version or config.APP_VERSION, pid=os.getpid(),
            headless=self._background, parent=self)
        dispatcher.activation_requested.connect(self.activation_requested)
        server = IpcServer(self._name, dispatcher.handle, self)
        dispatcher.identity = server.identity
        if not server.listen():
            logger.warning("单实例监听失败，将收不到其他启动的激活请求与命令行控制: %s", self._name)
            return False
        self.server = server
        self.dispatcher = dispatcher
        return True


def activate_main_window(window):
    """显示并激活主窗口：窗口隐藏在托盘或被最小化时都要恢复"""
    window.setWindowState((window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    window.show()
    window.raise_()
    window.activateWindow()
