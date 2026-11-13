"""
界面层服务的测试替身

与 ui/services 中的协议一一对应，页面测试通过注入它们来：
- 断言弹出了哪些提示（RecordingNotifier），
- 预设"用户在对话框里的选择"（ScriptedDialogs），
- 控制"现在几点"（FakeClock），
- 给出固定的进程表（FakeProcessQuery；读取列表可阻塞到测试放行的 BlockingProcessQuery），
- 指定哪些任务仍在运行（FakeTaskStatus），
- 给出固定的用户偏好（FakePreferences），
- 启动 / 关闭被测程序而不真的启动进程（FakeLauncher），
- 给出固定的全系统进程快照（FakeSnapshotSource、fake_process_sample、fake_system_snapshot，
  快照不可用的 unavailable_snapshot_hub）。
替身都不弹出任何窗口、不触达本机进程与配置文件。
"""
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from PyQt5.QtCore import QObject, pyqtSignal

import app_config
from ui.services.notifier import EVENT_GENERIC, NOTICE_LEVELS, NoticeAction, resolve_duration
from utils.metrics import LEAK_CHECK_PRESET

ProcessEntry = Tuple[int, str, Optional[float]]


# ---------------------------------------------------------------- 提示


class FakeInfoBar(QObject):
    """RecordingNotifier 返回的提示对象：只提供页面会用到的那部分 InfoBar 接口。

    close() 发出 closedSignal（模拟用户关闭或到时消失），用于测试"同一错误仍在显示时
    不再叠加"之类依赖关闭信号的逻辑；addWidget 记下追加的按钮，测试可直接 click()。
    """

    closedSignal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.widgets: List[Any] = []
        self.closed = False

    def addWidget(self, widget: Any, stretch: int = 0) -> None:
        self.widgets.append(widget)

    def close(self) -> bool:
        if not self.closed:
            self.closed = True
            self.closedSignal.emit()
        return True


@dataclass
class Notice:
    """一次提示调用的记录。duration 是调用方传入的原值（可能为 None），
    effective_duration 是按生产实现的默认值规则换算后的实际时长。"""

    level: str
    title: str
    content: str
    duration: Optional[int]
    parent: Any
    bar: FakeInfoBar
    # 经 route 发出时才有的信息：事件名、合并键、操作按钮（便捷写法 info/... 记 EVENT_GENERIC）
    event: str = EVENT_GENERIC
    key: Optional[str] = None
    action: Optional[NoticeAction] = None

    @property
    def effective_duration(self) -> int:
        return resolve_duration(self.level, self.duration, self.action is not None)


class RecordingNotifier:
    """记录型提示替身：每次调用都记一条 Notice，并返回一个 FakeInfoBar。"""

    def __init__(self):
        self.notices: List[Notice] = []

    def _record(self, level: str, parent, title: str, content: str,
                duration: Optional[int], **extra: Any) -> FakeInfoBar:
        bar = FakeInfoBar()
        self.notices.append(Notice(level, title, content, duration, parent, bar, **extra))
        return bar

    def route(self, event: str, level: str, title: str, content: str, *,
              action: Optional[NoticeAction] = None, key: Optional[str] = None,
              duration: Optional[int] = None, anchor=None) -> FakeInfoBar:
        if level not in NOTICE_LEVELS:
            raise ValueError(f"未知的提示级别：{level!r}")
        return self._record(level, anchor, title, content, duration,
                            event=event, key=key, action=action)

    def info(self, parent, title: str, content: str,
             duration: Optional[int] = None) -> FakeInfoBar:
        return self._record("info", parent, title, content, duration)

    def success(self, parent, title: str, content: str,
                duration: Optional[int] = None) -> FakeInfoBar:
        return self._record("success", parent, title, content, duration)

    def warning(self, parent, title: str, content: str,
                duration: Optional[int] = None) -> FakeInfoBar:
        return self._record("warning", parent, title, content, duration)

    def error(self, parent, title: str, content: str,
              duration: Optional[int] = None) -> FakeInfoBar:
        return self._record("error", parent, title, content, duration)

    # ---- 查询 ----

    def messages(self, level: Optional[str] = None) -> List[Notice]:
        """全部记录，或只取某一级别的记录（按调用顺序）。"""
        if level is not None and level not in NOTICE_LEVELS:
            raise ValueError(f"未知的提示级别：{level!r}")
        return [n for n in self.notices if level is None or n.level == level]

    def levels(self) -> List[str]:
        """各次提示的级别序列。"""
        return [n.level for n in self.notices]

    def last(self, level: Optional[str] = None) -> Notice:
        """最近一条记录（可限定级别）；没有记录时以断言失败说明原因。"""
        matched = self.messages(level)
        if not matched:
            scope = f"级别 {level!r} 的" if level else ""
            raise AssertionError(f"尚未弹出任何{scope}提示；已有记录：{self.levels()}")
        return matched[-1]

    def clear(self) -> None:
        self.notices.clear()


