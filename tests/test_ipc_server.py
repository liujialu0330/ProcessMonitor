"""
IPC 服务端（core/ipc/server.py）与命令处理器门禁（core/ipc/commands.py）：

- 真实 QLocalServer + 同进程 QLocalSocket 客户端：粘包、半行、超长行断开、旧版裸 activate、
  断开后不再访问连接、处理器异常回 internal、100 条连续请求不丢不乱序、DACL 选项、客户端 PID；
- 命令处理器：unknown_command / disabled / not_ready / forbidden 的门禁顺序、launch 未支持、
  无窗口模式的 activate、经真实管道连续 100 次 marker 的顺序与默认标签序列；
- 评审修复：身份在连接时确定（R1）、无窗口模式关闭自动化后仍可 quit（R2）、方案挂接失败回滚
  （R3）、异常路径不留延迟回复（R4）、资源上限（R6）、读不到自身完整性时拒绝危险命令、
  按名解析统一与拒绝监控自身、文本长度上限。
"""
import json
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

from core.ipc import protocol as p
from core.ipc import server as server_mod
from core.ipc.commands import CommandContext, CommandDispatcher, CommandSettings
from core.ipc.server import LEGACY_ACTIVATE, ClientIdentity, IpcServer
from core.monitor_manager import BASELINE_LABEL, MonitorManager, round_label
from tests.helpers import process_events_until, stop_and_join


def _unique_name() -> str:
    return f"pm_test_{uuid.uuid4().hex[:12]}"


class Recorder:
    """记录请求的处理器：普通命令立即回 ok；defer 不回（模拟延迟回复）；boom 抛异常。"""

    def __init__(self):
        self.requests = []
        self.conns = []

    def __call__(self, conn, request):
        self.requests.append(request)
        self.conns.append(conn)
        if request.cmd == 'boom':
            raise RuntimeError("处理器炸了")
        if request.cmd == 'defer':
            return
        conn.send_ok(request.id, {'cmd': request.cmd, 'n': request.args.get('n')})


class Client:
    """同进程的 QLocalSocket 客户端：write 原样写字节，replies(n) 处理事件直到收到 n 行。"""

    def __init__(self, name: str):
        self.sock = QLocalSocket()
        self.sock.connectToServer(name)
        assert self.sock.waitForConnected(2000), self.sock.errorString()
        self.framer = p.LineFramer()
        self.lines = []

    def write(self, data: bytes) -> None:
        self.sock.write(data)
        self.sock.flush()

    def _drain(self) -> None:
        if self.sock.bytesAvailable():
            for line in self.framer.feed(bytes(self.sock.readAll())):
                self.lines.append(json.loads(line))

    def replies(self, count: int, timeout: float = 5.0):
        def enough():
            self._drain()
            return len(self.lines) >= count
        assert process_events_until(enough, timeout), f"只收到 {len(self.lines)} 行"
        return self.lines

    def disconnected(self, timeout: float = 3.0) -> bool:
        return process_events_until(
            lambda: self.sock.state() == QLocalSocket.UnconnectedState, timeout)


@pytest.fixture
def sleeper():
    """一个可被监控的子进程（不能监控本测试进程自身：服务端拒绝监控本程序）。"""
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    yield proc
    proc.kill()
    proc.wait(10)


@pytest.fixture
def served(qapp):
    recorder = Recorder()
    server = IpcServer(_unique_name(), recorder)
    assert server.listen()
    clients = []

    def connect() -> Client:
        client = Client(server.name)
        clients.append(client)
        return client

    yield server, recorder, connect
    for client in clients:
        client.sock.abort()
    process_events_until(lambda: not server.connections(), 1.0)
    server.close()
    server.deleteLater()
    qapp.processEvents()


def _requests(*items) -> bytes:
    return b''.join(p.encode_request(i, cmd, args) for i, cmd, args in items)


# ---------- 服务端：传输与分帧 ----------

def test_listen_restricts_pipe_to_current_user(served):
    server, _, _ = served
    assert server.is_listening()
    assert server._server.socketOptions() == QLocalServer.UserAccessOption


def test_sticky_packets_get_replies_in_order(served):
    _, recorder, connect = served
    client = connect()
    client.write(_requests((1, 'a', {}), (2, 'b', {}), (3, 'c', {})))
    replies = client.replies(3)
    assert [r['id'] for r in replies] == [1, 2, 3]
    assert [r['result']['cmd'] for r in replies] == ['a', 'b', 'c']


def test_half_line_waits_for_rest(served, qapp):
    _, recorder, connect = served
    client = connect()
    data = p.encode_request(1, 'ping')
    client.write(data[:5])
    process_events_until(lambda: False, 0.1)
    assert recorder.requests == []
    client.write(data[5:])
    assert client.replies(1)[0]['ok'] is True


