"""
本地 IPC 客户端（只用标准库：ctypes 调 kernel32 / advapi32）

pmctl 与 tools/pmclient.py 共用。不用 open(r'\\\\.\\pipe\\…')：它没有读超时、管道忙时没法
WaitNamedPipe、错误码被压成 errno 22。这里直接用 CreateFileW / WaitNamedPipeW /
ReadFile / WriteFile / PeekNamedPipe：

- 连接：CreateFileW；ERROR_PIPE_BUSY 时 WaitNamedPipeW 再试；管道不存在为 PipeNotFound；
- 读：先 PeekNamedPipe 看有没有数据再 ReadFile（同步 ReadFile 会无限阻塞），自适应轮询：
  前 2 ms 只让出时间片，之后 1 ms 起翻倍到最多 20 ms；
- 防冒名：连上后 GetNamedPipeServerProcessId 取服务端 PID，与 ping 返回的 pid 比对，
  并核对该进程的用户 SID 与本进程相同（读不到服务端令牌时退而核对管道对象的所有者）；
- 拉起：GUI 不在时先 OpenMutexW(Global\\<实例名>，再查退回用的 Local\\<实例名>) 看是否正在
  启动，不在才以 --background --idle-exit-s N 拉起（标准输入输出全部 DEVNULL、
  CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP），再按 25 → 200 ms 退避等管道出现并 ping 到
  ready。等待期间轮询拉起的进程：它已退出就按退出码说明原因（不再干等到超时）。新拉起
  （或正在启动）的实例可能在同步升级数据库、事件循环暂时不转：第一次 ping 的等待上限用
  launch_timeout 而不是 CONNECT_TIMEOUT_S，期间每 WAITING_NOTICE_S 秒经 on_waiting 报一次。

错误分类与 pmctl 退出码的映射表也在这里（服务端只回错误码字符串）。
"""
import ctypes
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from core.ipc import protocol
from core.ipc.protocol import (
    ERR_BAD_REQUEST, ERR_BUSY, ERR_DISABLED, ERR_FORBIDDEN, ERR_INTERNAL, ERR_INVALID_ARGS,
    ERR_INVALID_STATE, ERR_NOT_FOUND, ERR_NOT_READY, ERR_TIMEOUT, ERR_UNKNOWN_COMMAND,
    EVENT_HEARTBEAT, LineFramer, ProtocolError, Response,
)

# ---------- 退出码（pmctl 与示例脚本共用） ----------
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONNECTION = 3
EXIT_RISING = 10
EXIT_INSUFFICIENT = 20
EXIT_TARGET_EXITED = 30

VERDICT_STABLE = 'stable'
VERDICT_RISING = 'rising'
VERDICT_INSUFFICIENT = 'insufficient'
VERDICT_TARGET_EXITED = 'target_exited'

VERDICT_EXIT_CODES: Dict[str, int] = {
    VERDICT_STABLE: EXIT_OK,
    VERDICT_RISING: EXIT_RISING,
    VERDICT_INSUFFICIENT: EXIT_INSUFFICIENT,
    VERDICT_TARGET_EXITED: EXIT_TARGET_EXITED,
}
# 多目标取最严重者：被测异常退出 > 持续上涨 > 数据不足 > 平稳
VERDICT_SEVERITY: Dict[str, int] = {
    VERDICT_STABLE: 0, VERDICT_INSUFFICIENT: 1, VERDICT_RISING: 2, VERDICT_TARGET_EXITED: 3,
}

# 服务端错误码 → 退出码（未列出的一律 EXIT_ERROR）
ERROR_EXIT_CODES: Dict[str, int] = {
    ERR_BAD_REQUEST: EXIT_USAGE,
    ERR_UNKNOWN_COMMAND: EXIT_USAGE,
    ERR_INVALID_ARGS: EXIT_USAGE,
    ERR_DISABLED: EXIT_CONNECTION,
    ERR_NOT_READY: EXIT_CONNECTION,
    ERR_NOT_FOUND: EXIT_ERROR,
    ERR_INVALID_STATE: EXIT_ERROR,
    ERR_BUSY: EXIT_ERROR,
    ERR_TIMEOUT: EXIT_ERROR,
    ERR_INTERNAL: EXIT_ERROR,
    ERR_FORBIDDEN: EXIT_ERROR,
}


