"""
本地 IPC 服务端（IpcServer：QLocalServer 的封装）

单实例的激活通道与 pmctl 的命令通道是同一条管道（名称 = 实例名）。四条硬规则（违反任何
一条都复现过进程直接终止或丢消息）：

1. 每个连接独立缓冲，按 \\n 切分（readAll 可能读到多行，也可能只有半行）；
2. disconnected 时先读空缓冲、处理完其中的完整请求，再打"已断开"标记，之后不再访问该
   连接的套接字（deleteLater 之后再访问会让 PyQt 直接终止进程）；
3. 所有槽函数 try/except：PyQt 槽内未捕获的异常会直接终止进程；
4. 延迟回包（wait / export 等）前检查"已断开"标记。

安全：
- 管道 DACL 只给当前用户（QLocalServer.UserAccessOption）；默认的 NoOptions 让任何人都能
  只读连上；
- 客户端身份在连接建立时就确定，不在执行命令时按 PID 事后查（客户端发完请求就退出、
  PID 被别的进程复用时，事后查到的是别人的令牌）：
  1. 首选连接令牌：ImpersonateNamedPipeClient → OpenThreadToken → RevertToSelf，直接读
     管道连接携带的客户端令牌的完整性级别，与 PID 无关、不存在复用窗口。管道上还没有
     任何数据时系统不允许模拟（ERROR_CANNOT_IMPERSONATE），所以连接时取不到就在第一次
     读到数据、或断开处理残留请求之前再取一次（实测：客户端写完即断开时，disconnected
     槽里仍可模拟）；
  2. 退路：连接时立即 GetNamedPipeClientProcessId + OpenProcess(PROCESS_QUERY_LIMITED_
     INFORMATION) 当场算出完整性级别，并持有进程句柄到连接结束（持有期间 PID 不会被复用，
     诊断日志里的 PID 始终指向同一个进程）；
  危险命令要求客户端完整性 ≥ 服务端（GUI 以管理员运行时，同用户的普通进程不能借它绕过
  UAC），取不到时按最低处理。核验在 commands 里按命令执行；
- 只认白名单命令（commands），不执行任何代码。

资源上限（防止同用户的失控脚本拖垮界面线程）：同时最多 MAX_CONNECTIONS 个连接（超出的
新连接直接断开）；每个连接的读缓冲上限为两行（setReadBufferSize）；一次读到的数据最多
处理 MAX_REQUESTS_PER_TURN 条请求，其余排队、经 singleShot(0) 在下一轮事件循环继续
（同一连接内仍严格按序）；协议错误断开连接时不同步等待写出。

兼容：1.x / 2.0 早期的从实例只发裸 b'activate'（无换行、发完就断开）；连接的第一批字节以
它开头时按 {"cmd":"activate"} 处理（保留一个版本）。
"""
import logging
from collections import deque
from typing import Callable, Deque, List, Optional

from PyQt5 import sip
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

from core.ipc import protocol
from core.ipc.protocol import LineFramer, LineTooLong, ProtocolError, Request

logger = logging.getLogger(__name__)

# 旧版激活消息（裸字节，无换行）
LEGACY_ACTIVATE = b'activate'
# 断开前等待回包写出的上限（毫秒）
FLUSH_TIMEOUT_MS = 500
# 同时保持的连接数上限：超出的新连接直接断开
MAX_CONNECTIONS = 16
# 一次处理的请求条数上限：超出的排队，下一轮事件循环继续
MAX_REQUESTS_PER_TURN = 32
# 每个连接套接字的读缓冲上限（字节）：两行足够分帧，客户端狂写时由管道本身形成背压
READ_BUFFER_BYTES = protocol.MAX_LINE_BYTES * 2

# 身份来源
IDENTITY_TOKEN = 'token'        # 管道连接令牌（模拟客户端）
IDENTITY_PROCESS = 'process'    # 连接时按 PID 打开的客户端进程

