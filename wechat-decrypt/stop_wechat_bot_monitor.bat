@echo off
cd /d "%~dp0"
echo stop requested at %date% %time% > wechat_bot_monitor.stop
echo 已请求停止微信自动监听。监听进程会在下一轮轮询内退出。
timeout /t 2 /nobreak >nul
exit /b 0