# ---------------------------------------------------------------- 窗口状态记忆


class FakeWindowMemory:
    """内存里的窗口状态记忆：不碰配置文件，屏幕列表（可用区域，主屏在前）由测试指定。

    memory 是"当前已存的状态"，saves 记下每次真的发生了变化的保存（内容相同不记，对应
    生产实现"没变就不写盘"）。
    """

    def __init__(self, memory=None, screens=None):
        from ui.window_state import WindowMemory
        self.memory = memory if memory is not None else WindowMemory()
        self.screen_areas = list(screens) if screens is not None else [(0, 0, 1920, 1040)]
        self.saves: List[Any] = []

    def load(self):
        return self.memory

    def save(self, memory) -> bool:
        if memory == self.memory:
            return False
        self.memory = memory
        self.saves.append(memory)
        return True

    def screens(self):
        return list(self.screen_areas)


# ---------------------------------------------------------------- 对话框


_UNSET = object()


@dataclass
class DialogCall:
    """一次对话框调用的记录：方法名与按名字展开的参数。"""

    method: str
    args: Dict[str, Any] = field(default_factory=dict)


class ScriptedDialogs:
    """脚本化对话框替身：每个方法的返回值事先预设，并记录每次调用的参数。

    预设方式（同一方法两者可并用，队列优先、耗尽后回落到固定值）：
    - 构造参数或 always(method, value)：固定返回值，可为 None（如"取消"）；
    - push(method, *values)：返回值队列，按调用顺序逐个弹出。
    预设值若是可调用对象，则在对话框"打开期间"执行它（不带参数），以其返回值作为
    结果：用于模拟"确认框开着时别处发生了事情"（任务结束、数据被删等）。本次调用
    在执行前已记入 calls，回调里可用 last_call 取参数；回调抛出的异常原样传出。
    预设值（或回调的返回值）若是异常实例则抛出它（例如模拟 open_folder 失败）。

    需要返回值的方法（confirm / ask_marker_label / choose_metrics / ask_save_path /
    ask_open_file / ask_directory / ask_close_action）未预设即被调用时断言失败，避免测试在不知情时走了默认分支；
    无需返回值的系统交互（open_folder / open_file / copy_pixmap / copy_text）与 open_url
    （默认返回 True）不预设也可调用，只做记录。
    """

    _REQUIRES_SCRIPT = (
        'confirm', 'ask_marker_label', 'choose_metrics', 'ask_save_path',
        'ask_open_file', 'ask_directory', 'ask_close_action',
    )
    _DEFAULTS = {
        'open_folder': None,
        'open_file': None,
        'open_url': True,
        'copy_pixmap': None,
        'copy_text': None,
    }
    METHODS = _REQUIRES_SCRIPT + tuple(_DEFAULTS)

    def __init__(self, **fixed: Any):
        self.calls: List[DialogCall] = []
        self._queues: Dict[str, deque] = {name: deque() for name in self.METHODS}
        self._fixed: Dict[str, Any] = {name: _UNSET for name in self.METHODS}
        for name, value in fixed.items():
            self.always(name, value)

    def _check_name(self, method: str) -> None:
        if method not in self.METHODS:
            raise ValueError(f"ScriptedDialogs 没有方法 {method!r}；可选：{self.METHODS}")

    def always(self, method: str, value: Any) -> "ScriptedDialogs":
        """设定某方法的固定返回值（队列耗尽后使用）。"""
        self._check_name(method)
        self._fixed[method] = value
        return self

    def push(self, method: str, *values: Any) -> "ScriptedDialogs":
        """向某方法的返回值队列追加若干值。"""
        self._check_name(method)
        self._queues[method].extend(values)
        return self

    def pending(self, method: str) -> int:
        """某方法队列中尚未用掉的预设值个数。"""
        self._check_name(method)
        return len(self._queues[method])

    def _answer(self, method: str, **args: Any) -> Any:
        self.calls.append(DialogCall(method, args))
        queue = self._queues[method]
        if queue:
            value = queue.popleft()
        elif self._fixed[method] is not _UNSET:
            value = self._fixed[method]
        elif method in self._DEFAULTS:
            value = self._DEFAULTS[method]
        else:
            raise AssertionError(
                f"ScriptedDialogs.{method} 未预设返回值（用 push/always 预设）；"
                f"本次调用参数：{args}")
        if callable(value):
            value = value()
        if isinstance(value, BaseException):
            raise value
        return value

    # ---- 查询 ----

    def calls_of(self, method: str) -> List[Dict[str, Any]]:
        """某方法各次调用的参数（按调用顺序）。"""
        self._check_name(method)
        return [call.args for call in self.calls if call.method == method]

    def last_call(self, method: str) -> Dict[str, Any]:
        """某方法最近一次调用的参数；从未调用时断言失败。"""
        calls = self.calls_of(method)
        if not calls:
            raise AssertionError(f"ScriptedDialogs.{method} 从未被调用")
        return calls[-1]

    def methods_called(self) -> List[str]:
        """调用过的方法名序列。"""
        return [call.method for call in self.calls]

    # ---- 与 ui.services.dialogs.Dialogs 一致的接口 ----

    def confirm(self, parent, title: str, content: str, confirm_text: str,
                cancel_text: str = "取消") -> bool:
        return self._answer('confirm', parent=parent, title=title, content=content,
                            confirm_text=confirm_text, cancel_text=cancel_text)

    def ask_marker_label(self, parent, default_label: str, timestamp: datetime, *,
                         process_name: str = '', pid: Optional[int] = None
                         ) -> Optional[str]:
        return self._answer('ask_marker_label', parent=parent,
                            default_label=default_label, timestamp=timestamp,
                            process_name=process_name, pid=pid)

    def choose_metrics(self, parent, selected: Sequence[str], *,
                       group: bool = False) -> Optional[List[str]]:
        # group 只在为真时记入参数：单进程路径的调用记录与旧版逐项相同
        extra = {'group': True} if group else {}
        value = self._answer('choose_metrics', parent=parent, selected=list(selected), **extra)
        return list(value) if value is not None else None

    def ask_save_path(self, parent, title: str, default_path: str,
                      name_filter: str) -> Optional[str]:
        return self._answer('ask_save_path', parent=parent, title=title,
                            default_path=default_path, name_filter=name_filter)

    def ask_open_file(self, parent, title: str, start_dir: str,
                      name_filter: str) -> Optional[str]:
        return self._answer('ask_open_file', parent=parent, title=title,
                            start_dir=start_dir, name_filter=name_filter)

    def ask_directory(self, parent, title: str, start_dir: str) -> Optional[str]:
        return self._answer('ask_directory', parent=parent, title=title, start_dir=start_dir)

    def ask_close_action(self, parent, running_count: int, can_tray: bool,
                         notice: str = "") -> str:
        extra = {'notice': notice} if notice else {}
        return self._answer('ask_close_action', parent=parent,
                            running_count=running_count, can_tray=can_tray, **extra)

    def open_folder(self, path: str) -> None:
        self._answer('open_folder', path=path)

    def open_file(self, path: str) -> None:
        self._answer('open_file', path=path)

    def open_url(self, url: str) -> bool:
        return self._answer('open_url', url=url)

    def copy_pixmap(self, pixmap) -> None:
        self._answer('copy_pixmap', pixmap=pixmap)

    def copy_text(self, text: str) -> None:
        self._answer('copy_text', text=text)


