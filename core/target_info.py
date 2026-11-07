"""
被测目标元数据探针

任务开始时记录"测的是哪个程序、在什么环境下测的"：被测 exe 的路径、文件版本资源、
命令行、进程创建时间、是否提权，以及本工具版本、是否管理员、操作系统、CPU、内存与
显示缩放。结果是不可变的 TargetInfo，序列化为 JSON 存进 tasks.meta_json，供报告
元数据行、证据包（core.evidence）与任务悬停提示使用。

探针的原则：任一字段失败只留 None，绝不抛出——被测进程可能已退出、可能以更高权限
运行（读不到路径与命令行）、exe 可能没有版本资源；这些都不应阻止任务开始。

本模块不依赖 Qt：显示缩放由界面层取得后经 dpi_scale 传入。版本资源经 ctypes 调用
version.dll（GetFileVersionInfoSizeW / GetFileVersionInfoW / VerQueryValueW），被测
进程是否提权经 OpenProcessToken + GetTokenInformation(TokenElevation) 读取。
"""
import ctypes
import json
import logging
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

import psutil

import config

logger = logging.getLogger(__name__)

T = TypeVar('T')

TARGET_INFO_SCHEMA_VERSION = 1

# Windows 11 的内部版本号从 22000 起；Python 3.11 及更早的 platform.win32_ver()
# 在 Windows 11 上仍报告 release '10'，需按版本号纠正
WIN11_FIRST_BUILD = 22000

# 版本资源里要读的字符串项 → TargetInfo 字段
_VERSION_STRING_KEYS = (
    ('FileVersion', 'file_version'),
    ('ProductVersion', 'product_version'),
    ('ProductName', 'product_name'),
    ('CompanyName', 'company'),
)
# 首个翻译项读不到字符串时依次尝试的常见"语言+代码页"
_FALLBACK_TRANSLATIONS = ('040904b0', '040904e4', '080404b0', '000004b0')
_VS_FIXEDFILEINFO_SIGNATURE = 0xFEEF04BD

_CPU_REGISTRY_KEY = r'HARDWARE\DESCRIPTION\System\CentralProcessor\0'
_CPU_REGISTRY_VALUE = 'ProcessorNameString'

# 摘要行里命令行的最大字符数（完整命令行在 JSON 里）
CMDLINE_SUMMARY_MAX_CHARS = 300

# ---- 摘要文案 ----
LABEL_TARGET = "被测程序"
LABEL_CMDLINE = "命令行"
LABEL_ENV = "环境"
TEXT_UNKNOWN_PATH = "未知（无权读取或进程已退出）"
TEXT_FILE_VERSION = "文件版本 {version}"
TEXT_TARGET_ELEVATED = "以管理员身份运行"
TEXT_CORES = "{count} 核"
TEXT_MEMORY = "{size} GB"
TEXT_TOOL = "工具 {version}"
TEXT_TOOL_ADMIN = "（管理员）"
TEXT_TOOL_NOT_ADMIN = "（非管理员）"
TEXT_DPI = "缩放 {percent}%"
TEXT_ELLIPSIS = "…"
SUMMARY_SEPARATOR = " · "


