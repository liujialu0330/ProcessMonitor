"""
启动被测程序（ProcessLauncher）

"开始监控"可以先启动被测程序：用拿到的 PID 与创建时间建任务，进程身份从一开始就是确定的
（不必再到进程列表里找，也不会选错同名进程）。可选"停止时关闭被测程序"：用户停止任务后
先给它的顶层窗口发 WM_CLOSE（让程序按正常流程退出、保存数据），等一会儿不退再结束进程。

- LaunchSpec：要启动什么（程序完整路径、参数列表、工作目录），可 JSON 往返（模板会存它）；
- ProcessLauncher.launch(spec) -> LaunchResult：subprocess.Popen 启动（不捕获输出、不建新
  控制台以外的任何特殊标志，继承本程序的桌面与权限；工作目录缺省为程序所在目录），随后
  用 psutil 取创建时间；文件不存在、拒绝访问、需要管理员权限、启动后立即退出都返回中文
  error，不抛异常；
- ProcessLauncher.terminate(pid, create_time)：先比对创建时间（不一致说明 PID 已被别的进程
  复用，一概不动），再发 WM_CLOSE、等 close_timeout_s 秒，仍在运行才 TerminateProcess。
  terminate 会阻塞数秒，调用方放到后台线程（core.workers.terminate_worker）。

本模块顶层只导入标准库（pmctl 也用 split_args，不能因此带进 psutil / PyQt5）。
"""
import ctypes
import json
import logging
import ntpath
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 启动后观察多久判断"立即退出"（秒）：缺 DLL、参数不对的程序通常在这段时间内就退出了
SETTLE_SECONDS = 0.3
# 发 WM_CLOSE 后等待程序自行退出的时长（秒）
CLOSE_TIMEOUT_SECONDS = 5.0
# TerminateProcess 之后确认退出的等待时长（秒）
KILL_WAIT_SECONDS = 2.0
# 创建时间比对的容差（秒），与任务线程识别 PID 复用的口径一致
CREATE_TIME_TOLERANCE = 0.01

# terminate 的结果
TERMINATE_CLOSED = 'closed'        # 收到 WM_CLOSE 后自行退出
TERMINATE_KILLED = 'killed'        # 等待超时后被结束
TERMINATE_GONE = 'gone'            # 进程本来就不在了
TERMINATE_REUSED = 'reused'        # PID 已被别的进程复用，没有动它
TERMINATE_FAILED = 'failed'        # 没能结束（拒绝访问等）

# ---------- 文案 ----------
ERR_EXE_EMPTY = "请填写要启动的程序"
ERR_EXE_NOT_ABSOLUTE = "程序路径必须是完整的绝对路径：{exe}"
ERR_EXE_NOT_FOUND = "找不到程序：{exe}"
ERR_CWD_NOT_FOUND = "工作目录不存在：{cwd}"
ERR_ACCESS_DENIED = "没有权限启动程序：{exe}"
ERR_ELEVATION_REQUIRED = "该程序需要管理员权限才能启动；请以管理员身份运行进程监控助手"
ERR_BAD_EXE = "不是有效的 Windows 程序：{exe}"
ERR_START_FAILED = "启动程序失败：{detail}"
ERR_EXITED_AT_ONCE = "程序启动后立即退出（退出码 {code}），请检查参数与工作目录"
ERR_NO_IDENTITY = "程序已启动（PID {pid}），但读不到它的进程信息：{detail}"

# Windows 错误码
_ERROR_ACCESS_DENIED = 5
_ERROR_BAD_EXE_FORMAT = 193
_ERROR_ELEVATION_REQUIRED = 740

_WM_CLOSE = 0x0010
_GW_OWNER = 4


class LaunchFormatError(ValueError):
    """LaunchSpec 的 JSON / 字典内容不合法。"""


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return token[1:-1]
    return token


