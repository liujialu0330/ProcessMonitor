"""单实例（命名互斥量判据 + QLocalServer/QLocalSocket 通道）与窗口激活用例：用唯一实例名，不影响真实实例"""
import hashlib
import logging
import os
import re
import subprocess
import sys
import uuid

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtNetwork import QLocalServer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QWidget

import app.single_instance as single_instance_module
from app.single_instance import (
    CONNECT_RETRY_TOTAL_MS, ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS, INSTALLER_MUTEX_NAME,
    SingleInstanceGuard, activate_main_window, data_dir_hash, normalize_data_dir,
    single_instance_name,
)
from tests.helpers import poll_until


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != 'win32', reason="命名互斥量仅 Windows 可用")


@pytest.fixture
def unique_name():
    return f"ProcessMonitorTest_{uuid.uuid4().hex}"


def _guard(name, **kwargs):
    """测试用守卫：默认不创建安装器探测互斥量，避免测试进程冒充正在运行的程序"""
    kwargs.setdefault('installer_mutex_name', None)
    return SingleInstanceGuard(name, **kwargs)


def _process_events_until(condition, timeout=5.0):
    return poll_until(lambda: (QTest.qWait(20), condition())[1], timeout=timeout, interval=0)


class _FakeMutexApi:
    """内存版互斥量接口：按名称记录是否已存在，可指定某些名称返回失败"""

    def __init__(self, existing=(), fail=None):
        self.existing = set(existing)
        self.fail = dict(fail or {})     # 名称 -> 错误码（句柄为 None）
        self.created = []
        self.closed = []
        self._next = 100

    def create(self, name):
        self.created.append(name)
        if name in self.fail:
            return None, self.fail[name]
        self._next += 1
        if name in self.existing:
            return self._next, ERROR_ALREADY_EXISTS
        self.existing.add(name)
        return self._next, 0

    def close(self, handle):
        self.closed.append(handle)


class _FakeTime:
    """可注入的假时钟：sleep 只推进时钟并记录毫秒数"""

    def __init__(self):
        self.now = 1000.0
        self.sleeps_ms = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps_ms.append(round(seconds * 1000))
        self.now += seconds


# ========== 真实互斥量 + 真实管道 ==========

def test_first_instance_becomes_primary(qapp, unique_name):
    guard = _guard(unique_name)
    assert guard.notify_existing_instance() is False
    assert guard.listen() is True