def test_hundred_requests_not_lost_or_reordered(served):
    _, recorder, connect = served
    client = connect()
    blob = _requests(*[(i, 'marker', {'n': i}) for i in range(100)])
    # 任意切块写入（模拟管道把数据切成不规则的段）
    for start in range(0, len(blob), 997):
        client.write(blob[start:start + 997])
    replies = client.replies(100)
    assert [r['id'] for r in replies] == list(range(100))
    assert [r['result']['n'] for r in replies] == list(range(100))
    assert [req.args['n'] for req in recorder.requests] == list(range(100))


def test_invalid_json_gets_bad_request_and_connection_survives(served):
    _, _, connect = served
    client = connect()
    client.write(b'{not json}\n')
    client.write(p.encode_request(2, 'ping'))
    first, second = client.replies(2)
    assert first['ok'] is False and first['error']['code'] == p.ERR_BAD_REQUEST
    assert first['id'] is None
    assert second['ok'] is True and second['id'] == 2


def test_version_mismatch_reports_supported(served):
    _, recorder, connect = served
    client = connect()
    client.write(b'{"v":2,"id":9,"cmd":"ping"}\n')
    reply = client.replies(1)[0]
    assert reply['id'] == 9
    assert reply['error']['code'] == p.ERR_BAD_REQUEST
    assert reply['error']['supported'] == [1]
    assert recorder.requests == []


def test_oversize_line_is_rejected_and_disconnected(served, monkeypatch):
    monkeypatch.setattr(server_mod, 'LineFramer', lambda: p.LineFramer(64))
    _, recorder, connect = served
    client = connect()
    client.write(b'x' * 200)
    reply = client.replies(1)[0]
    assert reply['error']['code'] == p.ERR_BAD_REQUEST
    assert client.disconnected()
    assert recorder.requests == []


def test_handler_exception_becomes_internal(served):
    _, _, connect = served
    client = connect()
    client.write(_requests((5, 'boom', {}), (6, 'ping', {})))
    boom, ping = client.replies(2)
    assert boom['id'] == 5 and boom['error']['code'] == p.ERR_INTERNAL
    assert ping['ok'] is True


@pytest.mark.parametrize('chunks', [[LEGACY_ACTIVATE], [b'act', b'ivate']])
def test_legacy_bare_activate(served, qapp, chunks):
    server, recorder, connect = served
    client = connect()
    for chunk in chunks:
        client.write(chunk)
        qapp.processEvents()
    client.sock.disconnectFromServer()
    assert process_events_until(lambda: recorder.requests, 3.0)
    request = recorder.requests[0]
    assert request.cmd == 'activate' and request.id is None
    assert recorder.conns[0].legacy is True


def test_request_sent_right_before_disconnect_is_still_handled(served, qapp):
    _, recorder, connect = served
    client = connect()
    client.write(p.encode_request(1, 'ping'))
    client.sock.disconnectFromServer()
    assert process_events_until(lambda: recorder.requests, 3.0)
    assert recorder.requests[0].cmd == 'ping'


def test_deferred_reply_after_disconnect_is_dropped(served):
    server, recorder, connect = served
    client = connect()
    client.write(p.encode_request(1, 'defer'))
    assert process_events_until(lambda: recorder.requests, 3.0)
    conn = recorder.conns[0]
    cancelled = []
    conn.on_closed(lambda: cancelled.append(True))
    client.sock.abort()
    assert process_events_until(lambda: conn.closed, 3.0)
    assert cancelled == [True]
    assert conn.writable is False
    # 断开后回包静默失败，不访问已释放的套接字
    assert conn.send_ok(1, {}) is False
    assert conn.send_event(1, p.EVENT_HEARTBEAT) is False
    conn.flush()
    conn.abort()
    assert process_events_until(lambda: not server.connections(), 3.0)


@pytest.mark.skipif(sys.platform != 'win32', reason="命名管道客户端身份只在 Windows 上可取")
def test_client_identity_is_resolved(served):
    _, recorder, connect = served
    client = connect()
    client.write(p.encode_request(1, 'ping'))
    client.replies(1)
    conn = recorder.conns[0]
    assert conn.client_pid() == os.getpid()
    identity = ClientIdentity()
    assert identity.own_integrity() is not None
    assert conn.client_integrity() == identity.own_integrity()


# ---------- 命令处理器：门禁 ----------

class FakeConn:
    def __init__(self, integrity=None):
        self.sent = []
        self.writable = True
        self.integrity = integrity
        self._callbacks = []

    def client_pid(self):
        return 4321

    def client_integrity(self):
        return self.integrity

    def on_closed(self, callback):
        self._callbacks.append(callback)

    def send_ok(self, request_id, result=None):
        self.sent.append({'id': request_id, 'ok': True, 'result': result})
        return True

    def send_error(self, request_id, code, message, **extra):
        self.sent.append({'id': request_id, 'ok': False, 'code': code, 'message': message,
                          **extra})
        return True

    def send_event(self, request_id, event, data=None):
        self.sent.append({'id': request_id, 'event': event, 'data': data})
        return True

    def flush(self, *args):
        pass


