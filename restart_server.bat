@echo off
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1

echo Terminating existing processes (Forced)...
powershell -Command "Get-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force" >nul 2>&1
timeout /t 2 /nobreak >nul

echo Cleaning stale logs...
:clearmonitor
if exist "logs\monitor.log" (
    del /f /q "logs\monitor.log" >nul 2>&1
    if exist "logs\monitor.log" (
        echo [!] WARNING: Cannot delete monitor.log. It is locked by another process.
        echo [!] Please close any programs holding it or run this script as Administrator.
        pause
        exit /b 1
    )
)

:clearflask
if exist "logs\flask.log" (
    del /f /q "logs\flask.log" >nul 2>&1
    if exist "logs\flask.log" (
        echo [!] WARNING: Cannot delete flask.log. It is locked by another process.
        echo [!] Please close any programs holding it or run this script as Administrator.
        pause
        exit /b 1
    )
)

echo Starting Polling Engine...
wscript.exe "%~dp0master_background.vbs"

echo Starting Web Dashboard...
wscript.exe "%~dp0main_background.vbs"

echo.
echo ======================================================
echo   Server and Polling Engine restarted (Clean State)
echo ======================================================
timeout /t 3 >nul
