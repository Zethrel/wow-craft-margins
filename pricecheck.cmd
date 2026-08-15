@echo off
REM Launcher for the price lookup window.
REM
REM Uses pythonw so no console window sits behind the app, and finds an
REM interpreter the same way run-scan.cmd does - by RUNNING each candidate,
REM because the Store build's real executable exists and then refuses to
REM start, so testing for its presence picks the one that cannot work.

setlocal
cd /d "%~dp0"

set "PY="
call :try "%WOWCRAFT_PYTHON%"
call :try "C:\Program Files\Python313\pythonw.exe"
call :try "C:\Program Files\Python312\pythonw.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
call :try "%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\pythonw.exe"
call :try "pythonw"
call :try "python"

if not defined PY (
    echo No usable Python found. Install python.org Python, or set
    echo WOWCRAFT_PYTHON to a working pythonw.exe.
    pause
    exit /b 9
)

start "" "%PY%" pricecheck.py %*
exit /b 0

:try
if defined PY exit /b 0
if "%~1"=="" exit /b 0
"%~1" -c "import tkinter" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