class FakeIdentity:
    def __init__(self, own):
        self.own = own

    def own_integrity(self):
        return self.own


def _request(cmd, args=None, request_id=1):
    return p.Request(id=request_id, cmd=cmd, args=dict(args or {}))


def _call(dispatcher, cmd, args=None, conn=None):
    conn = conn or FakeConn()
    dispatcher.handle(conn, _request(cmd, args))
    return conn.sent[-1]


@pytest.fixture
def manager(db, qapp):
    mgr = MonitorManager(db=db)
    yield mgr
    stop_and_join(mgr)


@pytest.fixture
def ready_dispatcher(manager, db):
    enabled = {'value': True}
    settings = CommandSettings(automation_enabled=lambda: enabled['value'],
                               default_metrics=lambda: ('memory_rss',))
    dispatcher = CommandDispatcher(app_version='9.9.9', settings=settings)
    dispatcher.set_context(CommandContext(manager=manager, db=db, auto_marker=None))
    dispatcher.enabled = enabled
    yield dispatcher
    dispatcher.deleteLater()


def test_unknown_command(qapp):
    dispatcher = CommandDispatcher(app_version='1')
    reply = _call(dispatcher, 'format_c')
    assert reply['code'] == p.ERR_UNKNOWN_COMMAND


def test_not_ready_before_context_but_ping_answers(qapp):
    dispatcher = CommandDispatcher(app_version='1.2.3', pid=777)
    ping = _call(dispatcher, 'ping')
    assert ping['ok'] is True
    assert ping['result']['ready'] is False and ping['result']['pid'] == 777
    assert ping['result']['proto'] == [1]
    assert _call(dispatcher, 'status')['code'] == p.ERR_NOT_READY


def test_disabled_blocks_everything_but_ping_and_activate(qapp):
    dispatcher = CommandDispatcher(
        app_version='1', settings=CommandSettings(automation_enabled=lambda: False))
    activated = []
    dispatcher.activation_requested.connect(lambda: activated.append(True))
    for cmd in ('status', 'start', 'marker', 'pause', 'resume', 'stop', 'wait', 'export',
                'verdict', 'quit'):
        assert _call(dispatcher, cmd)['code'] == p.ERR_DISABLED, cmd
    ping = _call(dispatcher, 'ping')
    assert ping['ok'] is True and ping['result']['automation_enabled'] is False
    assert _call(dispatcher, 'activate')['ok'] is True
    assert activated == [True]


def test_disabled_takes_effect_immediately(ready_dispatcher):
    assert _call(ready_dispatcher, 'status')['ok'] is True
    ready_dispatcher.enabled['value'] = False
    assert _call(ready_dispatcher, 'status')['code'] == p.ERR_DISABLED


def test_headless_activate_is_invalid_state(qapp):
    dispatcher = CommandDispatcher(app_version='1', headless=True)
    assert _call(dispatcher, 'activate')['code'] == p.ERR_INVALID_STATE


@pytest.mark.parametrize('cmd', ['start', 'stop', 'export', 'quit'])
@pytest.mark.parametrize('client', [0x2000, None])
def test_dangerous_commands_forbidden_for_lower_integrity(ready_dispatcher, cmd, client):
    ready_dispatcher.identity = FakeIdentity(0x3000)
    quit_calls = []
    ready_dispatcher.context.quit = lambda: quit_calls.append(True)
    reply = _call(ready_dispatcher, cmd, {'task_id': 'x', 'pid': 1, 'dir': 'C:\\x'},
                  conn=FakeConn(integrity=client))
    assert reply['code'] == p.ERR_FORBIDDEN
    assert quit_calls == []


def test_safe_commands_allowed_for_lower_integrity(ready_dispatcher):
    ready_dispatcher.identity = FakeIdentity(0x3000)
    conn = FakeConn(integrity=0x2000)
    assert _call(ready_dispatcher, 'status', conn=conn)['ok'] is True
    # marker 不是危险命令：越过门禁，由处理器按状态回复
    assert _call(ready_dispatcher, 'marker', {'task_id': 'all'},
                 conn=conn)['code'] == p.ERR_INVALID_STATE


def test_equal_integrity_passes_gate(ready_dispatcher):
    ready_dispatcher.identity = FakeIdentity(0x2000)
    reply = _call(ready_dispatcher, 'start', {'pid': 1, 'launch': 'x.exe'},
                  conn=FakeConn(integrity=0x2000))
    # 越过门禁，由处理器按参数回复（pid 与 launch 不能同时给）
    assert reply['code'] == p.ERR_INVALID_ARGS
    assert "launch" in reply['message']


def test_start_unknown_pid_not_found(ready_dispatcher):
    assert _call(ready_dispatcher, 'start', {'pid': 0x7FFFFFF0})['code'] == p.ERR_NOT_FOUND


