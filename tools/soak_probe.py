"""
浸泡探针：独立监控“进程监控助手”自己（只用标准库 + psutil；IPC 只用 core.ipc.client）

不依赖本工具的采集 / 数据库代码，所以本工具自身出问题（卡死、泄漏、崩溃）时探针的记录不受影响。
每个间隔（默认 10 秒）记录：私有字节、工作集、句柄、GDI、USER、线程数与 CPU，并经 IPC 的
status 读各任务的采样完成率（planned_cycles / done_cycles / missed_cycles）。输出目录里三个文件：

- soak_probe.csv   本工具自身的资源曲线（一个间隔一行，列见 PROBE_COLUMNS）
- soak_tasks.csv   各任务的采样完成情况（一个间隔每个运行中任务一行，列见 TASK_COLUMNS）
- soak_events.csv  事件：probe_start / target_found / target_exited / target_not_found /
                   ipc_lost / ipc_ok / probe_stop（列见 EVENT_COLUMNS）

每行写完立即 flush，探针被杀也不丢已写的数据；目录里已有文件时追加（不重复写表头），
所以探针可以中途重启。本工具崩溃或被关闭：记 target_exited 事件，默认继续等它重新出现
（--on-exit wait），也可以直接退出（--on-exit exit）。目录里出现 STOP 文件、到 --hours 时长、
或 Ctrl+C 都会让探针写 probe_stop 后正常退出。

找目标进程（三选一，缺省按 --name 进程监控助手.exe）：
  --pid N        先按这个 PID，之后重新找时退回按 --name 或 IPC
  --name 名      按进程名（可加 --cmdline-contains 区分同名的 python.exe）
  --by-ipc       经 IPC ping 取本工具的 PID（需要 --data-dir；源码运行时用它）
IPC 的数据目录：--data-dir；没给时是安装版的默认目录 %LOCALAPPDATA%\\进程监控助手\\data。
探针从不读写数据目录里的文件，只用它算出管道名。

用法示例（缩短演练，源码运行的无窗口实例）：
    python tools\\soak_probe.py --out-dir C:\\soak\\run1 --by-ipc --data-dir C:\\soak\\run1\\data --hours 0.25
"""
import argparse
import csv
import ctypes
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE_FILE = 'soak_probe.csv'
TASKS_FILE = 'soak_tasks.csv'
EVENTS_FILE = 'soak_events.csv'
STOP_FILE = 'STOP'

PROBE_COLUMNS = (
    'timestamp', 'unix_ts', 'pid', 'alive', 'private_kb', 'working_set_kb', 'handles', 'gdi',
    'user', 'threads', 'cpu_percent', 'ipc_ok', 'tasks_running', 'planned_cycles',
    'done_cycles', 'missed_cycles',
)
TASK_COLUMNS = (
    'timestamp', 'unix_ts', 'task_id', 'name', 'status', 'planned_cycles', 'done_cycles',
    'missed_cycles',
)
EVENT_COLUMNS = ('timestamp', 'unix_ts', 'event', 'detail')

EVENT_PROBE_START = 'probe_start'
EVENT_PROBE_STOP = 'probe_stop'
EVENT_TARGET_FOUND = 'target_found'
EVENT_TARGET_EXITED = 'target_exited'
EVENT_TARGET_NOT_FOUND = 'target_not_found'
EVENT_IPC_LOST = 'ipc_lost'
EVENT_IPC_OK = 'ipc_ok'

DEFAULT_INTERVAL_S = 10.0
DEFAULT_APP_NAME = "进程监控助手"
IPC_TIMEOUT_S = 5.0
# user32.GetGuiResources 的类型参数
GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---------- 读目标进程 ----------

_WIN_API: Optional[Tuple[Any, Any]] = None


def _win_api() -> Tuple[Any, Any]:
    """kernel32 / user32 只加载一次并设好函数签名（长时间运行不反复加载）。"""
    global _WIN_API
    if _WIN_API is None:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        user32.GetGuiResources.restype = ctypes.c_uint32
        user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        _WIN_API = (kernel32, user32)
    return _WIN_API


def _gui_resources(pid: int, flag: int) -> Optional[int]:
    """GDI / USER 对象数（user32.GetGuiResources）；读不到为 None。"""
    if sys.platform != 'win32':
        return None
    try:
        kernel32, user32 = _win_api()
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return None
        try:
            return int(user32.GetGuiResources(handle, flag))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


