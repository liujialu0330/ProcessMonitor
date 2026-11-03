"""单实例（QLocalServer/QLocalSocket）与窗口激活用例：用唯一服务名，不影响真实实例"""
import os
import subprocess
import sys
import uuid

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtNetwork import QLocalServer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QWidget

import main
from main import SingleInstanceGuard, activate_main_window, single_instance_name
from tests.helpers import poll_until


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def unique_name():
    return f"ProcessMonitorTest_{uuid.uuid4().hex}"


def _process_events_until(condition, timeout=5.0):
    return poll_until(lambda: (QTest.qWait(20), condition())[1], timeout=timeout, interval=0)


def test_first_instance_becomes_primary(qapp, unique_name):
    guard = SingleInstanceGuard(unique_name)
    assert guard.notify_existing_instance() is False
    assert guard.listen() is True


def _notify_from_other_process(name: str) -> subprocess.Popen:
    """另起一个进程扮演"第二个实例"：连接并发送激活消息，已有实例时退出码为 0"""
    code = os.linesep.join([
        'import sys',
        'from PyQt5.QtWidgets import QApplication',
        'app = QApplication([])',
        'from main import SingleInstanceGuard',
        f'sys.exit(0 if SingleInstanceGuard({name!r}).notify_existing_instance() else 1)',
    ])
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8')
    return subprocess.Popen([sys.executable, '-c', code], cwd=PROJECT_ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_second_instance_notifies_primary(qapp, unique_name):
    """真实场景：主实例在事件循环中运行，另一个进程发送激活消息并退出"""
    primary = SingleInstanceGuard(unique_name)
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


def test_unrelated_message_does_not_activate(qapp, unique_name):
    from PyQt5.QtNetwork import QLocalSocket
    primary = SingleInstanceGuard(unique_name)
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
    import logging
    monkeypatch.setattr(QLocalServer, 'listen', lambda self, name: False)
    guard = SingleInstanceGuard(unique_name)
    with caplog.at_level(logging.WARNING, logger="main"):
        assert guard.listen() is False     # 不抛异常，允许继续启动
    assert any("单实例监听失败" in r.getMessage() for r in caplog.records)


def test_server_name_contains_sanitized_username(monkeypatch):
    monkeypatch.setattr(main.getpass, 'getuser', lambda: 'Zhang San/x')
    name = single_instance_name()
    assert name == "ProcessMonitor_Zhang_San_x"  # 空格与斜杠均被替换为下划线


def test_server_name_falls_back_when_getuser_fails(monkeypatch):
    def _fail():
        raise OSError("no user")
    monkeypatch.setattr(main.getpass, 'getuser', _fail)
    monkeypatch.setenv('USERNAME', 'fallback')
    assert single_instance_name() == "ProcessMonitor_fallback"


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