def split_args(text: Optional[str]) -> List[str]:
    """把一行参数文本拆成参数列表：空白分隔，双引号括起的部分算一个参数（引号去掉），
    反斜杠原样保留（Windows 路径）。"""
    if not text or not text.strip():
        return []
    import shlex
    lexer = shlex.shlex(text, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ''
    return [_unquote(token) for token in lexer]


def join_args(args: Sequence[str]) -> str:
    """参数列表写回一行文本（含空白的参数加双引号），与 split_args 互逆。"""
    return " ".join(f'"{arg}"' if (not arg or any(c.isspace() for c in arg)) else arg
                    for arg in args)


@dataclass(frozen=True)
class LaunchSpec:
    """要启动的程序。args 为参数列表（不含程序本身）；cwd 为 None 时用程序所在目录。"""
    exe: str
    args: Tuple[str, ...] = ()
    cwd: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, 'exe', (self.exe or '').strip())
        object.__setattr__(self, 'args', tuple(self.args or ()))
        cwd = (self.cwd or '').strip() or None
        object.__setattr__(self, 'cwd', cwd)

    @property
    def name(self) -> str:
        """程序文件名（如 demo.exe），建任务前的显示名。"""
        return ntpath.basename(self.exe) or self.exe

    def effective_cwd(self) -> str:
        return self.cwd or os.path.dirname(self.exe)

    def command_line(self) -> str:
        """完整命令行（日志与提示用）。"""
        return subprocess.list2cmdline([self.exe, *self.args])

    # ---------- JSON 往返 ----------

    def to_dict(self) -> Dict[str, Any]:
        return {'exe': self.exe, 'args': list(self.args), 'cwd': self.cwd}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Any) -> "LaunchSpec":
        if not isinstance(data, dict):
            raise LaunchFormatError("launch 必须是 JSON 对象")
        unknown = set(data) - {'exe', 'args', 'cwd'}
        if unknown:
            raise LaunchFormatError(f"launch 含未知字段：{'、'.join(sorted(unknown))}")
        exe = data.get('exe')
        if not isinstance(exe, str) or not exe.strip():
            raise LaunchFormatError("launch.exe 必须是非空字符串")
        args = data.get('args', [])
        if args is None:
            args = []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise LaunchFormatError("launch.args 必须是字符串数组")
        cwd = data.get('cwd')
        if cwd is not None and not isinstance(cwd, str):
            raise LaunchFormatError("launch.cwd 必须是字符串")
        return cls(exe=exe, args=tuple(args), cwd=cwd)

    @classmethod
    def from_json(cls, text: str) -> "LaunchSpec":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LaunchFormatError(f"launch 不是合法的 JSON：{exc}") from None
        return cls.from_dict(data)


@dataclass(frozen=True)
class LaunchResult:
    """启动结果：成功时 pid / create_time / name 有值、error 为 None；失败时只有 error。"""
    pid: Optional[int] = None
    create_time: Optional[float] = None
    error: Optional[str] = None
    # 进程名（psutil 读到的，如 demo.exe）；读不到时为程序文件名
    name: str = ''

    @property
    def ok(self) -> bool:
        return self.error is None and self.pid is not None


def _is_absolute(path: str) -> bool:
    return bool(ntpath.splitdrive(path)[0] and ntpath.isabs(path)) or (
        os.name != 'nt' and os.path.isabs(path))


def validate_spec(spec: LaunchSpec) -> Optional[str]:
    """启动前的检查：路径为空 / 不是绝对路径 / 文件不存在 / 工作目录不存在时返回原因。"""
    if not spec.exe:
        return ERR_EXE_EMPTY
    if not _is_absolute(spec.exe):
        return ERR_EXE_NOT_ABSOLUTE.format(exe=spec.exe)
    if not os.path.isfile(spec.exe):
        return ERR_EXE_NOT_FOUND.format(exe=spec.exe)
    if spec.cwd and not os.path.isdir(spec.cwd):
        return ERR_CWD_NOT_FOUND.format(cwd=spec.cwd)
    return None