# ---------------------------------------------------------------- 时钟


DEFAULT_FAKE_NOW = datetime(2026, 1, 1, 12, 0, 0)


class FakeClock:
    """可控时钟：now() 返回当前设定值，advance/set 手动推进。"""

    def __init__(self, start: datetime = DEFAULT_FAKE_NOW):
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: Union[timedelta, float, int]) -> datetime:
        """向前推进 delta（timedelta，或以秒计的数值），返回推进后的时间。"""
        if not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        self.current += delta
        return self.current

    def set(self, dt: datetime) -> None:
        self.current = dt


# ---------------------------------------------------------------- 进程查询


DEFAULT_FAKE_SELF_PID = 9999


class FakeProcessQuery:
    """固定进程表替身。

    - processes：(pid, 进程名, 创建时间) 列表，测试可直接改写以模拟进程启动/退出；
    - alive：为 None 时 is_alive 按当前 processes 判定（加上 self_pid）；
      赋成集合后以该集合为准，用于"列表里有、但进程已退出"之类的场景。
    calls 记录每次查询 (方法名, 参数)，便于断言"某操作不应查询进程"。
    """

    def __init__(self, processes: Optional[Iterable[ProcessEntry]] = None,
                 self_pid: int = DEFAULT_FAKE_SELF_PID,
                 alive: Optional[Iterable[int]] = None):
        self.processes: List[ProcessEntry] = list(processes or [])
        self._self_pid = self_pid
        self.alive: Optional[Set[int]] = set(alive) if alive is not None else None
        self.calls: List[Tuple[str, Optional[int]]] = []

    def list_processes(self) -> List[ProcessEntry]:
        self.calls.append(('list_processes', None))
        return sorted(self.processes, key=lambda entry: entry[1].lower())

    def name_of(self, pid: int) -> Optional[str]:
        self.calls.append(('name_of', pid))
        for entry_pid, name, _create_time in self.processes:
            if entry_pid == pid:
                return name
        return None

    def is_alive(self, pid: int) -> bool:
        self.calls.append(('is_alive', pid))
        if self.alive is not None:
            return pid in self.alive
        return pid == self._self_pid or any(entry[0] == pid for entry in self.processes)

    def self_pid(self) -> int:
        return self._self_pid

    def find_pids_by_name(self, name: str,
                          exclude_self: bool = True) -> List[Tuple[int, Optional[float]]]:
        self.calls.append(('find_pids_by_name', None))
        wanted = name.lower()
        return [(pid, create_time)
                for pid, entry_name, create_time in sorted(
                    self.processes, key=lambda entry: entry[1].lower())
                if entry_name.lower() == wanted
                and not (exclude_self and pid == self._self_pid)]

    def kill(self, pid: int) -> None:
        """模拟进程退出：从进程表移除，并从显式 alive 集合中移除。"""
        self.processes = [entry for entry in self.processes if entry[0] != pid]
        if self.alive is not None:
            self.alive.discard(pid)


