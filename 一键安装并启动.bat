@echo off
setlocal
cd /d "%~dp0"

echo [INFO] Installing dependencies and starting bot...
call "%~dp0start-bot.cmd" -Install
echo.
pause
