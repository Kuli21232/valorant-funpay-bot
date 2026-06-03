@echo off
chcp 65001 >nul
if not exist ".venv" (
    echo [ERROR] Virtual environment not found. Run install.bat first.
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] .env not found. Run install.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python main.py
pause