class BlockingProcessQuery(FakeProcessQuery):
    """读取进程列表会阻塞的进程表替身（模拟 psutil 冷启动遍历要 2 秒上下）。

    list_processes 先记下调用所在线程（list_threads，threading.get_ident()）并置位
    entered，再等 release 放行（最长 block_timeout 秒）后返回固定进程表；release 放行
    后不再复位，之后的读取都不阻塞，需要再次阻塞时测试自行 release.clear()。
    fail_with 不为 None 时放行后抛出该异常（模拟读取失败）。其余查询与 FakeProcessQuery
    相同、不阻塞（直接输入 PID 不依赖列表）。
    """

    def __init__(self, processes: Optional[Iterable[ProcessEntry]] = None,
                 block_timeout: float = 5.0, **kwargs: Any):
        super().__init__(processes, **kwargs)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.block_timeout = block_timeout
        self.list_threads: List[int] = []
        self.fail_with: Optional[BaseException] = None

    def list_processes(self) -> List[ProcessEntry]:
        self.list_threads.append(threading.get_ident())
        self.entered.set()
        self.release.wait(self.block_timeout)
        if self.fail_with is not None:
            raise self.fail_with
        return super().list_processes()

    def list_calls(self) -> int:
        """list_processes 被调用的次数。"""
        return len(self.list_threads)


# ---------------------------------------------------------------- 启动被测程序