def exit_code_for_error(code: Optional[str]) -> int:
    return ERROR_EXIT_CODES.get(code or '', EXIT_ERROR)


def exit_code_for_verdict(verdict: Optional[str]) -> int:
    return VERDICT_EXIT_CODES.get(verdict or '', EXIT_ERROR)


def most_severe(verdicts: Sequence[str]) -> Optional[str]:
    """多个判定里最严重的一个（30 > 10 > 20 > 0）；空序列为 None。"""
    known = [v for v in verdicts if v in VERDICT_SEVERITY]
    if not known:
        return None
    return max(known, key=lambda v: VERDICT_SEVERITY[v])


# ---------- 超时与退避 ----------
CONNECT_TIMEOUT_S = 5.0
BUSY_WAIT_MS = 2000
# 客户端在长请求（wait / export 等）里多久收不到任何一行就判定连接已断（服务端每 5 秒一次心跳）
IDLE_TIMEOUT_S = 15.0
LAUNCH_TIMEOUT_S = 20.0
LAUNCH_BACKOFF_MS = (25, 50, 100, 200)
# 等待新拉起的实例就绪时，多久报一次"仍在等待"
WAITING_NOTICE_S = 2.0
# 新拉起的实例断开管道后，等它退出、取退出码的上限（秒）：关闭了命令行控制的无窗口实例
# 会先监听管道、读到设置后才退出，断开与退出之间有一小段间隔
LAUNCHED_EXIT_WAIT_S = 2.0
# pmctl 拉起的无窗口实例空闲多久自动退出（秒）；0 为不退出
DEFAULT_IDLE_EXIT_S = 300.0
IDLE_EXIT_ARG = '--idle-exit-s'
READ_CHUNK = 65536

# ---------- Win32 常量 ----------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_BROKEN_PIPE = 109
ERROR_SEM_TIMEOUT = 121
ERROR_PIPE_BUSY = 231
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
SYNCHRONIZE = 0x00100000
SE_KERNEL_OBJECT = 6
OWNER_SECURITY_INFORMATION = 0x1
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# ---------- 文案 ----------
MSG_PIPE_NOT_FOUND = "未找到正在运行的进程监控助手（管道 {name} 不存在）"
MSG_PIPE_DENIED = "无权连接进程监控助手的管道（可能由其他用户或以管理员身份运行）"
MSG_PIPE_BUSY = "进程监控助手的管道忙，稍后再试"
MSG_CONNECT_FAILED = "连接进程监控助手失败（错误码 {code}）"
MSG_PIPE_CLOSED = "连接已被进程监控助手关闭（错误码 {code}）"
MSG_PIPE_EOF = "连接已被进程监控助手关闭"
MSG_TIMEOUT = "等待进程监控助手响应超时（{seconds:g} 秒）"
MSG_IDLE_TIMEOUT = "{seconds:g} 秒内没有收到进程监控助手的任何消息（心跳中断），按连接断开处理"
MSG_IDENTITY_PID = "服务端身份校验失败：管道属于进程 {pipe_pid}，ping 却报告 {ping_pid}"
MSG_IDENTITY_USER = "服务端身份校验失败：管道所属进程（PID {pid}）不是当前用户运行的"
MSG_IDENTITY_UNKNOWN = "无法核验服务端身份（PID {pid}）：{detail}"
MSG_LAUNCH_FAILED = "无法启动进程监控助手：{detail}"
MSG_LAUNCH_TIMEOUT = "已启动进程监控助手，但 {seconds:g} 秒内没有就绪"
MSG_WAITING_READY = "正在等待实例就绪（数据库升级中）…"
MSG_LAUNCHED_EXITED = "拉起的进程监控助手已退出（退出码 {code}）：{reason}"
# 拉起的主程序退出码 → 原因（与 app.bootstrap 的 EXIT_* 对应）
LAUNCH_EXIT_REASONS: Dict[int, str] = {
    0: "设置里已关闭命令行控制（“允许命令行与脚本控制”），请在主界面的设置里打开后重试",
    2: "数据目录不可用，详见日志",
    3: "数据库升级失败，详见日志",
    4: "IPC 监听失败（管道名可能被占用），详见日志",
}
MSG_LAUNCH_EXIT_UNKNOWN = "启动失败，详见日志"
MSG_NOT_WINDOWS = "pmctl 只支持 Windows"
MSG_BAD_REPLY = "收到无法识别的响应：{detail}"
MSG_ID_MISMATCH = "收到不属于本次请求的响应（id={got!r}，期望 {expected!r}）"


