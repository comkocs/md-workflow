@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY goto nopy
echo Starting dashboard. Keep this window open; close it to stop.
%PY% server.py --open %*
echo.
echo Dashboard stopped, exit code %errorlevel%. See messages above.
pause
exit /b
:nopy
echo Python 3 not found. Install Python 3 and add it to PATH.
pause