def _iso(value: Optional[datetime]) -> Optional[str]:
    # 不截断微秒：与 from_dict 往返无损
    return value.isoformat() if value is not None else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class TargetInfo:
    """
    被测目标与测试环境的元数据。所有字段都可能为 None（取不到）。

    - cmdline：psutil 给出的参数列表（存为元组），从旧数据读回时也可能是整串字符串；
    - create_time / captured_at：本地时间（naive datetime），create_time 为被测进程创建时刻；
    - is_admin_target：被测进程是否提权运行（令牌 TokenElevation）；
    - tool_is_admin：本程序是否以管理员身份运行（读不到提权进程的路径时据此解释原因）；
    - dpi_scale：显示缩放倍数（1.5 表示 150%），由界面层传入。
    """
    pid: Optional[int] = None
    exe_path: Optional[str] = None
    file_version: Optional[str] = None
    product_version: Optional[str] = None
    product_name: Optional[str] = None
    company: Optional[str] = None
    cmdline: Union[Tuple[str, ...], str, None] = None
    create_time: Optional[datetime] = None
    is_admin_target: Optional[bool] = None
    tool_version: Optional[str] = None
    tool_is_admin: Optional[bool] = None
    os_version: Optional[str] = None
    cpu: Optional[str] = None
    logical_cores: Optional[int] = None
    total_memory_bytes: Optional[int] = None
    dpi_scale: Optional[float] = None
    captured_at: Optional[datetime] = None

    # ---------- 序列化 ----------

    def to_dict(self) -> Dict[str, Any]:
        """JSON 友好的字典（时间为 ISO 本地时间，命令行为列表），带 schema_version。"""
        data = asdict(self)
        data['create_time'] = _iso(self.create_time)
        data['captured_at'] = _iso(self.captured_at)
        if isinstance(self.cmdline, tuple):
            data['cmdline'] = list(self.cmdline)
        return {'schema_version': TARGET_INFO_SCHEMA_VERSION, **data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TargetInfo':
        """to_dict 的逆；未知键忽略、缺失键为 None（新旧版本互读）。"""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values['create_time'] = _parse_iso(values.get('create_time'))
        values['captured_at'] = _parse_iso(values.get('captured_at'))
        cmdline = values.get('cmdline')
        if isinstance(cmdline, list):
            values['cmdline'] = tuple(str(part) for part in cmdline)
        elif cmdline is not None and not isinstance(cmdline, str):
            values['cmdline'] = None
        return cls(**values)

    @classmethod
    def from_json(cls, text: str) -> 'TargetInfo':
        """解析 to_json 的结果；不是 JSON 对象时抛 ValueError（json.JSONDecodeError 是其子类）。"""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("被测元数据不是 JSON 对象")
        return cls.from_dict(data)

    # ---------- 展示 ----------

    def cmdline_text(self) -> Optional[str]:
        """命令行的单行文本（参数列表按 Windows 规则拼接）；没有命令行时为 None。"""
        if self.cmdline is None:
            return None
        text = (self.cmdline if isinstance(self.cmdline, str)
                else subprocess.list2cmdline(list(self.cmdline)))
        return _one_line(text) or None

    def summary_lines(self) -> List[str]:
        """
        中文摘要行（不带前缀），用于 CSV 元数据、报告与悬停提示：
        "被测程序: C:\\...\\app.exe（文件版本 1.2.3.4）"、"命令行: …"、
        "环境: Windows 11 10.0.26200 · 16 核 · 32 GB · 工具 2.0.0-beta.1（管理员）"。
        取不到的部分省略；整行都没有内容时不出该行。
        """
        lines: List[str] = []
        notes = []
        if self.file_version:
            notes.append(TEXT_FILE_VERSION.format(version=_one_line(self.file_version)))
        if self.is_admin_target:
            notes.append(TEXT_TARGET_ELEVATED)
        if self.exe_path or notes:
            path = _one_line(self.exe_path) if self.exe_path else TEXT_UNKNOWN_PATH
            suffix = f"（{'，'.join(notes)}）" if notes else ""
            lines.append(f"{LABEL_TARGET}: {path}{suffix}")

        cmdline = self.cmdline_text()
        if cmdline:
            if len(cmdline) > CMDLINE_SUMMARY_MAX_CHARS:
                cmdline = cmdline[:CMDLINE_SUMMARY_MAX_CHARS] + TEXT_ELLIPSIS
            lines.append(f"{LABEL_CMDLINE}: {cmdline}")

        env = []
        if self.os_version:
            env.append(_one_line(self.os_version))
        if self.logical_cores:
            env.append(TEXT_CORES.format(count=self.logical_cores))
        if self.total_memory_bytes:
            env.append(TEXT_MEMORY.format(size=_memory_gb_text(self.total_memory_bytes)))
        if self.dpi_scale:
            env.append(TEXT_DPI.format(percent=round(self.dpi_scale * 100)))
        if self.tool_version:
            admin = {True: TEXT_TOOL_ADMIN, False: TEXT_TOOL_NOT_ADMIN}.get(self.tool_is_admin, "")
            env.append(TEXT_TOOL.format(version=self.tool_version) + admin)
        if env:
            lines.append(f"{LABEL_ENV}: {SUMMARY_SEPARATOR.join(env)}")
        return lines


def _one_line(text: Optional[str]) -> str:
    """去掉换行与首尾空白：摘要行会写进以 "#" 开头的 CSV 元数据行，换行会破坏格式。"""
    if not text:
        return ''
    return str(text).replace('\r', ' ').replace('\n', ' ').strip()


def _memory_gb_text(total_bytes: int) -> str:
    """内存容量（GiB）：10 以上取整，否则保留一位小数并去掉 ".0"。"""
    gb = total_bytes / (1024 ** 3)
    text = f"{gb:.0f}" if gb >= 10 else f"{gb:.1f}"
    return text[:-2] if text.endswith('.0') else text


def target_info_from_json_or_none(text: Optional[str]) -> Optional[TargetInfo]:
    """tasks.meta_json → TargetInfo；为空或解析失败返回 None（只记调试日志）。"""
    if not text:
        return None
    try:
        return TargetInfo.from_json(text)
    except (ValueError, TypeError):
        logger.debug("被测元数据解析失败，已忽略", exc_info=True)
        return None


# ---------------------------------------------------------------- 探针


def _safe(label: str, func: Callable[[], T]) -> Optional[T]:
    """执行一个字段的取值；任何异常都只记调试日志并返回 None。"""
    try:
        return func()
    except Exception:   # noqa: BLE001 —— 探针字段失败一律留空，不影响任务开始
        logger.debug("目标元数据字段读取失败: %s", label, exc_info=True)
        return None


def probe(pid: int, *, clock, dpi_scale: Optional[float] = None) -> TargetInfo:
    """
    探测被测进程与本机环境，返回 TargetInfo。任一字段失败只留 None，不抛出。

    Args:
        pid: 被测进程 PID（进程不存在时进程相关字段全为 None，环境字段照常）
        clock: 时钟（只用 now()），captured_at 取自它
        dpi_scale: 显示缩放倍数，由界面层传入；缺省为 None
    """
    process = _safe('process', lambda: psutil.Process(pid))
    exe_path = cmdline = create_time = None
    if process is not None:
        exe_path = _safe('exe', lambda: process.exe() or None)
        cmdline = _safe('cmdline', lambda: tuple(process.cmdline()) or None)
        # System 等内核进程的创建时间报告为 0（1970 年），没有意义，留空
        create_time = _safe('create_time',
                            lambda: datetime.fromtimestamp(process.create_time())
                            if process.create_time() > 0 else None)
    version = (_safe('version_resource', lambda: read_version_info(exe_path))
               if exe_path else None) or {}
    memory = _safe('total_memory', lambda: int(psutil.virtual_memory().total))
    return TargetInfo(
        pid=pid,
        exe_path=exe_path,
        file_version=version.get('file_version'),
        product_version=version.get('product_version'),
        product_name=version.get('product_name'),
        company=version.get('company'),
        cmdline=cmdline,
        create_time=create_time,
        is_admin_target=(_safe('target_elevation', lambda: process_is_elevated(pid))
                         if process is not None else None),
        tool_version=config.APP_VERSION,
        tool_is_admin=_safe('tool_admin', current_user_is_admin),
        os_version=_safe('os_version', os_version_text),
        cpu=_safe('cpu', cpu_name),
        logical_cores=_safe('logical_cores', lambda: psutil.cpu_count(logical=True)
                            or os.cpu_count()),
        total_memory_bytes=memory,
        dpi_scale=dpi_scale,
        captured_at=_safe('captured_at', clock.now),
    )


# ---------------------------------------------------------------- 版本资源


def _version_api():
    """version.dll 的三个函数（带参数类型，64 位下指针不被截断）。"""
    from ctypes import wintypes
    version = ctypes.WinDLL('version', use_last_error=True)
    size_fn = version.GetFileVersionInfoSizeW
    size_fn.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    size_fn.restype = wintypes.DWORD
    info_fn = version.GetFileVersionInfoW
    info_fn.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    info_fn.restype = wintypes.BOOL
    query_fn = version.VerQueryValueW
    query_fn.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR,
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT))
    query_fn.restype = wintypes.BOOL
    return size_fn, info_fn, query_fn