class ClientError(Exception):
    """客户端错误基类：exit_code 是 pmctl 应返回的退出码。"""
    exit_code = EXIT_ERROR


class ConnectionFailed(ClientError):
    """连接类错误（找不到管道、拒绝访问、EOF、心跳超时、身份校验失败、拉起超时）。"""
    exit_code = EXIT_CONNECTION


class PipeNotFound(ConnectionFailed):
    pass


class PipeClosed(ConnectionFailed):
    pass


class PipeTimeout(ConnectionFailed):
    pass


class IdentityError(ConnectionFailed):
    pass


class LaunchedProcessExited(ConnectionFailed):
    """拉起的实例在就绪前就退出了（exit_code 是 pmctl 的退出码，returncode 是它的退出码）。"""

    def __init__(self, returncode: int):
        reason = LAUNCH_EXIT_REASONS.get(returncode, MSG_LAUNCH_EXIT_UNKNOWN)
        super().__init__(MSG_LAUNCHED_EXITED.format(code=returncode, reason=reason))
        self.returncode = returncode


class ServerError(ClientError):
    """服务端回了 ok:false。"""

    def __init__(self, response: Response):
        super().__init__(response.error_message or response.error_code or '')
        self.response = response
        self.code = response.error_code
        self.exit_code = exit_code_for_error(response.error_code)


# ---------- Win32 封装 ----------

