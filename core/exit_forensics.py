"""
退出取证：被测进程退出码的读取与中文解读

任务启动时对被测进程持有一个 SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION 句柄，进程退出
后句柄仍然有效，可以读到退出码——这是区分"正常退出"与"崩溃"的唯一可靠依据（psutil 只能知道
进程没了）。句柄藏在 ExitCodeProbe 接口后面，测试注入假实现；生产实现用 ctypes 调
OpenProcess / WaitForSingleObject / GetExitCodeProcess / CloseHandle。

本模块的文本与判断函数是不依赖 Qt 的纯函数：
- is_abnormal_exit：退出码是否表示异常终止（崩溃类：NTSTATUS 错误码 0xC...、预定义的警告类
  异常码 0x80000001..4、自定义异常 0xE...，如 C++ 未捕获异常）；
  普通的 0、1、taskkill 的 1 等都不算；Ctrl+C 终止（0xC000013A）与 HRESULT 形式的退出码
  （0x8007xxxx 等，程序自己用 HRESULT 当返回值）也不算，见下方"分类口径"；
- format_exit_code / crash_marker_label：把退出码写成"0xC0000005（访问冲突）"这类文本。

分类口径（退出码 = 无符号 32 位值，最高 4 位为严重性）：
- 崩溃：0xC 开头的 NTSTATUS 错误码（除 0xC000013A）；0x80000001..0x80000004 这四个预定义的
  警告类异常（保护页、未对齐、断点、单步）；0xE 开头的自定义异常（C++ 0xE06D7363、.NET
  0xE0434352 等）；
- 正常终止：0、1（taskkill /f）等程序自己返回的小整数；0xC000013A（STATUS_CONTROL_C_EXIT，
  用户按 Ctrl+C 或向控制台进程发 CTRL_C）——用户主动终止，不是崩溃，退出码照常记录并带文字
  说明；
- 其余 0x8 开头的值（0x80004005 E_FAIL、0x80070005 E_ACCESSDENIED 等 HRESULT）是程序用
  HRESULT 当退出码，不判崩溃；0x4（信息）、0x0（成功）、0xF（如 -1）同样视为程序自己的返回值。
"""
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

# ---------- 文案 ----------
TEXT_UNKNOWN_EXIT_CODE = "未知退出码"
TEXT_NORMAL_EXIT = "正常退出"
TEXT_UNKNOWN_EXCEPTION = "未知异常"
CRASH_MARKER_PREFIX = "异常退出"

# NTSTATUS / 异常码 -> 中文说明。只收常见的；表外的异常码仍判为异常，文本为"未知异常"
EXCEPTION_TEXTS = {
    0xC0000005: "访问冲突",
    0xC0000006: "页面内错误",
    0xC0000008: "无效句柄",
    0xC000001D: "非法指令",
    0xC0000017: "内存不足",
    0xC0000025: "不可继续的异常",
    0xC000008C: "数组越界",
    0xC000008E: "浮点除零",
    0xC0000090: "浮点无效操作",
    0xC0000094: "整数除零",
    0xC0000096: "特权指令",
    0xC00000FD: "栈溢出",
    0xC0000135: "缺少 DLL",
    0xC0000139: "入口点未找到",
    0xC0000142: "DLL 初始化失败",
    0xC0000194: "死锁",
    0xC0000374: "堆损坏",
    0xC0000409: "栈缓冲区溢出",
    0xC0000417: "无效参数（CRT）",
    0xC0000420: "断言失败",
    0x80000001: "保护页违规",
    0x80000002: "数据未对齐",
    0x80000003: "断点",
    0x80000004: "单步异常",
    0xE06D7363: "C++ 未捕获异常",
    0xE0434352: ".NET 未处理异常",
}

# 用户主动终止，不是崩溃：退出码照常显示并带文字，但 is_abnormal_exit 为假
TERMINATION_TEXTS = {
    0xC000013A: "被 Ctrl+C 终止",
}