class ProcessReader:
    """读一个进程的资源（psutil 对象保持住，CPU 百分比靠两次调用之间的差分）。"""

    def __init__(self, proc: psutil.Process):
        self.proc = proc
        self.pid = proc.pid
        proc.cpu_percent(None)

    def is_running(self) -> bool:
        try:
            return self.proc.is_running()
        except psutil.Error:
            return False

    def read(self) -> Dict[str, Optional[float]]:
        """一次读数；读不到的字段为 None。进程已退出时抛 psutil.NoSuchProcess。"""
        with self.proc.oneshot():
            mem = self.proc.memory_info()
            values: Dict[str, Optional[float]] = {
                'private_kb': getattr(mem, 'private', mem.vms) / 1024.0,
                'working_set_kb': getattr(mem, 'wset', mem.rss) / 1024.0,
                'threads': float(self.proc.num_threads()),
                'cpu_percent': self.proc.cpu_percent(None),
            }
            try:
                values['handles'] = float(self.proc.num_handles())
            except (AttributeError, psutil.Error):
                values['handles'] = None
        values['gdi'] = _gui_resources(self.pid, GR_GDIOBJECTS)
        values['user'] = _gui_resources(self.pid, GR_USEROBJECTS)
        return values


# ---------- 找目标进程 ----------

def find_by_name(name: str, *, cmdline_contains: Optional[str] = None,
                 exclude_pids: Sequence[int] = ()) -> Optional[psutil.Process]:
    """按进程名（不区分大小写，可选命令行子串）找；多个取最早创建的。"""
    wanted = name.lower()
    needle = cmdline_contains.lower() if cmdline_contains else None
    best: Optional[Tuple[float, psutil.Process]] = None
    for proc in psutil.process_iter(['pid', 'name', 'create_time']):
        info = proc.info
        if info['pid'] in exclude_pids or (info['name'] or '').lower() != wanted:
            continue
        if needle is not None:
            try:
                if needle not in ' '.join(proc.cmdline()).lower():
                    continue
            except psutil.Error:
                continue
        created = info['create_time'] or 0.0
        if best is None or created < best[0]:
            best = (created, proc)
    return best[1] if best else None


def default_data_dir() -> str:
    """安装版的默认数据目录（探针只用它算管道名）。"""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, DEFAULT_APP_NAME, 'data')


def ipc_pipe_path(data_dir: str) -> str:
    from utils.instance_name import pipe_path, single_instance_name
    return pipe_path(single_instance_name(data_dir))


def ipc_call(pipe: str, cmd: str, timeout_s: float = IPC_TIMEOUT_S) -> Dict[str, Any]:
    """连接管道、发一条命令、返回 result；失败抛异常（调用方统一当成 IPC 不通）。
    每次新建连接（与 pmctl 的用法一致），顺带检验连接 / 断开不泄漏。"""
    from core.ipc.client import IpcClient
    with IpcClient.connect(pipe) as client:
        return client.call(cmd, {}, timeout=timeout_s, idle_timeout=timeout_s)


def sum_task_cycles(tasks: Sequence[Dict[str, Any]]) -> Tuple[int, int, int]:
    """各运行中任务（planned, done, missed）求和；没有 quality 的任务不计。"""
    planned = done = missed = 0
    for task in tasks:
        quality = task.get('quality') or {}
        planned += int(quality.get('planned_cycles') or 0)
        done += int(quality.get('done_cycles') or 0)
        missed += int(quality.get('missed_cycles') or 0)
    return planned, done, missed


# ---------- CSV ----------

class CsvAppender:
    """追加写 CSV：文件不存在或为空时先写表头；每行 flush。UTF-8 带 BOM，Excel 可直接打开。"""

    def __init__(self, path: str, columns: Sequence[str]):
        self.path = path
        self.columns = tuple(columns)
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._handle = open(path, 'a', newline='', encoding='utf-8-sig' if fresh else 'utf-8')
        self._writer = csv.writer(self._handle)
        if fresh:
            self._writer.writerow(self.columns)
            self._handle.flush()

    def write(self, row: Dict[str, Any]) -> None:
        self._writer.writerow([_cell(row.get(column)) for column in self.columns])
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _cell(value: Any) -> Any:
    if value is None:
        return ''
    if isinstance(value, float):
        return f"{value:.3f}".rstrip('0').rstrip('.') if value != int(value) else str(int(value))
    return value


def _stamp(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts).isoformat(timespec='seconds')


# ---------- 探针 ----------

