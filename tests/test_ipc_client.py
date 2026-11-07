"""
IPC 客户端（core/ipc/client.py，ctypes 命名管道）：

客户端是阻塞调用，放到工作线程里跑；主线程一边处理 Qt 事件一边驱动真实的 IpcServer。
覆盖：请求/回复与心跳事件、总超时与心跳中断、服务端断开、身份校验（PID 不符）、
拉起与退避重试（假 popen）、正在启动时不重复拉起、ping 到 ready、退出码映射。
"""
import os
import subprocess
import sys
import threading
import uuid

import pytest

from core.ipc import client as ipc
from core.ipc import protocol as p
from core.ipc.server import IpcServer
from tests.helpers import process_events_until
from utils.instance_name import instance_mutex_name, pipe_path

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason="命名管道客户端只支持 Windows")


def _unique_name() -> str:
    return f"pm_test_{uuid.uuid4().hex[:12]}"


def run_blocking(fn, timeout: float = 10.0):
    """在工作线程里跑阻塞的客户端调用，主线程处理 Qt 事件直到它结束；返回值或重抛异常。"""
    box = {}

    def body():
        try:
            box['value'] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样带回主线程
            box['error'] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    assert process_events_until(lambda: not thread.is_alive(), timeout), "客户端调用未结束"
    if 'error' in box:
        raise box['error']
    return box.get('value')


class ScriptedHandler:
    """按命令名脚本化的服务端处理器。"""

    def __init__(self, pid=None):
        self.pid = pid if pid is not None else os.getpid()
        self.ready_after = 0
        self.pings = 0
        self.conns = []

    def __call__(self, conn, request):
        self.conns.append(conn)
        cmd = request.cmd
        if cmd == 'ping':
            self.pings += 1
            conn.send_ok(request.id, {'pid': self.pid, 'ready': self.pings > self.ready_after,
                                      'app_version': 't', 'headless': True})
        elif cmd == 'beats':
            conn.send_event(request.id, p.EVENT_HEARTBEAT, {'beats': 1})
            conn.send_event(request.id, p.EVENT_HEARTBEAT, {'beats': 2})
            conn.send_ok(request.id, {'done': True})
        elif cmd == 'silent':
            pass
        elif cmd == 'hangup':
            conn.abort()
        elif cmd == 'fail':
            conn.send_error(request.id, p.ERR_INVALID_ARGS, "参数不对")
        else:
            conn.send_ok(request.id, {'echo': request.args})


@pytest.fixture
def server(qapp):
    handler = ScriptedHandler()
    srv = IpcServer(_unique_name(), handler)
    srv.handler = handler
    assert srv.listen()
    yield srv
    srv.close()
    srv.deleteLater()
    qapp.processEvents()


def _connect(srv):
    return ipc.IpcClient.connect(pipe_path(srv.name))


# ---------- 请求与回复 ----------

def test_request_roundtrip_and_unicode(server):
    def go():
        with _connect(server) as client:
            return client.call('echo', {'label': '第 1 轮'})
    assert run_blocking(go) == {'echo': {'label': '第 1 轮'}}


def test_events_are_delivered_before_final_reply(server):
    events = []

    def go():
        with _connect(server) as client:
            return client.request('beats', on_event=events.append)
    response = run_blocking(go)
    assert response.ok and response.result == {'done': True}
    assert [e.data['beats'] for e in events] == [1, 2]


def test_ids_increase_per_request(server):
    def go():
        with _connect(server) as client:
            return [client.request('echo').id for _ in range(3)]
    assert run_blocking(go) == [1, 2, 3]


def test_server_error_maps_exit_code(server):
    def go():
        with _connect(server) as client:
            client.call('fail')
    with pytest.raises(ipc.ServerError) as info:
        run_blocking(go)
    assert info.value.code == p.ERR_INVALID_ARGS
    assert info.value.exit_code == ipc.EXIT_USAGE


def test_total_timeout(server):
    def go():
        with _connect(server) as client:
            client.request('silent', timeout=0.3)
    with pytest.raises(ipc.PipeTimeout) as info:
        run_blocking(go)
    assert info.value.exit_code == ipc.EXIT_CONNECTION
    assert "超时" in str(info.value)