def _four_part(ms: int, ls: int) -> str:
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def read_version_info(path: str) -> Dict[str, Optional[str]]:
    """
    读取 exe/dll 的版本资源：file_version / product_version / product_name / company。

    先取 \\VarFileInfo\\Translation 的第一组"语言+代码页"读 StringFileInfo 下的字符串，
    读不到再试几组常见组合；文件版本与产品版本最后用 VS_FIXEDFILEINFO 的数值版本兜底。
    文件没有版本资源时返回各项为 None 的字典；调用失败（非 Windows 等）抛异常，
    由调用方的 _safe 吞掉。
    """
    from ctypes import wintypes
    result: Dict[str, Optional[str]] = {name: None for _, name in _VERSION_STRING_KEYS}
    size_fn, info_fn, query_fn = _version_api()
    size = size_fn(path, None)
    if not size:
        return result
    buffer = ctypes.create_string_buffer(size)
    if not info_fn(path, 0, size, buffer):
        return result

    def query(sub_block: str) -> Tuple[Optional[int], int]:
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not query_fn(buffer, sub_block, ctypes.byref(pointer), ctypes.byref(length)):
            return None, 0
        return pointer.value, length.value

    translations: List[str] = []
    pointer, length = query('\\VarFileInfo\\Translation')
    if pointer and length >= 4:
        words = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
        translations.append(f"{words[0]:04x}{words[1]:04x}")
    translations += [t for t in _FALLBACK_TRANSLATIONS if t not in translations]

    for key, name in _VERSION_STRING_KEYS:
        for lang in translations:
            pointer, length = query(f'\\StringFileInfo\\{lang}\\{key}')
            if pointer and length:
                text = ctypes.wstring_at(pointer, length).split('\0', 1)[0].strip()
                if text:
                    result[name] = text
                    break

    if result['file_version'] is None or result['product_version'] is None:
        pointer, length = query('\\')
        if pointer and length >= 52:
            values = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
            if values[0] == _VS_FIXEDFILEINFO_SIGNATURE:
                # VS_FIXEDFILEINFO：签名、结构版本、文件版本高/低、产品版本高/低……
                if result['file_version'] is None:
                    result['file_version'] = _four_part(values[2], values[3])
                if result['product_version'] is None:
                    result['product_version'] = _four_part(values[4], values[5])
    return result