def _os_error_text(exc: OSError, spec: LaunchSpec) -> str:
    winerror = getattr(exc, 'winerror', None)
    if winerror == _ERROR_ELEVATION_REQUIRED:
        return ERR_ELEVATION_REQUIRED
    if winerror == _ERROR_BAD_EXE_FORMAT:
        return ERR_BAD_EXE.format(exe=spec.exe)
    if isinstance(exc, FileNotFoundError):
        return ERR_EXE_NOT_FOUND.format(exe=spec.exe)
    if isinstance(exc, PermissionError) or winerror == _ERROR_ACCESS_DENIED:
        return ERR_ACCESS_DENIED.format(exe=spec.exe)
    return ERR_START_FAILED.format(detail=exc.strerror or exc)


# ---------- 顶层窗口 ----------

def top_level_windows(pid: int) -> List[int]:
    """属于该进程、可见且无所有者的顶层窗口句柄（只在 Windows 上有，其余平台为空）。
    隐藏的辅助窗口（输入法、消息窗口）不算：给它们发 WM_CLOSE 没有意义甚至有害。"""
    if sys.platform != 'win32':
        return []
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    found: List[int] = []

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if (owner.value == pid and user32.IsWindowVisible(hwnd)
                and not user32.GetWindow(hwnd, _GW_OWNER)):
            found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found


def post_close(hwnds: Sequence[int]) -> int:
    """给这些窗口各发一个 WM_CLOSE（异步投递，不等待处理），返回投递成功的个数。"""
    if sys.platform != 'win32' or not hwnds:
        return 0
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                    wintypes.LPARAM]
    return sum(1 for hwnd in hwnds if user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))


