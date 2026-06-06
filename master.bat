@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM No Python bytecode or pip wheel cache for this project
set "PYTHONDONTWRITEBYTECODE=1"
set "PIP_NO_CACHE_DIR=1"

set "VENV_PY=%~dp0venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo Virtual environment not found in .\venv
    echo Run setup.bat once to install dependencies.
    echo.
    pause
    exit /b 1
)

"%VENV_PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo.
    echo The .\venv was created for a different machine or Python path.
    echo Run setup.bat to recreate it.
    echo.
    pause
    exit /b 1
)

"%VENV_PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Dependencies missing. Run setup.bat to install requirements.txt
    echo.
    pause
    exit /b 1
)

echo Removing bytecode caches outside venv ...
for /d /r "%~dp0" %%D in (__pycache__) do (
    echo %%D | findstr /i "\\venv\\" >nul || (
        if exist "%%D" rd /s /q "%%D" 2>nul
    )
)

"%VENV_PY%" -B "%~dp0master.py"