def test_start_requires_exactly_one_target(ready_dispatcher):
    assert _call(ready_dispatcher, 'start', {})['code'] == p.ERR_INVALID_ARGS
    assert _call(ready_dispatcher, 'start',
                 {'pid': 1, 'name': 'a.exe'})['code'] == p.ERR_INVALID_ARGS


def test_wait_rejects_bad_until(ready_dispatcher):
    reply = _call(ready_dispatcher, 'wait', {'task_id': 'x', 'until': 'forever'})
    assert reply['code'] == p.ERR_INVALID_ARGS


def test_export_requires_absolute_dir(ready_dispatcher):
    reply = _call(ready_dispatcher, 'export', {'task_id': 'x', 'dir': 'relative\\out'})
    assert reply['code'] == p.ERR_INVALID_ARGS


def test_quit_replies_before_quitting(ready_dispatcher):
    calls = []
    ready_dispatcher.context.quit = lambda: calls.append('quit')
    conn = FakeConn()
    ready_dispatcher.handle(conn, _request('quit'))
    assert conn.sent[-1]['ok'] is True
    assert calls == []  # 回包先走，退出在下一轮事件循环
    assert process_events_until(lambda: calls, 2.0)


# ---------- 经真实管道：连续 100 次 marker ----------

def test_hundred_markers_through_pipe_keep_order(ready_dispatcher, manager, sleeper):
    server = IpcServer(_unique_name(), ready_dispatcher.handle)
    assert server.listen()
    client = Client(server.name)
    try:
        client.write(p.encode_request(0, 'start', {'pid': sleeper.pid, 'interval': 1}))
        start = client.replies(1)[0]
        assert start['ok'] is True, start
        task_id = start['result']['task_id']
        client.lines.clear()
        client.write(b''.join(p.encode_request(i, 'marker', {'task_id': task_id})
                              for i in range(1, 101)))
        replies = client.replies(100, timeout=15.0)
        assert [r['id'] for r in replies] == list(range(1, 101))
        assert all(r['ok'] for r in replies)
        labels = [r['result']['markers'][0]['label'] for r in replies]
        assert labels == [BASELINE_LABEL] + [round_label(n) for n in range(1, 100)]
        stored = manager.list_markers(task_id)
        assert len(stored) == 100
        assert sum(1 for m in stored if m.kind == 'user') == 99
    finally:
        client.sock.abort()
        server.close()
        server.deleteLater()


# ---------- R1：客户端身份在连接时确定 ----------

class ScriptedIdentity:
    """可编排的身份提供者：记录调用顺序；token 依次返回 tokens 里的值（用完为 None）。"""

    def __init__(self, *, pid=4321, process_integrity=0x2000, tokens=(), own=0x2000):
        self.pid = pid
        self.process_integrity = process_integrity
        self.tokens = list(tokens)
        self.own = own
        self.calls = []
        # 事后按 PID 查到的级别（模拟 PID 被高完整性进程复用）
        self.later = 0x4000

    def pid_of(self, socket):
        self.calls.append('pid_of')
        return self.pid

    def open_process(self, pid):
        self.calls.append(('open_process', pid))
        return 99

    def handle_integrity(self, handle):
        self.calls.append(('handle_integrity', handle))
        return self.process_integrity

    def close_handle(self, handle):
        self.calls.append(('close_handle', handle))

    def token_integrity_of(self, socket):
        self.calls.append('token')
        return self.tokens.pop(0) if self.tokens else None

    def integrity_of(self, pid):
        self.calls.append(('integrity_of', pid))
        return self.later

    def own_integrity(self):
        return self.own


def _served_with(identity, handler):
    server = IpcServer(_unique_name(), handler, identity=identity)
    assert server.listen()
    return server


def test_integrity_is_fixed_at_connect_not_looked_up_later(qapp):
    """连接时就按 PID 打开进程算出完整性级别，之后 PID 被复用也不变；句柄持有到断开。"""
    identity = ScriptedIdentity(process_integrity=0x2000)
    recorder = Recorder()
    server = _served_with(identity, recorder)
    try:
        client = Client(server.name)
        assert process_events_until(lambda: server.connections(), 3.0)
        conn = server.connections()[0]
        # 还没有任何请求，身份已经确定
        assert ('open_process', 4321) in identity.calls
        assert conn.client_integrity() == 0x2000 and conn.identity_source == 'process'
        client.write(p.encode_request(1, 'ping'))
        client.replies(1)
        identity.process_integrity = 0x4000      # 之后再查会得到别的进程的级别
        assert conn.client_integrity() == 0x2000
        assert not any(isinstance(c, tuple) and c[0] == 'integrity_of' for c in identity.calls)
        client.sock.abort()
        assert process_events_until(lambda: conn.closed, 3.0)
        assert ('close_handle', 99) in identity.calls
    finally:
        server.close()
        server.deleteLater()