def test_idle_timeout_when_heartbeats_stop(server):
    def go():
        with _connect(server) as client:
            client.request('silent', timeout=None, idle_timeout=0.3)
    with pytest.raises(ipc.PipeTimeout) as info:
        run_blocking(go)
    assert "心跳" in str(info.value)


def test_server_hangup_is_pipe_closed(server):
    def go():
        with _connect(server) as client:
            client.request('hangup', timeout=5)
    with pytest.raises(ipc.PipeClosed):
        run_blocking(go)


def test_pipe_not_found():
    with pytest.raises(ipc.PipeNotFound) as info:
        ipc.IpcClient.connect(pipe_path(_unique_name()))
    assert info.value.exit_code == ipc.EXIT_CONNECTION


# ---------- 身份校验 ----------

def test_verify_server_accepts_same_process(server):
    def go():
        with _connect(server) as client:
            assert client.transport.server_process_id() == os.getpid()
            client.verify_server(os.getpid())
            return True
    assert run_blocking(go) is True


@pytest.mark.parametrize('claimed', [lambda: os.getpid() + 4, lambda: None, lambda: 'x'])
def test_verify_server_rejects_pid_mismatch(server, claimed):
    def go():
        with _connect(server) as client:
            client.verify_server(claimed())
    with pytest.raises(ipc.IdentityError) as info:
        run_blocking(go)
    assert info.value.exit_code == ipc.EXIT_CONNECTION


def test_open_session_rejects_impostor(server):
    """ping 报告的 pid 与管道服务端 PID 不符（冒名管道）→ 身份校验失败。"""
    server.handler.pid = os.getpid() + 4
    with pytest.raises(ipc.IdentityError):
        run_blocking(lambda: ipc.open_session(server.name, launch=None))


def test_own_user_sid_matches_own_process():
    api = ipc.win32()
    own = api.own_user_sid()
    assert own and api.process_user_sid(os.getpid()) == own


# ---------- 连接与拉起 ----------

class FakePopen:
    """假的 subprocess.Popen：记录参数，并通知主线程"进程已启动"（开始监听）。"""

    def __init__(self):
        self.calls = []
        self.started = threading.Event()

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        self.started.set()
        return object()


def test_open_session_without_launch_fails_fast():
    with pytest.raises(ipc.PipeNotFound):
        ipc.open_session(_unique_name(), launch=None)


def test_open_session_launches_once_and_waits_until_ready(qapp):
    name = _unique_name()
    handler = ScriptedHandler()
    handler.ready_after = 2   # 前两次 ping 回 ready:false（仍在启动）
    srv = IpcServer(name, handler)
    popen = FakePopen()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        threading.Event().wait(min(seconds, 0.05))

    box = {}

    def go():
        try:
            box['client'] = ipc.open_session(name, launch=['main.exe', '--background'],
                                             sleep=sleep, popen=popen,
                                             mutex_check=lambda _name: False)
        except BaseException as exc:  # noqa: BLE001
            box['error'] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    try:
        # "进程"启动后过一会儿才开始监听：客户端在这期间按退避重试
        assert process_events_until(popen.started.is_set, 5.0)
        process_events_until(lambda: len(sleeps) >= 3, 2.0)
        assert srv.listen()
        assert process_events_until(lambda: not thread.is_alive(), 10.0)
        assert 'error' not in box, box.get('error')
        client = box['client']
        assert client.server_info['ready'] is True
        assert handler.pings == 3
        client.close()
    finally:
        srv.close()
        srv.deleteLater()
    assert len(popen.calls) == 1, "只拉起一次"
    command, kwargs = popen.calls[0]
    assert command == ['main.exe', '--background']
    assert kwargs['stdin'] == kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
    assert kwargs['creationflags'] == ipc.CREATE_NO_WINDOW | ipc.CREATE_NEW_PROCESS_GROUP
    assert sleeps[0] == pytest.approx(0.025)
    assert max(sleeps) <= 0.2


