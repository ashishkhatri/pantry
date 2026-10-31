@echo off
setlocal
title Pantry (local test)
cd /d "%~dp0"
rem Local testing on this PC. The real install runs on the Proxmox LXC (see README).

if exist ".venv\Scripts\python.exe" goto deps
echo First run: creating a private Python environment in .venv ...
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" goto nopython

:deps
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul
if %errorlevel%==0 goto run
echo Installing packages ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto pipfail
copy /y requirements.txt ".venv\installed-requirements.txt" >nul

:run
".venv\Scripts\python.exe" -m pantry --browser %*
echo.
echo Pantry has stopped.
pause
exit /b 0

:nopython
echo Could not create a Python environment. Install Python 3.10+ from python.org with "Add python.exe to PATH".
pause
exit /b 1

:pipfail
echo Installing packages failed. Check your internet connection and run start.bat again.
pause
exit /b 1