# ---------------------------------------------------------------- 权限


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_CLASS = 20


def process_is_elevated(pid: int) -> Optional[bool]:
    """被测进程的令牌是否提权；打不开进程或令牌（权限不足、已退出）时为 None。"""
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE))
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation, returned = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                    token, _TOKEN_ELEVATION_CLASS, ctypes.byref(elevation),
                    ctypes.sizeof(elevation), ctypes.byref(returned)):
                return None
            return bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def current_user_is_admin() -> Optional[bool]:
    """本程序是否以管理员身份运行（shell32.IsUserAnAdmin）。"""
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


# ---------------------------------------------------------------- 环境


def os_version_text() -> Optional[str]:
    """如 "Windows 11 10.0.26200"；非 Windows 时退回 platform.platform()。"""
    release, version, _, _ = platform.win32_ver()
    if not version:
        return platform.platform() or None
    try:
        build = int(version.split('.')[2])
    except (IndexError, ValueError):
        build = 0
    if release == '10' and build >= WIN11_FIRST_BUILD:
        release = '11'
    return f"Windows {release} {version}".replace('  ', ' ').strip()


def cpu_name() -> Optional[str]:
    """CPU 型号：优先注册表 ProcessorNameString（如 "Intel(R) Core(TM) i7-..."），
    读不到用 platform.processor()。"""
    name = None
    if sys.platform == 'win32':
        name = _safe('cpu_registry', _cpu_name_from_registry)
    return (name or platform.processor() or '').strip() or None


def _cpu_name_from_registry() -> Optional[str]:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU_REGISTRY_KEY) as key:
        value, _ = winreg.QueryValueEx(key, _CPU_REGISTRY_VALUE)
    return str(value).strip() or None