def test_open_session_does_not_launch_while_instance_is_starting(qapp):
    """互斥量已存在（另一份正在启动）：不再拉起，只等管道出现。"""
    name = _unique_name()
    srv = IpcServer(name, ScriptedHandler())
    popen = FakePopen()
    checked = []
    box = {}

    def go():
        try:
            box['client'] = ipc.open_session(
                name, launch=['main.exe'], popen=popen,
                sleep=lambda s: threading.Event().wait(min(s, 0.05)),
                mutex_check=lambda full: checked.append(full) or True)
        except BaseException as exc:  # noqa: BLE001
            box['error'] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    try:
        assert process_events_until(lambda: checked, 5.0)
        assert srv.listen()
        assert process_events_until(lambda: not thread.is_alive(), 10.0)
        assert 'error' not in box, box.get('error')
        box['client'].close()
    finally:
        srv.close()
        srv.deleteLater()
    assert popen.calls == []
    assert checked[0] == instance_mutex_name(name)


def test_open_session_launch_timeout(qapp):
    popen = FakePopen()
    with pytest.raises(ipc.PipeTimeout) as info:
        ipc.open_session(_unique_name(), launch=['main.exe'], launch_timeout=0.2,
                         popen=popen, sleep=lambda s: threading.Event().wait(s),
                         mutex_check=lambda _n: False)
    assert info.value.exit_code == ipc.EXIT_CONNECTION
    assert len(popen.calls) == 1


def test_spawn_failure_is_connection_error():
    def broken(*_a, **_k):
        raise FileNotFoundError("没有这个程序")
    with pytest.raises(ipc.ConnectionFailed):
        ipc.spawn_background(['nope.exe'], popen=broken)


def test_mutex_exists_for_real_mutex():
    import ctypes
    name = instance_mutex_name(_unique_name())
    assert ipc.mutex_exists(name) is False
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, name)
    try:
        assert ipc.mutex_exists(name) is True
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


# ---------- 拉起命令行 ----------

def test_launch_command_frozen_uses_sibling_exe():
    command = ipc.launch_command(r'D:\数据', frozen=True,
                                 executable=r'C:\App\pmctl.exe', main_exe_name='进程监控助手.exe')
    assert command == [r'C:\App\进程监控助手.exe', '--background', '--data-dir', r'D:\数据']


def test_launch_command_dev_runs_main_py():
    command = ipc.launch_command(None, frozen=False, executable=r'C:\py\python.exe',
                                 project_root=r'D:\proj')
    assert command == [r'C:\py\python.exe', os.path.join(r'D:\proj', 'main.py'), '--background']


def test_launch_command_default_root_is_repository():
    command = ipc.launch_command(None, frozen=False)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert command[1] == os.path.join(root, 'main.py')
    assert os.path.isfile(command[1])


# ---------- 退出码 ----------

@pytest.mark.parametrize('code, expected', [
    (p.ERR_BAD_REQUEST, 2), (p.ERR_UNKNOWN_COMMAND, 2), (p.ERR_INVALID_ARGS, 2),
    (p.ERR_DISABLED, 3), (p.ERR_NOT_READY, 3),
    (p.ERR_NOT_FOUND, 1), (p.ERR_INVALID_STATE, 1), (p.ERR_BUSY, 1), (p.ERR_TIMEOUT, 1),
    (p.ERR_INTERNAL, 1), (p.ERR_FORBIDDEN, 1), ('something_new', 1), (None, 1),
])
def test_exit_code_for_error(code, expected):
    assert ipc.exit_code_for_error(code) == expected


def test_verdict_exit_codes_and_severity():
    assert [ipc.exit_code_for_verdict(v) for v in
            ('stable', 'rising', 'insufficient', 'target_exited', None)] == [0, 10, 20, 30, 1]
    assert ipc.most_severe(['stable', 'insufficient']) == 'insufficient'
    assert ipc.most_severe(['insufficient', 'rising', 'stable']) == 'rising'
    assert ipc.most_severe(['rising', 'target_exited']) == 'target_exited'
    assert ipc.most_severe([]) is None


# ---------- 评审修复：拉起后的首次 ping、拉起的进程提前退出、全局互斥量、空闲退出参数 ----------