# 严重性位（最高 4 位）：0xC 错误、0xE 自定义错误（如 C++ 异常 0xE06D7363）算崩溃；
# 0x8 只认预定义的警告类异常码（见 _WARNING_EXCEPTIONS），其余 0x8 值是 HRESULT。
# 其余（0x0 成功、0x4 信息、0xF 如 -1）视为程序自己返回的退出码
_ABNORMAL_SEVERITIES = frozenset({0xC, 0xE})
_WARNING_EXCEPTIONS = frozenset({0x80000001, 0x80000002, 0x80000003, 0x80000004})

_UINT32 = 0xFFFFFFFF
# GetExitCodeProcess 在进程仍在运行时返回的值；用 WaitForSingleObject 先确认进程已结束，
# 避免把"退出码恰为 259 的已结束进程"与"仍在运行"混淆
STILL_ACTIVE = 259


def normalize_exit_code(code: Optional[int]) -> Optional[int]:
    """退出码规整为无符号 32 位（-1073741819 与 0xC0000005 是同一个值）；None 原样返回。"""
    if code is None:
        return None
    return int(code) & _UINT32


def is_abnormal_exit(code: Optional[int]) -> bool:
    """退出码是否表示异常终止（崩溃类）。None（未知）不算。"""
    value = normalize_exit_code(code)
    if value is None or value in TERMINATION_TEXTS:
        return False
    return (value >> 28) in _ABNORMAL_SEVERITIES or value in _WARNING_EXCEPTIONS


def exception_text(code: Optional[int]) -> str:
    """异常码的中文说明（"访问冲突"）；退出码为 None 或不是异常码返回空串，表外异常码为"未知异常"。"""
    value = normalize_exit_code(code)
    if value is None or not is_abnormal_exit(value):
        return ""
    return EXCEPTION_TEXTS.get(value, TEXT_UNKNOWN_EXCEPTION)


def format_exit_code(code: Optional[int]) -> str:
    """退出码文本：异常码为"0xC0000005（访问冲突）"，普通退出码为十进制（"0"、"1"），None 为
    "未知退出码"。用于 CSV 元数据、证据包与日志。"""
    value = normalize_exit_code(code)
    if value is None:
        return TEXT_UNKNOWN_EXIT_CODE
    if is_abnormal_exit(value):
        return f"0x{value:08X}（{exception_text(value)}）"
    if value in TERMINATION_TEXTS:
        return f"0x{value:08X}（{TERMINATION_TEXTS[value]}）"
    if value >> 28 == 0x8:
        # HRESULT 形式（0x80070005 等）：十六进制才读得懂
        return f"0x{value:08X}"
    # 退出码 0 之外的普通值保留十进制：脚本的 exit 2 就是 2，不是 0x2
    return str(value)


def crash_marker_label(code: int) -> str:
    """异常退出系统打点的名称：异常退出 0xC0000005（访问冲突）。"""
    return f"{CRASH_MARKER_PREFIX} {format_exit_code(code)}"


# ---------------------------------------------------------------- 探针


class ExitCodeProbe(Protocol):
    """对被测进程的退出码句柄。任务启动时按 pid 取得，退出后读码，收尾时关闭。

    实现必须保证：
    - 构造 / 取得失败（权限不足、进程已不存在）时 available 为 False，不抛异常；
    - exit_code() 只在进程确已结束时返回退出码，仍在运行或读取失败返回 None；
    - close() 可重复调用，之后 exit_code() 返回 None。

    下面三个方法是可选的（调用方用 getattr 取，没有就当"不知道"）：
    - has_exited()：进程对象是否已进入"已结束"状态（零等待）。进程退出后只要还有别的句柄
      握着它（包括本探针自己），它在系统进程表里就仍以"僵尸"形式存在，按快照或进程表判断
      "还在不在"会看漏，用句柄直接问最可靠；
    - create_time()：进程创建时间（epoch 秒，GetProcessTimes），用来核对句柄打开的确实是
      预期的进程（PID 在取句柄前一刻被复用时不一致）；
    - exit_time()：进程退出时刻（epoch 秒，GetProcessTimes），仅在已结束后有值；续接时据此
      排除"早于旧进程退出"的同名进程。
    """

    @property
    def available(self) -> bool:
        ...

    def exit_code(self) -> Optional[int]:
        ...

    def has_exited(self) -> bool:
        ...

    def create_time(self) -> Optional[float]:
        ...

    def exit_time(self) -> Optional[float]:
        ...

    def close(self) -> None:
        ...


