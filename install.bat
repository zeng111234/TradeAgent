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

:: Install Python backend dependencies
echo [1/2] Installing Python dependencies...
pushd "%ROOT%backend"
pip install -r requirements.txt
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