@echo off
setlocal EnableExtensions
set PYTHONDONTWRITEBYTECODE=1
cd /d "%~dp0"

echo Removing application caches (skipping venv)...

for /d /r "%~dp0" %%D in (__pycache__ .pytest_cache .mypy_cache .ruff_cache) do (
    echo %%D | findstr /i "\\venv\\" >nul || (
        if exist "%%D" rd /s /q "%%D" 2>nul
    )
)

for /r "%~dp0" %%F in (*.pyc *.pyo) do (
    echo %%F | findstr /i "\\venv\\" >nul || del /q "%%F" 2>nul
)

for /d /r "%~dp0" %%D in (.cache) do (
    echo %%D | findstr /i "\\venv\\" >nul || (
        if exist "%%D" rd /s /q "%%D" 2>nul
    )
)

echo Done.
pause
