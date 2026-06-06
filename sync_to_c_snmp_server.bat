@echo off

setlocal EnableExtensions



set "SRC=%~dp0"

set "DST=C:\SNMP-Server"



if not exist "%DST%\" (

    echo ERROR: %DST% does not exist.

    pause

    exit /b 1

)



echo Copying from %SRC% to %DST% ...



copy /Y "%SRC%cache_guard.py" "%DST%\"

copy /Y "%SRC%snmp_check_descriptions.py" "%DST%\"

copy /Y "%SRC%main.py" "%DST%\"

copy /Y "%SRC%master.py" "%DST%\"

copy /Y "%SRC%master.bat" "%DST%\"

copy /Y "%SRC%INSTALL.txt" "%DST%\"

copy /Y "%SRC%templates\index.html" "%DST%\templates\"



echo.

echo Done. Run from %DST%:

echo   venv\Scripts\python.exe -B snmp_check_descriptions.py 10.160.4.1 BynetSec

echo.

pause

