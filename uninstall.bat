@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM --- Administrator Privilege Check ---
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    powershell -Command "Write-Host '====================================================' -ForegroundColor Red"
    powershell -Command "Write-Host '    PLEASE RUN UNINSTALL AS AN ADMINISTRATOR!    ' -ForegroundColor Red"
    powershell -Command "Write-Host '====================================================' -ForegroundColor Red"
    echo.
    echo Administrative privileges are required to stop 
    echo system tasks and delete protected folders.
    pause
    exit /b 1
)

set "TARGET_DIR=C:\SNMP-Server"
set "TASK_NAME=SNMP-Monitor-Backend"

REM --- Self-Relocation Logic ---
REM If we are running from the target folder, copy ourselves to %TMP% and re-launch.
if /i "%~dp0"=="%TARGET_DIR%\" (
    echo.
    echo Preparing uninstallation...
    copy /y "%~f0" "%TEMP%\snmp_uninstall_temp.bat" >nul
    echo Relocating to temporary execution space...
    start "" "%TEMP%\snmp_uninstall_temp.bat"
    exit /b 0
)

echo.
echo ======================================================================
echo   SNMP-Server - Uninstallation
echo ======================================================================
echo.

REM 1. Stop and Kill the background process
echo Stopping background monitor...
tasklist /FI "IMAGENAME eq python.exe" /V | findstr /i "master.py" >nul 2>&1
if %errorlevel%==0 (
    powershell -Command "Get-Process python | Where-Object { $_.CommandLine -like '*master.py*' } | Stop-Process -Force" >nul 2>&1
)

REM 2. Remove the scheduled task
echo Removing scheduled task: %TASK_NAME%
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

REM 3. Wipe the directory
if exist "%TARGET_DIR%" (
    echo Deleting folder: %TARGET_DIR% ...
    timeout /t 2 /nobreak >nul
    rmdir /s /q "%TARGET_DIR%" >nul 2>&1
    
    if exist "%TARGET_DIR%" (
        echo.
        powershell -Command "Write-Host '----------------------------------------------------' -ForegroundColor Yellow"
        powershell -Command "Write-Host '  WARNING: SOME FILES COULD NOT BE DELETED.' -ForegroundColor Yellow"
        powershell -Command "Write-Host '----------------------------------------------------' -ForegroundColor Yellow"
        echo The folder %TARGET_DIR% is still present. 
        echo Please ensure all CMD windows or editors are closed and delete it manually.
    ) else (
        echo Uninstallation successful. Project removed.
    )
) else (
    echo Project folder not found. Nothing to remove.
)

echo.
echo ======================================================================
echo   Uninstallation complete. 
echo ======================================================================
echo.
pause

REM Cleanup the relocated script
if "%~nx0"=="snmp_uninstall_temp.bat" (
    (goto) 2>nul & del "%~f0"
)
