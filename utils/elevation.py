"""Windows 提权启动与进程句柄，保持系统授权等待在调用线程内。"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Sequence


class ElevationCancelled(OSError):
    """用户取消 Windows 授权。"""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created: int
    user_sid: str

    def to_dict(self) -> dict:
        return {'pid': self.pid, 'created': self.created, 'user_sid': self.user_sid}


class WindowsProcess:
    """持有真实进程句柄，PID 被复用也不会改变等待对象。"""

    def __init__(self, handle: int):
        self.handle = handle
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.adv = ctypes.WinDLL('advapi32', use_last_error=True)
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessId.restype = wintypes.DWORD
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [
            wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.adv.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.adv.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        self.adv.GetLengthSid.argtypes = [ctypes.c_void_p]
        self.adv.GetLengthSid.restype = wintypes.DWORD

    @classmethod
    def open(cls, pid: int) -> 'WindowsProcess':
        if sys.platform != 'win32':
            raise OSError('管理员重启仅支持 Windows')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x00101000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return cls(handle)

    def _token_info(self, kind: int):
        token = wintypes.HANDLE()
        if not self.adv.OpenProcessToken(self.handle, 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            self.adv.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            if not self.adv.GetTokenInformation(
                    token, kind, buffer, size.value, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return buffer
        finally:
            self.kernel.CloseHandle(token)

    def identity(self) -> ProcessIdentity:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(
                self.handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = self._token_info(1)
        sid = ctypes.c_void_p.from_buffer(token_user).value
        length = self.adv.GetLengthSid(sid)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        return ProcessIdentity(
            pid=int(self.kernel.GetProcessId(self.handle)),
            created=(created.dwHighDateTime << 32) | created.dwLowDateTime,
            user_sid=ctypes.string_at(sid, length).hex())

    def is_elevated(self) -> bool:
        return bool(wintypes.DWORD.from_buffer(self._token_info(20)).value)

    def alive(self) -> bool:
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def is_elevated() -> bool:
    if sys.platform != 'win32':
        return False
    try:
        with WindowsProcess.open(os.getpid()) as process:
            return process.is_elevated()
    except OSError:
        return False


def launch_elevated(command: Sequence[str], *, cwd: str, owner: int = 0) -> WindowsProcess:
    """调用方在后台线程使用；返回的句柄须关闭，取消授权抛专用异常。"""
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG),
            ('hwnd', wintypes.HWND), ('lpVerb', wintypes.LPCWSTR),
            ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
            ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int),
            ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
            ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
            ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE),
            ('hProcess', wintypes.HANDLE)]

    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32')
    initialized = ole.CoInitializeEx(None, 2) >= 0
    try:
        info = ShellExecuteInfo()
        info.cbSize = ctypes.sizeof(info)
        # 后台线程没有消息循环，必须等待 Shell 完成进程创建并保留进程句柄。
        info.fMask = 0x00000040 | 0x00000100 | 0x00000400
        info.hwnd = owner or None
        info.lpVerb = 'runas'
        info.lpFile = command[0]
        info.lpParameters = subprocess.list2cmdline(list(command[1:]))
        info.lpDirectory = cwd
        info.nShow = 1
        shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
        shell.ShellExecuteExW.restype = wintypes.BOOL
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            error = ctypes.get_last_error()
            if error == 1223:
                raise ElevationCancelled('已取消管理员授权')
            raise ctypes.WinError(error)
        if not info.hProcess:
            raise OSError('系统未返回管理员进程句柄')
        return WindowsProcess(info.hProcess)
    finally:
        if initialized:
            ole.CoUninitialize()
