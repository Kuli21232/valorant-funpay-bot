@echo off
chcp 65001 >nul
if not exist ".venv" (
    echo [ERROR] Run install.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m email_client.test_cli
pause
