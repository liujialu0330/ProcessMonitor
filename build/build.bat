@echo off
setlocal enabledelayedexpansion
:: 本文件使用 GBK（Windows中文系统默认ANSI/OEM代码页）编码保存，不使用
:: chcp 65001：实测 UTF-8编码 + chcp 65001 组合在非交互式/管道式console
:: （如CI、部分自动化调用场景）下会触发cmd.exe批处理解析器的提前缓冲
:: 错位，导致后续纯ASCII命令行都被截断误判为"不是内部或外部命令"。
:: GBK编码与系统默认代码页一致，无需切换代码页，规避该问题。

echo ========================================
echo 进程监控助手 - 自动打包脚本（无人值守）
echo ========================================
echo.

:: 设置颜色（绿色成功，红色错误）
set "GREEN=[92m"
set "RED=[91m"
set "YELLOW=[93m"
set "NC=[0m"

:: 切换到项目根目录
cd /d "%~dp0.."

echo [1/8] 检查环境...
echo.

:: 检查Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo %RED%错误: 未找到Python，请先安装Python%NC%
    exit /b 1
)
echo %GREEN%√ Python已安装%NC%

:: 检查图标文件是否存在（main.py/spec/setup.iss均依赖此图标）
if not exist "build\app_green_icon.ico" (
    echo %RED%错误: 未找到图标文件 build\app_green_icon.ico%NC%
    exit /b 1
)
echo %GREEN%√ 图标文件存在%NC%
echo.

echo [2/8] 读取版本号（单源: config.APP_VERSION）...
echo.

:: 依赖开发环境安装 requirements.txt（含4个运行依赖），推荐使用
:: requirements-lock.txt 复现干净环境: pip install -r requirements-lock.txt
for /f "delims=" %%v in ('python -c "import config;print(config.APP_VERSION)"') do set "VER=%%v"
if "%VER%"=="" (
    echo %RED%错误: 读取 config.APP_VERSION 失败%NC%
    exit /b 1
)
echo %GREEN%√ 版本号: %VER%%NC%
echo.

echo [3/8] 检查项目依赖...
echo.

python build\check_dependencies.py
if %errorlevel% neq 0 (
    echo.
    echo %RED%错误: 缺少必需的依赖包%NC%
    echo 请先安装依赖: pip install -r requirements-lock.txt
    exit /b 1
)

:: 检查PyInstaller（PyInstaller 6.21.0 实测：裸命令不在PATH，必须用 python -m PyInstaller）
python -m pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo %YELLOW%警告: 未找到PyInstaller，正在安装...%NC%
    python -m pip install pyinstaller
    if %errorlevel% neq 0 (
        echo %RED%错误: PyInstaller安装失败%NC%
        exit /b 1
    )
)
echo %GREEN%√ PyInstaller已安装%NC%
echo.

echo [4/8] 生成版本信息文件（build\version_info.txt）...
echo.

python build\gen_version_info.py
if %errorlevel% neq 0 (
    echo %RED%错误: 生成version_info.txt失败%NC%
    exit /b 1
)
echo %GREEN%√ version_info.txt已生成%NC%
echo.

echo [5/8] 清理旧的打包文件...
echo.

if exist "dist\进程监控助手" (
    rmdir /s /q "dist\进程监控助手"
    echo %GREEN%√ 已删除旧的onedir产物%NC%
)

if exist "build\ProcessMonitor" (
    rmdir /s /q "build\ProcessMonitor"
    echo %GREEN%√ 已删除旧的临时文件%NC%
)
echo.

echo [6/8] 开始PyInstaller打包（onedir模式）...
echo.
echo 这可能需要几分钟时间，请耐心等待...
echo.

python -m PyInstaller --clean build\ProcessMonitor.spec

if %errorlevel% neq 0 (
    echo.
    echo %RED%错误: PyInstaller打包失败！%NC%
    echo 请检查上面的错误信息
    exit /b 1
)
echo.
echo %GREEN%√ PyInstaller打包完成%NC%
echo.

