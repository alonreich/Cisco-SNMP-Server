@echo off
cd /d "%~dp0"
setlocal EnableExtensions EnableDelayedExpansion

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo.
    powershell -Command "Write-Host '===========================================================================' -ForegroundColor Red"
    powershell -Command "Write-Host '                                                                           ' -BackgroundColor Red -ForegroundColor White"
    powershell -Command "Write-Host '        SCRIPT DID NOT RUN AS ADMINISTRATOR!                               ' -BackgroundColor Red -ForegroundColor White"
    powershell -Command "Write-Host '                                                                           ' -BackgroundColor Red -ForegroundColor White"
    powershell -Command "Write-Host '        PLEASE RUN AS ADMINISTRATOR AND TRY AGAIN!                         ' -BackgroundColor Red -ForegroundColor White"
    powershell -Command "Write-Host '                                                                           ' -BackgroundColor Red -ForegroundColor White"
    powershell -Command "Write-Host '===========================================================================' -ForegroundColor Red"
    echo.
    echo.
    pause
    exit /b 1
)

set "TARGET_DIR=C:\SNMP-Server"
set "TASK_NAME=SNMP-Monitor-Backend"

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

echo [1/4] Killing all project Python processes...
powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*SNMP-Server*' } | Stop-Process -Force" >nul 2>&1
powershell -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*SNMP-Server*' } | Stop-Process -Force" >nul 2>&1

echo [2/4] Killing any lingering wscript launchers...
powershell -Command "Get-Process wscript -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*SNMP-Server*' } | Stop-Process -Force" >nul 2>&1

timeout /t 2 /nobreak >nul

echo [3/4] Removing scheduled task: %TASK_NAME%
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

echo [4/4] Deleting project folder: %TARGET_DIR%
if exist "%TARGET_DIR%" (
    rmdir /s /q "%TARGET_DIR%" >nul 2>&1

    if exist "%TARGET_DIR%" (
        echo.
        echo Retrying with forced handle release...
        timeout /t 3 /nobreak >nul
        rmdir /s /q "%TARGET_DIR%" >nul 2>&1
    )

    if exist "%TARGET_DIR%" (
        echo.
        powershell -Command "Write-Host '----------------------------------------------------' -ForegroundColor Yellow"
        powershell -Command "Write-Host '  WARNING: SOME FILES COULD NOT BE DELETED.' -ForegroundColor Yellow"
        powershell -Command "Write-Host '----------------------------------------------------' -ForegroundColor Yellow"
        echo The folder %TARGET_DIR% is still present.
        echo Please close any CMD windows, editors, or browsers
        echo accessing this folder, then delete it manually.
    ) else (
        echo Folder deleted successfully.
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

if "%~nx0"=="snmp_uninstall_temp.bat" (
    (goto) 2>nul & del "%~f0"
)
