@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo stop requested at %date% %time% > wechat_bot_monitor.stop
echo Stop requested. The monitor will exit on its next polling cycle.
powershell -NoProfile -Command "Start-Sleep -Seconds 2" >nul 2>nul
call "%~dp0status_wechat_bot_monitor.bat"
exit /b %ERRORLEVEL%
