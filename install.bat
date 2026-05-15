@echo off
chcp 65001 >nul
title TradeAgent - Install Dependencies

set "ROOT=%~dp0"

echo ========================================
echo   TradeAgent - Install Dependencies
echo ========================================
echo.

:: Copy .env if not exists
if not exist "%ROOT%backend\.env" (
    copy "%ROOT%backend\.env.example" "%ROOT%backend\.env" >nul
    echo [INFO] Created backend\.env from .env.example
    echo [INFO] Please edit backend\.env to set your API key.
)

:: Use .venv if available
set "PIP_CMD=pip"
set "PYTHON_CMD=python"
if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PIP_CMD=%ROOT%.venv\Scripts\pip.exe"
    set "PYTHON_CMD=%ROOT%.venv\Scripts\python.exe"
    echo [INFO] Using .venv Python
) else (
    echo [INFO] Creating .venv virtual environment...
    python -m venv "%ROOT%.venv"
    set "PIP_CMD=%ROOT%.venv\Scripts\pip.exe"
    set "PYTHON_CMD=%ROOT%.venv\Scripts\python.exe"
)

:: Install Python backend dependencies
echo [1/2] Installing Python dependencies...
pushd "%ROOT%backend"
"%PIP_CMD%" install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python dependencies!
    echo [INFO] Make sure Python and pip are installed and in PATH.
    popd
    pause
    exit /b 1
)
popd
echo [OK] Python dependencies installed.
echo.

:: Install Frontend dependencies
echo [2/2] Installing Frontend dependencies...
pushd "%ROOT%frontend"
call npm install
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install frontend dependencies!
    echo [INFO] Make sure Node.js and npm are installed and in PATH.
    popd
    pause
    exit /b 1
)
popd
echo [OK] Frontend dependencies installed.
echo.

echo ========================================
echo   All dependencies installed!
echo   Run start.bat to start the app.
echo ========================================
pause