class SlowPingHandler(ScriptedHandler):
    """第一次 ping 晚 delay_ms 才回（模拟实例在同步升级数据库，事件循环不转）。"""

    def __init__(self, delay_ms):
        super().__init__()
        self.delay_ms = delay_ms

    def __call__(self, conn, request):
        if request.cmd == 'ping' and self.pings == 0:
            from PyQt5.QtCore import QTimer
            self.pings += 1
            QTimer.singleShot(self.delay_ms, lambda: conn.send_ok(
                request.id, {'pid': self.pid, 'ready': True, 'app_version': 't',
                             'headless': True}))
            return
        super().__call__(conn, request)


def _launch_in_thread(name, **kwargs):
    box = {}

    def go():
        try:
            box['client'] = ipc.open_session(name, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            box['error'] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, box


def test_first_ping_after_launch_waits_launch_timeout_with_notices(qapp, monkeypatch):
    """新拉起的实例第一次 ping 晚于 CONNECT_TIMEOUT_S 才回：仍等到 launch_timeout，期间
    每 WAITING_NOTICE_S 提示一次"正在等待实例就绪"（修复前 5 秒就报连接失败）。"""
    monkeypatch.setattr(ipc, 'CONNECT_TIMEOUT_S', 0.3)
    monkeypatch.setattr(ipc, 'WAITING_NOTICE_S', 0.1)
    name = _unique_name()
    handler = SlowPingHandler(delay_ms=1200)
    srv = IpcServer(name, handler)
    popen = FakePopen()
    notices = []
    thread, box = _launch_in_thread(
        name, launch=['main.exe', '--background'], launch_timeout=10.0, popen=popen,
        sleep=lambda s: threading.Event().wait(min(s, 0.05)), mutex_check=lambda _n: False,
        on_waiting=notices.append)
    try:
        assert process_events_until(popen.started.is_set, 5.0)
        assert srv.listen()
        assert process_events_until(lambda: not thread.is_alive(), 10.0)
        assert 'error' not in box, box.get('error')
        box['client'].close()
    finally:
        srv.close()
        srv.deleteLater()
    assert notices and set(notices) == {ipc.MSG_WAITING_READY}
    assert "数据库升级中" in ipc.MSG_WAITING_READY


def test_first_ping_to_running_instance_keeps_short_timeout(qapp, monkeypatch):
    """不是新拉起的实例：第一次 ping 仍按 CONNECT_TIMEOUT_S 超时，也不提示。"""
    monkeypatch.setattr(ipc, 'CONNECT_TIMEOUT_S', 0.3)
    monkeypatch.setattr(ipc, 'WAITING_NOTICE_S', 0.1)
    name = _unique_name()
    srv = IpcServer(name, SlowPingHandler(delay_ms=3000))
    assert srv.listen()
    notices = []
    thread, box = _launch_in_thread(name, launch=['main.exe'], popen=FakePopen(),
                                    mutex_check=lambda _n: True, on_waiting=notices.append)
    try:
        assert process_events_until(lambda: not thread.is_alive(), 10.0)
    finally:
        srv.close()
        srv.deleteLater()
    assert isinstance(box.get('error'), ipc.PipeTimeout)
    assert notices == []


class ExitedPopen(FakePopen):
    """拉起后立刻以 returncode 退出的"进程"。"""

    def __init__(self, returncode):
        super().__init__()
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        super().__call__(command, **kwargs)
        return self

    def poll(self):
        return self.returncode


@pytest.mark.parametrize('returncode, hint', [
    (0, "设置里已关闭命令行控制"),
    (4, "IPC 监听失败"),
    (3, "数据库升级失败"),
    (99, "启动失败"),
])
def test_launched_process_exit_is_reported(returncode, hint):
    popen = ExitedPopen(returncode)
    with pytest.raises(ipc.LaunchedProcessExited) as info:
        ipc.open_session(_unique_name(), launch=['main.exe'], launch_timeout=5.0, popen=popen,
                         sleep=lambda s: None, mutex_check=lambda _n: False)
    assert info.value.exit_code == ipc.EXIT_CONNECTION
    assert info.value.returncode == returncode
    assert hint in str(info.value) and f"退出码 {returncode}" in str(info.value)
    assert len(popen.calls) == 1


def test_launched_secondary_exit_zero_keeps_waiting_while_mutex_exists():
    """退出码 0 而互斥量仍在：是"已有实例在启动"的从实例，接着等管道（到超时）。"""
    popen = ExitedPopen(0)
    checks = []

    def mutex_check(full):
        checks.append(full)
        return len(checks) > 2      # 拉起前两个名字都不存在；之后另一份实例已持有

    with pytest.raises(ipc.PipeTimeout):
        ipc.open_session(_unique_name(), launch=['main.exe'], launch_timeout=0.3, popen=popen,
                         sleep=lambda s: threading.Event().wait(0.01), mutex_check=mutex_check)
    assert len(popen.calls) == 1


@pytest.mark.parametrize('existing', ['global', 'local'])
def test_instance_starting_checks_global_then_local(existing):
    """实例互斥量用全局名；全局名创建失败时实例退回会话局部名——两个都算"正在启动"。"""
    from utils.instance_name import instance_mutex_names
    name = _unique_name()
    global_name, local_name = instance_mutex_names(name)
    assert global_name == 'Global\\' + name and local_name == 'Local\\' + name
    present = {'global': global_name, 'local': local_name}[existing]
    popen = FakePopen()
    with pytest.raises(ipc.PipeTimeout):
        ipc.open_session(name, launch=['main.exe'], launch_timeout=0.2, popen=popen,
                         sleep=lambda s: threading.Event().wait(0.01),
                         mutex_check=lambda full: full == present)
    assert popen.calls == []


def test_mutex_exists_sees_real_global_mutex():
    """pmctl 的 OpenMutexW 用与实例相同的全局名。"""
    import ctypes
    name = instance_mutex_name(_unique_name())
    assert name.startswith('Global\\')
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, name)
    assert handle, ctypes.get_last_error()
    try:
        assert ipc.mutex_exists(name) is True
        assert ipc.mutex_exists('Local\\' + name[len('Global\\'):]) is False
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def test_launch_command_idle_exit():
    command = ipc.launch_command(r'D:\数据', frozen=True, executable=r'C:\App\pmctl.exe',
                                 main_exe_name='进程监控助手.exe', idle_exit_s=300)
    assert command == [r'C:\App\进程监控助手.exe', '--background', '--idle-exit-s', '300',
                       '--data-dir', r'D:\数据']
    assert ipc.launch_command(None, frozen=True, executable=r'C:\App\pmctl.exe',
                              main_exe_name='m.exe', idle_exit_s=-5)[2:] == ['--idle-exit-s', '0']
    assert ipc.DEFAULT_IDLE_EXIT_S == 300



