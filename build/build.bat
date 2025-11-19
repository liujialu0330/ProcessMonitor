@echo off
chcp 65001 >nul
echo ========================================
echo 进程监控助手 - 自动打包脚本
echo 版本: 1.0.5
echo ========================================
echo.

:: 设置颜色（绿色成功，红色错误）
set "GREEN=[92m"
set "RED=[91m"
set "YELLOW=[93m"
set "NC=[0m"

:: 切换到项目根目录
cd /d "%~dp0.."

echo [1/6] 检查环境...
echo.

:: 检查Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo %RED%错误: 未找到Python，请先安装Python%NC%
    pause
    exit /b 1
)
echo %GREEN%✓ Python已安装%NC%

:: 检查项目依赖
echo.
echo 检查项目依赖...
python build\check_dependencies.py
if %errorlevel% neq 0 (
    echo.
    echo %RED%错误: 缺少必需的依赖包%NC%
    echo 请先安装依赖: pip install -r requirements.txt
    pause
    exit /b 1
)

:: 检查PyInstaller
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo %YELLOW%警告: 未找到PyInstaller，正在安装...%NC%
    pip install pyinstaller
    if %errorlevel% neq 0 (
        echo %RED%错误: PyInstaller安装失败%NC%
        pause
        exit /b 1
    )
)
echo %GREEN%✓ PyInstaller已安装%NC%
echo.

echo [2/6] 清理旧的打包文件...
echo.

:: 删除旧的打包文件
if exist "dist\进程监控助手.exe" (
    del /f /q "dist\进程监控助手.exe"
    echo %GREEN%✓ 已删除旧的exe文件%NC%
)

if exist "build\ProcessMonitor" (
    rmdir /s /q "build\ProcessMonitor"
    echo %GREEN%✓ 已删除旧的临时文件%NC%
)
echo.

echo [3/6] 开始PyInstaller打包...
echo.
echo 这可能需要几分钟时间，请耐心等待...
echo.

:: 执行PyInstaller打包
pyinstaller --clean build\ProcessMonitor.spec

if %errorlevel% neq 0 (
    echo.
    echo %RED%错误: PyInstaller打包失败！%NC%
    echo 请检查上面的错误信息
    pause
    exit /b 1
)
echo.
echo %GREEN%✓ PyInstaller打包完成%NC%
echo.

echo [4/6] 验证打包结果...
echo.

:: 检查exe是否生成
if not exist "dist\进程监控助手.exe" (
    echo %RED%错误: 未找到打包后的exe文件%NC%
    pause
    exit /b 1
)

:: 显示文件大小
for %%F in ("dist\进程监控助手.exe") do (
    set size=%%~zF
)
set /a sizeMB=%size%/1024/1024
echo %GREEN%✓ exe文件已生成%NC%
echo   文件路径: dist\进程监控助手.exe
echo   文件大小: %sizeMB% MB
echo.

echo [5/6] 测试exe是否可运行...
echo.
echo 正在尝试启动exe进行快速测试...
echo （程序会在5秒后自动关闭，请检查是否正常显示）
echo.

:: 启动exe并等待5秒（仅做快速测试）
start "" "dist\进程监控助手.exe"
timeout /t 5 /nobreak >nul
taskkill /f /im "进程监控助手.exe" >nul 2>&1

echo.
echo [6/6] 打包完成！
echo.
echo ========================================
echo 下一步操作:
echo ========================================
echo.
echo 方式1: 直接运行exe（测试用）
echo   运行: dist\进程监控助手.exe
echo.
echo 方式2: 制作安装包（推荐）
echo   1. 安装Inno Setup: https://jrsoftware.org/isdl.php
echo   2. 用Inno Setup打开: build\setup.iss
echo   3. 点击菜单: Build -^> Compile
echo   4. 生成的安装包位于: dist\installer\
echo.
echo ========================================
echo.

:: 询问是否打开dist目录
choice /c YN /m "是否打开dist目录查看文件"
if %errorlevel%==1 (
    explorer dist
)

pause