class _Win32:
    """kernel32 / advapi32 的最小封装（独立 WinDLL 实例，不影响其他模块的 argtypes）。"""

    def __init__(self):
        from ctypes import wintypes
        self.wintypes = wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        adv = ctypes.WinDLL('advapi32', use_last_error=True)
        self.k32, self.adv = k32, adv
        H, D, B, P = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
        self._sig(k32.CreateFileW, [wintypes.LPCWSTR, D, D, P, D, D, H], H)
        self._sig(k32.WaitNamedPipeW, [wintypes.LPCWSTR, D], B)
        self._sig(k32.ReadFile, [H, P, D, ctypes.POINTER(D), P], B)
        self._sig(k32.WriteFile, [H, P, D, ctypes.POINTER(D), P], B)
        self._sig(k32.PeekNamedPipe, [H, P, D, ctypes.POINTER(D), ctypes.POINTER(D),
                                      ctypes.POINTER(D)], B)
        self._sig(k32.CloseHandle, [H], B)
        self._sig(k32.GetNamedPipeServerProcessId, [H, ctypes.POINTER(wintypes.ULONG)], B)
        self._sig(k32.OpenProcess, [D, B, D], H)
        self._sig(k32.GetCurrentProcess, [], H)
        self._sig(k32.OpenMutexW, [D, B, wintypes.LPCWSTR], H)
        self._sig(k32.LocalFree, [P], P)
        self._sig(adv.OpenProcessToken, [H, D, ctypes.POINTER(H)], B)
        self._sig(adv.GetTokenInformation, [H, ctypes.c_int, P, D, ctypes.POINTER(D)], B)
        self._sig(adv.EqualSid, [P, P], B)
        self._sig(adv.GetSecurityInfo, [H, ctypes.c_int, D, ctypes.POINTER(P),
                                        ctypes.POINTER(P), ctypes.POINTER(P),
                                        ctypes.POINTER(P), ctypes.POINTER(P)], D)
        self.invalid_handle = wintypes.HANDLE(-1).value

    @staticmethod
    def _sig(func, argtypes, restype):
        func.argtypes = argtypes
        func.restype = restype

    @staticmethod
    def last_error() -> int:
        return ctypes.get_last_error()

    # ---- 进程令牌与 SID ----

    def token_user_sid(self, process_handle) -> Optional[bytes]:
        """进程令牌的用户 SID（原始字节）；打不开令牌时为 None。"""
        wintypes = self.wintypes
        token = wintypes.HANDLE()
        if not self.adv.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            size = wintypes.DWORD(0)
            self.adv.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if not self.adv.GetTokenInformation(token, TOKEN_USER_CLASS, buf, size,
                                                ctypes.byref(size)):
                return None
            psid = ctypes.c_void_p.from_buffer(buf).value
            return _sid_bytes(psid)
        finally:
            self.k32.CloseHandle(token)

    def process_user_sid(self, pid: int) -> Optional[bytes]:
        handle = self.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return self.token_user_sid(handle)
        finally:
            self.k32.CloseHandle(handle)

    def own_user_sid(self) -> Optional[bytes]:
        return self.token_user_sid(self.k32.GetCurrentProcess())

    def object_owner_sid(self, handle) -> Optional[bytes]:
        """内核对象（这里是管道）的所有者 SID。"""
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = self.adv.GetSecurityInfo(handle, SE_KERNEL_OBJECT, OWNER_SECURITY_INFORMATION,
                                          ctypes.byref(owner), None, None, None,
                                          ctypes.byref(descriptor))
        if status != 0:
            return None
        try:
            return _sid_bytes(owner.value)
        finally:
            if descriptor.value:
                self.k32.LocalFree(descriptor)


def _sid_bytes(psid: Optional[int]) -> Optional[bytes]:
    """SID 指针 → 原始字节（长度 = 8 + 4 × 子授权数）。"""
    if not psid:
        return None
    count = ctypes.c_ubyte.from_address(psid + 1).value
    return ctypes.string_at(psid, 8 + 4 * count)


_WIN32: Optional[_Win32] = None


def win32() -> _Win32:
    global _WIN32
    if sys.platform != 'win32':
        raise ConnectionFailed(MSG_NOT_WINDOWS)
    if _WIN32 is None:
        _WIN32 = _Win32()
    return _WIN32


def mutex_exists(full_name: str) -> bool:
    """命名互斥量是否存在（OpenMutexW 能打开，或存在但无权打开）。"""
    api = win32()
    handle = api.k32.OpenMutexW(SYNCHRONIZE, False, full_name)
    if handle:
        api.k32.CloseHandle(handle)
        return True
    return api.last_error() == ERROR_ACCESS_DENIED


def instance_starting(instance_name: str, mutex_check: Callable[[str], bool]) -> bool:
    """实例互斥量（全局名，或创建全局名失败时退回的会话局部名）是否已存在。"""
    from utils.instance_name import instance_mutex_names
    return any(mutex_check(name) for name in instance_mutex_names(instance_name))


# ---------- 传输层 ----------

