"""
应用程序入口
"""
import sys
import os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from qfluentwidgets import setTheme

from ui.main_window import MainWindow
from ui.typography import configure_application_typography
from utils.logger import setup_logging
from utils.crash_handler import install_excepthook
from app_config import load_app_config, cfg
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

    # 关闭主窗口不再意味着应用退出（v1.3.0 批4，D 托盘驻留）：默认 Qt 行为是
    # "最后一个可见窗口关闭时自动 quit()"，托盘驻留要求"隐藏到托盘"时应用继续
    # 在后台运行。关闭 QApplication 这条隐式退出路径后，唯一真退出路径变为
    # MainWindow.closeEvent 清理完成后显式调用的 QApplication.quit()（该行也是
    # 撤回本功能时唯一需要连带撤掉的开关，见方案风险登记表）。
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
