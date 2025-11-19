# -*- mode: python ; coding: utf-8 -*-

"""
PyInstaller打包配置文件
用于将进程监控助手打包成单个exe文件
"""

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
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='进程监控助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标文件（和spec文件同目录）- 这个图标会应用到exe文件，显示在桌面、任务栏等
    icon='app_green_icon.ico',
    # 版本信息
    version_file=None,
)