class PipeTransport:
    """一条命名管道连接：按行收发字节（不关心 JSON）。"""

    def __init__(self, pipe_path: str, *, busy_wait_ms: int = BUSY_WAIT_MS,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.pipe_path = pipe_path
        self._sleep = sleep
        self._clock = clock
        self._framer = LineFramer()
        self._lines: List[bytes] = []
        self._api = win32()
        self.handle = self._open(busy_wait_ms)

    def _open(self, busy_wait_ms: int):
        api = self._api
        deadline = self._clock() + max(busy_wait_ms, 0) / 1000.0
        while True:
            handle = api.k32.CreateFileW(self.pipe_path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                         OPEN_EXISTING, 0, None)
            if handle and handle != api.invalid_handle:
                return handle
            error = api.last_error()
            if error == ERROR_FILE_NOT_FOUND:
                raise PipeNotFound(MSG_PIPE_NOT_FOUND.format(name=self.pipe_path))
            if error == ERROR_ACCESS_DENIED:
                raise ConnectionFailed(MSG_PIPE_DENIED)
            if error == ERROR_PIPE_BUSY:
                remaining = int((deadline - self._clock()) * 1000)
                if remaining > 0 and api.k32.WaitNamedPipeW(self.pipe_path, remaining):
                    continue
                if remaining > 0 and api.last_error() == ERROR_FILE_NOT_FOUND:
                    raise PipeNotFound(MSG_PIPE_NOT_FOUND.format(name=self.pipe_path))
                raise ConnectionFailed(MSG_PIPE_BUSY)
            raise ConnectionFailed(MSG_CONNECT_FAILED.format(code=error))

    def server_process_id(self) -> Optional[int]:
        pid = self._api.wintypes.ULONG(0)
        if not self._api.k32.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
            return None
        return int(pid.value)

    def close(self) -> None:
        if self.handle:
            self._api.k32.CloseHandle(self.handle)
            self.handle = None

    def write(self, data: bytes) -> None:
        api = self._api
        buf = ctypes.create_string_buffer(data, len(data))
        offset = 0
        while offset < len(data):
            written = api.wintypes.DWORD(0)
            if not api.k32.WriteFile(self.handle, ctypes.byref(buf, offset), len(data) - offset,
                                     ctypes.byref(written), None):
                error = api.last_error()
                raise PipeClosed(MSG_PIPE_CLOSED.format(code=error))
            offset += written.value

    def _available(self) -> int:
        api = self._api
        avail = api.wintypes.DWORD(0)
        if not api.k32.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(avail), None):
            error = api.last_error()
            if error in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA):
                raise PipeClosed(MSG_PIPE_EOF)
            raise PipeClosed(MSG_PIPE_CLOSED.format(code=error))
        return avail.value

    def _read_chunk(self) -> bytes:
        api = self._api
        buf = ctypes.create_string_buffer(READ_CHUNK)
        got = api.wintypes.DWORD(0)
        if not api.k32.ReadFile(self.handle, buf, READ_CHUNK, ctypes.byref(got), None):
            error = api.last_error()
            if error in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA):
                raise PipeClosed(MSG_PIPE_EOF)
            if error != ERROR_MORE_DATA:
                raise PipeClosed(MSG_PIPE_CLOSED.format(code=error))
        if not got.value:
            raise PipeClosed(MSG_PIPE_EOF)
        return buf.raw[:got.value]

    def read_line(self, timeout: Optional[float]) -> bytes:
        """读一行（不含换行符）；timeout 秒内没有完整的一行抛 PipeTimeout，None 为不限。"""
        deadline = None if timeout is None else self._clock() + timeout
        while not self._lines:
            spin_until = self._clock() + 0.002
            delay = 0.001
            while self._available() <= 0:
                now = self._clock()
                if deadline is not None and now >= deadline:
                    raise PipeTimeout(MSG_TIMEOUT.format(seconds=timeout))
                if now < spin_until:
                    self._sleep(0)
                else:
                    wait = delay if deadline is None else min(delay, deadline - now)
                    self._sleep(max(wait, 0))
                    delay = min(delay * 2, 0.02)
            try:
                self._lines.extend(self._framer.feed(self._read_chunk()))
            except ProtocolError as exc:
                raise ClientError(MSG_BAD_REPLY.format(detail=exc.message)) from None
        return self._lines.pop(0)


# ---------- 请求/响应 ----------