class NullExitCodeProbe:
    """取不到句柄时的占位：永远读不到退出码（对应"未知退出码"）。"""

    available = False

    def exit_code(self) -> Optional[int]:
        return None

    def has_exited(self) -> bool:
        return False

    def create_time(self) -> Optional[float]:
        return None

    def exit_time(self) -> Optional[float]:
        return None

    def close(self) -> None:
        return None


_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_win32 = None
# FILETIME（1601-01-01 起的 100 纳秒数）到 epoch 秒的偏移，与 psutil 的创建时间同一口径
_FILETIME_EPOCH_OFFSET = 116444736000000000
_TICKS_PER_SECOND = 10_000_000


def _kernel32():
    """惰性声明 Win32 原型（显式 argtypes/restype，避免 64 位句柄被截断）。"""
    global _win32
    if _win32 is not None:
        return _win32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME)]
    k32.GetProcessTimes.restype = wintypes.BOOL
    _win32 = k32
    return k32


def _filetime_ticks(value) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


class Win32ExitCodeProbe:
    """生产实现：OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)。

    取不到句柄（如目标是提权进程而本工具不是）时 available 为 False——退出后按"未知退出码"
    处理，任务照常工作（对应日志由采集线程按任务记一次）。
    """

    def __init__(self, pid: int):
        self._pid = pid
        self._handle = None
        if sys.platform != "win32" or not pid or pid <= 0:
            return
        try:
            k32 = _kernel32()
            handle = k32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                                     False, pid)
            if handle:
                self._handle = handle
            else:
                logger.debug("无法打开进程 %s 的退出码句柄（错误码 %s）",
                             pid, ctypes.get_last_error())
        except Exception:
            logger.info("打开进程 %s 的退出码句柄失败", pid, exc_info=True)

    @property
    def available(self) -> bool:
        return self._handle is not None

    def exit_code(self) -> Optional[int]:
        handle = self._handle
        if handle is None:
            return None
        try:
            k32 = _kernel32()
            # 零等待：只有进程对象已进入"已结束"状态才读退出码
            if k32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0:
                return None
            value = wintypes.DWORD(0)
            if not k32.GetExitCodeProcess(handle, ctypes.byref(value)):
                return None
            return int(value.value)
        except Exception:
            logger.info("读取进程 %s 的退出码失败", self._pid, exc_info=True)
            return None

    def has_exited(self) -> bool:
        handle = self._handle
        if handle is None:
            return False
        try:
            return _kernel32().WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0
        except Exception:
            logger.info("查询进程 %s 是否已结束失败", self._pid, exc_info=True)
            return False

    def _times(self):
        """GetProcessTimes 的 (创建, 退出) 两个 FILETIME 整数；失败为 None。"""
        handle = self._handle
        if handle is None:
            return None
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _kernel32().GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                               ctypes.byref(kernel), ctypes.byref(user)):
                return None
            return (_filetime_ticks(created), _filetime_ticks(exited))
        except Exception:
            logger.info("读取进程 %s 的时间失败", self._pid, exc_info=True)
            return None

    def create_time(self) -> Optional[float]:
        times = self._times()
        if times is None or times[0] <= 0:
            return None
        return (times[0] - _FILETIME_EPOCH_OFFSET) / _TICKS_PER_SECOND

    def exit_time(self) -> Optional[float]:
        times = self._times()
        # 进程还在运行时退出时间为 0
        if times is None or times[1] <= 0 or not self.has_exited():
            return None
        return (times[1] - _FILETIME_EPOCH_OFFSET) / _TICKS_PER_SECOND

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _kernel32().CloseHandle(handle)
        except Exception:
            logger.info("关闭进程 %s 的退出码句柄失败", self._pid, exc_info=True)


def open_exit_probe(pid: int) -> ExitCodeProbe:
    """生产用的探针工厂（MonitorWorker 的默认 exit_probe_factory）。"""
    return Win32ExitCodeProbe(pid)