class SoakProbe:
    """探针主体。外部依赖（找进程、读进程、读 status、时钟）都可注入，测试不碰真实进程。"""

    def __init__(self, out_dir: str, *, interval_s: float = DEFAULT_INTERVAL_S,
                 find_target: Callable[[], Optional[Any]],
                 make_reader: Callable[[Any], Any] = ProcessReader,
                 read_status: Optional[Callable[[], Dict[str, Any]]] = None,
                 on_exit: str = 'wait', hours: float = 0.0,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print):
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir = out_dir
        self.interval_s = interval_s
        self._find_target = find_target
        self._make_reader = make_reader
        self._read_status = read_status
        self.on_exit = on_exit
        self.hours = hours
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._log = log
        self._probe = CsvAppender(os.path.join(out_dir, PROBE_FILE), PROBE_COLUMNS)
        self._tasks = CsvAppender(os.path.join(out_dir, TASKS_FILE), TASK_COLUMNS)
        self._events = CsvAppender(os.path.join(out_dir, EVENTS_FILE), EVENT_COLUMNS)
        self._reader: Optional[Any] = None
        self._waiting_logged = False
        self._ipc_state: Optional[bool] = None
        self._gone = False
        self.samples = 0

    # -- 事件 --

    def event(self, name: str, detail: str = '') -> None:
        now = self._clock()
        self._events.write({'timestamp': _stamp(now), 'unix_ts': now, 'event': name,
                            'detail': detail})
        self._log(f"[{_stamp(now)}] {name} {detail}".rstrip())

    # -- 目标 --

    def _ensure_target(self) -> bool:
        """保证有一个活着的目标读取器；没有则尝试找一个。返回当前是否有目标。"""
        if self._reader is not None and not self._reader.is_running():
            self.event(EVENT_TARGET_EXITED, f"pid={self._reader.pid}")
            self._reader = None
            if self.on_exit == 'exit':
                self._gone = True
                return False
        if self._reader is None:
            try:
                proc = self._find_target()
            except Exception as exc:
                proc = None
                self._log(f"找目标进程出错：{exc}")
            if proc is None:
                if not self._waiting_logged:
                    self._waiting_logged = True
                    self.event(EVENT_TARGET_NOT_FOUND, "等待目标进程出现")
                return False
            try:
                self._reader = self._make_reader(proc)
            except psutil.Error as exc:
                self._log(f"附着目标进程失败：{exc}")
                return False
            self._waiting_logged = False
            self.event(EVENT_TARGET_FOUND, f"pid={self._reader.pid}")
        return True

    # -- 一次采样 --

    def _status(self) -> Tuple[bool, List[Dict[str, Any]]]:
        if self._read_status is None:
            return False, []
        detail = ''
        try:
            result = self._read_status()
            tasks = [t for t in (result.get('tasks') or []) if isinstance(t, dict)]
            ok = True
        except Exception as exc:
            tasks, ok = [], False
            detail = f"{type(exc).__name__}: {exc}"
        previous = self._ipc_state
        # 只在状态变化时记事件；第一次就通不算变化
        if ok != previous and not (previous is None and ok):
            self.event(EVENT_IPC_OK if ok else EVENT_IPC_LOST, detail)
        self._ipc_state = ok
        return ok, tasks

    def sample_once(self) -> bool:
        """采一个点，写 CSV（目标已退出的那一行 alive=0）。返回是否应继续（on_exit=exit
        且目标已退出时为 False）。"""
        have = self._ensure_target()
        now = self._clock()
        row: Dict[str, Any] = {'timestamp': _stamp(now), 'unix_ts': now,
                               'alive': 1 if have else 0,
                               'pid': self._reader.pid if (have and self._reader) else None}
        if have and self._reader is not None:
            try:
                row.update(self._reader.read())
            except psutil.Error:
                # 读的瞬间退出：本行按已退出记，下一次 _ensure_target 补 target_exited 事件
                row['alive'] = 0
        ok, tasks = self._status()
        row['ipc_ok'] = 1 if ok else 0
        if ok:
            planned, done, missed = sum_task_cycles(tasks)
            row.update(tasks_running=len(tasks), planned_cycles=planned, done_cycles=done,
                       missed_cycles=missed)
            for task in tasks:
                quality = task.get('quality') or {}
                self._tasks.write({
                    'timestamp': row['timestamp'], 'unix_ts': now,
                    'task_id': task.get('task_id'), 'name': task.get('name'),
                    'status': task.get('status'),
                    'planned_cycles': quality.get('planned_cycles'),
                    'done_cycles': quality.get('done_cycles'),
                    'missed_cycles': quality.get('missed_cycles')})
        self._probe.write(row)
        self.samples += 1
        return not self._gone

    # -- 主循环 --

    def _stop_requested(self) -> bool:
        return os.path.exists(os.path.join(self.out_dir, STOP_FILE))

    def run(self) -> int:
        """跑到 --hours、STOP 文件、Ctrl+C 或（on_exit=exit 时）目标退出。返回样本数。"""
        started = self._monotonic()
        self.event(EVENT_PROBE_START,
                   f"interval={self.interval_s:g}s hours={self.hours:g} on_exit={self.on_exit}")
        reason = 'done'
        deadline = started
        try:
            while True:
                if not self.sample_once():
                    reason = 'target_exited'
                    break
                if self.hours > 0 and self._monotonic() - started >= self.hours * 3600.0:
                    break
                if self._stop_requested():
                    reason = 'stop_file'
                    break
                deadline += self.interval_s
                now = self._monotonic()
                if now > deadline + self.interval_s:
                    # 落后超过一个间隔（休眠唤醒、探针被挂起）：不补采，从当前起重新对齐
                    deadline = now + self.interval_s
                wait = deadline - now
                if wait > 0:
                    self._sleep(wait)
        except KeyboardInterrupt:
            reason = 'interrupted'
        finally:
            self.event(EVENT_PROBE_STOP, f"reason={reason} samples={self.samples}")
            for sink in (self._probe, self._tasks, self._events):
                sink.close()
        return self.samples