def test_connection_token_wins_once_data_arrives(qapp):
    """连接时管道上还没有数据、模拟失败；读到第一批数据时取到连接令牌并以它为准。"""
    identity = ScriptedIdentity(process_integrity=0x1000, tokens=[None, 0x3000])
    recorder = Recorder()
    server = _served_with(identity, recorder)
    try:
        client = Client(server.name)
        client.write(p.encode_request(1, 'ping'))
        client.replies(1)
        conn = recorder.conns[0]
        assert conn.client_integrity() == 0x3000 and conn.identity_source == 'token'
        client.sock.abort()
    finally:
        server.close()
        server.deleteLater()


CLIENT_WRITE_AND_EXIT = r"""
import ctypes, sys
from ctypes import wintypes
k32 = ctypes.WinDLL('kernel32', use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
h = k32.CreateFileW(sys.argv[1], 0xC0000000, 0, None, 3, 0, None)
assert h and h != wintypes.HANDLE(-1).value, ctypes.get_last_error()
data = sys.argv[2].encode('ascii') + b'\n'
n = wintypes.DWORD()
assert k32.WriteFile(h, data, len(data), ctypes.byref(n), None)
k32.CloseHandle(h)
"""


@pytest.mark.skipif(sys.platform != 'win32', reason="命名管道客户端身份只在 Windows 上可取")
@pytest.mark.parametrize("foreign_late_reply", [False, True], ids=["alone", "with_foreign_late_reply"])
def test_stop_written_right_before_client_exits_is_executed(ready_dispatcher, manager, sleeper,
                                                            monkeypatch, foreign_late_reply):
    """短命客户端：写完 stop 就断开并退出，进程已不在了服务端才处理——命令照常执行
    （修复前事后按 PID 查完整性查不到，按最低处理而误拒）。处理器先等客户端进程退出、
    关掉测试自己持有的进程句柄，再交给命令处理器，保证"处理时客户端已经退出"；此时身份
    必须已经确定（连接令牌或连接时打开的进程句柄），不再依赖事后按 PID 查。"""
    from core.contracts import TaskStatus
    sent = []
    original_send = server_mod.IpcConnection._send

    def recording_send(self, encode, request_id):
        # 补丁打在类上，进程里别的服务端 / 早先用例遗留连接的延迟回包也会经过这里（机器忙时
        # 它们的定时器恰好落进本用例的事件循环）：只记本用例这台服务端的连接
        if self in server.connections():
            sent.append(json.loads(encode()))
        return original_send(self, encode, request_id)
    monkeypatch.setattr(server_mod.IpcConnection, '_send', recording_send)
    client, identified = {}, []
    if foreign_late_reply:
        # 复现全量高负载下的偶发失败：别处遗留连接的延迟回包落进本用例的事件循环
        # （曾被类级补丁记成"服务端已回复"，此时处理器根本还没跑）
        from PyQt5.QtCore import QTimer
        stale = server_mod.IpcConnection(None, None)
        QTimer.singleShot(0, lambda: stale.send_ok(7, {'late': True}))

    handler_errors = []

    def handle_after_client_exit(conn, request):
        # 处理器里的异常会被服务端吞成 internal 回复、让失败看起来像"命令没执行"：
        # 记下真实原因，断言时一并给出
        try:
            proc = client['proc']
            proc.wait(10)
            proc._handle.Close()        # 测试自己不再持有客户端进程句柄
            identified.append((conn.identity_source, conn.client_integrity()))
        except BaseException as exc:
            handler_errors.append(repr(exc))
            raise
        ready_dispatcher.handle(conn, request)

    server = IpcServer(_unique_name(), handle_after_client_exit)
    assert server.listen()
    try:
        task_id = _call(ready_dispatcher, 'start', {'pid': sleeper.pid})['result']['task_id']
        # 从这里起按真实完整性级别把关：客户端级别必须在连接时取到才能通过
        ready_dispatcher.identity = FakeIdentity(ClientIdentity().own_integrity())
        line = json.dumps({'v': 1, 'id': 1, 'cmd': 'stop', 'args': {'task_id': task_id}})
        # 管道没有缓冲时 WriteFile 要等服务端读走，所以客户端异步跑、这边转事件循环
        client['proc'] = subprocess.Popen(
            [sys.executable, '-c', CLIENT_WRITE_AND_EXIT, '\\\\.\\pipe\\' + server.name, line],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert process_events_until(lambda: sent, 15.0), (
            f"15 秒内服务端没有回复：处理器异常={handler_errors} 客户端退出码={client['proc'].poll()}")
        assert not handler_errors, handler_errors
        assert client['proc'].returncode == 0
        assert identified[0][0] in (server_mod.IDENTITY_TOKEN, server_mod.IDENTITY_PROCESS), identified
        assert identified[0][1] is not None, identified
        assert sent[0].get('ok') is True, (sent, identified)
        info = manager.get_task_info(task_id)
        assert info is None or info.is_stopping or info.status == TaskStatus.STOPPED
    finally:
        server.close()
        server.deleteLater()


# ---------- R6：资源上限 ----------

def test_connection_limit_drops_extra_connections(served, monkeypatch):
    monkeypatch.setattr(server_mod, 'MAX_CONNECTIONS', 2)
    server, recorder, connect = served
    first, second = connect(), connect()
    assert process_events_until(lambda: len(server.connections()) == 2, 3.0)
    third = connect()
    assert third.disconnected()
    assert len(server.connections()) == 2
    first.write(p.encode_request(1, 'ping'))
    second.write(p.encode_request(2, 'ping'))
    assert first.replies(1)[0]['ok'] and second.replies(1)[0]['ok']


def test_read_buffer_is_bounded(served):
    server, recorder, connect = served
    client = connect()
    client.write(p.encode_request(1, 'ping'))
    client.replies(1)
    assert recorder.conns[0]._socket.readBufferSize() == server_mod.READ_BUFFER_BYTES


def test_one_turn_handles_at_most_32_requests_and_keeps_order(qapp):
    recorder = Recorder()
    server = IpcServer(_unique_name(), recorder, identity=ScriptedIdentity())
    conn = server_mod.IpcConnection(None, None)
    blob = _requests(*[(i, 'marker', {'n': i}) for i in range(100)])
    server._consume(conn, blob)
    assert len(recorder.requests) == server_mod.MAX_REQUESTS_PER_TURN
    assert process_events_until(lambda: len(recorder.requests) == 100, 3.0)
    assert [r.args['n'] for r in recorder.requests] == list(range(100))
    server.deleteLater()


def test_abort_does_not_block_on_flush(served, monkeypatch):
    _, recorder, connect = served
    client = connect()
    client.write(p.encode_request(1, 'ping'))
    client.replies(1)
    conn = recorder.conns[0]
    monkeypatch.setattr(conn, 'flush', lambda *a: pytest.fail("abort 不应同步等待写出"))
    conn.abort()
    assert client.disconnected()


# ---------- R2：无窗口模式关闭自动化后仍可 quit ----------

def test_headless_quit_allowed_when_automation_disabled(manager, db):
    dispatcher = CommandDispatcher(
        app_version='1', headless=True,
        settings=CommandSettings(automation_enabled=lambda: False))
    calls = []
    dispatcher.set_context(CommandContext(manager=manager, db=db, auto_marker=None,
                                          quit=lambda: calls.append('quit')))
    assert _call(dispatcher, 'status')['code'] == p.ERR_DISABLED
    assert _call(dispatcher, 'quit')['ok'] is True
    assert process_events_until(lambda: calls, 2.0)
    dispatcher.deleteLater()


# ---------- 建议 1：读不到自身完整性时拒绝危险命令 ----------

@pytest.mark.parametrize('platform, expected', [('win32', p.ERR_FORBIDDEN),
                                                ('linux', p.ERR_INVALID_ARGS)])
def test_unknown_own_integrity_is_refused_on_windows(ready_dispatcher, monkeypatch, platform,
                                                     expected):
    from core.ipc import commands as commands_mod
    monkeypatch.setattr(commands_mod.sys, 'platform', platform)
    ready_dispatcher.identity = FakeIdentity(None)
    reply = _call(ready_dispatcher, 'start', {}, conn=FakeConn(integrity=0x2000))
    assert reply['code'] == expected


# ---------- R4：异常路径不留延迟回复 ----------

def _beats(conn):
    return [s for s in conn.sent if s.get('event') == p.EVENT_HEARTBEAT]


@pytest.fixture
def fast_dispatcher(manager, db):
    settings = CommandSettings(automation_enabled=lambda: True,
                               default_metrics=lambda: ('memory_rss',))
    dispatcher = CommandDispatcher(app_version='9', settings=settings, heartbeat_ms=20,
                                   wait_check_ms=20)
    dispatcher.set_context(CommandContext(manager=manager, db=db, auto_marker=None))
    yield dispatcher
    dispatcher.deleteLater()


def test_failures_after_pending_leave_no_heartbeats(fast_dispatcher, manager, sleeper,
                                                    monkeypatch):
    d = fast_dispatcher
    conn = FakeConn()
    task_id = _call(d, 'start', {'pid': sleeper.pid}, conn=conn)['result']['task_id']

    # 1) start wait=true：登记等待时出错
    monkeypatch.setattr(d.wait_service, 'add', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("登记失败")))
    with pytest.raises(RuntimeError):
        d.handle(conn, _request('start', {'name': 'no_such_proc_xyz.exe', 'wait': True}))
    # 2) wait：首次检查条件时出错
    monkeypatch.setattr(d, '_check_wait', lambda job: (_ for _ in ()).throw(
        RuntimeError("检查失败")))
    with pytest.raises(RuntimeError):
        d.handle(conn, _request('wait', {'task_id': task_id, 'until': 'stopped'}))
    # 3) verdict：建分析线程时出错
    d.context.analysis_worker_factory = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("建线程失败"))
    with pytest.raises(RuntimeError):
        d.handle(conn, _request('verdict', {'task_id': task_id}))

    assert d.has_pending() is False
    assert d._wait_jobs == []
    sent = len(conn.sent)
    process_events_until(lambda: False, 0.2)
    assert _beats(conn) == [] and len(conn.sent) == sent
    # 同一连接上的下一个请求照常
    assert _call(d, 'status', conn=conn)['ok'] is True