class FakeLauncher:
    """启动被测程序的替身（与 core.process_launcher.ProcessLauncher 同接口），不启动任何进程。

    - launch(spec)：记入 launched，按 results 队列（耗尽后用 result）返回 LaunchResult；
      默认成功，PID 从 next_pid 起递增、创建时间 = PID + 0.5、进程名取程序文件名；
    - fail(error)：之后的 launch 一律返回该错误；
    - terminate(pid, create_time)：记入 terminated（线程安全，可在后台线程调用），返回 outcome。
    """

    def __init__(self, next_pid: int = 5001):
        self.launched: List[Any] = []
        self.terminated: List[Tuple[int, Optional[float]]] = []
        self.results: deque = deque()
        self.result = None
        self.next_pid = next_pid
        self.outcome = 'closed'
        self._lock = threading.Lock()

    def fail(self, error: str) -> "FakeLauncher":
        from core.process_launcher import LaunchResult
        self.result = LaunchResult(error=error)
        return self

    def launch(self, spec):
        from core.process_launcher import LaunchResult
        self.launched.append(spec)
        if self.results:
            return self.results.popleft()
        if self.result is not None:
            return self.result
        pid = self.next_pid
        self.next_pid += 1
        return LaunchResult(pid=pid, create_time=pid + 0.5, name=spec.name)

    def terminate(self, pid: int, create_time: Optional[float]) -> str:
        with self._lock:
            self.terminated.append((pid, create_time))
        return self.outcome


# ---------------------------------------------------------------- 任务状态


class FakeTaskStatus:
    """任务运行状态替身：running 集合里的任务 ID 视为仍在采集。

    all_running=True 时任意任务 ID 都视为运行中（不必事先知道库里生成的 ID），
    可随时改属性切换。
    """

    def __init__(self, running_ids: Iterable[str] = (), all_running: bool = False):
        self.running: Set[str] = set(running_ids)
        self.all_running = all_running

    def is_running(self, task_id: str) -> bool:
        return self.all_running or task_id in self.running

    def set_running(self, task_id: str, running: bool = True) -> None:
        if running:
            self.running.add(task_id)
        else:
            self.running.discard(task_id)


# ---------------------------------------------------------------- 用户偏好


class FakePreferences:
    """固定用户偏好替身。

    构造后可直接改属性 interval / retention / tray 模拟用户改了设置，
    interval_bounds / retention_choices 是取值范围（默认同 app_config）；
    remember_last_metrics 原样记下（不做归一化），历次调用记在 remembered 中。
    """

    def __init__(self, last_metrics: Optional[Sequence[str]] = None,
                 default_interval: int = 1, retention_days: int = 0,
                 close_to_tray: bool = False):
        self._last_metrics: List[str] = list(
            last_metrics if last_metrics is not None else LEAK_CHECK_PRESET)
        self.interval = default_interval
        self.retention = retention_days
        self.tray = close_to_tray
        # 取值范围默认与生产配置一致；测试可改写以验证页面确实从这里取
        self.interval_bounds: Tuple[int, int] = app_config.interval_range()
        self.retention_choices: List[int] = app_config.retention_options()
        self.remembered: List[List[str]] = []

    def last_metrics(self) -> List[str]:
        return list(self._last_metrics)

    def remember_last_metrics(self, metrics: Iterable[str]) -> None:
        value = list(metrics)
        self.remembered.append(value)
        self._last_metrics = list(value)

    def default_interval(self) -> int:
        return self.interval

    def retention_days(self) -> int:
        return self.retention

    def close_to_tray(self) -> bool:
        return self.tray

    def interval_range(self) -> Tuple[int, int]:
        return self.interval_bounds

    def retention_options(self) -> List[int]:
        return list(self.retention_choices)

    # 窗口不在前台时是否发托盘气泡：属性可直接改写
    background_notify = True

    def notify_in_background(self) -> bool:
        return self.background_notify

    # 全局打点快捷键：属性 hotkey 可直接改写；set_marker_hotkey 的历次调用记在 hotkeys_set
    hotkey = "Ctrl+Alt+M"
    hotkeys_set: List[str]

    def marker_hotkey(self) -> str:
        return self.hotkey

    def set_marker_hotkey(self, text: str) -> None:
        if not hasattr(self, 'hotkeys_set'):
            self.hotkeys_set = []
        self.hotkeys_set.append(text)
        self.hotkey = text


