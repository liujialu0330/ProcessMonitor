"""
依赖检查脚本
在打包前运行此脚本，确保所有必需的依赖都已正确安装
"""
import sys


def check_dependencies():
    """检查所有依赖是否已安装"""
    print("=" * 60)
    print("检查打包依赖")
    print("=" * 60)
    print()

    required_modules = {
        'PyQt5': 'PyQt5',
        'qfluentwidgets': 'PyQt-Fluent-Widgets',
        'qframelesswindow': 'PyQt5-Frameless-Window',
        'psutil': 'psutil',
        'pyqtgraph': 'pyqtgraph',
    }

    missing = []
    installed = []

    for module_name, package_name in required_modules.items():
        try:
            __import__(module_name)
            print(f"[OK] {package_name:30s} 已安装")
            installed.append(package_name)
        except ImportError:
            print(f"[X]  {package_name:30s} 未安装")
            missing.append(package_name)

    print()
    print("=" * 60)

    if missing:
        print("[错误] 缺少以下依赖，请先安装：")
        print()
        for pkg in missing:
            print(f"   pip install {pkg}")
        print()
        print("或者一次性安装所有依赖：")
        print("   pip install -r requirements.txt")
        print("=" * 60)
        return False
    else:
        print("[成功] 所有依赖都已安装，可以开始打包！")
        print("=" * 60)
        print()
        print("下一步：运行 build.bat 开始打包")
        return True


if __name__ == "__main__":
    success = check_dependencies()
    sys.exit(0 if success else 1)
