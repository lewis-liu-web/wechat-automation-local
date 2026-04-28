@echo off
cd /d "%~dp0"
python stop_wechat_bot.py
timeout /t 2 /nobreak >nul
if exist wechat_bot_monitor.stop del wechat_bot_monitor.stop
python wechat_bot_monitor.py --config wechat_bot_targets.json
pause
