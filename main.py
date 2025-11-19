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

    # 设置应用程序图标（任务栏和窗口图标）
    if getattr(sys, 'frozen', False):
        # 打包后：优先从临时解压目录读取（PyInstaller的_MEIPASS）
        if hasattr(sys, '_MEIPASS'):
            icon_path = os.path.join(sys._MEIPASS, 'app_green_icon.ico')
        else:
            # 备用：从exe同目录读取（Inno Setup复制的）
            icon_path = os.path.join(config.BASE_DIR, 'app_green_icon.ico')
    else:
        # 开发环境：图标在build目录
        icon_path = os.path.join(config.BASE_DIR, 'build', 'app_green_icon.ico')

    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 设置主题（可选：Light或Auto）
    setTheme(Theme.AUTO)

    # 创建并显示主窗口
    window = MainWindow()
    window.show()

    # 运行应用
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