def _notify_from_other_process(name: str) -> subprocess.Popen:
    """另起一个进程扮演"第二个实例"：判定已有实例并发送激活消息，已有实例时退出码为 0"""
    code = os.linesep.join([
        'import sys',
        'from PyQt5.QtWidgets import QApplication',
        'app = QApplication([])',
        'from app.single_instance import SingleInstanceGuard',
        f'guard = SingleInstanceGuard({name!r}, installer_mutex_name=None)',
        'sys.exit(0 if guard.notify_existing_instance() else 1)',
    ])
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
    return subprocess.Popen([sys.executable, '-c', code], cwd=PROJECT_ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_second_instance_notifies_primary(qapp, unique_name):
    """真实场景：主实例在事件循环中运行，另一个进程发送激活消息并退出"""
    primary = _guard(unique_name)
    assert primary.notify_existing_instance() is False
    assert primary.listen() is True
    activations = []
    primary.activation_requested.connect(lambda: activations.append(1))

    for expected in (1, 2):   # 连续两次启动，每次都会触发激活
        proc = _notify_from_other_process(unique_name)
        assert _process_events_until(
            lambda: proc.poll() is not None and len(activations) >= expected, timeout=30), (
            f"主实例未收到第 {expected} 次激活消息")
        assert proc.returncode == 0, "第二实例应判定已有实例并退出"
    assert activations == [1, 1]


@WINDOWS_ONLY
def test_real_mutex_already_exists_means_secondary(qapp, unique_name, monkeypatch):
    """同一进程内第二个守卫拿到 ERROR_ALREADY_EXISTS：判定为从实例，即使管道没人监听也退出"""
    primary = _guard(unique_name)
    assert primary.notify_existing_instance() is False      # 持有互斥量，但不 listen()
    fake = _FakeTime()
    secondary = _guard(unique_name, sleep=fake.sleep, clock=fake.clock)
    assert secondary.notify_existing_instance() is True
    assert sum(fake.sleeps_ms) == CONNECT_RETRY_TOTAL_MS    # 一直重试到总时长上限


@WINDOWS_ONLY
def test_mutex_is_the_only_criterion_not_the_pipe(qapp, unique_name):
    """回归：管道有人监听但互斥量不存在时，本进程仍是主实例（旧方案会被管道判成从实例）"""
    listener = QLocalServer()
    assert listener.listen(unique_name)
    try:
        guard = _guard(unique_name)
        assert guard.notify_existing_instance() is False
    finally:
        listener.close()


@WINDOWS_ONLY
def test_installer_mutex_is_held_by_primary(qapp, unique_name):
    """主实例持有安装器探测互斥量（会话局部名），OpenMutexW 能打开它"""
    import ctypes
    from ctypes import wintypes
    installer_name = f"{unique_name}.Running"
    guard = SingleInstanceGuard(unique_name, installer_mutex_name=installer_name)
    assert guard.notify_existing_instance() is False

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_mutex.restype = wintypes.HANDLE
    synchronize = 0x00100000
    # Inno Setup 用不带前缀的名称查找，即当前会话的局部命名空间
    handle = open_mutex(synchronize, False, installer_name)
    assert handle, "安装器按 AppMutex 名称应能找到正在运行的程序"
    kernel32.CloseHandle(handle)


def test_unrelated_message_does_not_activate(qapp, unique_name):
    from PyQt5.QtNetwork import QLocalSocket
    primary = _guard(unique_name)
    primary.listen()
    activations = []
    primary.activation_requested.connect(lambda: activations.append(1))

    sock = QLocalSocket()
    sock.connectToServer(unique_name)
    assert sock.waitForConnected(1000)
    sock.write(b'garbage')
    sock.flush()
    sock.waitForBytesWritten(1000)
    QTest.qWait(200)

    assert activations == []


def test_listen_failure_degrades_gracefully(qapp, unique_name, monkeypatch, caplog):
    monkeypatch.setattr(QLocalServer, 'listen', lambda self, name: False)
    guard = _guard(unique_name)
    with caplog.at_level(logging.WARNING, logger="app.single_instance"):
        assert guard.listen() is False     # 不抛异常，允许继续启动
    assert any("单实例监听失败" in r.getMessage() for r in caplog.records)


# ========== 名称规则 ==========

def test_server_name_contains_sanitized_username(monkeypatch):
    monkeypatch.setattr(single_instance_module.getpass, 'getuser', lambda: 'Zhang San/x')
    name = single_instance_name()
    assert name == "ProcessMonitor_Zhang_San_x"  # 空格与斜杠均被替换为下划线


def test_server_name_falls_back_when_getuser_fails(monkeypatch):
    def _fail():
        raise OSError("no user")
    monkeypatch.setattr(single_instance_module.getpass, 'getuser', _fail)
    monkeypatch.setenv('USERNAME', 'fallback')
    assert single_instance_name() == "ProcessMonitor_fallback"


def test_name_contains_data_dir_hash(monkeypatch, tmp_path):
    """实例名 = ProcessMonitor_<用户名>_<数据目录规范化路径 sha1 前 12 位>"""
    monkeypatch.setattr(single_instance_module.getpass, 'getuser', lambda: 'Zhang San/x')
    data_dir = str(tmp_path / "数据")
    expected_hash = hashlib.sha1(
        os.path.normcase(os.path.abspath(data_dir)).encode('utf-8')).hexdigest()[:12]
    assert data_dir_hash(data_dir) == expected_hash
    assert single_instance_name(data_dir) == f"ProcessMonitor_Zhang_San_x_{expected_hash}"
    assert re.fullmatch(r"ProcessMonitor_Zhang_San_x_[0-9a-f]{12}", single_instance_name(data_dir))


def test_different_data_dirs_get_different_names(tmp_path):
    assert single_instance_name(str(tmp_path / "a")) != single_instance_name(str(tmp_path / "b"))


def test_equivalent_data_dir_spellings_share_one_name(tmp_path):
    """同一目录的不同写法（尾部分隔符、.. 回退、Windows 大小写与斜杠方向）得到同一实例名"""
    base = str(tmp_path / "data")
    variants = [base, base + os.sep, os.path.join(base, "sub", "..")]
    if sys.platform == 'win32':
        variants += [base.upper(), base.replace('\\', '/')]
    assert {normalize_data_dir(v) for v in variants} == {normalize_data_dir(base)}
    assert {single_instance_name(v) for v in variants} == {single_instance_name(base)}


def test_guard_name_derived_from_data_dir(tmp_path):
    data_dir = str(tmp_path / "d")
    assert SingleInstanceGuard(data_dir=data_dir).name == single_instance_name(data_dir)
    assert SingleInstanceGuard("explicit", data_dir=data_dir).name == "explicit"


# ========== 互斥量判定（假接口） ==========

def test_mutex_uses_global_namespace_and_installer_names(qapp, unique_name):
    """实例互斥量用全局名（与管道一样跨会话唯一，名字含用户名）"""
    api = _FakeMutexApi()
    guard = SingleInstanceGuard(unique_name, mutex_api_factory=lambda: api)
    assert guard.notify_existing_instance() is False
    assert api.created == [
        "Global\\" + unique_name,
        "Local\\" + INSTALLER_MUTEX_NAME,
        "Global\\" + INSTALLER_MUTEX_NAME,
    ]
    assert api.closed == []        # 主实例持有全部句柄，不关闭


def test_existing_mutex_means_secondary_and_closes_handle(qapp, unique_name, monkeypatch):
    api = _FakeMutexApi(existing={"Global\\" + unique_name})
    sent = []
    guard = _guard(unique_name, mutex_api_factory=lambda: api)
    monkeypatch.setattr(guard, '_send_activation', lambda: sent.append(1) or True)
    assert guard.notify_existing_instance() is True
    assert sent == [1]
    assert len(api.closed) == 1    # 指向既有互斥量的句柄用完即关
    assert api.created == ["Global\\" + unique_name]   # 从实例不创建安装器互斥量


def test_access_denied_means_secondary(qapp, unique_name, monkeypatch, caplog):
    """同名互斥量由更高权限的实例创建、当前进程打不开：按已有实例处理"""
    api = _FakeMutexApi(fail={"Global\\" + unique_name: ERROR_ACCESS_DENIED})
    guard = _guard(unique_name, mutex_api_factory=lambda: api)
    monkeypatch.setattr(guard, '_send_activation', lambda: True)
    with caplog.at_level(logging.WARNING, logger="app.single_instance"):
        assert guard.notify_existing_instance() is True
    assert any("无权访问" in r.getMessage() for r in caplog.records)


def test_other_create_error_allows_start(qapp, unique_name, monkeypatch, caplog):
    api = _FakeMutexApi(fail={"Global\\" + unique_name: 87, "Local\\" + unique_name: 87})
    guard = _guard(unique_name, mutex_api_factory=lambda: api)
    monkeypatch.setattr(guard, '_send_activation', lambda: pytest.fail("不应连接管道"))
    with caplog.at_level(logging.WARNING, logger="app.single_instance"):
        assert guard.notify_existing_instance() is False
    assert any("创建单实例互斥量失败" in r.getMessage() for r in caplog.records)


def test_global_create_failure_falls_back_to_local(qapp, unique_name, caplog):
    """全局名创建失败（非"已存在"、非"拒绝访问"）：退回会话局部名并记日志"""
    api = _FakeMutexApi(fail={"Global\\" + unique_name: 87})
    guard = SingleInstanceGuard(unique_name, mutex_api_factory=lambda: api,
                                installer_mutex_name=None)
    with caplog.at_level(logging.WARNING, logger="app.single_instance"):
        assert guard.notify_existing_instance() is False
    assert api.created == ["Global\\" + unique_name, "Local\\" + unique_name]
    assert any("Local" in r.getMessage() or "会话" in r.getMessage() for r in caplog.records)
    # 退回的局部名已被另一份（同样退回的）实例持有：照常判为从实例
    other = _FakeMutexApi(fail={"Global\\" + unique_name: 87},
                          existing={"Local\\" + unique_name})
    guard = SingleInstanceGuard(unique_name, mutex_api_factory=lambda: other,
                                installer_mutex_name=None)
    guard._send_activation = lambda: True
    assert guard.notify_existing_instance() is True


def test_global_access_denied_does_not_fall_back(qapp, unique_name, monkeypatch):
    """全局名"拒绝访问"说明同名实例已存在（如以管理员运行）：不退回局部名"""
    api = _FakeMutexApi(fail={"Global\\" + unique_name: ERROR_ACCESS_DENIED})
    guard = _guard(unique_name, mutex_api_factory=lambda: api)
    monkeypatch.setattr(guard, '_send_activation', lambda: True)
    assert guard.notify_existing_instance() is True
    assert api.created == ["Global\\" + unique_name]


def test_real_mutex_is_global_and_visible_to_pmctl(qapp, unique_name):
    """真实 Win32：主实例持有的是 Global\\ 名，pmctl 的 OpenMutexW 用同一名字能看到"""
    if sys.platform != 'win32':
        pytest.skip("只在 Windows 上有命名互斥量")
    from core.ipc.client import instance_starting, mutex_exists
    guard = SingleInstanceGuard(unique_name, installer_mutex_name=None)
    assert guard.notify_existing_instance(activate=False) is False
    try:
        assert mutex_exists("Global\\" + unique_name) is True
        assert mutex_exists("Local\\" + unique_name) is False
        assert instance_starting(unique_name, mutex_exists) is True
    finally:
        import ctypes
        ctypes.WinDLL('kernel32').CloseHandle(ctypes.c_void_p(guard._instance_mutex))


def test_installer_mutex_failures_are_ignored(qapp, unique_name):
    """Global\\ 常因权限失败、会话局部名可能已被另一数据目录的实例创建：都不影响成为主实例"""
    api = _FakeMutexApi(existing={"Local\\" + INSTALLER_MUTEX_NAME},
                        fail={"Global\\" + INSTALLER_MUTEX_NAME: ERROR_ACCESS_DENIED})
    guard = SingleInstanceGuard(unique_name, mutex_api_factory=lambda: api)
    assert guard.notify_existing_instance() is False
    assert api.closed == []        # 已存在的安装器互斥量句柄也保留，前一个实例退出后它仍存在


def test_ctypes_unavailable_allows_start(qapp, unique_name, monkeypatch, caplog):
    """kernel32 不可用（ctypes 加载失败）：降级为允许启动并记日志"""
    def _broken():
        raise OSError("kernel32 不可用")
    monkeypatch.setattr(single_instance_module, '_WinMutexApi', _broken)
    monkeypatch.setattr(single_instance_module.sys, 'platform', 'win32')
    guard = _guard(unique_name)
    monkeypatch.setattr(guard, '_send_activation', lambda: pytest.fail("不应连接管道"))
    with caplog.at_level(logging.ERROR, logger="app.single_instance"):
        assert guard.notify_existing_instance() is False
    assert any("互斥量不可用" in r.getMessage() for r in caplog.records)


def test_ctypes_call_raising_allows_start(qapp, unique_name, monkeypatch, caplog):
    class _Raising(_FakeMutexApi):
        def create(self, name):
            raise OSError("调用失败")
    guard = _guard(unique_name, mutex_api_factory=_Raising)
    with caplog.at_level(logging.ERROR, logger="app.single_instance"):
        assert guard.notify_existing_instance() is False
    assert any("互斥量不可用" in r.getMessage() for r in caplog.records)


def test_non_windows_falls_back_to_pipe_criterion(qapp, unique_name, monkeypatch):
    """平台不支持互斥量（接口工厂返回 None）：退化为管道判据，连上即为从实例"""
    guard = _guard(unique_name, mutex_api_factory=lambda: None)
    monkeypatch.setattr(guard, '_send_activation', lambda: True)
    assert guard.notify_existing_instance() is True
    monkeypatch.setattr(guard, '_send_activation', lambda: False)
    assert guard.notify_existing_instance() is False


# ========== 从实例退避重试 ==========

def _secondary_with_fake_time(name, monkeypatch, results):
    """互斥量已存在的从实例；_send_activation 依次返回 results（用完后一直 False）"""
    api = _FakeMutexApi(existing={"Global\\" + name})
    fake = _FakeTime()
    guard = _guard(name, mutex_api_factory=lambda: api, sleep=fake.sleep, clock=fake.clock)
    calls = []
    seq = iter(results)
    monkeypatch.setattr(guard, '_send_activation',
                        lambda: calls.append(fake.now) or next(seq, False))
    return guard, fake, calls


def test_secondary_retries_with_backoff_until_connected(qapp, unique_name, monkeypatch):
    guard, fake, calls = _secondary_with_fake_time(unique_name, monkeypatch, [False, False, True])
    assert guard.notify_existing_instance() is True
    assert fake.sleeps_ms == [50, 100]
    assert len(calls) == 3


def test_secondary_gives_up_after_about_3s_and_still_exits(qapp, unique_name, monkeypatch, caplog):
    """始终连不上：50/100/200/400/800 后按 800 继续，总等待封顶 3 s，仍返回 True（退出）"""
    guard, fake, calls = _secondary_with_fake_time(unique_name, monkeypatch, [])
    with caplog.at_level(logging.WARNING, logger="app.single_instance"):
        assert guard.notify_existing_instance() is True
    assert fake.sleeps_ms == [50, 100, 200, 400, 800, 800, 650]
    assert sum(fake.sleeps_ms) == CONNECT_RETRY_TOTAL_MS
    assert len(calls) == 8
    assert any("未连上其管道" in r.getMessage() for r in caplog.records)


def test_secondary_first_attempt_success_does_not_sleep(qapp, unique_name, monkeypatch):
    guard, fake, calls = _secondary_with_fake_time(unique_name, monkeypatch, [True])
    assert guard.notify_existing_instance() is True
    assert fake.sleeps_ms == [] and len(calls) == 1


@WINDOWS_ONLY
def test_secondary_retry_reaches_primary_that_listens_late(qapp, unique_name):
    """真实进程：主实例持有互斥量但 300 ms 后才 listen()，从实例靠退避重试仍把激活消息送到"""
    from PyQt5.QtCore import QTimer
    primary = _guard(unique_name)
    assert primary.notify_existing_instance() is False
    activations = []
    primary.activation_requested.connect(lambda: activations.append(1))

    code = os.linesep.join([
        'import logging, sys',
        'logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")',
        'from PyQt5.QtCore import QCoreApplication',
        'app = QCoreApplication([])',
        'from app.single_instance import SingleInstanceGuard',
        f'guard = SingleInstanceGuard({unique_name!r}, installer_mutex_name=None)',
        'print("READY", flush=True)',
        'sys.exit(0 if guard.notify_existing_instance() else 1)',
    ])
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
    proc = subprocess.Popen([sys.executable, '-c', code], cwd=PROJECT_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            encoding='utf-8')
    try:
        assert proc.stdout.readline().strip() == "READY"
        QTimer.singleShot(300, primary.listen)
        assert _process_events_until(
            lambda: proc.poll() is not None and activations == [1], timeout=15)
        output = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    attempts = re.search(r"第 (\d+) 次尝试连上已运行实例", output)
    assert attempts and int(attempts.group(1)) > 1, output


def test_setup_iss_app_mutex_matches_installer_mutex_name():
    """安装器 AppMutex 必须与程序持有的固定名互斥量一致（会话局部名 + Global\\ 名）"""
    with open(os.path.join(PROJECT_ROOT, 'build', 'setup.iss'), encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip().startswith('AppMutex=')]
    assert lines == [f"AppMutex={INSTALLER_MUTEX_NAME},Global\\{INSTALLER_MUTEX_NAME}"]


# ========== 窗口激活与入口 ==========

def test_activate_main_window_restores_hidden_and_minimized(qapp):
    window = QWidget()
    window.show()
    window.hide()                       # 模拟隐藏到托盘
    activate_main_window(window)
    assert window.isVisible() is True

    window.setWindowState(window.windowState() | Qt.WindowMinimized)   # 模拟最小化
    activate_main_window(window)
    assert not (window.windowState() & Qt.WindowMinimized)
    assert window.isVisible() is True
    window.close()


def test_main_module_is_thin_entry():
    """入口文件只剩 run() 调用：导入 main 不得连带导入 ui/core/data（日志须先于它们初始化）"""
    code = os.linesep.join([
        'import sys, main',
        'heavy = [m for m in sys.modules if m.split(".")[0] in ("ui", "core", "data")]',
        'sys.exit(1 if heavy else 0)',
    ])
    proc = subprocess.run([sys.executable, '-c', code], cwd=PROJECT_ROOT,
                          env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')


# ========== 2.0：JSON 激活请求、无窗口模式 ==========

def test_activate_request_is_a_protocol_request():
    from core.ipc import protocol
    from app.single_instance import ACTIVATE_REQUEST
    assert ACTIVATE_REQUEST.endswith(b'\n') and ACTIVATE_REQUEST.count(b'\n') == 1
    request = protocol.parse_request(protocol.decode_line(ACTIVATE_REQUEST[:-1]))
    assert request.cmd == 'activate' and request.version == protocol.PROTOCOL_VERSION


def test_background_secondary_does_not_notify(qapp, unique_name):
    """--background 的从实例只判定、不发激活消息，也不做连接重试"""
    primary = _guard(unique_name)
    primary.listen()
    activations = []
    primary.activation_requested.connect(lambda: activations.append(1))
    fake = _FakeTime()
    secondary = _guard(unique_name, sleep=fake.sleep, clock=fake.clock,
                       mutex_api_factory=lambda: _FakeMutexApi(
                           existing={'Global\\' + unique_name}))
    assert secondary.notify_existing_instance(activate=False) is True
    QTest.qWait(200)
    assert activations == [] and fake.sleeps_ms == []


def _activation_error_from_other_process(name: str) -> str:
    """另起进程发激活请求，返回它读到的 activation_error（字符串化）"""
    code = os.linesep.join([
        'from PyQt5.QtWidgets import QApplication',
        'app = QApplication([])',
        'from app.single_instance import SingleInstanceGuard',
        f'guard = SingleInstanceGuard({name!r}, installer_mutex_name=None)',
        'print(guard._send_activation(), guard.activation_error)',
    ])
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
    return subprocess.Popen([sys.executable, '-c', code], cwd=PROJECT_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


@pytest.mark.parametrize('background, expected', [(False, 'True None'),
                                                  (True, 'True invalid_state')])
def test_secondary_reads_activation_reply(qapp, unique_name, background, expected):
    """主实例是无窗口模式时回 invalid_state（组合根据此提示用户），有窗口时激活成功"""
    primary = _guard(unique_name, background=background)
    assert primary.listen() is True
    assert primary.dispatcher.headless is background
    activations = []
    primary.activation_requested.connect(lambda: activations.append(1))
    proc = _activation_error_from_other_process(unique_name)
    assert _process_events_until(lambda: proc.poll() is not None, timeout=30)
    assert proc.stdout.read().decode('utf-8').strip().splitlines()[-1] == expected
    assert activations == ([] if background else [1])


def test_listen_creates_dispatcher_and_server(qapp, unique_name):
    guard = _guard(unique_name, app_version='9.8.7')
    assert guard.listen() is True
    assert guard.server.is_listening()
    assert guard.dispatcher.app_version == '9.8.7'
    assert guard.dispatcher.pid == os.getpid()
    assert guard.dispatcher.identity is guard.server.identity
    assert guard.dispatcher.ready is False      # 组合根给上下文之前回 not_ready
