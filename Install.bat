@echo off
cd /d "%~dp0"
setlocal EnableExtensions EnableDelayedExpansion
set PYTHONDONTWRITEBYTECODE=1

echo.
echo ======================================================================
echo   SNMP-Server — Installation
echo ======================================================================
echo.

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

set "INSTALL_DIR=%~dp0"
set "VENV_DIR=%INSTALL_DIR%venv"
set "REQUIREMENTS=%INSTALL_DIR%config\requirements.txt"
set "TASK_NAME=SNMP-Monitor-Backend"

REM ──────────────────────────────────────────────────────────────────────
REM  STEP 1 — Ensure Python is available
REM ──────────────────────────────────────────────────────────────────────
echo [1/5] Checking for Python...

set "PYTHON_CMD="
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    where py >nul 2>&1 && set "PYTHON_CMD=py"
)

if defined PYTHON_CMD (
    for /f "tokens=*" %%V in ('!PYTHON_CMD! --version 2^>^&1') do (
        echo        Found: %%V
    )
    goto :step2
)

echo        Python not found in PATH. Attempting automatic install...

where winget >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    powershell -Command "Write-Host '  FATAL: Neither Python nor winget were found.' -ForegroundColor Red"
    echo.
    echo        Please install Python 3.14+ manually:
    echo        https://www.python.org/downloads/
    echo        IMPORTANT: Check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo        Installing Python via winget (this may take a minute)...
winget install -e --id Python.Python.3 --accept-package-agreements --accept-source-agreements --silent
if %errorlevel% neq 0 (
    echo.
    powershell -Command "Write-Host '  FATAL: winget Python installation failed.' -ForegroundColor Red"
    echo        Please install Python 3.14+ manually from https://www.python.org
    pause
    exit /b 1
)

echo        Refreshing system PATH...
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USR_PATH=%%B"
set "PATH=!SYS_PATH!;!USR_PATH!"

set "PYTHON_CMD="
where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
    where py >nul 2>&1 && set "PYTHON_CMD=py"
)
if not defined PYTHON_CMD (
    echo.
    powershell -Command "Write-Host '  FATAL: Python still not reachable after install.' -ForegroundColor Red"
    echo        Log out and back in so Windows refreshes PATH, then re-run Install.bat.
    pause
    exit /b 1
)
for /f "tokens=*" %%V in ('!PYTHON_CMD! --version 2^>^&1') do (
    echo        Installed: %%V
)

:step2
REM ──────────────────────────────────────────────────────────────────────
REM  STEP 2 — Create virtual environment
REM ──────────────────────────────────────────────────────────────────────
echo [2/5] Setting up virtual environment...

if exist "%VENV_DIR%\Scripts\python.exe" (
    echo        Existing venv detected — reusing.
) else (
    echo        Creating venv...
    !PYTHON_CMD! -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        powershell -Command "Write-Host '  FATAL: Failed to create virtual environment.' -ForegroundColor Red"
        pause
        exit /b 1
    )
    echo        Virtual environment created.
)

set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

REM ──────────────────────────────────────────────────────────────────────
REM  STEP 3 — Upgrade pip
REM ──────────────────────────────────────────────────────────────────────
echo [3/5] Upgrading pip...
"%VENV_PYTHON%" -m pip install --upgrade pip --quiet >nul 2>&1
echo        Done.

REM ──────────────────────────────────────────────────────────────────────
REM  STEP 4 — Install dependencies
REM ──────────────────────────────────────────────────────────────────────
echo [4/5] Installing dependencies from config\requirements.txt...

if not exist "%REQUIREMENTS%" (
    powershell -Command "Write-Host '  WARNING: config\requirements.txt not found. Skipping.' -ForegroundColor Yellow"
    goto :step5
)

"%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS%" --quiet
if %errorlevel% neq 0 (
    powershell -Command "Write-Host '  WARNING: Some packages may have failed to install.' -ForegroundColor Yellow"
    echo        Review the output above and retry if needed.
) else (
    echo        All dependencies installed.
)

:step5
REM ──────────────────────────────────────────────────────────────────────
REM  STEP 5 — Register Windows startup task
REM ──────────────────────────────────────────────────────────────────────
echo [5/5] Registering startup scheduled task...

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

schtasks /create /tn "%TASK_NAME%" /tr "wscript.exe \"%INSTALL_DIR%launchers\startup.vbs\"" /sc onlogon /rl highest /f >nul 2>&1
if %errorlevel%==0 (
    echo        Task "%TASK_NAME%" registered (triggers at user logon).
) else (
    powershell -Command "Write-Host '  WARNING: Could not create scheduled task.' -ForegroundColor Yellow"
    echo        You can manually add launchers\startup.vbs to your Startup folder.
)

REM ──────────────────────────────────────────────────────────────────────
REM  First Launch
REM ──────────────────────────────────────────────────────────────────────
echo.
echo Starting services for the first time...
echo.

if not exist "%INSTALL_DIR%logs" mkdir "%INSTALL_DIR%logs"

wscript.exe "%INSTALL_DIR%launchers\master_background.vbs"
timeout /t 3 /nobreak >nul
wscript.exe "%INSTALL_DIR%launchers\main_background.vbs"

echo.
echo ======================================================================
echo   Installation Complete!
echo.
echo   Dashboard : http://localhost:8000
echo   Task Name : %TASK_NAME% (runs at every logon)
echo   Uninstall : Run uninstall.bat as Administrator
echo ======================================================================
echo.
pause
