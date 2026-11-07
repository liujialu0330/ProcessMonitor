# 进程监控助手 - 打包说明文档

## 📋 目录

1. [打包方案概述](#打包方案概述)
2. [环境准备](#环境准备)
3. [快速开始](#快速开始)
4. [详细步骤](#详细步骤)
5. [文件说明](#文件说明)
6. [常见问题](#常见问题)
7. [发布前冒烟](#发布前冒烟)
8. [发布校验](#发布校验)
9. [注意事项](#注意事项)

---

## 🎯 打包方案概述

本项目采用 **PyInstaller + Inno Setup** 的双阶段打包方案，`build\build.bat` 已封装为
无人值守一键脚本（含版本号读取、version_info.txt 生成、PyInstaller 打包、冒烟测试、
Inno Setup 编译、产物命名，全流程一次跑完）：

```
源代码 → PyInstaller打包成onedir目录 → Inno Setup制作安装包 → 最终安装包
```

**打包产物**（v1.2.0 起改为 onedir 模式，不再是单文件exe）：
- **onedir目录**：`dist\进程监控助手\`（主exe + `_internal\` 依赖目录）
- **安装包**：`dist\installer\进程监控助手_v{版本}_Setup.exe`（另附一份
  `dist\installer\Windows_v{版本}_Setup.exe`，供 release 资产命名使用）

> 为什么改用 onedir：onefile 模式每次启动都要把依赖解压到系统临时目录，
> 启动慢且临时文件可能被杀毒软件误报；onedir 模式启动更快、覆盖安装更可控。

**安装后目录结构**：
```
用户选择的安装目录\
├── 进程监控助手.exe        # 主程序（引导exe）
├── _internal\               # PyInstaller onedir 依赖目录（Qt/Python运行时等）
├── app_green_icon.ico       # 应用图标
└── （数据目录不在安装目录内，见下方"数据存储位置"）
```

**数据存储位置**：数据目录固定在 `%LOCALAPPDATA%\进程监控助手\data\monitor.db`
（与安装目录无关，覆盖安装/卸载重装不影响该目录，详见 `app/paths.py` 的
`AppPaths.resolve()`）。

---

## 🔧 环境准备

### 必需软件

| 软件 | 用途 | 下载地址 |
|------|------|---------|
| **Python 3.8+** | 开发环境 | https://www.python.org/downloads/ |
| **PyInstaller** | 打包exe（本机实测 6.21.0，注意下方 PATH 提示） | `pip install pyinstaller` |
| **Inno Setup 6** | 制作安装包 | https://jrsoftware.org/isdl.php |

> **PyInstaller PATH 提示**：部分环境下 `pip install pyinstaller` 后裸命令
> `pyinstaller` 不在 PATH 中，`build.bat` 已统一改用 `python -m PyInstaller`
> 调用，手动打包时也建议这样调用以避免"命令未找到"。

### 安装步骤

1. **安装Python依赖**（项目根目录下执行；推荐用 `requirements-lock.txt`
   复现打包时验证过的确切依赖版本，`requirements.txt` 仅声明宽松的最低版本
   约束供开发使用）：
   ```bash
   pip install -r requirements-lock.txt
   ```
   如需开发环境（不含 pyinstaller）：
   ```bash
   pip install -r requirements.txt
   ```

2. **下载安装Inno Setup**：
   - 访问 https://jrsoftware.org/isdl.php
   - 下载最新版本（推荐：Inno Setup 6.x）
   - 安装时选择安装语言包（Chinese Simplified）
   - `build.bat` 会依次探测
     `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`、
     `%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe`、PATH 中的 `ISCC`，
     三者都找不到会直接失败并给出提示

---

## 🚀 快速开始

### 一键打包（推荐，无人值守）

`build\build.bat` 已是全流程无人值守脚本，一次运行完成
"读取版本号 → 生成 version_info.txt → PyInstaller 打包 onedir → 冒烟测试 →
Inno Setup 编译安装包 → 产物重命名"，中途任何一步失败都会 `exit /b 1` 并停止，
不会卡在交互提示上：

```bash
cd build
build.bat
```

完成后：
- onedir 产物位于 `dist\进程监控助手\`
- 安装包位于 `dist\installer\进程监控助手_v{版本}_Setup.exe` 与
  `dist\installer\Windows_v{版本}_Setup.exe`（内容相同，仅文件名不同）

### 手动分步执行（调试用）

如需单独执行某一步排查问题，可参考下方"详细步骤"分别运行
`gen_version_info.py`、`python -m PyInstaller`、`ISCC.exe`。

---

## 📝 详细步骤

### 阶段零：生成版本信息文件（version_info.txt）

```bash
# 从 config.APP_VERSION 单源动态生成 build\version_info.txt（4元组版本号）
# 该文件是打包产物，已加入 .gitignore，不入库；每次打包前都会重新生成
python build\gen_version_info.py
```

### 阶段一：PyInstaller打包（onedir模式）

#### 手动打包命令

```bash
# 切换到项目根目录
cd <项目根目录>

# 使用spec文件打包（注意用 python -m PyInstaller，避免PATH问题）
python -m PyInstaller --clean build\ProcessMonitor.spec

# 打包完成后，产物位于 dist\进程监控助手\ 目录（主exe + _internal\依赖）
```

#### spec配置说明

`build\ProcessMonitor.spec` 关键配置：

| 配置项 | 值 | 说明 |
|-------|-----|------|
| 打包模式 | `EXE(exclude_binaries=True)` + `COLLECT` | onedir 模式，依赖收进 `_internal\` |
| `console` | `False` | 不显示控制台窗口 |
| `name` | `进程监控助手` | exe文件名 / onedir目录名 |
| `icon` | `app_green_icon.ico` | 应用图标（编译期图标路径固定，不随版本变化） |
| `version` | `version_info.txt` | 由 `gen_version_info.py` 动态生成 |
| `upx` | `False` | onedir 模式下未启用UPX压缩 |
| `excludes` | 含 `pytest`/`_pytest`/`pluggy`/`py` | 防止 `database.py` 的 `__main__` 里 `import pytest` 被静态收集进产物 |

#### 打包时间

- 首次打包：约3-5分钟
- 后续打包：约1-2分钟（有缓存）

#### 打包产物

```
dist\
└── 进程监控助手\        # onedir产物目录
    ├── 进程监控助手.exe  # 引导exe
    ├── pmctl.exe         # 命令行控制端（console 程序，2.0 起）
    └── _internal\        # Python运行时、PyQt5、qfluentwidgets等依赖（两个 exe 共用）

build\
└── ProcessMonitor\      # 临时文件（可删除）
```

#### pmctl.exe（命令行控制端）

spec 里第二组 `Analysis` / `PYZ` / `EXE` 生成 `pmctl.exe`，与主程序共用同一个 `COLLECT`：

| 配置项 | 值 | 说明 |
|-------|-----|------|
| 入口 | `pmctl.py`（仓库根） | 只用标准库 + `core.ipc` 客户端 |
| `console` | `True` | 在 cmd / CI 里输出文字并返回退出码 |
| `excludes` | `PyQt5`、`psutil`、`qfluentwidgets`、`ui`、`data` 等 | pmctl 的 PYZ 不带界面与采集依赖 |
| 产物位置 | `dist\进程监控助手\pmctl.exe` | 与主程序同目录：pmctl 拉起无窗口实例时找的就是同目录的 `进程监控助手.exe` |

- 安装包无需改动：`setup.iss` 的 `Source: "..\dist\进程监控助手\*" ... recursesubdirs` 已经包含 `pmctl.exe`。
- 打包后冒烟：在 `dist\进程监控助手\` 下执行
  `pmctl.exe --data-dir %TEMP%\pm_smoke ping`（应自动拉起无窗口实例并打印版本）、
  `pmctl.exe --data-dir %TEMP%\pm_smoke quit`，两条都应以退出码 0 结束，任务管理器里不应残留进程。
- 用法与退出码见仓库 `tools\README.md`。

---

### 阶段二：Inno Setup制作安装包

`build.bat` 已自动完成本阶段（探测 ISCC.exe → 传入
`/DMyAppVersion=%VER%` → 编译）。如需手动操作：

#### 使用Inno Setup编译器

1. **打开Inno Setup**
   - 启动 Inno Setup Compiler

2. **加载脚本**
   - 菜单：`File` → `Open`
   - 选择：`build\setup.iss`

3. **编译安装包**
   - 菜单：`Build` → `Compile`
   - 或按快捷键：`Ctrl + F9`
   - 注意：直接用 IDE 编译时版本号取 `setup.iss` 内 `#ifndef MyAppVersion`
     兜底值，可能与 `config.py` 不一致，正式发布请用 `build.bat` 或命令行
     显式传参

4. **查看结果**
   - 编译成功后，安装包位于：
   - `dist\installer\进程监控助手_v{版本}_Setup.exe`

#### 命令行编译（高级）

```bash
# 需要先将Inno Setup安装目录添加到环境变量，或使用完整路径
"C:\Users\<用户名>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" /DMyAppVersion={版本号} build\setup.iss
```

#### setup.iss配置说明

| 配置项 | 值 | 说明 |
|-------|-----|------|
| `MyAppVersion` | 外部 `/DMyAppVersion` 传入，未传时取 `#ifndef` 兜底值 | 与 `config.APP_VERSION` 保持一致 |
| `DefaultDirName` | `{autopf}\进程监控助手` | 默认安装目录 |
| `PrivilegesRequired` | `lowest` | 普通用户即可安装 |
| `Compression` | `lzma2/max` | 最大压缩率 |
| `SolidCompression` | `yes` | 固实压缩 |
| `[Files]` | `Source: "..\dist\进程监控助手\*"` + `ignoreversion recursesubdirs createallsubdirs` | 整体复制onedir目录；`ignoreversion` 防止Qt DLL被版本比较跳过覆盖 |
| `AppMutex` | `ProcessMonitor.Running,Global\ProcessMonitor.Running` | 程序运行中时安装/卸载会提示先关闭；名称须与 `app/single_instance.py` 的 `INSTALLER_MUTEX_NAME` 一致 |

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
├── ProcessMonitor.spec       # PyInstaller配置文件（onedir模式）
├── gen_version_info.py       # 从config.APP_VERSION生成version_info.txt
├── setup.iss                 # Inno Setup脚本
├── build.bat                 # 一键打包脚本（无人值守，含冒烟测试）
├── check_dependencies.py     # 打包前依赖检查
├── dist_stats.py             # 统计onedir产物总大小与文件数（build.bat 验证打包结果时调用）
├── verify_release.py         # GitHub Release 发布校验（版本一致、安装包资产、digest、prerelease，见“发布校验”）
├── clear_icon_cache.bat      # 清理Windows图标缓存（安装后快捷方式图标不刷新时手动运行）
├── app_green_icon.ico        # 应用图标（spec/setup.iss/build.bat 均引用此文件）
└── README_打包说明.md        # 本文档
```

### 各文件作用

#### ProcessMonitor.spec

PyInstaller打包配置文件，主要功能：
- 指定主入口文件 `main.py`
- 配置隐藏导入（PyQt5、qfluentwidgets等）
- 设置 onedir 模式（`EXE(exclude_binaries=True)` + `COLLECT`）
- 配置窗口模式（不显示控制台）
- 排除不必要的模块（含测试相关的 `pytest`/`_pytest`/`pluggy`/`py`）

#### gen_version_info.py

从 `config.APP_VERSION` 单源动态生成 `build\version_info.txt`（Windows
exe 版本信息4元组），供 spec 的 `version=` 参数引用；每次打包前由
`build.bat` 自动调用，不需要手动维护版本号。

#### dist_stats.py

统计 onedir 产物目录的总大小（MB取整）与文件数，以两行纯数字输出到标准输出，
供 `build.bat` 在"验证打包结果"一步用 `for /f` 逐行捕获。单独写成脚本是为了
避免在批处理里内联 PowerShell 与嵌套引号带来的编码和解析问题；手动排查时也可
运行 `python build\dist_stats.py dist\进程监控助手`。

#### setup.iss

Inno Setup安装脚本，主要功能：
- 设置应用信息（名称、版本、发布者）——版本号由外部命令行 `/DMyAppVersion`
  传入，`#ifndef` 提供兜底值
- 配置安装目录和权限
- 创建快捷方式
- 配置卸载逻辑（保留用户数据选项）
- 设置安装界面语言

#### build.bat

无人值守全流程打包脚本，主要流程：
1. 检查Python、图标文件是否存在
2. 读取版本号（`config.APP_VERSION` 单源）
3. 检查项目依赖与PyInstaller
4. 生成 `version_info.txt`
5. 清理旧的打包文件
6. 执行 `python -m PyInstaller` 打包（onedir）
7. 验证打包结果（体积/文件数）并冒烟测试（启动exe → `tasklist` 确认 → `taskkill` 收尾）
8. 探测 ISCC 并编译安装包，产物重命名为发布资产命名

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

**说明**：v1.2.0 起已改用 onedir 模式（`dist\进程监控助手\` 目录，主exe +
`_internal\` 依赖），不再需要每次启动解压到临时目录，启动速度已优于此前的
onefile 模式。若仍觉得慢，通常是首次启动杀毒软件扫描 `_internal\` 内大量
DLL 导致，属一次性开销。

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

## 🧪 发布前冒烟

除 `python -m pytest tests/ -v`（含 `tests/e2e/` 的 offscreen 断言态冒烟）全绿外，
正式发布前建议额外用真窗口观察态跑一次 GUI 端到端冒烟测试（`tests/e2e/test_gui_smoke.py`），
用肉眼确认"创建监控任务 → 采集数据 → 停止任务并落库 → 历史页查看图表/表格 →
导出页导出 CSV → 关闭窗口"全链路在真实窗口下无异常——断言逻辑与 offscreen 跑法
完全一致，仅追加了停顿方便观察，不会因为看不看窗口而改变通过/失败结果：

```powershell
$env:QT_QPA_PLATFORM = 'windows'
$env:GUI_SMOKE_VISIBLE = '1'
python -m pytest tests/e2e/test_gui_smoke.py -s
```

该命令会短暂弹出真实主窗口并自动完成整条链路操作（无需人工点击），肉眼确认无异常
（不闪退、无卡死、无异常弹窗）即可。跑完后清除这两个环境变量，恢复默认的
offscreen 断言态供日常回归/CI 使用：

```powershell
Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
Remove-Item Env:\GUI_SMOKE_VISIBLE -ErrorAction SilentlyContinue
python -m pytest tests/e2e/ -q
```

---

## 🔎 发布校验

`build\verify_release.py`（只用标准库）按更新通道契约检查 GitHub Release。契约的依据是已发出的
1.4.1 更新器：它只取**第一个**文件名含 `setup` 的 `.exe`、不校验 digest、不限制下载域名；2.x 的
更新器则必须有 `sha256:` digest 才自动安装。契约由 `tests\test_update_contract_v141.py`（1.4.1
冻结副本 `tests\fixtures\frozen_v1_4_1\update_checker.py`）与 `tests\test_update_fake_release.py`
（本地假 GitHub 端到端）守护，脚本自身由 `tests\test_verify_release.py` 覆盖。

### 怎么跑

```powershell
# 发布前：草稿已建好、安装包已上传（查草稿必须带有 repo 权限的令牌）
python build\verify_release.py --tag v2.0.0 --token <令牌> --allow-draft --local-setup dist\installer\Windows_v2.0.0_Setup.exe

# 发布后复核（不带 --allow-draft；同时检查 releases/latest）
python build\verify_release.py --tag v2.0.0 --local-setup dist\installer\Windows_v2.0.0_Setup.exe

# 离线：用保存的 API 返回体（gh api repos/liujialu0330/ProcessMonitor/releases/tags/v2.0.0 > release.json）
python build\verify_release.py --tag v2.0.0 --release-json release.json [--latest-json latest.json]
```

- `--repo` 缺省取 `config.py` 的 `GITHUB_OWNER/GITHUB_REPO`；`--token` 缺省读环境变量 `GITHUB_TOKEN`。
- 逐项输出 ✓ 通过 / ✗ 失败 / - 跳过（控制台是 GBK 时显示 √ ×）；全部通过退出码 0，有失败 1，参数错误 2。

### 各项含义

| 规则 | 检查 | 为什么 |
|---|---|---|
| (a) | `config.APP_VERSION`、tag 去 `v`、`CHANGELOG.md` 顶部版本段、Release 名称四处是同一版本（预发布期间 CHANGELOG 顶部可以仍是“未发布”） | 安装包、关于页、更新判断与发布说明对得上 |
| (b) | 恰好一个文件名含 `setup`（不区分大小写）且以 `.exe` 结尾的资产，名为 ASCII 的 `Windows_v{版本}_Setup.exe`，且已上传完成 | 1.4.1 只取第一个匹配项；非 ASCII 文件名可能被 GitHub 改写 |
| (c) | 该资产带 `digest: sha256:…`；给了 `--local-setup` 时摘要与大小都与本地文件一致 | 2.x 缺 digest 拒绝自动安装；确认上传的就是测过的那个包 |
| (d) | `-beta.N` / `-rc.N` 必须勾 prerelease；正式版必须非 prerelease、非 draft | 漏标的 beta 会成为 latest 并推给全部 1.4.1 用户；标成 prerelease 的正式版没人收得到 |
| (e) | 正文 ≤ 60 行（发布说明的目标是 40 行）；正式版含“升级须知” | 更新对话框篇幅有限，升级风险必须在第一屏 |
| (f) | tag 以 `v` 开头，形如 `vX.Y.Z` / `vX.Y.Z-beta.N` / `vX.Y.Z-rc.N` | 版本比较与 (d) 的判断依赖这三种形态 |
| (g) | 在线或给了 `--latest-json` 时：正式版发布后 `releases/latest` 指向本 tag，预发布不得是 latest | 这是 1.4.1 与 2.x 检查更新实际读取的端点 |

### GA 后 1 小时内：用真实 1.4.1 验证自动更新

1. 在测试机（或专用 Windows 账户，避免动到自己的数据目录）从 v1.4.1 Release 下载
   `Windows_v1.4.1_Setup.exe`，核对其 digest 后安装；放入一份带历史任务的数据库更好。
2. `verify_release.py --tag v2.0.0` 不带 `--allow-draft` 全部通过（尤其 (g)：latest 已是 v2.0.0）。
3. 1.4.1 关于页“检查更新”：出现“发现新版本 v2.0.0”；更新说明在 1.4.1 里是纯文本（Markdown 不渲染、只截前 4000 字、
   框高约 8 行），确认升级须知在最前面且可读。
4. “现在更新” → 进度走完 → “立即安装” → 程序退出、安装向导启动 → 覆盖安装完成
   （1.4.1 不校验 digest，下载的包是否正确全靠第 2 步的 (c)）。
5. 启动 2.0.0：标题与设置页版本为 2.0.0；数据库升级提示出现，`monitor.db.bak_v1` 存在，历史任务可见。
6. 2.0.0 的“检查更新”显示“已是最新版本（v2.0.0）”。
7. 任何一步失败：立即把 Release 改为 draft 或 prerelease，让 `releases/latest` 回到 1.4.1，再排查。

---

## ⚠️ 注意事项

### 打包前检查

- [ ] 确保代码无错误，能正常运行
- [ ] 更新 `config.py` 中的 `APP_VERSION`（版本号单源，`setup.iss`/`build.bat`
  会自动读取，无需手动同步）
- [ ] 确认应用图标 `build\app_green_icon.ico` 存在（`build.bat` 会检查，缺失直接失败）
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
1. 完成代码开发和测试（`python -m pytest tests/ -v` 全绿）
2. 更新版本号（仅需改 `config.py` 的 `APP_VERSION`，单源）
3. 更新更新日志

### 打包阶段
```bash
# 一步完成：打包exe + 制作安装包（无人值守）
cd build
build.bat
```

### 测试阶段
1. 测试exe直接运行
2. 测试安装包安装
3. 测试功能完整性
4. 测试卸载功能

### 发布阶段
1. 准备发布说明（正文 ≤ 60 行，正式版含“升级须知”）
2. 建草稿 Release，只上传 `Windows_v{版本}_Setup.exe`
3. `python builderify_release.py --tag v{版本} --allow-draft --local-setup ...` 全部通过后发布
4. 发布后不带 `--allow-draft` 复核一次；正式版 1 小时内用真实 1.4.1 走一遍自动更新（见“发布校验”）

---

## 📞 技术支持

如有问题，请检查：
1. 本文档的常见问题部分
2. PyInstaller官方文档：https://pyinstaller.org/
3. Inno Setup官方文档：https://jrsoftware.org/ishelp/

---

**文档版本**：v1.1
**最后更新**：2026-07-12
**适用版本**：进程监控助手 v1.3.0