echo [7/8] 验证打包结果并冒烟测试...
echo.

:: 检查onedir产物exe是否生成
if not exist "dist\进程监控助手\进程监控助手.exe" (
    echo %RED%错误: 未找到打包后的exe文件%NC%
    exit /b 1
)

:: 统计onedir目录总大小（MB）与文件数（用python脚本而非内联PowerShell，
:: 避免批处理里嵌套引号/括号解析问题）
set "sizeMB=0"
set "filecount=0"
set "_i=0"
for /f "delims=" %%s in ('python build\dist_stats.py "dist\进程监控助手"') do (
    set /a _i+=1
    if !_i! equ 1 set "sizeMB=%%s"
    if !_i! equ 2 set "filecount=%%s"
)
echo %GREEN%√ exe文件已生成%NC%
echo   目录路径: dist\进程监控助手\
echo   目录大小: %sizeMB% MB（%filecount% 个文件）
echo.

:: 冒烟测试：启动exe，等待数秒，用tasklist确认进程存在，再收尾
echo 正在冒烟测试: 启动exe进行快速验证...
start "" "dist\进程监控助手\进程监控助手.exe"
%SystemRoot%\System32\ping.exe -n 6 127.0.0.1 >nul

tasklist /fi "imagename eq 进程监控助手.exe" | %SystemRoot%\System32\find.exe /i "进程监控助手.exe" >nul
if %errorlevel% neq 0 (
    echo %RED%错误: 冒烟测试失败，未检测到进程运行%NC%
    exit /b 1
)
echo %GREEN%√ 冒烟测试通过，进程正常运行%NC%

taskkill /f /im "进程监控助手.exe" >nul 2>&1
echo.

echo [8/8] 制作安装包（Inno Setup）...
echo.

:: 探测ISCC.exe：本机路径 -> Program Files(x86)标准路径 -> PATH
set "ISCC="
set "PF86=%ProgramFiles(x86)%"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" (
    set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) else if exist "!PF86!\Inno Setup 6\ISCC.exe" (
    set "ISCC=!PF86!\Inno Setup 6\ISCC.exe"
) else (
    where ISCC >nul 2>&1
    if !errorlevel! equ 0 (
        set "ISCC=ISCC"
    )
)

if "%ISCC%"=="" (
    echo %RED%错误: 未找到 ISCC.exe，请安装 Inno Setup 6: https://jrsoftware.org/isdl.php%NC%
    echo 已探测路径:
    echo   %LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
    echo   !PF86!\Inno Setup 6\ISCC.exe
    echo   PATH 中的 ISCC
    exit /b 1
)
echo %GREEN%√ 已找到 ISCC: %ISCC%%NC%

if not exist "dist\installer" mkdir "dist\installer"

"%ISCC%" /DMyAppVersion=%VER% build\setup.iss
if %errorlevel% neq 0 (
    echo %RED%错误: Inno Setup 编译失败！%NC%
    exit /b 1
)
echo %GREEN%√ 安装包编译完成%NC%
echo.

:: 产物重命名: 保留中文名安装包（现有 setup.iss OutputBaseFilename 未改），
:: 同时复制一份英文名，符合 release 资产命名惯例
set "SRC_INSTALLER=dist\installer\进程监控助手_v%VER%_Setup.exe"
set "DST_INSTALLER=dist\installer\Windows_v%VER%_Setup.exe"
if exist "%SRC_INSTALLER%" (
    copy /y "%SRC_INSTALLER%" "%DST_INSTALLER%" >nul
    echo %GREEN%√ 已生成 release 资产命名副本: %DST_INSTALLER%%NC%
) else (
    echo %RED%错误: 未找到 Inno Setup 输出的安装包 %SRC_INSTALLER%%NC%
    exit /b 1
)
echo.

echo ========================================
echo 打包完成！版本 v%VER%
echo ========================================
echo.
echo 安装包位置:
echo   %SRC_INSTALLER%
echo   %DST_INSTALLER%
echo.
echo ========================================

exit /b 0
