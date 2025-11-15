"""
应用程序入口
"""
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from qfluentwidgets import setTheme, Theme

from ui.main_window import MainWindow
import config


def main():
    """主函数"""
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

    # 设置主题（可选：Light或Auto）
    setTheme(Theme.AUTO)

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    # 运行应用
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
