"""
应用程序入口
"""
import sys
import os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from qfluentwidgets import setTheme, Theme

from ui.main_window import MainWindow
from utils.logger import setup_logging
from utils.crash_handler import install_excepthook
import config


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

    # 设置应用信息
    app.setApplicationName(config.APP_NAME)
    app.setApplicationVersion(config.APP_VERSION)

    # 设置应用程序图标（任务栏和窗口图标）
    icon_path = config.get_icon_path()

    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 设置主题（可选：Light或Auto）
    setTheme(Theme.AUTO)

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

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