@pytest.mark.parametrize('cmd, args', [
    ('wait', {'until': 'stopped', 'timeout_s': 10 ** 12}),
    ('wait', {'until': 'stopped', 'timeout_s': 10 ** 400}),
    ('wait', {'until': 'elapsed>=99999999999'}),
    ('start', {'name': 'a.exe', 'wait': True, 'wait_timeout_s': 10 ** 12}),
    ('start', {'pid': 2 ** 40}),
])
def test_huge_numbers_are_invalid_args_without_pending(fast_dispatcher, cmd, args):
    args = dict(args)
    if cmd == 'wait':
        args['task_id'] = 'x'
    conn = FakeConn()
    reply = _call(fast_dispatcher, cmd, args, conn=conn)
    assert reply['code'] == p.ERR_INVALID_ARGS, reply
    assert fast_dispatcher.has_pending() is False


# ---------- R6：每连接延迟回复上限、verdict 并发上限 ----------

def test_pending_per_connection_limit(fast_dispatcher):
    conn = FakeConn()
    request = {'name': 'no_such_proc_xyz.exe', 'wait': True}
    for _ in range(4):
        fast_dispatcher.handle(conn, _request('start', request))
    assert len(fast_dispatcher._pending) == 4
    reply = _call(fast_dispatcher, 'start', request, conn=conn)
    assert reply['code'] == p.ERR_BUSY
    # 别的连接不受影响
    other = FakeConn()
    fast_dispatcher.handle(other, _request('start', request))
    assert len(fast_dispatcher._pending) == 5
    for pending in list(fast_dispatcher._pending):
        pending.cancel()


