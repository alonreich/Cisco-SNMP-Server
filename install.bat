@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --- Administrator Privilege Check ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    powershell -Command "Write-Host '====================================================' -ForegroundColor Red"
    powershell -Command "Write-Host '      PLEASE RUN THIS SETUP AS AN ADMINISTRATOR!     ' -ForegroundColor Red"
    powershell -Command "Write-Host '====================================================' -ForegroundColor Red"
    echo.
    echo This setup requires administrative privileges to register 
    echo the background task and manage system processes.
    echo.
    echo Please right-click install.bat and select 'Run as administrator'.
    echo.
    pause
    exit /b 1
)

set "TARGET_DIR=C:\SNMP-Server"
set "TASK_NAME=SNMP-Monitor-Backend"
set "CURRENT_DIR=%~dp0"
set "CURRENT_DIR=%CURRENT_DIR:~0,-1%"

REM --- 1. Probe and Kill Existing Process ---
echo Probing for running background processes...
tasklist /FI "IMAGENAME eq python.exe" /V | findstr /i "master.py" >nul 2>&1
if %errorlevel%==0 (
    echo Found running monitor process. Terminating...
    powershell -Command "Get-Process python | Where-Object { $_.CommandLine -like '*master.py*' } | Stop-Process -Force" >nul 2>&1
)

REM --- 2. Probe and Delete Old Task ---
echo Probing for existing scheduled task...
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel%==0 (
    echo Deleting existing background task...
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
)

REM --- 3. Probe Directory and Handle Wipe ---
if exist "%TARGET_DIR%" (
    echo.
    powershell -Command "Write-Host '----------------------------------------------------------------------' -ForegroundColor Yellow"
    powershell -Command "Write-Host ' %TARGET_DIR%\ folder already exists.' -ForegroundColor Yellow"
    powershell -Command "Write-Host ' This installation would wipe clean the folder and its settings.' -ForegroundColor Yellow"
    powershell -Command "Write-Host '----------------------------------------------------------------------' -ForegroundColor Yellow"
    set /p "CONFIRM=Are you sure you want to proceed? (Y/N): "
    if /i not "!CONFIRM!"=="Y" (
        echo.
        echo Installation aborted by user.
        pause
        exit /b 0
    )
    
    echo Attempting to delete existing directory: %TARGET_DIR% ...
    rmdir /s /q "%TARGET_DIR%" >nul 2>&1
    
    REM --- Strict Verification of Deletion ---
    if exist "%TARGET_DIR%" (
        echo.
        powershell -Command "Write-Host '====================================================' -ForegroundColor Red"
        powershell -Command "Write-Host '      CRITICAL ERROR: FOLDER DELETION FAILED!       ' -ForegroundColor Red"
        powershell -Command "Write-Host '====================================================' -ForegroundColor Red"
        echo.
        echo The installer could not delete the folder: %TARGET_DIR%
        echo This is usually because a file is locked by another program 
        echo or a background process (like an editor or cmd) is still open.
        echo.
        echo PLEASE RESOLVE THE FOLLOWING MANUALLY:
        echo 1. Close any windows or editors pointing to %TARGET_DIR%
        echo 2. Manually delete the folder %TARGET_DIR%
        echo 3. Run install.bat again as Administrator.
        echo.
        pause
        exit /b 1
    )
    echo Directory successfully cleared.
)

REM --- Re-create Target Directory and Place Files ---
if /i not "%CURRENT_DIR%"=="%TARGET_DIR%" (
    echo Creating target directory: %TARGET_DIR%
    mkdir "%TARGET_DIR%" 2>nul
    echo Syncing project files to %TARGET_DIR% ...
    xcopy /s /e /y /q "%~dp0*.*" "%TARGET_DIR%\"
    echo.
    echo Files placed successfully. Please run install.bat from %TARGET_DIR%.
    pause
    exit /b 0
)

REM --- Standard Installation Logic (at C:\SNMP-Server) ---
set "PYTHONDONTWRITEBYTECODE=1"
set "PIP_NO_CACHE_DIR=1"

echo.
echo ======================================================================
echo   SNMP-Server - Finalizing Installation
echo ======================================================================
echo.

set "VENV_PY=%TARGET_DIR%\venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Creating virtual environment...
    py -3.14 -m venv "%TARGET_DIR%\venv" || python -m venv "%TARGET_DIR%\venv"
)

echo Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r "%TARGET_DIR%\requirements.txt" >nul 2>&1

echo Registering background task...
set "VBS_PATH=%TARGET_DIR%\master_background.vbs"
schtasks /create /tn "%TASK_NAME%" /tr "wscript.exe \"%VBS_PATH%\"" /sc onstart /ru SYSTEM /rl highest >nul 2>&1

echo Starting background process...
schtasks /run /tn "%TASK_NAME%" >nul 2>&1

echo.
echo ======================================================================
echo   Installation Complete. 
echo   - Backend: Running in background (Task: %TASK_NAME%)
echo   - Dashboard: http://localhost:8000
echo ======================================================================
echo.
pause
