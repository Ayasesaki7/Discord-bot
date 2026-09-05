@echo off
setlocal
cd /d "%~dp0"

echo [INFO] Stopping bot...
call "%~dp0stop-bot.cmd"
echo.
pause
