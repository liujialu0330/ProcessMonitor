# -*- mode: python ; coding: utf-8 -*-

"""
PyInstaller打包配置文件
用于将进程监控助手打包成单个exe文件
"""

import struct

# 全系统进程快照（core/system_snapshot.py）的结构布局只在 64 位下验证过；32 位 Python
# 构建出的程序即便在 64 位系统上（WOW64）也会走另一套结构，运行时会禁用快照回退 psutil，
# 性能预算不再成立。所以发布构建必须用 64 位 Python。
if struct.calcsize('P') != 8:
    raise SystemExit(
        "打包终止：构建用的 Python 不是 64 位（指针宽度 %d 位）。全系统进程快照的结构"
        "布局只在 64 位下验证过，32 位构建会在运行时禁用快照并回退 psutil 逐进程采集，"
        "请改用 64 位 Python 重新构建。" % (struct.calcsize('P') * 8))

block_cipher = None


a = Analysis(
    ['../main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 打包图标文件到exe同目录
        ('app_green_icon.ico', '.'),
    ],
    hiddenimports=[
        # PyQt5核心模块和子模块
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'PyQt5.QtPrintSupport',
        'PyQt5.sip',
        # qfluentwidgets及其依赖
        'qfluentwidgets',
        'qfluentwidgets._rc',
        'qfluentwidgets.components',
        'qfluentwidgets.components.widgets',
        'qfluentwidgets.components.dialog_box',
        'qfluentwidgets.components.settings',
        'qfluentwidgets.common',
        'qfluentwidgets.common.config',
        'qfluentwidgets.common.icon',
        'qfluentwidgets.common.style_sheet',
        'qfluentwidgets.window',
        'qfluentwidgets.window.fluent_window',
        # PyQt5-Frameless-Window（qfluentwidgets的关键依赖）
        'qframelesswindow',
        'qframelesswindow.windows',
        'qframelesswindow.linux',
        'qframelesswindow.utils',
        # psutil进程监控
        'psutil',
        'psutil._pswindows',
        # pyqtgraph数据可视化（如果未安装会在导入时报错）
        'pyqtgraph',
        'pyqtgraph.graphicsItems',
        'pyqtgraph.graphicsItems.PlotItem',
        'pyqtgraph.graphicsItems.ViewBox',
        'pyqtgraph.graphicsItems.AxisItem',
        'pyqtgraph.graphicsItems.PlotDataItem',
        'pyqtgraph.graphicsItems.PlotCurveItem',
        'pyqtgraph.graphicsItems.ScatterPlotItem',
        # 标准库
        'sqlite3',
        'csv',
        'datetime',
        'typing',
        'dataclasses',
        'uuid',
        'contextlib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的模块以减小体积
        'tkinter',
        'matplotlib',
        'PIL',
        'scipy',
        'pandas',
        'jupyter',
        'IPython',
        'setuptools',
        'distutils',
        # 排除测试相关模块（data/database.py 的 __main__ 中 import pytest
        # 会被 PyInstaller 静态依赖分析收集，此处显式排除兜底——批1评审遗留 M1）
        'pytest',
        '_pytest',
        'pluggy',
        'py',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir 模式（v1.2.0 架构重构批4）：exe 仅含引导代码，依赖收进同目录
# _internal\ 文件夹，配合 COLLECT 生成 dist\进程监控助手\ 目录。相比 onefile
# 省去每次启动解压到临时目录的开销，安装/卸载/覆盖安装也更可控。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='进程监控助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标文件（和spec文件同目录）- 这个图标会应用到exe文件，显示在桌面、任务栏等
    icon='app_green_icon.ico',
    # 版本信息：由 build\gen_version_info.py 从 config.APP_VERSION 动态生成
    version='version_info.txt',
)

# pmctl.exe：命令行控制端（2.0 B-2）。只用标准库 + core.ipc 客户端，单独分析以免把
# PyQt5 / psutil 打进它的 PYZ；console=True 以便在 cmd / CI 里输出文字与退出码。
# 与主程序共用同一个 COLLECT，产物都在 dist\进程监控助手\ 下（setup.iss 的
# dist\进程监控助手\* recursesubdirs 规则自动带上 pmctl.exe）。
pmctl_a = Analysis(
    ['../pmctl.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'core.ipc.client',
        'core.ipc.protocol',
        'utils.instance_name',
        'app.paths',
        'config',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt5',
        'psutil',
        'qfluentwidgets',
        'qframelesswindow',
        'pyqtgraph',
        'numpy',
        'ui',
        'data',
        'sqlite3',
        'tkinter',
        'matplotlib',
        'PIL',
        'pytest',
        '_pytest',
        'pluggy',
        'py',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pmctl_pyz = PYZ(pmctl_a.pure, pmctl_a.zipped_data, cipher=block_cipher)

pmctl_exe = EXE(
    pmctl_pyz,
    pmctl_a.scripts,
    [],
    exclude_binaries=True,
    name='pmctl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 命令行程序：需要控制台输出与退出码
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app_green_icon.ico',
    version='version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    pmctl_exe,
    pmctl_a.binaries,
    pmctl_a.zipfiles,
    pmctl_a.datas,
    strip=False,
    upx=False,
    name='进程监控助手',
)
