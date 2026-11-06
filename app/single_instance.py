"""
单实例守卫

同一用户同时只运行一个本程序实例：第二次启动时通知已运行的实例把主窗口激活到前台，
自己随即退出。基于 QLocalServer/QLocalSocket（Windows 上为命名管道），服务名含用户名，
同一台机器上的多个用户（如远程桌面）互不影响。

使用顺序（见 app.bootstrap.run）：QApplication 创建之后、其余初始化之前构造守卫，
notify_existing_instance() 为 True 时直接退出；否则 listen() 成为主实例，
主窗口建好后把 activation_requested 连到 activate_main_window。
"""
import getpass
import logging
import os
import re
from typing import Optional

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

# 激活消息与连接超时（毫秒）
ACTIVATE_MESSAGE = b'activate'
SINGLE_INSTANCE_TIMEOUT_MS = 500


def single_instance_name() -> str:
    """单实例服务名：含用户名，避免同一台机器上多个用户（如远程桌面）互相冲突"""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get('USERNAME', 'user')
    return f"ProcessMonitor_{re.sub(r'[^0-9A-Za-z_.-]', '_', user)}"


class SingleInstanceGuard(QObject):
    """
    基于 QLocalServer/QLocalSocket 的单实例守卫。

    启动时先尝试连接已有实例：连上则发送激活消息，调用方应随即退出；
    连不上则自己监听，之后收到其他实例的激活消息时发出 activation_requested。
    任何环节失败都降级为"允许启动"并记日志，不能因为单实例机制本身阻止用户使用。
    """

    # 其他实例请求激活本实例主窗口
    activation_requested = pyqtSignal()

    def __init__(self, name: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._name = name or single_instance_name()
        self._server: Optional[QLocalServer] = None

    def notify_existing_instance(self) -> bool:
        """
        尝试通知已运行的实例激活窗口。

        Returns:
            bool: True 表示已有实例并已发送激活消息（本进程应退出）；
                  False 表示没有已运行实例（或探测失败，允许继续启动）
        """
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(SINGLE_INSTANCE_TIMEOUT_MS):
            return False
        try:
            socket.write(ACTIVATE_MESSAGE)
            socket.flush()
            if not socket.waitForBytesWritten(SINGLE_INSTANCE_TIMEOUT_MS):
                logger.warning("向已运行实例发送激活消息超时，仍按已有实例处理")
            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.UnconnectedState:
                socket.waitForDisconnected(SINGLE_INSTANCE_TIMEOUT_MS)
        except Exception:
            logger.error("通知已运行实例失败", exc_info=True)
        return True

    def listen(self) -> bool:
        """成为主实例并开始监听；失败（如命名管道被占用）记日志并返回 False，允许继续启动"""
        # 清理异常退出可能遗留的同名服务（Windows 命名管道随进程消失，此处为跨平台兜底）
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        if not server.listen(self._name):
            logger.warning("单实例监听失败，将允许多开: %s", server.errorString())
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def _on_new_connection(self):
        """接收所有排队的连接；数据可能在连接建立前就已到达，立即读一次"""
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._on_ready_read(socket)

    def _on_ready_read(self, socket: QLocalSocket):
        """只认激活消息，其他内容一律忽略"""
        data = bytes(socket.readAll())
        if data.startswith(ACTIVATE_MESSAGE):
            self.activation_requested.emit()


def activate_main_window(window):
    """显示并激活主窗口：窗口隐藏在托盘或被最小化时都要恢复"""
    window.setWindowState((window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    window.show()
    window.raise_()
    window.activateWindow()
