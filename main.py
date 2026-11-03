"""
应用程序入口
"""
import getpass
import logging
import re
import sys
import os
from typing import Optional
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from qfluentwidgets import setTheme

from ui.main_window import MainWindow
from ui.typography import configure_application_typography
from utils.logger import setup_logging
from utils.crash_handler import install_excepthook
from app_config import load_app_config, cfg
import config

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
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._on_ready_read(socket)

    def _on_ready_read(self, socket: QLocalSocket):
        data = bytes(socket.readAll())
        if data.startswith(ACTIVATE_MESSAGE):
            self.activation_requested.emit()


def activate_main_window(window):
    """显示并激活主窗口：窗口隐藏在托盘或被最小化时都要恢复"""
    window.setWindowState((window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
    window.show()
    window.raise_()
    window.activateWindow()


def main():
    """主函数"""
    # 日志与全局异常兜底须在入口最早处初始化，确保后续任何初始化异常都能被记录/兜底
    log_dir = setup_logging()
    install_excepthook(log_dir)

    # 启用高DPI缩放
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

    # 创建应用程序
    app = QApplication(sys.argv)

    # 单实例：已有实例时通知其激活窗口，本进程直接退出（尚未创建任何线程/窗口，
    # 用 os._exit 与下方退出约定保持一致）。需在 QApplication 之后、其余初始化之前。
    guard = SingleInstanceGuard()
    if guard.notify_existing_instance():
        logging.getLogger(__name__).info("检测到已运行的实例，已通知其激活窗口，本进程退出")
        os._exit(0)
    guard.listen()

    # 关闭主窗口不再意味着应用退出：默认 Qt 行为是"最后一个可见窗口关闭时自动
    # quit()"，托盘驻留要求"隐藏到托盘"时应用继续在后台运行。关闭这条隐式退出路径
    # 后，唯一真退出路径是 MainWindow.closeEvent 清理完成后显式调用的
    # QApplication.quit()。
    app.setQuitOnLastWindowClosed(False)

    # 设置应用信息
    app.setApplicationName(config.APP_NAME)
    app.setApplicationVersion(config.APP_VERSION)

    # 设置应用程序图标（任务栏和窗口图标）
    icon_path = config.get_icon_path()

    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 加载应用配置（qfluentwidgets QConfig 体系，JSON 落盘于 DATA_DIR/config.json）：
    # 必须在 QApplication 创建之后（ConfigItem 依赖的 Qt 信号机制需要它）、
    # MainWindow 创建之前（导航栏与各页面构造时已经会读取 cfg 的值，如设置页
    # 主题单选状态、监控页采集周期默认值）调用
    load_app_config()

    # 统一中文界面、原生 Qt 控件与 pyqtgraph 的字体继承链。必须在窗口构造前
    # 执行，否则已创建的 Fluent 标签会保留各自构造时拿到的旧字体。
    configure_application_typography(app)

    # 应用已保存的主题偏好：这里必须传 cfg.get(cfg.themeMode) 这个原始配置值
    # （可能是 Theme.AUTO），不能用 qconfig.theme（那是"跟随系统"解析后的具体
    # 浅/深色值）——否则每次启动都会把用户"跟随系统"的选择静默覆写成当次启动时
    # 系统所处的具体主题，设置页里的主题单选状态也会跟着显示错乱
    setTheme(cfg.get(cfg.themeMode))

    # 创建并显示主窗口
    window = MainWindow()
    window.show()
    guard.activation_requested.connect(lambda: activate_main_window(window))

    # 运行应用
    exit_code = app.exec_()

    # 关窗时 MainWindow.closeEvent 已尽力清理后台线程（shutdown_thread 按超时等待），
    # 但仍可能有线程未能在超时内停止（如下载线程阻塞在 urlopen() 的 connect() 阶段，
    # 取消标志只在读取循环内被检查，对尚未建立连接的阻塞调用不生效）。若走 Python
    # 解释器的正常关闭流程，Qt 在析构一个仍在运行的 QThread 时会触发致命中止
    # （表现为 Windows 上的 0xC0000409），因此这里改用 os._exit 直接终止进程：
    # 不经过 Python/Qt 对象析构，由操作系统统一回收所有线程与句柄，从根上规避该崩溃。
    # 日志按行 flush，不受影响；本应用没有依赖 atexit 清理逻辑，可安全跳过。
    os._exit(exit_code)


if __name__ == "__main__":
    main()