class IpcClient:
    """一条已连接的 IPC 会话：call() 发一条请求并等到最终回复（中途的事件交给 on_event）。"""

    def __init__(self, transport: PipeTransport):
        self.transport = transport
        self._next_id = 1
        self.server_info: Dict[str, Any] = {}

    @classmethod
    def connect(cls, pipe_path: str, **kwargs) -> "IpcClient":
        return cls(PipeTransport(pipe_path, **kwargs))

    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def send(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> int:
        request_id = self._next_id
        self._next_id += 1
        self.transport.write(protocol.encode_request(request_id, cmd, args))
        return request_id

    def receive(self, timeout: Optional[float]) -> Response:
        line = self.transport.read_line(timeout)
        try:
            return protocol.parse_response(protocol.decode_line(line))
        except ProtocolError as exc:
            raise ClientError(MSG_BAD_REPLY.format(detail=exc.message)) from None

    def request(self, cmd: str, args: Optional[Dict[str, Any]] = None, *,
                timeout: Optional[float] = None, idle_timeout: float = IDLE_TIMEOUT_S,
                on_event: Optional[Callable[[Response], None]] = None) -> Response:
        """
        发请求并返回最终回复（ok 为 True 或 False，不抛 ServerError）。

        Args:
            timeout: 总等待上限（秒）；None 为不限（长命令靠服务端的超时与心跳）
            idle_timeout: 连续多久收不到任何一行（含心跳）即按连接断开处理
        """
        request_id = self.send(cmd, args)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = idle_timeout
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise PipeTimeout(MSG_TIMEOUT.format(seconds=timeout))
                wait = min(wait, left) if wait else left
            try:
                response = self.receive(wait)
            except PipeTimeout:
                if deadline is not None and time.monotonic() >= deadline:
                    raise PipeTimeout(MSG_TIMEOUT.format(seconds=timeout)) from None
                raise PipeTimeout(MSG_IDLE_TIMEOUT.format(seconds=idle_timeout)) from None
            if response.id != request_id:
                raise ClientError(MSG_ID_MISMATCH.format(got=response.id, expected=request_id))
            if response.is_event:
                if on_event is not None:
                    on_event(response)
                continue
            return response

    def call(self, cmd: str, args: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
        """同 request，但 ok:false 抛 ServerError，成功返回 result。"""
        response = self.request(cmd, args, **kwargs)
        if not response.ok:
            raise ServerError(response)
        return response.result or {}

    # ---- 身份校验 ----

    def verify_server(self, ping_pid: Any) -> None:
        """防冒名：管道服务端 PID 必须等于 ping 报告的 pid，且该进程属于当前用户。"""
        pipe_pid = self.transport.server_process_id()
        if pipe_pid is None or not isinstance(ping_pid, int) or pipe_pid != ping_pid:
            raise IdentityError(MSG_IDENTITY_PID.format(pipe_pid=pipe_pid, ping_pid=ping_pid))
        api = win32()
        own = api.own_user_sid()
        if own is None:
            raise IdentityError(MSG_IDENTITY_UNKNOWN.format(pid=pipe_pid, detail="读不到本进程的用户"))
        other = api.process_user_sid(pipe_pid)
        if other is None:
            # 服务端以管理员身份运行时普通进程可能打不开它的令牌：退而核对管道对象的所有者
            other = api.object_owner_sid(self.transport.handle)
            if other is None:
                raise IdentityError(MSG_IDENTITY_UNKNOWN.format(
                    pid=pipe_pid, detail="读不到服务端进程与管道的所有者"))
        if other != own:
            raise IdentityError(MSG_IDENTITY_USER.format(pid=pipe_pid))


# ---------- 连接与拉起 ----------

def launch_command(data_dir: Optional[str], *, frozen: Optional[bool] = None,
                   executable: Optional[str] = None,
                   main_exe_name: Optional[str] = None,
                   project_root: Optional[str] = None,
                   idle_exit_s: Optional[float] = None) -> List[str]:
    """以 --background 拉起主程序的命令行：打包时是同目录的主程序，开发时是 python main.py。
    idle_exit_s 给出时追加 --idle-exit-s（空闲多久自动退出；0 为不退出）。"""
    if frozen is None:
        frozen = bool(getattr(sys, 'frozen', False))
    executable = executable or sys.executable
    if frozen:
        if main_exe_name is None:
            import config
            main_exe_name = config.APP_NAME + '.exe'
        command = [os.path.join(os.path.dirname(executable), main_exe_name)]
    else:
        root = project_root or os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        command = [executable, os.path.join(root, 'main.py')]
    command.append('--background')
    if idle_exit_s is not None:
        command += [IDLE_EXIT_ARG, f"{max(0.0, float(idle_exit_s)):g}"]
    if data_dir:
        command += ['--data-dir', data_dir]
    return command


def spawn_background(command: Sequence[str], *, env: Optional[Dict[str, str]] = None,
                     popen=subprocess.Popen):
    """拉起后台实例：标准输入输出全部 DEVNULL（否则 CI 捕获管道会被拖到 GUI 退出），
    不建控制台窗口、单独的进程组（pmctl 收到 Ctrl+C 不连带结束它）。"""
    try:
        return popen(list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, env=env,
                     creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    except OSError as exc:
        raise ConnectionFailed(MSG_LAUNCH_FAILED.format(detail=exc)) from None


class _Notifier:
    """每 WAITING_NOTICE_S 秒最多报一次"仍在等待"。"""

    def __init__(self, on_waiting: Optional[Callable[[str], None]]):
        self._on_waiting = on_waiting
        self._last: Optional[float] = None

    def tick(self) -> None:
        if self._on_waiting is None:
            return
        now = time.monotonic()
        if self._last is None:
            self._last = now
            return
        if now - self._last >= WAITING_NOTICE_S:
            self._last = now
            try:
                self._on_waiting(MSG_WAITING_READY)
            except Exception:
                pass


def _check_launched(proc, wait_s: float = 0.0) -> None:
    """拉起的进程已退出时抛 LaunchedProcessExited（没有 poll 的替身对象跳过）。
    wait_s > 0 时先最多等这么久让它退出（管道刚被它断开时用）。"""
    poll = getattr(proc, 'poll', None)
    if poll is None:
        return
    if wait_s > 0 and hasattr(proc, 'wait'):
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
    code = poll()
    if code is not None:
        raise LaunchedProcessExited(int(code))


def _first_ping(client: IpcClient, timeout: float, notifier: _Notifier, proc,
                timeout_message: str) -> Response:
    """发一次 ping 并等回复；timeout 秒内分段等待（每段 ≤ WAITING_NOTICE_S），每段之间
    检查拉起的进程是否已退出、并报"仍在等待"。服务端在同步升级数据库时事件循环不转，
    回复会晚到。"""
    request_id = client.send('ping')
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise PipeTimeout(timeout_message)
        try:
            response = client.receive(min(left, WAITING_NOTICE_S))
        except PipeTimeout:
            _check_launched(proc)
            notifier.tick()
            continue
        except PipeClosed:
            # 拉起的实例断开了管道：多半是它正在退出（如设置里关闭了命令行控制），
            # 等它退出后按退出码报告原因
            _check_launched(proc, LAUNCHED_EXIT_WAIT_S)
            raise
        if response.id != request_id:
            raise ClientError(MSG_ID_MISMATCH.format(got=response.id, expected=request_id))
        if response.is_event:
            continue
        return response


def _ping_until_ready(client: IpcClient, deadline: float, sleep, *,
                      notifier: Optional[_Notifier] = None, proc=None,
                      launch_timeout: float = LAUNCH_TIMEOUT_S) -> Dict[str, Any]:
    notifier = notifier or _Notifier(None)
    delays = iter(LAUNCH_BACKOFF_MS)
    delay = LAUNCH_BACKOFF_MS[0]
    while True:
        left = deadline - time.monotonic()
        response = _first_ping(client, max(left, CONNECT_TIMEOUT_S), notifier, proc,
                               MSG_LAUNCH_TIMEOUT.format(seconds=launch_timeout))
        if not response.ok:
            raise ServerError(response)
        result = response.result or {}
        if result.get('ready'):
            return result
        if time.monotonic() >= deadline:
            raise PipeTimeout(MSG_LAUNCH_TIMEOUT.format(seconds=launch_timeout))
        notifier.tick()
        delay = next(delays, LAUNCH_BACKOFF_MS[-1])
        sleep(delay / 1000.0)


def open_session(instance_name: str, *, launch: Optional[Sequence[str]] = None,
                 launch_timeout: float = LAUNCH_TIMEOUT_S,
                 verify: bool = True, wait_ready: bool = True,
                 env: Optional[Dict[str, str]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 popen=subprocess.Popen,
                 mutex_check: Callable[[str], bool] = mutex_exists,
                 on_waiting: Optional[Callable[[str], None]] = None) -> IpcClient:
    """
    连接实例（必要时拉起）、ping 到就绪并核验服务端身份，返回已连接的客户端。

    Args:
        instance_name: 实例名（utils.instance_name.single_instance_name）
        launch: GUI 不在时拉起后台实例的命令行；None 表示不拉起（--no-launch）
        launch_timeout: 拉起后等待就绪的总时长（秒）；新拉起或正在启动的实例，第一次 ping
            的等待上限也是它（实例可能在同步升级数据库）
        verify: 是否核验服务端身份（防冒名）
        wait_ready: ping 到 ready 才返回（程序刚启动时会先回 ready:false）
        on_waiting: 等待新拉起的实例就绪期间每 WAITING_NOTICE_S 秒调用一次（参数为提示文案）
    """
    from utils.instance_name import pipe_path
    path = pipe_path(instance_name)
    deadline = time.monotonic() + launch_timeout
    launched = False
    proc = None
    notifier = _Notifier(on_waiting)
    delays = iter(LAUNCH_BACKOFF_MS)
    while True:
        try:
            client = IpcClient.connect(path)
            break
        except PipeNotFound:
            if launch is None:
                raise
            if not launched and not instance_starting(instance_name, mutex_check):
                proc = spawn_background(launch, env=env, popen=popen)
            elif proc is not None:
                try:
                    _check_launched(proc)
                except LaunchedProcessExited as exc:
                    # 退出码 0 也可能是"已有实例在启动"的从实例：互斥量还在就接着等管道
                    if exc.returncode != 0 or not instance_starting(instance_name, mutex_check):
                        raise
                    proc = None
            launched = True
            if time.monotonic() >= deadline:
                raise PipeTimeout(MSG_LAUNCH_TIMEOUT.format(seconds=launch_timeout)) from None
            notifier.tick()
            sleep(next(delays, LAUNCH_BACKOFF_MS[-1]) / 1000.0)
    try:
        # 新拉起（或正在启动）的实例可能在同步升级数据库：第一次 ping 等到 launch_timeout
        if launched:
            first_timeout = max(deadline - time.monotonic(), CONNECT_TIMEOUT_S)
            message = MSG_LAUNCH_TIMEOUT.format(seconds=launch_timeout)
        else:
            first_timeout = CONNECT_TIMEOUT_S
            message = MSG_TIMEOUT.format(seconds=CONNECT_TIMEOUT_S)
        response = _first_ping(client, first_timeout,
                               notifier if launched else _Notifier(None), proc, message)
        if not response.ok:
            raise ServerError(response)
        info = response.result or {}
        if verify:
            client.verify_server(info.get('pid'))
        if wait_ready and not info.get('ready'):
            info = _ping_until_ready(client, max(deadline, time.monotonic() + CONNECT_TIMEOUT_S),
                                     sleep, notifier=notifier, proc=proc,
                                     launch_timeout=launch_timeout)
        client.server_info = info
        return client
    except BaseException:
        client.close()
        raise


__all__ = [
    'ClientError', 'ConnectionFailed', 'IdentityError', 'IpcClient', 'LaunchedProcessExited',
    'PipeClosed',
    'PipeNotFound', 'PipeTimeout', 'PipeTransport', 'ServerError', 'EVENT_HEARTBEAT',
    'exit_code_for_error', 'exit_code_for_verdict', 'launch_command', 'most_severe',
    'open_session', 'spawn_background', 'mutex_exists',
]