# 完整性级别（令牌完整性 SID 的最后一个子授权）
INTEGRITY_UNTRUSTED = 0x0000
INTEGRITY_LOW = 0x1000
INTEGRITY_MEDIUM = 0x2000
INTEGRITY_HIGH = 0x3000
INTEGRITY_SYSTEM = 0x4000

MSG_LISTEN_FAILED = "IPC 监听失败，将收不到其他启动的激活请求与命令行控制: %s"
MSG_TOO_MANY_CONNECTIONS = "IPC 连接数已达上限 %d，断开新连接"


# ---------- 客户端身份 ----------

class _IdentityApi:
    """GetNamedPipeClientProcessId 与令牌完整性级别（Windows 专用，失败一律返回 None）。"""

    TOKEN_INTEGRITY_LEVEL = 25
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    ERROR_CANNOT_IMPERSONATE = 1368

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        adv = ctypes.WinDLL('advapi32', use_last_error=True)
        k32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE,
                                                    ctypes.POINTER(wintypes.ULONG)]
        k32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        adv.ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
        adv.ImpersonateNamedPipeClient.restype = wintypes.BOOL
        adv.RevertToSelf.argtypes = []
        adv.RevertToSelf.restype = wintypes.BOOL
        adv.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                                        ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenThreadToken.restype = wintypes.BOOL
        k32.GetCurrentThread.argtypes = []
        k32.GetCurrentThread.restype = wintypes.HANDLE
        self.k32, self.adv = k32, adv

    def client_pid(self, pipe_handle: int) -> Optional[int]:
        pid = self.wintypes.ULONG(0)
        if not self.k32.GetNamedPipeClientProcessId(self.wintypes.HANDLE(pipe_handle),
                                                    self.ctypes.byref(pid)):
            return None
        return int(pid.value) or None

    def _token_integrity(self, process_handle) -> Optional[int]:
        ctypes, wintypes = self.ctypes, self.wintypes
        token = wintypes.HANDLE()
        if not self.adv.OpenProcessToken(process_handle, self.TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD(0)
            self.adv.GetTokenInformation(token, self.TOKEN_INTEGRITY_LEVEL, None, 0,
                                         ctypes.byref(size))
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if not self.adv.GetTokenInformation(token, self.TOKEN_INTEGRITY_LEVEL, buf, size,
                                                ctypes.byref(size)):
                return None
            psid = ctypes.c_void_p.from_buffer(buf).value
            count = self.adv.GetSidSubAuthorityCount(psid)[0]
            if count == 0:
                return None
            return int(self.adv.GetSidSubAuthority(psid, count - 1)[0])
        finally:
            self.k32.CloseHandle(token)

    def process_integrity(self, pid: int) -> Optional[int]:
        handle = self.open_process(pid)
        if not handle:
            return None
        try:
            return self._token_integrity(handle)
        finally:
            self.k32.CloseHandle(handle)

    def open_process(self, pid: int) -> Optional[int]:
        """以查询权限打开进程，返回句柄（调用方负责 close_handle）；失败为 None。"""
        handle = self.k32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return int(handle) if handle else None

    def handle_integrity(self, process_handle: int) -> Optional[int]:
        return self._token_integrity(self.wintypes.HANDLE(process_handle))

    def close_handle(self, handle: int) -> None:
        self.k32.CloseHandle(self.wintypes.HANDLE(handle))

    def pipe_client_integrity(self, pipe_handle: int) -> Optional[int]:
        """模拟管道客户端，读其令牌（连接令牌）的完整性级别后立即恢复。管道上还没有数据
        （ERROR_CANNOT_IMPERSONATE）或客户端只允许匿名级模拟时返回 None。"""
        ctypes, wintypes = self.ctypes, self.wintypes
        if not self.adv.ImpersonateNamedPipeClient(wintypes.HANDLE(pipe_handle)):
            return None
        try:
            token = wintypes.HANDLE()
            # OpenAsSelf=TRUE：按本进程身份做访问检查（标识级模拟令牌也能读）
            if not self.adv.OpenThreadToken(self.k32.GetCurrentThread(), self.TOKEN_QUERY, True,
                                            ctypes.byref(token)):
                return None
            try:
                return self._token_integrity(token)
            finally:
                self.k32.CloseHandle(token)
        finally:
            if not self.adv.RevertToSelf():
                # 恢复失败意味着界面线程仍以客户端身份运行：只能记录，交给上层处理
                logger.critical("模拟管道客户端后恢复自身身份失败（错误码 %s）",
                                ctypes.get_last_error())

    def own_integrity(self) -> Optional[int]:
        return self._token_integrity(self.k32.GetCurrentProcess())


class ClientIdentity:
    """
    客户端身份提供者：pid_of(socket)、token_integrity_of(socket)（连接令牌）、
    open_process(pid) / handle_integrity(handle) / close_handle(handle)（按 PID 的退路）、
    integrity_of(pid)（兼容旧调用），以及服务端自身的完整性级别。
    任何系统调用失败都返回 None（调用方按"最低"处理）。测试可替换整个对象。
    """

    def __init__(self, api_factory: Callable[[], object] = _IdentityApi):
        try:
            self._api = api_factory()
        except Exception:
            logger.warning("客户端身份核验不可用（非 Windows 或系统接口加载失败）", exc_info=True)
            self._api = None
        self._own: Optional[int] = None

    def pid_of(self, socket: QLocalSocket) -> Optional[int]:
        if self._api is None:
            return None
        try:
            return self._api.client_pid(int(socket.socketDescriptor()))
        except Exception:
            logger.debug("取客户端 PID 失败", exc_info=True)
            return None

    def integrity_of(self, pid: Optional[int]) -> Optional[int]:
        if self._api is None or not pid:
            return None
        try:
            return self._api.process_integrity(pid)
        except Exception:
            logger.debug("取客户端完整性级别失败: pid=%s", pid, exc_info=True)
            return None

    def token_integrity_of(self, socket: QLocalSocket) -> Optional[int]:
        """连接令牌的完整性级别（模拟客户端）；取不到为 None。"""
        if self._api is None:
            return None
        try:
            return self._api.pipe_client_integrity(int(socket.socketDescriptor()))
        except Exception:
            logger.debug("模拟管道客户端取完整性级别失败", exc_info=True)
            return None

    def open_process(self, pid: Optional[int]) -> Optional[int]:
        if self._api is None or not pid:
            return None
        try:
            return self._api.open_process(pid)
        except Exception:
            logger.debug("打开客户端进程失败: pid=%s", pid, exc_info=True)
            return None

    def handle_integrity(self, handle: Optional[int]) -> Optional[int]:
        if self._api is None or not handle:
            return None
        try:
            return self._api.handle_integrity(handle)
        except Exception:
            logger.debug("按进程句柄取完整性级别失败", exc_info=True)
            return None

    def close_handle(self, handle: Optional[int]) -> None:
        if self._api is None or not handle:
            return
        try:
            self._api.close_handle(handle)
        except Exception:
            logger.debug("关闭客户端进程句柄失败", exc_info=True)

    def own_integrity(self) -> Optional[int]:
        if self._own is None and self._api is not None:
            try:
                self._own = self._api.own_integrity()
            except Exception:
                logger.debug("取本进程完整性级别失败", exc_info=True)
        return self._own


# ---------- 连接 ----------

class IpcConnection:
    """一个客户端连接：独立缓冲、已断开标记与身份信息。

    处理器只经 send_*() 回包；连接断开后这些方法静默返回 False，绝不访问已释放的套接字。
    on_closed 里登记的回调在断开时调用一次（取消该连接上的等待）。
    """

    def __init__(self, socket: Optional[QLocalSocket], identity: Optional[ClientIdentity]):
        self._socket: Optional[QLocalSocket] = socket
        self.framer = LineFramer()
        self.closed = False
        # 断开后正在处理缓冲里剩余的请求：回包没有意义，也不再写套接字
        self.closing = False
        # 因协议错误主动断开：排队中的请求不再处理
        self.aborted = False
        self.seen_data = False
        self.legacy = False
        # 已分帧、尚未处理的请求行（一次最多处理 MAX_REQUESTS_PER_TURN 条，其余排在这里）
        self.backlog: Deque[bytes] = deque()
        self.draining = False
        self._identity = identity
        self._client_pid: Optional[int] = None
        self._integrity: Optional[int] = None
        # 完整性级别的来源：IDENTITY_TOKEN / IDENTITY_PROCESS / None（取不到）
        self.identity_source: Optional[str] = None
        # 连接时打开的客户端进程句柄（持有到连接结束，期间 PID 不会被复用）
        self._process_handle: Optional[int] = None
        self._close_callbacks: List[Callable[[], None]] = []
        # 身份必须在连接活着时确定：断开后句柄失效，事后按 PID 查可能查到别的进程
        self._resolve_at_connect()

    @property
    def writable(self) -> bool:
        return (not self.closed and not self.closing and self._socket is not None
                and not sip.isdeleted(self._socket))

    def _resolve_at_connect(self) -> None:
        identity, socket = self._identity, self._socket
        if identity is None or socket is None:
            return
        try:
            self._client_pid = identity.pid_of(socket)
            self._process_handle = identity.open_process(self._client_pid)
            if self._process_handle:
                self._integrity = identity.handle_integrity(self._process_handle)
                if self._integrity is not None:
                    self.identity_source = IDENTITY_PROCESS
        except Exception:
            logger.error("连接时识别客户端进程失败", exc_info=True)
        self.refresh_identity()

    def refresh_identity(self) -> None:
        """尚未取到连接令牌时再试一次（连接时管道上可能还没有数据，系统不允许模拟）。
        只在套接字仍有效时调用：读到数据时、断开处理残留请求之前。"""
        if self.identity_source == IDENTITY_TOKEN or self._identity is None:
            return
        socket = self._socket
        if socket is None or sip.isdeleted(socket):
            return
        try:
            integrity = self._identity.token_integrity_of(socket)
        except Exception:
            logger.debug("取连接令牌失败", exc_info=True)
            return
        if integrity is not None:
            self._integrity = integrity
            self.identity_source = IDENTITY_TOKEN

    def client_pid(self) -> Optional[int]:
        """连接时识别的客户端 PID（诊断用）。"""
        return self._client_pid

    def client_integrity(self) -> Optional[int]:
        """连接时确定的客户端完整性级别（不在调用时现查）；取不到为 None。"""
        return self._integrity

    def _release_process_handle(self) -> None:
        handle, self._process_handle = self._process_handle, None
        if handle and self._identity is not None:
            self._identity.close_handle(handle)

    def on_closed(self, callback: Callable[[], None]) -> None:
        if self.closed:
            callback()
        else:
            self._close_callbacks.append(callback)

    def _write(self, data: bytes) -> bool:
        if not self.writable:
            return False
        try:
            self._socket.write(data)
            self._socket.flush()
            return True
        except Exception:
            logger.error("IPC 回包失败", exc_info=True)
            return False

    def send_ok(self, request_id, result=None) -> bool:
        return self._send(lambda: protocol.encode_ok(request_id, result), request_id)

    def send_error(self, request_id, code: str, message: str, **extra) -> bool:
        return self._send(lambda: protocol.encode_error(request_id, code, message, **extra),
                          request_id)

    def send_event(self, request_id, event: str, data=None) -> bool:
        return self._send(lambda: protocol.encode_event(request_id, event, data), request_id)

    def _send(self, encode: Callable[[], bytes], request_id) -> bool:
        if not self.writable:
            return False
        try:
            data = encode()
        except (TypeError, ValueError, ProtocolError) as exc:
            logger.error("IPC 回包无法编码: %s", exc, exc_info=True)
            data = protocol.encode_error(request_id, protocol.ERR_INTERNAL,
                                         "回复内容无法编码，详见日志")
        return self._write(data)

    def flush(self, timeout_ms: int = FLUSH_TIMEOUT_MS) -> None:
        """尽量等待已写入的回包送出（随后要做阻塞操作时调用，如退出收尾）。"""
        if not self.writable:
            return
        try:
            if self._socket.bytesToWrite() > 0:
                self._socket.waitForBytesWritten(timeout_ms)
        except Exception:
            logger.debug("等待回包写出失败", exc_info=True)

    def abort(self) -> None:
        """断开（超长行等协议错误）。不同步等待写出（那会阻塞界面线程最多 FLUSH_TIMEOUT_MS）：
        disconnectFromServer 本身会在已写入的数据送完后再关闭。"""
        self.aborted = True
        self.backlog.clear()
        if self._socket is None or self.closed:
            return
        try:
            if not sip.isdeleted(self._socket):
                self._socket.disconnectFromServer()
        except Exception:
            logger.debug("断开 IPC 连接失败", exc_info=True)

    def _mark_closed(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._socket = None
        self._release_process_handle()
        callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.error("IPC 连接断开回调出错", exc_info=True)


# ---------- 服务端 ----------

RequestHandler = Callable[[IpcConnection, Request], None]


class IpcServer(QObject):
    """
    本地 IPC 服务端。handler(conn, request) 处理每条合法请求（在界面线程）；协议错误
    （不是 JSON、版本不符、超长）由这里直接回 bad_request。

    信号：
        connection_count_changed(int)：当前连接数（诊断用）
    """

    connection_count_changed = pyqtSignal(int)

    def __init__(self, name: str, handler: RequestHandler, parent: Optional[QObject] = None, *,
                 identity: Optional[ClientIdentity] = None):
        super().__init__(parent)
        self._name = name
        self._handler = handler
        self._identity = identity if identity is not None else ClientIdentity()
        self._server: Optional[QLocalServer] = None
        self._connections: List[IpcConnection] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def identity(self) -> ClientIdentity:
        return self._identity

    def is_listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    def connections(self) -> List[IpcConnection]:
        return list(self._connections)

    def set_handler(self, handler: RequestHandler) -> None:
        self._handler = handler

    def listen(self) -> bool:
        """开始监听（只允许当前用户连接）。失败只记日志并返回 False，不抛异常。"""
        # 清理异常退出可能遗留的同名服务（Windows 命名管道随进程消失，此处为跨平台兜底）
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if not server.listen(self._name):
            logger.warning(MSG_LISTEN_FAILED, server.errorString())
            server.deleteLater()
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()

    # ---------- 槽：一律 try/except ----------

    def _on_new_connection(self) -> None:
        try:
            while self._server is not None and self._server.hasPendingConnections():
                socket = self._server.nextPendingConnection()
                if socket is None:
                    break
                if len(self._connections) >= MAX_CONNECTIONS:
                    logger.warning(MSG_TOO_MANY_CONNECTIONS, MAX_CONNECTIONS)
                    socket.abort()
                    socket.deleteLater()
                    continue
                socket.setReadBufferSize(READ_BUFFER_BYTES)
                conn = IpcConnection(socket, self._identity)
                self._connections.append(conn)
                socket.readyRead.connect(lambda c=conn, s=socket: self._on_ready_read(c, s))
                socket.disconnected.connect(lambda c=conn, s=socket: self._on_disconnected(c, s))
                self.connection_count_changed.emit(len(self._connections))
                # 数据可能在连接建立前就已到达
                if socket.bytesAvailable():
                    self._on_ready_read(conn, socket)
        except Exception:
            logger.error("接受 IPC 连接失败", exc_info=True)

    def _on_ready_read(self, conn: IpcConnection, socket: QLocalSocket) -> None:
        try:
            if conn.closed or sip.isdeleted(self) or sip.isdeleted(socket):
                return
            data = bytes(socket.readAll())
            if data:
                conn.refresh_identity()
            self._consume(conn, data)
        except Exception:
            logger.error("处理 IPC 数据失败", exc_info=True)

    def _on_disconnected(self, conn: IpcConnection, socket: QLocalSocket) -> None:
        try:
            # 服务端对象已随父对象销毁（套接字在析构途中发出 disconnected）：只打标记
            alive = not sip.isdeleted(self) and not sip.isdeleted(socket)
            if alive and not conn.closed:
                # 先读空缓冲：客户端可能发完请求立刻断开（旧版激活消息就是这样）
                conn.closing = True
                # 客户端写完即断开时 disconnected 可能先于 readyRead 到达：此时套接字仍有效，
                # 先补取连接令牌，再处理残留请求（不能等到断开之后再按 PID 查）
                conn.refresh_identity()
                try:
                    remaining = bytes(socket.readAll())
                except Exception:
                    remaining = b''
                self._consume(conn, remaining, at_eof=True)
        except Exception:
            logger.error("处理断开前的 IPC 数据失败", exc_info=True)
        try:
            conn._mark_closed()
            if sip.isdeleted(self):
                return
            if conn in self._connections:
                self._connections.remove(conn)
                self.connection_count_changed.emit(len(self._connections))
            if not sip.isdeleted(socket):
                socket.deleteLater()
        except Exception:
            logger.error("IPC 连接断开收尾失败", exc_info=True)

    # ---------- 分帧与分发 ----------

    def _consume(self, conn: IpcConnection, data: bytes, at_eof: bool = False) -> None:
        if not conn.seen_data:
            data = conn.framer.pending + data
            conn.framer.clear()
            if not data:
                return
            if LEGACY_ACTIVATE.startswith(data[:len(LEGACY_ACTIVATE)]) and not data.startswith(b'{'):
                if len(data) < len(LEGACY_ACTIVATE) and not at_eof:
                    conn.framer.feed(data)      # 先攒着，等凑够旧版激活消息的长度
                    return
                if data.startswith(LEGACY_ACTIVATE):
                    conn.seen_data = True
                    conn.legacy = True
                    self._dispatch(conn, Request(id=None, cmd='activate', args={}))
                    data = data[len(LEGACY_ACTIVATE):].lstrip(b'\r\n')
            conn.seen_data = True
        try:
            lines = conn.framer.feed(data)
        except LineTooLong as exc:
            logger.warning("IPC 请求超长，断开连接")
            conn.framer.clear()
            conn.send_error(None, exc.code, exc.message)
            conn.abort()
            return
        conn.backlog.extend(lines)
        self._drain(conn)

    def _drain(self, conn: IpcConnection) -> None:
        """按序处理排队的请求，一轮最多 MAX_REQUESTS_PER_TURN 条，其余下一轮事件循环继续。
        处理中（处理器里可能嵌套触发 disconnected 等）再到的数据只入队，不插队。"""
        if conn.draining:
            return
        conn.draining = True
        try:
            handled = 0
            while conn.backlog and not conn.aborted:
                if handled >= MAX_REQUESTS_PER_TURN:
                    QTimer.singleShot(0, lambda c=conn: self._continue_drain(c))
                    return
                line = conn.backlog.popleft()
                handled += 1
                self._handle_line(conn, line)
        finally:
            conn.draining = False

    def _continue_drain(self, conn: IpcConnection) -> None:
        try:
            if sip.isdeleted(self):
                return
            self._drain(conn)
        except Exception:
            logger.error("继续处理排队的 IPC 请求失败", exc_info=True)

    def _handle_line(self, conn: IpcConnection, line: bytes) -> None:
        if not line.strip():
            return
        obj = None
        try:
            obj = protocol.decode_line(line)
            request = protocol.parse_request(obj)
        except ProtocolError as exc:
            conn.send_error(protocol.request_id_of(obj), exc.code, exc.message, **exc.extra)
            return
        self._dispatch(conn, request)

    def _dispatch(self, conn: IpcConnection, request: Request) -> None:
        try:
            self._handler(conn, request)
        except Exception:
            logger.error("IPC 命令处理失败: cmd=%s", request.cmd, exc_info=True)
            conn.send_error(request.id, protocol.ERR_INTERNAL, "命令处理出错，详见日志")