class _IdleWorker:
    """不会结束的分析线程替身。"""

    def __init__(self, *args, **kwargs):
        from PyQt5.QtCore import QObject, pyqtSignal

        class Signals(QObject):
            result = pyqtSignal(object)
            failed = pyqtSignal(str)
        self._signals = Signals()
        self.result, self.failed = self._signals.result, self._signals.failed

    def start(self):
        pass

    def cancel(self):
        pass


def test_verdict_concurrency_limit(fast_dispatcher, sleeper):
    d = fast_dispatcher
    task_id = _call(d, 'start', {'pid': sleeper.pid})['result']['task_id']
    d.context.analysis_worker_factory = _IdleWorker
    d.handle(FakeConn(), _request('verdict', {'task_id': task_id}))
    d.handle(FakeConn(), _request('verdict', {'task_id': task_id}))
    assert _call(d, 'verdict', {'task_id': task_id})['code'] == p.ERR_BUSY
    for pending in list(d._pending):
        pending.cancel()


# ---------- R5：被测进程退出是否在停止条件达成之前 ----------

class _ResultWorker(_IdleWorker):
    snapshot = None

    def start(self):
        from PyQt5.QtCore import QTimer
        data = SimpleNamespace(snapshots={'memory_rss': _ResultWorker.snapshot})
        QTimer.singleShot(0, lambda: self.result.emit(data))


def _snapshot(verdict):
    return SimpleNamespace(error=None, judgement=SimpleNamespace(verdict=verdict, reason=None),
                           points=[1, 2], stats={'count': 2}, baseline=None, total_count=2,
                           rounds=[])


@pytest.mark.parametrize('plan_rounds, rounds_done, expected, before', [
    (None, 0, 'target_exited', True),       # 没有停止条件：被测进程退出即异常
    (5, 2, 'target_exited', True),          # 条件未达成就退出
    (2, 2, 'stable', False),                # 条件达成后才退出：照常判定，退出信息另附
])
def test_verdict_target_exit_relative_to_stop_condition(fast_dispatcher, manager, sleeper,
                                                        monkeypatch, plan_rounds, rounds_done,
                                                        expected, before):
    from core.contracts import StopReason
    d = fast_dispatcher
    args = {'pid': sleeper.pid}
    if plan_rounds:
        args['plan'] = {'stop_after_rounds': plan_rounds}
    task_id = _call(d, 'start', args)['result']['task_id']
    monkeypatch.setattr(d, '_rounds', lambda _tid: rounds_done)
    d._on_task_stopped(task_id, StopReason.PROCESS_EXITED)
    _ResultWorker.snapshot = _snapshot('stable')
    d.context.analysis_worker_factory = _ResultWorker
    conn = FakeConn()
    d.handle(conn, _request('verdict', {'task_id': task_id}))
    assert process_events_until(lambda: any('ok' in s for s in conn.sent), 3.0)
    result = conn.sent[-1]['result']
    assert result['verdict'] == expected
    assert result['target_exit'] == {'reason': 'process_exited', 'before_stop_condition': before}


