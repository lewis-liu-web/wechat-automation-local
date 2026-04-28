@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0wechat_bot_monitor.py"
set "PYTHONW=C:\Users\Lewis\AppData\Local\Programs\Python\Python312\pythonw.exe"

rem 单实例保护：已有 wechat_bot_monitor.py 监听进程则不重复启动
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'wechat_bot_monitor\.py' -and $_.Name -match '^pythonw?\.exe$' };" ^
  "if ($procs) { Write-Host ('微信自动监听已在运行，未重复启动。PID: ' + (($procs | ForEach-Object ProcessId) -join ',')); exit 10 } else { exit 0 }"

if "%ERRORLEVEL%"=="10" (
  timeout /t 2 /nobreak >nul
  exit /b 0
)

if exist wechat_bot_monitor.stop del /f /q wechat_bot_monitor.stop >nul 2>nul

start "" "%PYTHONW%" "%SCRIPT%"
echo 已启动微信自动监听。
timeout /t 2 /nobreak >nul
exit /b 0