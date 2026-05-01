@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Generic CLI launcher for wechat_bot_monitor.py.
rem Usage: start_wechat_bot_monitor.bat [config_path]
rem Default config: .\wechat_bot_targets.json
rem Notes: avoid the Windows py launcher because it may point to a stale Python install.

set "SCRIPT=%~dp0wechat_bot_monitor.py"
set "CONFIG=%~1"
if not defined CONFIG set "CONFIG=%~dp0wechat_bot_targets.json"
set "STDOUT_LOG=%~dp0monitor_detached_stdout.log"
set "STDERR_LOG=%~dp0monitor_detached_stderr.log"

if not exist "%SCRIPT%" (
  echo ERROR: script not found: "%SCRIPT%"
  exit /b 2
)
if not exist "%CONFIG%" (
  echo ERROR: config not found: "%CONFIG%"
  exit /b 2
)

if exist "%~dp0wechat_bot_monitor.stop" del /q "%~dp0wechat_bot_monitor.stop" >nul 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine -like '*wechat_bot_monitor.py*' }; if($procs){ Write-Host ('wechat_bot_monitor already running. PID: ' + (($procs | ForEach-Object ProcessId) -join ',')); exit 10 }"
if %ERRORLEVEL% EQU 10 goto :STATUS

where pythonw >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  start "wechat_bot_monitor" /min pythonw "%SCRIPT%" --config "%CONFIG%" 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"
) else (
  where python >nul 2>nul || (echo ERROR: python/pythonw not found in PATH.& exit /b 3)
  start "wechat_bot_monitor" /min python "%SCRIPT%" --config "%CONFIG%" 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"
)

:STATUS
powershell -NoProfile -Command "Start-Sleep -Seconds 3" >nul 2>nul
call "%~dp0status_wechat_bot_monitor.bat"
exit /b %ERRORLEVEL%
