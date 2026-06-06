@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONDONTWRITEBYTECODE=1"
set "PIP_NO_CACHE_DIR=1"

echo.
echo ======================================================================
echo   SNMP-Server - First-time setup
echo ======================================================================
echo.

set "VENV_PY=%~dp0venv\Scripts\python.exe"

REM --- Find a working Python (py launcher preferred on Windows) ---
set "BOOT_PY="
where py >nul 2>&1
if %errorlevel%==0 (
    for %%V in (-3.14 -3.13 -3.12 -3.11 -3) do (
        if not defined BOOT_PY (
            py %%V -c "import sys" >nul 2>&1
            if not errorlevel 1 set "BOOT_PY=py %%V"
        )
    )
)
if not defined BOOT_PY (
    where python >nul 2>&1
    if not errorlevel 1 set "BOOT_PY=python"
)

REM Fallback: common per-user Python 3.14 path (edit INSTALL.txt if yours differs)
if not defined BOOT_PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
        set "BOOT_PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    )
)

if not defined BOOT_PY (
    echo ERROR: No Python found.
    echo Install Python 3.11+ or see INSTALL.txt for manual steps.
    pause
    exit /b 1
)

echo Using bootstrap Python: %BOOT_PY%
echo.

REM --- Recreate venv if missing or broken (wrong user/path baked in) ---
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo Removing broken virtual environment...
        rmdir /s /q "%~dp0venv" 2>nul
    )
)

if not exist "%VENV_PY%" (
    echo Creating virtual environment in .\venv ...
    %BOOT_PY% -m venv "%~dp0venv"
    if errorlevel 1 (
        echo ERROR: Failed to create venv.
        pause
        exit /b 1
    )
)

echo Installing dependencies from requirements.txt ...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo.
echo Verifying imports ...
"%VENV_PY%" -c "import fastapi, uvicorn, jinja2; import pysnmp; print('PySNMP OK')"
if errorlevel 1 (
    echo ERROR: Package verification failed.
    pause
    exit /b 1
)

echo.
echo Do NOT run: pip install bulkWalkCmd  (not a package)
echo Do NOT run: pip install pysnmp-lextudio on Python 3.14
echo Use master.bat only — it runs .\venv\Scripts\python.exe
echo.
echo Cleaning bytecode caches outside venv ...
for /d /r "%~dp0" %%D in (__pycache__) do (
    echo %%D | findstr /i "\\venv\\" >nul || (
        if exist "%%D" rd /s /q "%%D" 2>nul
    )
)
for /r "%~dp0" %%F in (*.pyc *.pyo) do (
    echo %%F | findstr /i "\\venv\\" >nul || del /q "%%F" 2>nul
)

echo.
echo ======================================================================
echo   Setup complete. Start the server with:  master.bat
echo ======================================================================
echo.
pause
