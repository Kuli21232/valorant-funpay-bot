@echo off
chcp 65001 >nul

echo ============================================================
echo   Valorant FunPay Bot - Installer
echo ============================================================
echo.

REM Find Python 3.11, 3.12 or 3.13 via py launcher
set "PYEXE="

py -3.12 --version >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3.12"

if not defined PYEXE (
    py -3.13 --version >nul 2>&1
    if not errorlevel 1 set "PYEXE=py -3.13"
)

if not defined PYEXE (
    py -3.11 --version >nul 2>&1
    if not errorlevel 1 set "PYEXE=py -3.11"
)

if not defined PYEXE (
    echo [ERROR] Suitable Python version not found.
    echo.
    echo This bot requires Python 3.11, 3.12, or 3.13.
    echo Python 3.14 is too new - many libraries have no
    echo prebuilt Windows wheels for it yet.
    echo.
    echo Please install Python 3.12 from:
    echo   https://www.python.org/downloads/release/python-3127/
    echo.
    echo During install: check "Add Python to PATH" and "py launcher".
    echo.
    pause
    exit /b 1
)

echo [OK] Using %PYEXE%
echo.

REM Create venv
if not exist ".venv" (
    echo [1/5] Creating virtual environment...
    %PYEXE% -m venv .venv
    if errorlevel 1 goto fail_venv
) else (
    echo [1/5] Virtual environment already exists
)
echo.

REM Activate and install
echo [2/5] Installing Python dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 (
    echo Retrying with source builds allowed...
    pip install -r requirements.txt
    if errorlevel 1 goto fail_pip
)
echo [OK] Dependencies installed
echo.

REM Playwright
echo [3/5] Installing Chromium for Playwright...
python -m playwright install chromium
if errorlevel 1 goto fail_playwright
echo [OK] Chromium installed
echo.

REM .env setup
echo [4/5] Configuration setup...
if exist ".env" (
    set /p REWRITE="File .env already exists. Overwrite? (y/N): "
    if /i not "%REWRITE%"=="y" goto skip_env
)
python setup_wizard.py
if errorlevel 1 goto fail_wizard
:skip_env
echo.

REM Init DB
echo [5/5] Initializing database...
python -c "import asyncio; from db.database import init_db; asyncio.run(init_db())"
if errorlevel 1 goto fail_db
echo [OK] Database created
echo.

echo ============================================================
echo   Installation complete!
echo ============================================================
echo.
echo Start bot:        run.bat
echo Manage accounts:  add_account.bat
echo.
pause
exit /b 0

:fail_venv
echo [ERROR] Failed to create virtual environment.
pause
exit /b 1

:fail_pip
echo [ERROR] Failed to install dependencies.
pause
exit /b 1

:fail_playwright
echo [ERROR] Failed to install Chromium.
pause
exit /b 1

:fail_wizard
echo [ERROR] Setup wizard failed.
pause
exit /b 1

:fail_db
echo [ERROR] Failed to initialize database.
pause
exit /b 1