def test_stop_before_task_row_keeps_plan_for_verdict(fast_dispatcher, manager, sleeper,
                                                     monkeypatch):
    """任务在采集线程写入任务行之前就结束：方案存不进库，必须留在内存里，否则判定拿不到
    停止条件，会把"条件已达成后才退出"误判成 target_exited（曾是间歇失败的根因）。"""
    from core.automation.plan import AutomationPlan
    from core.contracts import StopReason
    d = fast_dispatcher
    task_id = _call(d, 'start', {'pid': sleeper.pid})['result']['task_id']
    plan = AutomationPlan(stop_after_rounds=2)
    d._unsaved_plans[task_id] = plan
    monkeypatch.setattr(d.context.db.tasks, 'save_plan', lambda *_a, **_k: False)
    monkeypatch.setattr(d, '_rounds', lambda _tid: 2)
    d._on_task_stopped(task_id, StopReason.PROCESS_EXITED)
    assert d._plan_of(task_id) is plan
    assert d._exited_before_condition(task_id, None, StopReason.PROCESS_EXITED) is False


# ---------- R3：方案挂接失败时回滚任务 ----------

def test_create_rolls_back_when_plan_attach_fails(manager, db, sleeper):
    class BrokenAuto:
        def attach(self, task_id, plan):
            raise OverflowError("计时溢出")

        def detach(self, task_id):
            self.detached = task_id

        def plan_of(self, task_id):
            return None

    auto = BrokenAuto()
    d = CommandDispatcher(app_version='9', settings=CommandSettings(
        default_metrics=lambda: ('memory_rss',)))
    d.set_context(CommandContext(manager=manager, db=db, auto_marker=auto))
    reply = _call(d, 'start', {'pid': sleeper.pid, 'plan': {'timed_marker_seconds': 60}})
    assert reply['code'] == p.ERR_INTERNAL
    assert manager.list_task_infos() == []
    assert manager.get_task_count() == 0
    assert d._unsaved_plans == {}
    assert getattr(auto, 'detached', None) is not None
    assert db.get_task(auto.detached) is None
    d.deleteLater()


# ---------- 建议 3 / 4：按名解析统一、拒绝监控自身、长度上限 ----------

def test_start_refuses_own_process(ready_dispatcher):
    reply = _call(ready_dispatcher, 'start', {'pid': os.getpid()})
    assert reply['code'] == p.ERR_INVALID_ARGS and "本程序自身" in reply['message']


class _Lookup:
    def __init__(self, matches):
        self.matches = matches

    def find_pids_by_name(self, name):
        return list(self.matches.get(name, []))

    def identity_of(self, pid):
        return None


def test_name_resolving_to_own_process_is_refused(ready_dispatcher):
    ready_dispatcher.context.process_lookup = _Lookup({'me.exe': [(os.getpid(), 1.0)]})
    ready_dispatcher.wait_service._query = ready_dispatcher.context.process_lookup
    reply = _call(ready_dispatcher, 'start', {'name': 'me.exe'})
    assert reply['code'] == p.ERR_INVALID_ARGS


def test_waited_process_with_duplicates_is_ambiguous_like_name_start(ready_dispatcher):
    """等待到进程出现时若已有多个同名进程：与 start name=… 一致，回 invalid_args 并列出 PID。"""
    lookup = _Lookup({})
    ready_dispatcher.context.process_lookup = lookup
    ready_dispatcher.wait_service._query = lookup
    conn = FakeConn()
    ready_dispatcher.handle(conn, _request('start', {'name': 'dup.exe', 'wait': True}))
    assert ready_dispatcher.has_pending()
    lookup.matches['dup.exe'] = [(111, 1.0), (222, 2.0)]
    ready_dispatcher.poll_start_waits()
    reply = conn.sent[-1]
    assert reply['code'] == p.ERR_INVALID_ARGS and reply['pids'] == [111, 222]
    assert ready_dispatcher.has_pending() is False


@pytest.mark.parametrize('cmd, args', [
    ('marker', {'task_id': 'all', 'label': 'x' * 201}),
    ('start', {'pid': 1, 'note': 'x' * 2001}),
])
def test_text_length_limits(ready_dispatcher, cmd, args):
    reply = _call(ready_dispatcher, cmd, args)
    assert reply['code'] == p.ERR_INVALID_ARGS and "字符" in reply['message']