class ClosingPopen(FakePopen):
    """拉起后立刻开始监听、读到"已关闭命令行控制"后断开并以 0 退出的"进程"：
    断开时 poll 还是 None，wait 之后才拿到退出码。"""

    def __init__(self):
        super().__init__()
        self.returncode = None
        self.waited = []

    def __call__(self, command, **kwargs):
        super().__call__(command, **kwargs)
        return self

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited.append(timeout)
        self.returncode = 0
        return 0


def test_launched_instance_that_hangs_up_reports_exit_reason(qapp):
    """无窗口实例先监听管道、读到设置后关闭管道退出：pmctl 等它退出，按退出码 0 提示
    "设置里已关闭命令行控制"，而不是笼统的"连接已被关闭"。"""
    name = _unique_name()
    srv = IpcServer(name, lambda conn, request: conn.abort())
    popen = ClosingPopen()
    thread, box = _launch_in_thread(name, launch=['main.exe'], launch_timeout=5.0, popen=popen,
                                    sleep=lambda s: threading.Event().wait(min(s, 0.05)),
                                    mutex_check=lambda _n: False)
    try:
        assert process_events_until(popen.started.is_set, 5.0)
        assert srv.listen()
        assert process_events_until(lambda: not thread.is_alive(), 10.0)
    finally:
        srv.close()
        srv.deleteLater()
    error = box.get('error')
    assert isinstance(error, ipc.LaunchedProcessExited), error
    assert "设置里已关闭命令行控制" in str(error)
    assert popen.waited == [ipc.LAUNCHED_EXIT_WAIT_S]
