@echo off
setlocal
cd /d "%~dp0"

echo [INFO] Starting bot...
call "%~dp0start-bot.cmd"
echo.
pause
