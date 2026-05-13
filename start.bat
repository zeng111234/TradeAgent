@echo off
chcp 65001 >nul
title TradeAgent Starter

set "ROOT=%~dp0"

echo ========================================
echo   TradeAgent - AI Foreign Trade Assistant
echo ========================================
echo.

:: Copy .env if not exists
if not exist "%ROOT%backend\.env" (
    copy "%ROOT%backend\.env.example" "%ROOT%backend\.env" >nul
    echo [INFO] Created .env from .env.example
    echo [INFO] Please edit backend\.env to set your API key.
)

:: Start backend
echo [1/2] Starting Backend on http://localhost:8000 ...
pushd "%ROOT%backend"
start "TradeAgent-Backend" cmd /c "python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
popd

:: Wait for backend
timeout /t 5 /nobreak >nul

:: Install frontend deps if needed
echo [2/2] Starting Frontend on http://localhost:5173 ...
if not exist "%ROOT%frontend\node_modules" (
    echo [INFO] Installing frontend dependencies...
    pushd "%ROOT%frontend"
    call npm install
    popd
)

:: Start frontend
pushd "%ROOT%frontend"
start "TradeAgent-Frontend" cmd /c "npm run dev"
popd

:: Wait for frontend to be ready
echo [INFO] Waiting for services to start...
timeout /t 12 /nobreak >nul

:: Open browser
echo [INFO] Opening browser...
start "" "http://localhost:5173"

echo.
echo ========================================
echo   All services started!
echo   Backend:  http://localhost:8000
echo   API Docs: http://localhost:8000/docs
echo   Frontend: http://localhost:5173
echo.
echo   Press Ctrl+C to stop all services.
echo   Or just close this window.
echo ========================================
echo.

:: Keep alive until user closes window or presses Ctrl+C
:loop
timeout /t 5 /nobreak >nul
tasklist /FI "WINDOWTITLE eq TradeAgent-Backend*" 2>nul | find "cmd" >nul
if %errorlevel% neq 0 (
    tasklist /FI "WINDOWTITLE eq TradeAgent-Frontend*" 2>nul | find "cmd" >nul
    if %errorlevel% neq 0 goto cleanup
)
goto loop

:cleanup
echo Stopping services...
taskkill /FI "WINDOWTITLE eq TradeAgent-Backend*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq TradeAgent-Frontend*" /F >nul 2>&1
echo Done.