# 进程监控助手 - 打包说明文档

## 📋 目录

1. [打包方案概述](#打包方案概述)
2. [环境准备](#环境准备)
3. [快速开始](#快速开始)
4. [详细步骤](#详细步骤)
5. [文件说明](#文件说明)
6. [常见问题](#常见问题)
7. [注意事项](#注意事项)

---

## 🎯 打包方案概述

本项目采用 **PyInstaller + Inno Setup** 的双阶段打包方案：

```
源代码 → PyInstaller打包成exe → Inno Setup制作安装包 → 最终安装包
```

**打包产物**：
- **单文件exe**：`dist\进程监控助手.exe`（约80-100MB）
- **安装包**：`dist\installer\进程监控助手_v1.0.5_Setup.exe`（约80-100MB）

**安装后目录结构**：
```
用户选择的安装目录\
├── 进程监控助手.exe    # 主程序
└── data\                # 数据目录（运行时自动创建）
    └── monitor.db       # SQLite数据库
```

---

## 🔧 环境准备

### 必需软件

| 软件 | 用途 | 下载地址 |
|------|------|---------|
| **Python 3.8+** | 开发环境 | https://www.python.org/downloads/ |
| **PyInstaller** | 打包exe | `pip install pyinstaller` |
| **Inno Setup** | 制作安装包 | https://jrsoftware.org/isdl.php |

### 安装步骤

1. **安装Python依赖**：
   ```bash
   cd D:\08_TestTool\ProcessMonitor
   pip install -r requirements.txt
   pip install pyinstaller
   ```

2. **下载安装Inno Setup**：
   - 访问 https://jrsoftware.org/isdl.php
   - 下载最新版本（推荐：Inno Setup 6.x）
   - 安装时选择安装语言包（Chinese Simplified）

---

## 🚀 快速开始

### 方式一：一键打包exe（推荐新手）

1. 双击运行 `build\build.bat`
2. 等待打包完成（约2-5分钟）
3. 生成的exe位于 `dist\进程监控助手.exe`

### 方式二：制作完整安装包（推荐分发）

**步骤1：打包exe**
```bash
cd build
build.bat
```

**步骤2：制作安装包**
1. 打开Inno Setup
2. 选择 `File` → `Open`，打开 `build\setup.iss`
3. 点击 `Build` → `Compile`
4. 等待编译完成（约30秒）
5. 安装包位于 `dist\installer\进程监控助手_v1.0.5_Setup.exe`

---

## 📝 详细步骤

### 阶段一：PyInstaller打包exe

#### 手动打包命令

```bash
# 切换到项目根目录
cd D:\08_TestTool\ProcessMonitor

# 使用spec文件打包
pyinstaller --clean build\ProcessMonitor.spec

# 打包完成后，exe位于 dist\进程监控助手.exe
```

#### spec配置说明

`build\ProcessMonitor.spec` 关键配置：

| 配置项 | 值 | 说明 |
|-------|-----|------|
| `onefile` | `True` | 打包成单个exe文件 |
| `console` | `False` | 不显示控制台窗口 |
| `name` | `进程监控助手` | exe文件名 |
| `icon` | `app.ico` | 应用图标（可选） |
| `upx` | `True` | 使用UPX压缩（减小体积） |

#### 打包时间

- 首次打包：约3-5分钟
- 后续打包：约1-2分钟（有缓存）

#### 打包产物

```
dist\
└── 进程监控助手.exe    # 单文件exe（约80-100MB）

build\
└── ProcessMonitor\     # 临时文件（可删除）
```

---

### 阶段二：Inno Setup制作安装包

#### 使用Inno Setup编译器

1. **打开Inno Setup**
   - 启动 Inno Setup Compiler

2. **加载脚本**
   - 菜单：`File` → `Open`
   - 选择：`build\setup.iss`

3. **编译安装包**
   - 菜单：`Build` → `Compile`
   - 或按快捷键：`Ctrl + F9`

4. **查看结果**
   - 编译成功后，安装包位于：
   - `dist\installer\进程监控助手_v1.0.5_Setup.exe`

#### 命令行编译（高级）

```bash
# 需要先将Inno Setup安装目录添加到环境变量
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\setup.iss
```

#### setup.iss配置说明

| 配置项 | 值 | 说明 |
|-------|-----|------|
| `DefaultDirName` | `{autopf}\进程监控助手` | 默认安装目录 |
| `PrivilegesRequired` | `lowest` | 普通用户即可安装 |
| `Compression` | `lzma2/max` | 最大压缩率 |
| `SolidCompression` | `yes` | 固实压缩 |

#### 安装包功能

- ✅ 自定义安装目录
- ✅ 创建开始菜单快捷方式
- ✅ 可选创建桌面快捷方式
- ✅ 自动生成卸载程序
- ✅ 卸载时询问是否保留数据

---

## 📁 文件说明

### build文件夹结构

```
build\
├── ProcessMonitor.spec       # PyInstaller配置文件
├── setup.iss                 # Inno Setup脚本
├── build.bat                 # 一键打包脚本
└── README_打包说明.md        # 本文档
```

### 各文件作用

#### ProcessMonitor.spec

PyInstaller打包配置文件，主要功能：
- 指定主入口文件 `main.py`
- 配置隐藏导入（PyQt5、qfluentwidgets等）
- 设置单文件模式
- 配置窗口模式（不显示控制台）
- 排除不必要的模块（减小体积）

#### setup.iss

Inno Setup安装脚本，主要功能：
- 设置应用信息（名称、版本、发布者）
- 配置安装目录和权限
- 创建快捷方式
- 配置卸载逻辑（保留用户数据选项）
- 设置安装界面语言

#### build.bat

自动化打包脚本，主要流程：
1. 检查Python和PyInstaller
2. 清理旧的打包文件
3. 执行PyInstaller打包
4. 验证打包结果
5. 提示下一步操作

---

## ❓ 常见问题

### Q1: 打包后运行报错"找不到模块"

**原因**：某些模块未被正确打包

**解决**：在 `ProcessMonitor.spec` 的 `hiddenimports` 中添加缺失的模块：

```python
hiddenimports=[
    # 添加缺失的模块
    'missing_module_name',
]
```

### Q2: 打包后exe体积过大

**原因**：包含了不必要的依赖

**解决**：在 `ProcessMonitor.spec` 的 `excludes` 中排除不需要的模块：

```python
excludes=[
    'tkinter',
    'matplotlib',
    'numpy.tests',
    # 添加其他不需要的模块
]
```

### Q3: 打包后启动慢

**原因**：单文件模式需要解压到临时目录

**解决**：
- 方式1：接受1-2秒的启动延迟（推荐）
- 方式2：改用文件夹模式（修改spec文件，将所有binaries等放到one_dir模式）

### Q4: 安装时提示需要管理员权限

**原因**：Inno Setup默认需要管理员权限

**解决**：已在 `setup.iss` 中配置 `PrivilegesRequired=lowest`，无需管理员权限

### Q5: 卸载后数据丢失

**原因**：默认行为

**解决**：已在 `setup.iss` 中添加卸载询问逻辑，用户可选择保留数据

### Q6: 打包后无法连接到数据库

**原因**：`config.py` 中的路径配置问题

**解决**：已修改 `config.py`，使用 `sys.frozen` 检测打包环境，自动适配路径

### Q7: Inno Setup编译失败

**常见原因**：
- exe文件不存在：先运行 `build.bat` 生成exe
- 路径错误：检查 `setup.iss` 中的路径是否正确
- 语法错误：检查 `setup.iss` 语法

**调试方法**：
查看Inno Setup编译器的错误提示信息

---

## ⚠️ 注意事项

### 打包前检查

- [ ] 确保代码无错误，能正常运行
- [ ] 更新 `config.py` 中的版本号
- [ ] 更新 `setup.iss` 中的版本号
- [ ] 准备应用图标 `app.ico`（可选）
- [ ] 清理测试数据库文件

### 打包过程

- [ ] 关闭杀毒软件（可能误报）
- [ ] 确保有足够的磁盘空间（至少500MB）
- [ ] 不要在打包过程中运行应用
- [ ] 打包完成后测试exe是否正常运行

### 分发前测试

- [ ] 在干净的Windows系统上测试安装
- [ ] 测试安装到不同目录
- [ ] 测试卸载功能
- [ ] 测试数据保留功能
- [ ] 测试快捷方式是否正常

### 数据安全

- [ ] 提醒用户定期备份数据（data目录）
- [ ] 在README中说明数据存储位置
- [ ] 卸载前提示用户备份数据

### 版本管理

- [ ] 每次发布前更新版本号
- [ ] 在更新日志中记录变更
- [ ] 保留旧版本安装包

---

## 🎯 完整打包流程总结

### 开发阶段
1. 完成代码开发和测试
2. 更新版本号（`config.py`、`setup.iss`）
3. 更新更新日志

### 打包阶段
```bash
# 步骤1: 打包exe
cd build
build.bat

# 步骤2: 制作安装包
# 用Inno Setup打开setup.iss，点击编译
```

### 测试阶段
1. 测试exe直接运行
2. 测试安装包安装
3. 测试功能完整性
4. 测试卸载功能

### 发布阶段
1. 重命名安装包（如果需要）
2. 准备发布说明
3. 上传到发布平台
4. 通知用户更新

---

## 📞 技术支持

如有问题，请检查：
1. 本文档的常见问题部分
2. PyInstaller官方文档：https://pyinstaller.org/
3. Inno Setup官方文档：https://jrsoftware.org/ishelp/

---

**文档版本**：v1.0
**最后更新**：2025-11-19
**适用版本**：进程监控助手 v1.0.5
