@echo off
chcp 65001 >nul
echo ========================================
echo 清理Windows图标缓存
echo ========================================
echo.
echo 此脚本将清理Windows图标缓存
echo 解决快捷方式图标不更新的问题
echo.
pause

echo.
echo 正在清理图标缓存...
echo.

:: 结束explorer.exe进程
taskkill /f /im explorer.exe

:: 删除图标缓存文件
del /a /q "%localappdata%\IconCache.db"
del /a /f /q "%localappdata%\Microsoft\Windows\Explorer\iconcache*"

:: 重启explorer.exe
start explorer.exe

echo.
echo ========================================
echo 清理完成！
echo ========================================
echo.
echo 请重新创建快捷方式或重启电脑
echo 快捷方式图标应该会显示正确了
echo.
pause
