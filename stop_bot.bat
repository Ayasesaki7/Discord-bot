@echo off
setlocal EnableExtensions
set "BOT_WINDOW_TITLE=MusicBotRunner"

for /f "tokens=2" %%P in ('tasklist /v /fo table /fi "WINDOWTITLE eq %BOT_WINDOW_TITLE%" ^| findstr /i "%BOT_WINDOW_TITLE%"') do (
    echo [INFO] Stopping bot PID %%P ...
    taskkill /pid %%P /t /f >nul 2>nul
    echo [OK] Bot stopped.
    goto :end
)

echo [INFO] Bot is not running.

:end
pause
exit /b