# ---------- 命令行 ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='soak_probe', description="浸泡探针：独立记录进程监控助手自身的资源与各任务采样完成率")
    parser.add_argument('--out-dir', required=True, help="输出目录（CSV 与事件文件放这里）")
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL_S, help="采样间隔（秒）")
    parser.add_argument('--hours', type=float, default=0.0, help="运行多少小时后退出（0 = 直到 STOP 文件或 Ctrl+C）")
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--pid', type=int, help="本工具的 PID")
    target.add_argument('--name', help=f"本工具的进程名（缺省 {DEFAULT_APP_NAME}.exe）")
    target.add_argument('--by-ipc', action='store_true', help="经 IPC ping 取本工具的 PID（需要 --data-dir）")
    parser.add_argument('--cmdline-contains', help="按名称找时，命令行还必须含这段文字")
    parser.add_argument('--data-dir', help="本工具的数据目录（只用来算 IPC 管道名；缺省为安装版默认目录）")
    parser.add_argument('--no-ipc', action='store_true', help="不读 status（只记资源曲线）")
    parser.add_argument('--on-exit', choices=('wait', 'exit'), default='wait',
                        help="本工具退出后：wait 继续等它重新出现（缺省），exit 探针也退出")
    return parser


def build_finder(args: argparse.Namespace, pipe: Optional[str]) -> Callable[[], Optional[psutil.Process]]:
    own = (os.getpid(),)
    state = {'first': True}

    def by_pid(pid: int) -> Optional[psutil.Process]:
        try:
            return psutil.Process(pid)
        except psutil.Error:
            return None

    def by_ipc() -> Optional[psutil.Process]:
        if pipe is None:
            return None
        try:
            pid = ipc_call(pipe, 'ping').get('pid')
        except Exception:
            return None
        return by_pid(int(pid)) if isinstance(pid, int) else None

    def find() -> Optional[psutil.Process]:
        if args.pid and state['first']:
            state['first'] = False
            found = by_pid(args.pid)
            if found is not None:
                return found
        state['first'] = False
        if args.by_ipc:
            return by_ipc()
        name = args.name or f"{DEFAULT_APP_NAME}.exe"
        return find_by_name(name, cmdline_contains=args.cmdline_contains, exclude_pids=own)

    return find


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    if args.by_ipc and (not args.data_dir or args.no_ipc):
        parser.error("--by-ipc 需要 --data-dir，且不能与 --no-ipc 同用")
    data_dir = args.data_dir or default_data_dir()
    pipe = None if args.no_ipc else ipc_pipe_path(data_dir)
    status = None if pipe is None else (lambda: ipc_call(pipe, 'status'))
    probe = SoakProbe(args.out_dir, interval_s=args.interval,
                      find_target=build_finder(args, pipe), read_status=status,
                      on_exit=args.on_exit, hours=args.hours)
    probe.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
