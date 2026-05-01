@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem CLI status checker for wechat_bot_monitor.py.
rem Usage: status_wechat_bot_monitor.bat

powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and $_.CommandLine -like '*wechat_bot_monitor.py*' }; if($procs){ Write-Host 'RUNNING'; $procs | Select-Object ProcessId,Name,CommandLine | Format-List; } else { Write-Host 'STOPPED'; }; if(Test-Path '.\wechat_bot_monitor.stop'){ Write-Host 'STOP_FILE_PRESENT'; }; if(Test-Path '.\wechat_bot_monitor.log'){ Write-Host '--- log tail ---'; Get-Content '.\wechat_bot_monitor.log' -Tail 20 }"

exit /b 0