class ProcessLauncher:
    """启动与关闭被测程序（生产实现）。launch 在界面线程调用（只短暂等待 settle_s），
    terminate 会阻塞到 close_timeout_s，须在后台线程调用。

    Args:
        popen: 启动进程的函数（同 subprocess.Popen）
        settle_s: 启动后观察多久判断"立即退出"
        close_timeout_s: 发 WM_CLOSE 后等待自行退出的时长
        window_finder / closer: 枚举顶层窗口、投递 WM_CLOSE（测试可替换）
    """

    def __init__(self, *, popen: Callable[..., Any] = subprocess.Popen,
                 settle_s: float = SETTLE_SECONDS,
                 close_timeout_s: float = CLOSE_TIMEOUT_SECONDS,
                 window_finder: Callable[[int], List[int]] = top_level_windows,
                 closer: Callable[[Sequence[int]], int] = post_close):
        self._popen = popen
        self.settle_s = max(0.0, float(settle_s))
        self.close_timeout_s = max(0.0, float(close_timeout_s))
        self._find_windows = window_finder
        self._post_close = closer
        # 启动过的子进程对象：持有引用直到确认退出，免得 Popen 析构时告警，也便于回收
        self._children: Dict[int, Any] = {}
        self._lock = threading.Lock()

    # ---------- 启动 ----------

    def launch(self, spec: LaunchSpec) -> LaunchResult:
        problem = validate_spec(spec)
        if problem is not None:
            return LaunchResult(error=problem)
        command = [spec.exe, *spec.args]
        try:
            # 不捕获输出、不改控制台与优先级：被测程序应当像用户自己双击启动的一样
            proc = self._popen(command, cwd=spec.effective_cwd(), creationflags=0,
                               close_fds=True)
        except OSError as exc:
            logger.warning("启动被测程序失败: %s", spec.command_line(), exc_info=True)
            return LaunchResult(error=_os_error_text(exc, spec))
        except (ValueError, TypeError) as exc:
            logger.warning("启动被测程序失败: %s", spec.command_line(), exc_info=True)
            return LaunchResult(error=ERR_START_FAILED.format(detail=exc))
        pid = proc.pid
        with self._lock:
            self._children[pid] = proc
        if self.settle_s > 0:
            try:
                code = proc.wait(timeout=self.settle_s)
            except subprocess.TimeoutExpired:
                code = None
        else:
            code = proc.poll()
        if code is not None:
            self._forget(pid)
            logger.warning("被测程序启动后立即退出: %s 退出码=%s", spec.command_line(), code)
            return LaunchResult(error=ERR_EXITED_AT_ONCE.format(code=code))
        import psutil
        try:
            process = psutil.Process(pid)
            create_time = process.create_time()
            name = process.name() or spec.name
        except psutil.NoSuchProcess:
            self._forget(pid)
            code = proc.poll()
            return LaunchResult(error=ERR_EXITED_AT_ONCE.format(
                code=code if code is not None else '未知'))
        except (psutil.AccessDenied, OSError) as exc:
            # 已经在运行，却读不到身份：交给用户处理，不悄悄监控一个身份不明的进程
            logger.warning("读取被测程序进程信息失败: pid=%s", pid, exc_info=True)
            return LaunchResult(error=ERR_NO_IDENTITY.format(pid=pid, detail=exc))
        logger.info("已启动被测程序: pid=%s %s（工作目录 %s）", pid, spec.command_line(),
                    spec.effective_cwd())
        return LaunchResult(pid=pid, create_time=create_time, name=name)

    def _forget(self, pid: int) -> None:
        with self._lock:
            self._children.pop(pid, None)

    # ---------- 关闭 ----------

    def terminate(self, pid: int, create_time: Optional[float]) -> str:
        """关闭被测程序：PID 复用保护 → WM_CLOSE → 等待 → TerminateProcess。
        返回 TERMINATE_*；任何情况都不抛异常。阻塞，须在后台线程调用。
        没有创建时间就无法确认身份，宁可不关（返回 TERMINATE_FAILED）。"""
        import psutil
        if create_time is None:
            logger.warning("被测程序缺少创建时间，无法确认身份，不关闭: pid=%s", pid)
            return TERMINATE_FAILED
        try:
            process = psutil.Process(pid)
            actual = process.create_time()
        except psutil.NoSuchProcess:
            self._forget(pid)
            return TERMINATE_GONE
        except (psutil.AccessDenied, OSError):
            logger.warning("读取被测程序进程信息失败，不关闭: pid=%s", pid, exc_info=True)
            return TERMINATE_FAILED
        if abs(actual - create_time) > CREATE_TIME_TOLERANCE:
            logger.warning("PID %s 已被别的进程复用（创建时间 %.3f ≠ %.3f），不关闭",
                           pid, actual, create_time)
            self._forget(pid)
            return TERMINATE_REUSED

        try:
            windows = self._find_windows(pid)
        except Exception:
            logger.warning("枚举被测程序窗口失败: pid=%s", pid, exc_info=True)
            windows = []
        if windows and self._post_close(windows):
            logger.info("已请求被测程序关闭窗口: pid=%s 窗口 %d 个", pid, len(windows))
            try:
                process.wait(timeout=self.close_timeout_s)
                self._forget(pid)
                logger.info("被测程序已自行退出: pid=%s", pid)
                return TERMINATE_CLOSED
            except psutil.TimeoutExpired:
                logger.info("被测程序 %.0f 秒内没有退出，结束进程: pid=%s",
                            self.close_timeout_s, pid)
            except psutil.NoSuchProcess:
                self._forget(pid)
                return TERMINATE_CLOSED
        return self._kill(process, pid)

    def _kill(self, process, pid: int) -> str:
        import psutil
        try:
            # psutil 结束进程前会再核对一次身份（创建时间），等待期间 PID 被复用也不会误杀
            process.terminate()
            process.wait(timeout=KILL_WAIT_SECONDS)
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, psutil.TimeoutExpired, OSError):
            logger.warning("结束被测程序失败: pid=%s", pid, exc_info=True)
            return TERMINATE_FAILED
        self._forget(pid)
        logger.info("已结束被测程序: pid=%s", pid)
        return TERMINATE_KILLED
