@echo off
chcp 65001 >nul
if not exist ".venv" (
    echo [ERROR] Run install.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
where mitmweb >nul 2>nul
if errorlevel 1 (
    echo Installing mitmproxy first...
    pip install mitmproxy
)
echo.
echo =====================================================================
echo  mitmproxy запущен
echo =====================================================================
echo.
echo   Web UI:    http://127.0.0.1:8081
echo   Proxy on:  127.0.0.1:8080
echo.
echo   1. Откройте http://127.0.0.1:8081 в Chrome
echo   2. В системных настройках Chrome поставьте прокси 127.0.0.1:8080
echo   3. Откройте account.riotgames.com, логиньтесь
echo   4. В Web UI смотрите запросы (фильтруйте по riotgames.com)
echo   5. ПКМ на запросе - Save - сохраните .flow файл
echo.
echo   Для остановки: Ctrl+C в этом окне
echo.
mitmweb --listen-port 8080 --web-port 8081 --no-web-open-browser
pause