# ---------------------------------------------------------------- 全系统快照


def fake_process_sample(pid: int, **fields: Any):
    """构造 core.system_snapshot.ProcessSample：未给出的字段取互不相同的缺省值。

    字节类缺省为 KB 整数倍再加零头，便于发现漏换算；时间类为秒。
    """
    from core.system_snapshot import ProcessSample
    defaults = dict(
        pid=pid, parent_pid=1, name=f"proc{pid}.exe", create_time=1_700_000_000.0 + pid,
        num_threads=17, handle_count=345, session_id=1, base_priority=8,
        working_set=10240 * 1024, peak_working_set=20480 * 1024,
        private_bytes=3072 * 1024 + 512, pagefile=3072 * 1024 + 512,
        peak_pagefile=4096 * 1024, virtual_size=1 << 32, peak_virtual=(1 << 32) + 4096,
        paged_pool=1536, peak_paged_pool=2560, nonpaged_pool=768, peak_nonpaged_pool=1280,
        page_faults=12345, read_count=11, write_count=22, other_count=33,
        read_bytes=5 * 1024 * 1024, write_bytes=3000, other_bytes=1024 * 7 + 256,
        user_time=1.25, kernel_time=0.75, cycle_time=123456789,
    )
    unknown = set(fields) - set(defaults)
    if unknown:
        raise TypeError(f"ProcessSample 没有字段 {sorted(unknown)}")
    defaults.update(fields)
    return ProcessSample(**defaults)


def fake_system_snapshot(samples: Iterable[Any] = (), monotonic: float = 0.0,
                         wall: float = 1_800_000_000.0,
                         context_switches: Optional[Dict[int, int]] = None):
    """构造 SystemSnapshot。context_switches 给出时按 {pid: 次数} 伪造线程数组
    （每个进程一个线程），使 snapshot.context_switches(pid) 可用。"""
    import struct as _struct
    from core.system_snapshot import SystemSnapshot, TakenAt
    processes = {s.pid: s for s in samples}
    if not context_switches:
        return SystemSnapshot(taken_at=TakenAt(monotonic, wall), processes=processes)
    raw = bytearray()
    threads = {}
    for pid, count in context_switches.items():
        threads[pid] = (len(raw), 1)
        raw += _struct.pack('<I', count)
    return SystemSnapshot(taken_at=TakenAt(monotonic, wall), processes=processes,
                          _threads=threads, _raw=memoryview(bytes(raw)),
                          _thread_size=4, _ctx_offset=0)


class FakeSnapshotSource:
    """SnapshotHub 的快照来源替身（代替 NtProcessSnapshotter）。

    - snapshots：snapshot() 依次返回的快照；用完后重复最后一个；元素可以是异常实例
      （该次抛出）或无参可调用（每次调用现算）；
    - available / reason：可用性，available=False 时 snapshot() 抛 SnapshotUnavailable；
    - delay：每次 snapshot() 前真实睡眠的秒数（并发用例用来拉长取样窗口）。
    calls 记录 snapshot() 被调用的次数（线程安全）。
    """

    def __init__(self, snapshots: Optional[Sequence[Any]] = None, available: bool = True,
                 reason: Optional[str] = None, delay: float = 0.0):
        self.snapshots: List[Any] = list(snapshots or [])
        self._available = available
        self.reason = reason if reason is not None else (None if available else "测试禁用")
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> Optional[str]:
        return None if self._available else self.reason

    def snapshot(self):
        from core.system_snapshot import SnapshotUnavailable
        with self._lock:
            self.calls += 1
            index = self.calls - 1
        if self.delay:
            import time as _time
            _time.sleep(self.delay)
        if not self._available:
            raise SnapshotUnavailable(self.reason)
        if not self.snapshots:
            return fake_system_snapshot()
        item = self.snapshots[min(index, len(self.snapshots) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item


class ManualMonotonic:
    """只提供 monotonic() 的手动时钟（SnapshotHub 的 clock 参数用）。"""

    def __init__(self, start: float = 1000.0):
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


def unavailable_snapshot_hub():
    """快照不可用的 SnapshotHub：用于强制走 psutil 回退路径。"""
    from core.snapshot_hub import SnapshotHub
    return SnapshotHub(source=FakeSnapshotSource(available=False))